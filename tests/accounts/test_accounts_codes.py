"""Шестизначные коды: выпуск, хранение, срок, попытки, повторная отправка."""
import hashlib
from datetime import timedelta

import pytest

from rabota.accounts import codes, service
from tests.accounts.harness import START

SECRET = b"k" * 32


@pytest.fixture
def uid(conn):
    conn.execute("INSERT INTO users(email, password_hash, role) VALUES ('t@example.test', 'h', 'job_seeker')")
    conn.commit()
    return conn.execute("SELECT id FROM users").fetchone()["id"]


def test_code_is_six_digits_and_random():
    values = {codes.new_code() for _ in range(50)}
    assert len(values) > 30
    assert all(len(v) == 6 and v.isdigit() for v in values)


def test_clean_code_accepts_spaces_and_dashes_only(conn):
    assert codes.clean_code(" 123 456 ") == "123456"
    assert codes.clean_code("123-456") == "123456"
    for bad in ("", "12345", "1234567", "12345a", "abcdef", "１２３４５６", None):
        assert codes.clean_code(bad) is None


def test_only_hmac_is_stored(conn, uid):
    code = codes.issue_code(conn, SECRET, uid, "verify", START)
    row = conn.execute("SELECT * FROM email_codes").fetchone()
    assert row["code_hash"] == codes.hash_code(SECRET, uid, "verify", code)
    assert len(row["code_hash"]) == 64 and code not in row["code_hash"]
    assert code not in str(tuple(row))
    # это не голый sha256: без ключа хеш шести цифр не перебрать
    assert row["code_hash"] != hashlib.sha256(code.encode()).hexdigest()
    assert codes.hash_code(b"other" * 8, uid, "verify", code) != row["code_hash"]
    assert codes.hash_code(SECRET, uid, "reset", code) != row["code_hash"]


def test_correct_code_works_once(conn, uid):
    code = codes.issue_code(conn, SECRET, uid, "verify", START)
    assert codes.check_code(conn, SECRET, uid, "verify", code, START + timedelta(minutes=1)) == "ok"
    assert codes.check_code(conn, SECRET, uid, "verify", code, START + timedelta(minutes=1)) == "none"


def test_wrong_purpose_does_not_match(conn, uid):
    code = codes.issue_code(conn, SECRET, uid, "verify", START)
    assert codes.check_code(conn, SECRET, uid, "reset", code, START) == "none"
    assert codes.check_code(conn, SECRET, uid, "verify", code, START) == "ok"


def test_code_expires(conn, uid):
    code = codes.issue_code(conn, SECRET, uid, "verify", START)
    ttl = codes.CODE_TTL["verify"]
    assert timedelta(minutes=10) <= ttl <= timedelta(minutes=15)
    assert codes.check_code(conn, SECRET, uid, "verify", code, START + ttl + timedelta(seconds=1)) == "expired"
    code2 = codes.issue_code(conn, SECRET, uid, "reset", START)
    assert codes.check_code(conn, SECRET, uid, "reset", code2, START + codes.CODE_TTL["reset"]) == "expired"


def test_wrong_code_spends_attempts_and_locks(conn, uid):
    code = codes.issue_code(conn, SECRET, uid, "verify", START)
    bad = f"{(int(code) + 1) % 10**6:06d}"
    results = [codes.check_code(conn, SECRET, uid, "verify", bad, START) for _ in range(codes.MAX_ATTEMPTS)]
    assert results == ["bad"] * (codes.MAX_ATTEMPTS - 1) + ["locked"]
    # после исчерпания попыток верный код уже не принимается
    assert codes.check_code(conn, SECRET, uid, "verify", code, START) in ("none", "locked")
    assert conn.execute("SELECT used_at FROM email_codes").fetchone()["used_at"]


def test_correct_code_within_attempts_still_works(conn, uid):
    code = codes.issue_code(conn, SECRET, uid, "verify", START)
    bad = f"{(int(code) + 1) % 10**6:06d}"
    for _ in range(codes.MAX_ATTEMPTS - 1):
        assert codes.check_code(conn, SECRET, uid, "verify", bad, START) == "bad"
    assert codes.check_code(conn, SECRET, uid, "verify", code, START) == "ok"


