from datetime import timedelta

import pytest

from rabota.accounts import service, tokens
from tests.accounts.harness import PASSWORD, START

NOW = START


def reg(conn, email="ivan@example.test", **kw):
    args = {"email": email, "password": PASSWORD, "name": "Иван", "role": "job_seeker", "consent": True, "now": NOW}
    args.update(kw)
    return service.register(conn, **args)


def verified(conn, email="ivan@example.test", **kw):
    r = reg(conn, email, **kw)
    service.verify_email(conn, r.token, NOW)
    return r.user_id


# ------------------------------------------------------------------ ввод

@pytest.mark.parametrize("raw,expected", [
    ("  Ivan@Example.TEST ", "ivan@example.test"),
    ("A@B.RU", "a@b.ru"),
    ("", ""),
])
def test_normalize_email(raw, expected):
    assert service.normalize_email(raw) == expected


@pytest.mark.parametrize("email", ["a@b.ru", "ivan.petrov+tag@example.test", "имя@работа-донецк.рф"])
def test_valid_emails(email):
    assert service.valid_email(email)


@pytest.mark.parametrize("email", ["", "plain", "a@b", "a@@b.ru", "a b@c.ru", "a@b.ru\nBcc: x@y.ru", "<a>@b.ru", "a@b.r", "x" * 250 + "@b.ru"])
def test_invalid_emails(email):
    assert not service.valid_email(email)


@pytest.mark.parametrize("raw,expected", [
    ("", ""), ("  ", ""), ("+7 949 123-45-67", "+79491234567"), ("8 (949) 123-45-67", "+79491234567"),
    ("949 123 45 67", "+79491234567"), ("+380501234567", "+380501234567"), ("12345", None), ("abc", None),
    ("+7 949 123-45-67 доб 5", None),
])
def test_normalize_phone(raw, expected):
    assert service.normalize_phone(raw) == expected


def test_clean_name_collapses_spaces_and_controls():
    assert service.clean_name("  Иван \n  Петров\t") == "Иван Петров"
    assert len(service.clean_name("я" * 200)) == 80


def test_validate_registration_collects_all_errors():
    errors = service.validate_registration("bad", "123", "", "hacker", False)
    assert set(errors) == {"email", "name", "role", "password", "consent"}
    assert service.validate_registration("a@b.ru", PASSWORD, "Иван", "employer", True) == {}


# ------------------------------------------------------------------ регистрация

def test_register_creates_unverified_user_with_consent_and_hashed_password(conn):
    r = reg(conn, "Ivan@Example.test")
    assert r.kind == "new" and r.token
    row = service.get_user_by_email(conn, "ivan@example.test")
    assert row["email"] == "ivan@example.test"
    assert row["role"] == "job_seeker" and row["status"] == "active"
    assert row["email_verified_at"] is None
    assert row["consent_at"] == tokens.iso(NOW)
    assert row["password_hash"].startswith("scrypt$") and PASSWORD not in row["password_hash"]


def test_register_existing_verified_changes_nothing(conn):
    uid = verified(conn)
    before = dict(service.get_user_by_email(conn, "ivan@example.test"))
    r = reg(conn, password="completely-other-pw-1", name="Другой", role="employer")
    assert r.kind == "exists" and r.token is None and r.user_id == uid
    assert dict(service.get_user_by_email(conn, "ivan@example.test")) == before


def test_register_existing_unverified_replaces_data_and_token(conn):
    """Тот, кто занял чужую почту раньше хозяина, не должен получить доступ после подтверждения."""
    first = reg(conn, password="attacker-password-1", name="Злоумышленник", role="employer")
    second = reg(conn, password="owner-password-2", name="Хозяин", role="job_seeker")
    assert second.kind == "pending" and second.user_id == first.user_id
    assert service.verify_email(conn, first.token, NOW) is None  # старая ссылка погашена
    assert service.verify_email(conn, second.token, NOW)
    assert service.authenticate(conn, "ivan@example.test", "attacker-password-1").status == "bad"
    res = service.authenticate(conn, "ivan@example.test", "owner-password-2")
    assert res.status == "ok" and res.user["name"] == "Хозяин" and res.user["role"] == "job_seeker"


def test_register_does_not_leak_hash(conn):
    verified(conn)
    user = service.get_user(conn, 1)
    assert "password_hash" not in user and user["verified"] is True and user["role_label"] == "Соискатель"


def test_verify_email_marks_verified_once(conn):
    r = reg(conn)
    user = service.verify_email(conn, r.token, NOW + timedelta(minutes=5))
    assert user["verified"] and user["email_verified_at"] == tokens.iso(NOW + timedelta(minutes=5))
    assert service.verify_email(conn, r.token, NOW) is None


