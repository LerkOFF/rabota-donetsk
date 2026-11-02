"""Подписки без веба: создание, лимиты, подтверждение кодом и ссылкой, Telegram, отписка, чистка."""
from datetime import timedelta

import pytest

from rabota.admin import settings
from rabota.alerts import service
from tests.alerts.helpers import NOW, SECRET, user, vac

SEARCH = {"q": "бариста", "city": "Донецк"}


# ------------------------------------------------------------------ вошедший

def test_user_subscription_is_active_at_once_and_counts_only_new_vacancies(conn):
    old = vac(conn)
    me = user(conn)
    row, created = service.subscribe_user(conn, me, SEARCH, now=NOW)
    assert created and row["status"] == "active" and row["channel"] == "email" and row["frequency"] == "daily"
    assert row["title"] == "бариста в Донецке" and row["baseline_id"] == old
    assert service.filters_of(row) == {"q": "бариста", "city": "Донецк"}


def test_same_search_is_not_duplicated_and_disabled_one_is_enabled_again(conn):
    me = user(conn)
    first, _ = service.subscribe_user(conn, me, SEARCH, now=NOW)
    service.pause(conn, me, first["id"])
    vac(conn)
    again, created = service.subscribe_user(conn, me, {"city": "Донецк", "q": " Бариста "}, now=NOW)
    assert not created and again["id"] == first["id"] and again["status"] == "active"
    assert again["baseline_id"] > first["baseline_id"]  # паузу не догоняем лавиной старых вакансий
    assert service.count_for_user(conn, me) == 1


def test_empty_search_is_refused(conn):
    with pytest.raises(service.AlertError) as exc:
        service.subscribe_user(conn, user(conn), {"sort": "pay"}, now=NOW)
    assert exc.value.code == "empty"


def test_unknown_channel_or_frequency_is_refused(conn):
    me = user(conn)
    with pytest.raises(service.AlertError):
        service.subscribe_user(conn, me, SEARCH, channel="sms", now=NOW)
    with pytest.raises(service.AlertError):
        service.subscribe_user(conn, me, SEARCH, frequency="hourly", now=NOW)


def test_per_user_limit_comes_from_admin_settings(conn):
    me = user(conn)
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('alerts_max_per_user', '2', 'x')")
    conn.commit()
    assert settings.effective(conn, "alerts_max_per_user") == 2
    service.subscribe_user(conn, me, {"q": "а1"}, now=NOW)
    service.subscribe_user(conn, me, {"q": "б2"}, now=NOW)
    with pytest.raises(service.AlertError) as exc:
        service.subscribe_user(conn, me, {"q": "в3"}, now=NOW)
    assert exc.value.code == "limit" and "2" in exc.value.text
    other = user(conn, "boris@example.test")
    service.subscribe_user(conn, other, {"q": "в3"}, now=NOW)  # лимит на человека, а не на сайт


def test_ownership_other_user_cannot_touch_subscription(conn):
    me, other = user(conn), user(conn, "boris@example.test")
    row, _ = service.subscribe_user(conn, me, SEARCH, now=NOW)
    assert service.get_owned(conn, other, row["id"]) is None
    assert not service.pause(conn, other, row["id"])
    assert not service.delete(conn, other, row["id"])
    assert not service.resume(conn, other, row["id"], NOW)
    assert not service.set_frequency(conn, other, row["id"], "instant")
    assert service.get(conn, row["id"])["status"] == "active"


def test_pause_resume_frequency_delete(conn):
    me = user(conn)
    row, _ = service.subscribe_user(conn, me, SEARCH, now=NOW)
    assert service.pause(conn, me, row["id"]) and service.get(conn, row["id"])["status"] == "paused"
    assert service.resume(conn, me, row["id"], NOW) and service.get(conn, row["id"])["status"] == "active"
    assert service.set_frequency(conn, me, row["id"], "instant") and service.get(conn, row["id"])["frequency"] == "instant"
    with pytest.raises(service.AlertError):
        service.set_frequency(conn, me, row["id"], "never")
    assert service.delete(conn, me, row["id"]) and service.get(conn, row["id"]) is None