def test_new_code_replaces_old(conn, uid):
    first = codes.issue_code(conn, SECRET, uid, "verify", START)
    later = START + codes.RESEND_COOLDOWN + timedelta(seconds=1)
    second = codes.issue_code(conn, SECRET, uid, "verify", later)
    if first != second:  # совпадение двух случайных кодов возможно, но тогда проверять нечего
        assert codes.check_code(conn, SECRET, uid, "verify", first, later) in ("bad", "none")
    assert codes.check_code(conn, SECRET, uid, "verify", second, later) == "ok"


def test_resend_cooldown_and_hourly_limit(conn, uid):
    assert codes.issue_code(conn, SECRET, uid, "verify", START)
    assert codes.issue_code(conn, SECRET, uid, "verify", START + timedelta(seconds=30)) is None
    t = START
    issued = 1
    for _ in range(20):
        t += codes.RESEND_COOLDOWN + timedelta(seconds=1)
        if codes.issue_code(conn, SECRET, uid, "verify", t):
            issued += 1
    assert issued == codes.MAX_PER_HOUR
    # через час счёт обнуляется
    assert codes.issue_code(conn, SECRET, uid, "verify", START + timedelta(hours=2))


def test_cooldown_can_be_skipped_but_hour_limit_stays(conn, uid):
    for _ in range(codes.MAX_PER_HOUR):
        assert codes.issue_code(conn, SECRET, uid, "verify", START, cooldown=False)
    assert codes.issue_code(conn, SECRET, uid, "verify", START, cooldown=False) is None


def test_unknown_purpose_rejected(conn, uid):
    with pytest.raises(ValueError):
        codes.issue_code(conn, SECRET, uid, "login", START)


def test_purge_removes_old_codes(conn, uid):
    codes.issue_code(conn, SECRET, uid, "verify", START)
    assert codes.purge_codes(conn, START + timedelta(days=3)) == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM email_codes").fetchone()["n"] == 0


def test_codes_deleted_with_user(conn, uid):
    codes.issue_code(conn, SECRET, uid, "verify", START)
    service.delete_account(conn, uid)
    assert conn.execute("SELECT COUNT(*) AS n FROM email_codes").fetchone()["n"] == 0


def test_verify_by_code_activates_account(conn):
    reg = service.register(conn, email="a@example.test", password="tulip-garden-77", name="А", role="job_seeker",
                           consent=True, now=START)
    code = service.issue_verify_code(conn, SECRET, reg.user_id, START, cooldown=False)
    assert service.verify_by_code(conn, SECRET, "a@example.test", "000000" if code != "000000" else "111111", START)[0] == "bad"
    status, user = service.verify_by_code(conn, SECRET, "A@Example.test", code, START)
    assert status == "ok" and user["verified"]
    # ссылка из того же письма после кода уже не нужна
    assert service.verify_email(conn, reg.token, START) is None


def test_verify_by_code_unknown_or_verified_is_none(conn):
    assert service.verify_by_code(conn, SECRET, "ghost@example.test", "123456", START) == ("none", None)
    reg = service.register(conn, email="a@example.test", password="tulip-garden-77", name="А", role="job_seeker",
                           consent=True, now=START)
    service.verify_email(conn, reg.token, START)
    assert service.issue_verify_code(conn, SECRET, reg.user_id, START) is None
    assert service.verify_by_code(conn, SECRET, "a@example.test", "123456", START) == ("none", None)


def test_reset_by_code_sets_password_and_closes_sessions(conn):
    reg = service.register(conn, email="a@example.test", password="tulip-garden-77", name="А", role="job_seeker",
                           consent=True, now=START)
    service.verify_email(conn, reg.token, START)
    service.create_session(conn, reg.user_id, START)
    found = service.request_reset_code(conn, SECRET, "a@example.test", START)
    assert found
    _, code = found
    status, user = service.reset_password_by_code(conn, SECRET, "a@example.test", code, "brand-new-pass-9", START)
    assert status == "ok" and user
    assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 0
    assert service.authenticate(conn, "a@example.test", "brand-new-pass-9").status == "ok"
    # код израсходован
    again = service.reset_password_by_code(conn, SECRET, "a@example.test", code, "another-pass-77", START)
    assert again[0] == "none"


def test_request_reset_code_unknown_and_blocked(conn):
    assert service.request_reset_code(conn, SECRET, "ghost@example.test", START) is None
    reg = service.register(conn, email="a@example.test", password="tulip-garden-77", name="А", role="job_seeker",
                           consent=True, now=START)
    service.set_blocked(conn, reg.user_id, True)
    assert service.request_reset_code(conn, SECRET, "a@example.test", START) is None