def test_verify_expired_token(conn):
    r = reg(conn)
    assert service.verify_email(conn, r.token, NOW + timedelta(hours=24, seconds=1)) is None
    assert service.get_user(conn, r.user_id)["verified"] is False


def test_resend_verification(conn):
    r = reg(conn)
    user, token = service.resend_verification(conn, "IVAN@example.test ", NOW)
    assert user["id"] == r.user_id and token != r.token
    assert service.verify_email(conn, r.token, NOW) is None
    assert service.verify_email(conn, token, NOW)
    assert service.resend_verification(conn, "ivan@example.test", NOW) is None  # уже подтверждён
    assert service.resend_verification(conn, "nobody@example.test", NOW) is None


# ------------------------------------------------------------------ вход

def test_authenticate_states(conn):
    r = reg(conn)
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "unverified"
    service.verify_email(conn, r.token, NOW)
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "ok"
    assert service.authenticate(conn, "IVAN@example.test", PASSWORD).status == "ok"
    assert service.authenticate(conn, "ivan@example.test", "wrong-password").status == "bad"
    assert service.authenticate(conn, "nobody@example.test", PASSWORD).status == "bad"
    assert service.authenticate(conn, "ivan@example.test", "x" * 1000).status == "bad"
    service.set_blocked(conn, r.user_id, True)
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "blocked"
    assert service.authenticate(conn, "ivan@example.test", "wrong-password").status == "bad"  # не раскрываем блокировку
    service.set_blocked(conn, r.user_id, False)
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "ok"


def test_authenticate_upgrades_weak_hash(conn, monkeypatch):
    uid = verified(conn)
    from rabota.accounts import passwords
    weak = "$".join(("scrypt", "1024", "8", "1", passwords._b64(b"s" * 16), passwords._b64(passwords._derive(PASSWORD, b"s" * 16, 1024, 8, 1))))
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (weak, uid))
    conn.commit()
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "ok"
    assert not passwords.needs_rehash(conn.execute("SELECT password_hash FROM users").fetchone()[0])


# ------------------------------------------------------------------ сессии

def test_session_stores_only_hash(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW, "203.0.113.5", "UA/1.0")
    row = conn.execute("SELECT * FROM sessions").fetchone()
    assert row["token_hash"] == tokens.hash_token(raw) and raw not in str(tuple(row))
    assert row["ip"] == "203.0.113.5" and row["user_agent"] == "UA/1.0"
    assert row["expires_at"] == tokens.iso(NOW + timedelta(days=30))
    assert conn.execute("SELECT last_login_at FROM users").fetchone()[0] == tokens.iso(NOW)


def test_resolve_session_returns_user_without_hash(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW)
    user, refreshed = service.resolve_session(conn, raw, NOW + timedelta(minutes=1))
    assert user["id"] == uid and "password_hash" not in user and refreshed is None


def test_session_sliding_expiry(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW)
    later = NOW + timedelta(days=2)
    user, refreshed = service.resolve_session(conn, raw, later)
    assert user and refreshed == later + timedelta(days=30)
    assert conn.execute("SELECT expires_at FROM sessions").fetchone()[0] == tokens.iso(later + timedelta(days=30))
    # вторая проверка сразу после продления срок не двигает
    assert service.resolve_session(conn, raw, later + timedelta(minutes=5))[1] is None


def test_session_expires_after_30_days_without_activity(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW)
    assert service.resolve_session(conn, raw, NOW + timedelta(days=30, seconds=1)) == (None, None)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_session_active_user_keeps_alive_for_months(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW)
    day = NOW
    for _ in range(90):
        day += timedelta(days=1)
        user, _ = service.resolve_session(conn, raw, day)
        assert user is not None


def test_unknown_session_token(conn):
    assert service.resolve_session(conn, "nope", NOW) == (None, None)
    assert service.resolve_session(conn, "", NOW) == (None, None)
    assert service.resolve_session(conn, "x" * 500, NOW) == (None, None)


def test_blocked_user_session_is_dead(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW)
    conn.execute("UPDATE users SET status='blocked' WHERE id=?", (uid,))
    conn.commit()
    assert service.resolve_session(conn, raw, NOW) == (None, None)


def test_set_blocked_closes_sessions(conn):
    uid = verified(conn)
    service.create_session(conn, uid, NOW)
    service.create_session(conn, uid, NOW)
    service.set_blocked(conn, uid, True)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_delete_session_and_keep_one(conn):
    uid = verified(conn)
    a, b, c = (service.create_session(conn, uid, NOW) for _ in range(3))
    service.delete_session(conn, a)
    assert service.resolve_session(conn, a, NOW)[0] is None
    service.delete_user_sessions(conn, uid, keep_raw=b)
    assert service.resolve_session(conn, b, NOW)[0] is not None
    assert service.resolve_session(conn, c, NOW)[0] is None