def test_deleting_the_user_removes_subscriptions_and_sent_log(conn):
    me = user(conn)
    row, _ = service.subscribe_user(conn, me, SEARCH, now=NOW)
    conn.execute("INSERT INTO alert_sent(subscription_id, vacancy_id, sent_at) VALUES (?,?,?)", (row["id"], 1, "x"))
    conn.commit()
    from rabota.accounts import service as accounts

    accounts.delete_account(conn, me["id"])
    assert conn.execute("SELECT COUNT(*) FROM alert_subscriptions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM alert_sent").fetchone()[0] == 0


def test_guest_subscriptions_by_email_are_erased_with_the_account_and_seen_in_cabinet(conn):
    me = user(conn, "anna@example.test")
    guest, code = service.subscribe_guest(conn, "Anna@Example.test", SEARCH, secret=SECRET, now=NOW)
    assert service.check_code(conn, guest["id"], code, SECRET, NOW) == "ok"
    assert [r["id"] for r in service.list_for_user(conn, me)] == [guest["id"]]  # гостевая подписка на ту же почту видна в кабинете
    assert service.get_owned(conn, me, guest["id"]) is not None
    from rabota.accounts import service as accounts

    accounts.delete_account(conn, me["id"])
    assert service.get(conn, guest["id"]) is None


# ------------------------------------------------------------------ гость и подтверждение

def test_guest_flow_by_code(conn):
    row, code = service.subscribe_guest(conn, " Ivan@Example.test ", SEARCH, frequency="instant", secret=SECRET, now=NOW)
    assert row["status"] == "pending" and row["email"] == "ivan@example.test" and row["user_id"] is None
    assert code and len(code) == 6 and code.isdigit()
    assert service.check_code(conn, row["id"], "000000" if code != "000000" else "111111", SECRET, NOW) == "bad"
    assert service.check_code(conn, row["id"], f"{code[:3]} {code[3:]}", SECRET, NOW) == "ok"
    done = service.get(conn, row["id"])
    assert done["status"] == "active" and done["confirmed_at"] and done["code_hash"] == ""
    assert service.check_code(conn, row["id"], code, SECRET, NOW) == "none"  # код одноразовый


def test_code_stores_only_hash_and_is_bound_to_the_subscription(conn):
    a, code_a = service.subscribe_guest(conn, "a@example.test", {"q": "а1"}, secret=SECRET, now=NOW)
    b, _ = service.subscribe_guest(conn, "b@example.test", {"q": "б2"}, secret=SECRET, now=NOW)
    assert code_a not in service.get(conn, a["id"])["code_hash"]
    assert service.check_code(conn, b["id"], code_a, SECRET, NOW) in ("bad", "ok")  # чужой код подходит лишь случайно
    assert service.check_code(conn, a["id"], code_a, b"other-secret-other-secret-xxxxxxxx", NOW) in ("bad", "locked")


def test_code_attempts_expiry_and_garbage(conn):
    row, code = service.subscribe_guest(conn, "ivan@example.test", SEARCH, secret=SECRET, now=NOW)
    wrong = "123456" if code != "123456" else "654321"
    results = [service.check_code(conn, row["id"], wrong, SECRET, NOW) for _ in range(service.CODE_ATTEMPTS)]
    assert results[:-1] == ["bad"] * (service.CODE_ATTEMPTS - 1) and results[-1] == "locked"
    assert service.check_code(conn, row["id"], code, SECRET, NOW) == "none"  # после блокировки верный код не работает
    row2, code2 = service.subscribe_guest(conn, "ivan2@example.test", SEARCH, secret=SECRET, now=NOW)
    assert service.check_code(conn, row2["id"], "abc", SECRET, NOW) == "bad"
    assert service.check_code(conn, row2["id"], code2, SECRET, NOW + service.CODE_TTL + timedelta(seconds=1)) == "expired"


def test_resend_has_cooldown_and_hourly_cap(conn):
    row, code = service.subscribe_guest(conn, "ivan@example.test", SEARCH, secret=SECRET, now=NOW)
    assert code
    assert service.issue_code(conn, row["id"], SECRET, NOW + timedelta(seconds=10)) is None
    later = NOW + timedelta(seconds=61)
    fresh = service.issue_code(conn, row["id"], SECRET, later)
    assert fresh
    assert service.check_code(conn, row["id"], code, SECRET, later) in ("bad", "ok")  # старый код погашен новым (если не совпал случайно)
    issued = 2
    t = later
    while True:
        t += timedelta(seconds=61)
        if t - NOW >= timedelta(hours=1):
            break
        if service.issue_code(conn, row["id"], SECRET, t):
            issued += 1
    assert issued == service.CONFIRM_PER_HOUR