def test_purge_expired(conn):
    uid = verified(conn)
    service.create_session(conn, uid, NOW)
    service.purge_expired(conn, NOW + timedelta(days=31))
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_purge_unverified_accounts(conn):
    reg(conn, "old@example.test")
    reg(conn, "fresh@example.test", now=NOW + timedelta(days=7))
    verified(conn, "ok@example.test")
    removed = service.purge_unverified(conn, NOW + timedelta(days=8))
    assert removed == 1
    assert service.get_user_by_email(conn, "old@example.test") is None
    assert service.get_user_by_email(conn, "fresh@example.test")
    assert service.get_user_by_email(conn, "ok@example.test")


# ------------------------------------------------------------------ пароль

def test_change_password_closes_other_sessions_and_old_reset_links(conn):
    uid = verified(conn)
    keep = service.create_session(conn, uid, NOW)
    other = service.create_session(conn, uid, NOW)
    _, reset_token = service.request_reset(conn, "ivan@example.test", NOW)
    service.change_password(conn, uid, "brand-new-pass-9", NOW, keep_session=keep)
    assert service.resolve_session(conn, keep, NOW)[0] is not None
    assert service.resolve_session(conn, other, NOW)[0] is None
    assert service.authenticate(conn, "ivan@example.test", "brand-new-pass-9").status == "ok"
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "bad"
    assert service.reset_password(conn, reset_token, "again-new-pass-8", NOW) is None


def test_reset_flow(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW)
    _, token = service.request_reset(conn, "IVAN@example.test", NOW)
    user = service.reset_password(conn, token, "brand-new-pass-9", NOW + timedelta(minutes=10))
    assert user["id"] == uid
    assert service.resolve_session(conn, raw, NOW)[0] is None  # все сессии закрыты
    assert service.reset_password(conn, token, "third-pass-word-7", NOW) is None  # одноразовая
    assert service.authenticate(conn, "ivan@example.test", "brand-new-pass-9").status == "ok"


def test_reset_token_expires_after_an_hour(conn):
    verified(conn)
    _, token = service.request_reset(conn, "ivan@example.test", NOW)
    assert service.reset_password(conn, token, "brand-new-pass-9", NOW + timedelta(hours=1, seconds=1)) is None
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "ok"


def test_reset_unknown_and_blocked_get_nothing(conn):
    uid = verified(conn)
    assert service.request_reset(conn, "nobody@example.test", NOW) is None
    service.set_blocked(conn, uid, True)
    assert service.request_reset(conn, "ivan@example.test", NOW) is None


def test_reset_confirms_unverified_mailbox(conn):
    reg(conn)
    _, token = service.request_reset(conn, "ivan@example.test", NOW)
    assert service.reset_password(conn, token, "brand-new-pass-9", NOW)["verified"]


def test_second_reset_request_cancels_first(conn):
    verified(conn)
    _, first = service.request_reset(conn, "ivan@example.test", NOW)
    _, second = service.request_reset(conn, "ivan@example.test", NOW)
    assert service.reset_password(conn, first, "brand-new-pass-9", NOW) is None
    assert service.reset_password(conn, second, "brand-new-pass-9", NOW)


# ------------------------------------------------------------------ профиль, почта

def test_update_profile(conn):
    uid = verified(conn)
    service.update_profile(conn, uid, "Пётр", "+79491234567")
    u = service.get_user(conn, uid)
    assert (u["name"], u["phone"]) == ("Пётр", "+79491234567")
    service.update_profile(conn, uid, "Пётр", "")
    assert service.get_user(conn, uid)["phone"] is None


def test_check_password(conn):
    uid = verified(conn)
    assert service.check_password(conn, uid, PASSWORD)
    assert not service.check_password(conn, uid, "nope-nope-nope")
    assert not service.check_password(conn, 9999, PASSWORD)


def test_email_change_flow(conn):
    uid = verified(conn)
    raw_session = service.create_session(conn, uid, NOW)
    token = service.request_email_change(conn, uid, " NEW@Example.test ", NOW)
    assert service.get_user(conn, uid)["email"] == "ivan@example.test"  # пока не подтверждено
    user, old = service.confirm_email_change(conn, token, NOW + timedelta(hours=1))
    assert user["email"] == "new@example.test" and old == "ivan@example.test" and user["verified"]
    assert service.resolve_session(conn, raw_session, NOW)[0] is None
    assert service.authenticate(conn, "new@example.test", PASSWORD).status == "ok"
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "bad"
    assert service.confirm_email_change(conn, token, NOW) is None  # одноразовая


def test_email_change_taken_address(conn):
    uid = verified(conn)
    verified(conn, "other@example.test")
    assert service.request_email_change(conn, uid, "other@example.test", NOW) is None
    assert service.request_email_change(conn, uid, "OTHER@example.test", NOW) is None