def test_active_guest_subscription_gives_no_code_and_no_hint(conn):
    row, code = service.subscribe_guest(conn, "ivan@example.test", SEARCH, secret=SECRET, now=NOW)
    service.check_code(conn, row["id"], code, SECRET, NOW)
    again, new_code = service.subscribe_guest(conn, "ivan@example.test", SEARCH, secret=SECRET, now=NOW + timedelta(minutes=5))
    assert new_code is None and again["id"] == row["id"] and again["status"] == "active"
    assert service.check_code(conn, row["id"], "123456", SECRET, NOW) == "none"


def test_guest_limit_per_email_and_bad_email(conn):
    for i in range(service.MAX_PER_EMAIL):
        service.subscribe_guest(conn, "ivan@example.test", {"q": f"профессия{i}"}, secret=SECRET, now=NOW)
    with pytest.raises(service.AlertError) as exc:
        service.subscribe_guest(conn, "ivan@example.test", {"q": "лишняя"}, secret=SECRET, now=NOW)
    assert exc.value.code == "limit"
    for bad in ("", "no-at", "a@b", "x@\nexample.test"):
        with pytest.raises(service.AlertError) as err:
            service.subscribe_guest(conn, bad, SEARCH, secret=SECRET, now=NOW)
        assert err.value.code == "email"


def test_confirm_by_link(conn):
    row, _ = service.subscribe_guest(conn, "ivan@example.test", SEARCH, secret=SECRET, now=NOW)
    assert service.confirm_by_link(conn, row["id"], NOW) is True
    assert service.get(conn, row["id"])["status"] == "active"
    assert service.confirm_by_link(conn, row["id"], NOW) is True  # повтор безопасен
    old, _ = service.subscribe_guest(conn, "old@example.test", SEARCH, secret=SECRET, now=NOW)
    assert service.confirm_by_link(conn, old["id"], NOW + service.LINK_TTL + timedelta(minutes=1)) is False
    assert service.confirm_by_link(conn, 9999, NOW) is False


def test_baseline_is_set_on_confirmation_not_on_request(conn):
    row, code = service.subscribe_guest(conn, "ivan@example.test", SEARCH, secret=SECRET, now=NOW)
    between = vac(conn)
    service.check_code(conn, row["id"], code, SECRET, NOW)
    assert service.get(conn, row["id"])["baseline_id"] == between


# ------------------------------------------------------------------ токены

def test_tokens_roundtrip_and_are_purpose_bound(conn):
    token = service.make_token(7, "unsub", SECRET)
    assert service.parse_token(token, "unsub", SECRET) == 7
    assert service.parse_token(token, "confirm", SECRET) is None
    assert service.parse_token(token, "unsub", b"another-secret-another-secret-xx") is None
    _head, sig = token.split(".")
    assert service.parse_token(f"8.{sig}", "unsub", SECRET) is None  # подмена номера
    for bad in ("", "x", "7", "7.", ".abc", "7.abc", "-7." + sig, "7" * 20 + "." + sig, token + "0", token[:-1]):
        assert service.parse_token(bad, "unsub", SECRET) is None, bad
    assert service.parse_token("1." + "a" * 200, "unsub", SECRET) is None


# ------------------------------------------------------------------ Telegram

def test_telegram_binding_activates_and_code_is_single_use(conn):
    me = user(conn)
    row, code = service.subscribe_telegram(conn, me, SEARCH, frequency="instant", now=NOW)
    assert row["status"] == "pending" and row["channel"] == "telegram" and code
    assert service.get(conn, row["id"])["tg_code_hash"] != code
    status, bound = service.bind_telegram(conn, code, 555, NOW)
    assert status == "ok" and bound["status"] == "active" and bound["tg_chat_id"] == 555 and bound["user_id"] == me["id"]
    assert service.bind_telegram(conn, code, 777, NOW)[0] == "bad"
    assert service.subs_for_chat(conn, 555)[0]["id"] == row["id"]


def test_telegram_guest_without_account_and_baseline(conn):
    vac(conn)
    row, code = service.subscribe_telegram(conn, None, SEARCH, now=NOW)
    assert row["user_id"] is None and row["email"] == ""
    later = vac(conn)
    _, bound = service.bind_telegram(conn, code, 42, NOW)
    assert bound["baseline_id"] == later


@pytest.mark.parametrize("code", ["", "short", "bad code!", "a" * 100, "ВВЕРХ" * 3])
def test_telegram_garbage_codes(conn, code):
    assert service.bind_telegram(conn, code, 1, NOW) == ("bad", None)


def test_telegram_code_expires(conn):
    _, code = service.subscribe_telegram(conn, None, SEARCH, now=NOW)
    assert service.bind_telegram(conn, code, 1, NOW + service.TG_CODE_TTL + timedelta(seconds=1))[0] == "bad"


def test_telegram_chat_limit(conn):
    for i in range(service.MAX_PER_CHAT):
        _, code = service.subscribe_telegram(conn, None, {"q": f"п{i}"}, now=NOW)
        assert service.bind_telegram(conn, code, 9, NOW)[0] == "ok"
    _, code = service.subscribe_telegram(conn, None, {"q": "лишняя"}, now=NOW)
    assert service.bind_telegram(conn, code, 9, NOW)[0] == "limit"
    assert service.bind_telegram(conn, code, 10, NOW)[0] == "ok"  # другой чат принимает


def test_stop_chat_disables_only_that_chat(conn):
    _, c1 = service.subscribe_telegram(conn, None, {"q": "а1"}, now=NOW)
    _, c2 = service.subscribe_telegram(conn, None, {"q": "б2"}, now=NOW)
    service.bind_telegram(conn, c1, 1, NOW)
    service.bind_telegram(conn, c2, 2, NOW)
    assert service.stop_chat(conn, 1, NOW, "бот заблокирован") == 1
    assert service.subs_for_chat(conn, 1) == [] and len(service.subs_for_chat(conn, 2)) == 1
    assert service.stop_chat(conn, 1, NOW) == 0


def test_add_telegram_to_existing_email_subscription(conn):
    me = user(conn)
    row, _ = service.subscribe_user(conn, me, SEARCH, frequency="instant", now=NOW)
    tg, code = service.add_telegram_to(conn, me, row["id"], NOW)
    assert tg["channel"] == "telegram" and tg["frequency"] == "instant" and tg["filters_key"] == row["filters_key"] and code
    assert service.add_telegram_to(conn, user(conn, "z@example.test"), row["id"], NOW) is None


def _both(conn, me, frequency="daily"):
    mail, _ = service.subscribe_user(conn, me, SEARCH, frequency=frequency, now=NOW)
    tg, code = service.add_telegram_to(conn, me, mail["id"], NOW)
    return mail, tg, code


def test_email_and_telegram_are_one_subscription(conn):
    me = user(conn)
    mail, tg, _ = _both(conn, me)
    groups = service.list_groups(conn, me)
    assert len(groups) == 1 and [r["channel"] for r in groups[0]] == ["email", "telegram"]
    assert service.count_for_user(conn, me) == 1
    assert [r["id"] for r in service.siblings(conn, me, tg)] == [mail["id"], tg["id"]]


def test_second_channel_does_not_hit_the_subscription_limit(conn):
    me = user(conn)
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES ('alerts_max_per_user', '1', 'x')")
    conn.commit()
    _both(conn, me)  # одна подписка с двумя каналами умещается в лимит 1
    with pytest.raises(service.AlertError):
        service.subscribe_user(conn, me, {"q": "другой поиск"}, now=NOW)


def test_pause_resume_frequency_delete_act_on_every_channel(conn):
    me = user(conn)
    mail, tg, code = _both(conn, me)
    service.bind_telegram(conn, code, 55, NOW)
    assert service.pause(conn, me, mail["id"])
    assert {r["status"] for r in service.siblings(conn, me, mail)} == {"paused"}
    assert service.resume(conn, me, tg["id"], NOW)
    assert {r["status"] for r in service.siblings(conn, me, mail)} == {"active"}
    assert service.set_frequency(conn, me, mail["id"], "instant")
    assert {r["frequency"] for r in service.siblings(conn, me, mail)} == {"instant"}
    assert service.delete(conn, me, tg["id"])
    assert service.list_for_user(conn, me) == []