def test_email_change_race_taken_after_request(conn):
    uid = verified(conn)
    token = service.request_email_change(conn, uid, "new@example.test", NOW)
    verified(conn, "new@example.test")
    assert service.confirm_email_change(conn, token, NOW) is None
    assert service.get_user(conn, uid)["email"] == "ivan@example.test"


def test_email_change_expires(conn):
    uid = verified(conn)
    token = service.request_email_change(conn, uid, "new@example.test", NOW)
    assert service.confirm_email_change(conn, token, NOW + timedelta(hours=25)) is None


def test_email_change_cancels_other_pending_tokens(conn):
    uid = verified(conn)
    _, reset = service.request_reset(conn, "ivan@example.test", NOW)
    token = service.request_email_change(conn, uid, "new@example.test", NOW)
    service.confirm_email_change(conn, token, NOW)
    assert service.reset_password(conn, reset, "brand-new-pass-9", NOW) is None


# ------------------------------------------------------------------ удаление

def test_delete_account_cascades_everything(conn):
    uid = verified(conn)
    other = verified(conn, "boss@example.test", role="employer")
    service.create_session(conn, uid, NOW)
    cur = conn.execute("INSERT INTO vacancies(profession, owner_user_id, origin) VALUES ('повар', ?, 'site')", (other,))
    vac = cur.lastrowid
    own_vac = conn.execute("INSERT INTO vacancies(profession, owner_user_id, origin) VALUES ('сварщик', ?, 'site')", (uid,)).lastrowid
    conn.execute("INSERT INTO companies(owner_user_id, name) VALUES (?, 'Моя фирма')", (uid,))
    conn.execute("INSERT INTO resumes(user_id, title) VALUES (?, 'резюме')", (uid,))
    conn.execute("INSERT INTO applications(vacancy_id, user_id) VALUES (?, ?)", (vac, uid))
    conn.execute("INSERT INTO applications(vacancy_id, user_id) VALUES (?, ?)", (own_vac, other))
    conn.execute("INSERT INTO favorites(user_id, vacancy_id) VALUES (?, ?)", (uid, vac))
    conn.execute("INSERT INTO user_blocks(blocker_user_id, blocked_user_id) VALUES (?, ?)", (uid, other))
    conv = conn.execute(
        "INSERT INTO conversations(employer_user_id, seeker_user_id, vacancy_id, started_by) VALUES (?,?,?,?)",
        (other, uid, vac, uid)).lastrowid
    conn.execute("INSERT INTO messages(conversation_id, sender_user_id, body) VALUES (?,?, 'привет')", (conv, uid))
    conn.execute("INSERT INTO reports(reporter_user_id, target_type, target_id, reason) VALUES (?, 'vacancy', ?, 'x')", (uid, vac))
    conn.commit()

    service.delete_account(conn, uid)

    count = lambda t, col="user_id": conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {col}=?", (uid,)).fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?", (uid,)).fetchone()[0] == 0
    assert count("sessions") == 0 and count("email_tokens") == 0
    assert count("companies", "owner_user_id") == 0 and count("resumes") == 0
    assert count("applications") == 0 and count("favorites") == 0
    assert count("user_blocks", "blocker_user_id") == 0
    assert conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE id=?", (own_vac,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM applications WHERE vacancy_id=?", (own_vac,)).fetchone()[0] == 0
    assert conn.execute("SELECT reporter_user_id FROM reports").fetchone()[0] is None  # жалоба остаётся
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE id=?", (vac,)).fetchone()[0] == 1  # чужое не тронуто
    assert conn.execute("SELECT COUNT(*) FROM users WHERE id=?", (other,)).fetchone()[0] == 1


# ------------------------------------------------------------------ администратор

def test_create_admin_new(conn):
    uid = service.create_admin(conn, " Admin@Example.test ", "admin-pass-phrase-5", NOW)
    u = service.get_user(conn, uid)
    assert u["role"] == "admin" and u["verified"] and u["email"] == "admin@example.test" and u["is_admin"]
    assert service.authenticate(conn, "admin@example.test", "admin-pass-phrase-5").status == "ok"


def test_create_admin_promotes_existing_and_resets_password(conn):
    uid = verified(conn)
    raw = service.create_session(conn, uid, NOW)
    assert service.create_admin(conn, "ivan@example.test", "admin-pass-phrase-5", NOW) == uid
    assert service.get_user(conn, uid)["role"] == "admin"
    assert service.authenticate(conn, "ivan@example.test", PASSWORD).status == "bad"
    assert service.resolve_session(conn, raw, NOW)[0] is None


@pytest.mark.parametrize("email,pw", [("not-an-email", "admin-pass-phrase-5"), ("a@example.test", "123"), ("a@example.test", "password")])
def test_create_admin_validates(conn, email, pw):
    with pytest.raises(ValueError):
        service.create_admin(conn, email, pw, NOW)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