def test_resume_skips_telegram_without_a_chat(conn):
    me = user(conn)
    mail, _tg, _ = _both(conn, me)
    service.pause(conn, me, mail["id"])
    service.resume(conn, me, mail["id"], NOW)
    rows = {r["channel"]: r for r in service.siblings(conn, me, mail)}
    assert rows["email"]["status"] == "active" and rows["telegram"]["status"] == "pending"


def test_add_email_to_telegram_only_subscription(conn):
    me = user(conn)
    tg, _ = service.subscribe_telegram(conn, me, SEARCH, frequency="instant", now=NOW)
    mail = service.add_email_to(conn, me, tg["id"], NOW)
    assert mail["channel"] == "email" and mail["status"] == "active" and mail["frequency"] == "instant"
    assert len(service.list_groups(conn, me)) == 1
    assert service.add_email_to(conn, user(conn, "z@example.test"), tg["id"], NOW) is None


# ------------------------------------------------------------------ отписка, чистка, цифры

def test_unsubscribe_is_repeatable_and_keeps_the_row(conn):
    row, _ = service.subscribe_guest(conn, "ivan@example.test", SEARCH, secret=SECRET, now=NOW)
    service.confirm_by_link(conn, row["id"], NOW)
    first = service.unsubscribe(conn, row["id"], NOW)
    again = service.unsubscribe(conn, row["id"], NOW + timedelta(hours=1))
    assert first["status"] == again["status"] == "unsubscribed" and again["unsubscribed_at"] == first["unsubscribed_at"]
    assert service.unsubscribe(conn, 9999, NOW) is None


def test_purge_removes_stale_pending_old_guest_unsubscribed_and_logs(conn):
    pending, _ = service.subscribe_guest(conn, "p@example.test", {"q": "а1"}, secret=SECRET, now=NOW)
    fresh, _ = service.subscribe_guest(conn, "f@example.test", {"q": "б2"}, secret=SECRET, now=NOW + timedelta(days=2))
    gone, _ = service.subscribe_guest(conn, "g@example.test", {"q": "в3"}, secret=SECRET, now=NOW)
    service.confirm_by_link(conn, gone["id"], NOW)
    service.unsubscribe(conn, gone["id"], NOW)
    me = user(conn)
    kept, _ = service.subscribe_user(conn, me, SEARCH, now=NOW)
    service.unsubscribe(conn, kept["id"], NOW)  # у аккаунта отключённая подписка остаётся в списке
    service.log_delivery(conn, NOW, channel="email", kind="digest")
    conn.execute("INSERT INTO alert_sent(subscription_id, vacancy_id, sent_at) VALUES (?,?,?)", (kept["id"], 1, NOW.isoformat()))
    conn.commit()
    first = service.purge(conn, NOW + service.PENDING_KEEP + timedelta(hours=1))
    assert first["pending"] == 1 and service.get(conn, pending["id"]) is None
    assert service.get(conn, fresh["id"]) is not None and service.get(conn, gone["id"]) is not None
    out = service.purge(conn, NOW + service.GUEST_UNSUB_KEEP + timedelta(days=61))
    assert out["unsubscribed"] == 1 and out["log"] >= 1 and out["sent"] == 1
    assert service.get(conn, gone["id"]) is None and service.get(conn, kept["id"]) is not None


def test_stats_counts(conn):
    me = user(conn)
    service.subscribe_user(conn, me, {"q": "а1"}, now=NOW)
    service.subscribe_guest(conn, "g@example.test", {"q": "б2"}, secret=SECRET, now=NOW)
    service.log_delivery(conn, NOW, channel="email", kind="digest", vacancies=3)
    service.log_delivery(conn, NOW, channel="email", kind="digest", ok=False, error="SMTP: down")
    stats = service.stats(conn, NOW)
    assert stats["total"] == 2 and stats["active"] == 1 and stats["pending"] == 1 and stats["email"] == 1
    assert stats["mail_sent_24h"] == 1 and stats["mail_errors_24h"] == 1 and stats["vacancies_sent_24h"] == 3
    assert stats["last_error"] == "SMTP: down"


def test_migration_is_repeatable(conn):
    from rabota.alerts.schema import migrate

    migrate(conn)
    migrate(conn)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"alert_subscriptions", "alert_sent", "alert_log", "channel_posts", "channel_vacancies"} <= names
