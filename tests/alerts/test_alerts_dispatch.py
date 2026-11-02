"""Рассылка писем: поиск новых вакансий, дедуп отправленного, лимиты, режимы без почты, содержимое письма."""
import email
from datetime import timedelta

from rabota.accounts.mailer import MemoryMailer, build_message
from rabota.alerts import conf, dispatch, service
from tests.alerts.helpers import NOW, SECRET, SITE, user, vac

SEARCH = {"q": "бариста", "city": "Донецк"}


def run(conn, mailer, now=NOW + timedelta(hours=1), **kw):
    kw.setdefault("sleep", lambda s: None)
    return dispatch.run_email_cycle(conn, mailer, now, site_url=SITE, secret=SECRET, **kw)


def sub_for(conn, me=None, data=SEARCH, frequency="instant"):
    me = me or user(conn)
    row, _ = service.subscribe_user(conn, me, data, frequency=frequency, now=NOW)
    return row


def setting(conn, key, value):
    conn.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,'x') ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    conn.commit()


# ------------------------------------------------------------------ что считается новым

def test_only_new_matching_visible_vacancies_are_found(conn):
    vac(conn)  # было до подписки
    sub = sub_for(conn)
    good = vac(conn, profession="бариста")
    vac(conn, profession="кассир")
    vac(conn, profession="бариста", city="Макеевка")
    vac(conn, profession="бариста", status="hidden")
    vac(conn, profession="бариста", suspicious=True)
    vac(conn, profession="бариста", duplicate_of=good)
    pending = vac(conn, profession="бариста")
    conn.execute("UPDATE vacancies SET moderation_status='pending' WHERE id=?", (pending,))
    conn.commit()
    assert [r["id"] for r in dispatch.find_new(conn, sub)] == [good]


def test_filters_work_like_the_feed(conn):
    me = user(conn)
    sub = sub_for(conn, me, {"side_job": True, "pay_from": 30000, "daily": True})
    ok = vac(conn, employment="подработка", pay_min=40000, pay_max=40000, pay_period="месяц", pay_daily=True)
    vac(conn, employment="подработка", pay_min=20000, pay_period="месяц", pay_daily=True)
    vac(conn, employment="постоянно", pay_min=40000, pay_period="месяц", pay_daily=True)
    vac(conn, employment="подработка", pay_min=40000, pay_period="месяц", pay_daily=False)
    assert [r["id"] for r in dispatch.find_new(conn, sub)] == [ok]


def test_new_vacancies_come_newest_first(conn):
    sub = sub_for(conn)
    a, b = vac(conn, published_at=None), vac(conn, published_at=None)
    ids = [r["id"] for r in dispatch.find_new(conn, sub)]
    assert set(ids) == {a, b}


# ------------------------------------------------------------------ отправка и дедуп

def test_mail_goes_out_once_and_vacancies_are_not_repeated(conn):
    sub = sub_for(conn)
    first = vac(conn)
    mailer = MemoryMailer()
    res = run(conn, mailer)
    assert res["sent"] == 1 and len(mailer.outbox) == 1
    assert conn.execute("SELECT vacancy_id FROM alert_sent WHERE subscription_id=?", (sub["id"],)).fetchall()[0][0] == first
    assert run(conn, mailer, NOW + timedelta(hours=2))["sent"] == 0  # ничего нового
    second = vac(conn)
    assert run(conn, mailer, NOW + timedelta(hours=3))["sent"] == 1
    body = mailer.outbox[-1].body
    assert f"/vacancy/{second}" in body and f"/vacancy/{first}" not in body


def test_two_subscriptions_dedupe_independently(conn):
    a = sub_for(conn, user(conn, "a@example.test"))
    b = sub_for(conn, user(conn, "b@example.test"))
    vac(conn)
    mailer = MemoryMailer()
    assert run(conn, mailer)["sent"] == 2
    assert {m.to for m in mailer.outbox} == {"a@example.test", "b@example.test"}
    assert conn.execute("SELECT COUNT(*) FROM alert_sent WHERE subscription_id IN (?,?)", (a["id"], b["id"])).fetchone()[0] == 2


def test_nothing_new_nothing_sent_even_for_many_cycles(conn):
    sub_for(conn)
    mailer = MemoryMailer()
    for i in range(3):
        assert run(conn, mailer, NOW + timedelta(hours=i + 1))["sent"] == 0
    assert mailer.outbox == []


def test_digest_has_ten_cards_and_a_link_to_the_rest(conn):
    sub_for(conn)
    ids = [vac(conn) for _ in range(13)]
    mailer = MemoryMailer()
    assert run(conn, mailer)["sent"] == 1
    mail = mailer.outbox[0]
    assert mail.body.count("/vacancy/") == 10 and "И ещё 3 на сайте" in mail.body
    assert mail.html.count("/vacancy/") == 10 and "Все вакансии" in mail.html
    assert conn.execute("SELECT COUNT(*) FROM alert_sent").fetchone()[0] == len(ids)  # остаток тоже помечен: вторым письмом не придёт
    assert run(conn, mailer, NOW + timedelta(hours=3))["sent"] == 0


def test_mail_content_subject_headers_and_links(conn):
    sub = sub_for(conn)
    vid = vac(conn, employer="Кофейня Зерно", district="Ворошиловский", pay_min=25000, pay_max=35000, pay_period="месяц")
    mailer = MemoryMailer()
    run(conn, mailer)
    mail = mailer.outbox[0]
    assert mail.subject == "Новые вакансии: бариста в Донецке"
    assert "Бариста" in mail.body and "Кофейня Зерно" in mail.body and "Ворошиловский" in mail.body
    assert f"{SITE}/vacancy/{vid}" in mail.body and f"{SITE}/vacancy/{vid}" in mail.html
    assert f"{SITE}/vacancies?q=" in mail.body  # «Все вакансии» ведёт на ту же выдачу
    unsub = f"{SITE}/alerts/unsubscribe/{service.make_token(sub['id'], 'unsub', SECRET)}"
    assert unsub in mail.body and unsub in mail.html
    headers = dict(mail.headers)
    assert headers["List-Unsubscribe"] == f"<{unsub}>" and headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    for text in (mail.body, mail.html, mail.subject):
        assert "\u2014" not in text
    msg = email.message_from_bytes(bytes(build_message(mail, "noreply@example.test")))
    assert msg["List-Unsubscribe"] == f"<{unsub}>" and msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert msg.is_multipart() and {p.get_content_type() for p in msg.walk()} >= {"text/plain", "text/html"}


def test_html_escapes_vacancy_text(conn):
    sub_for(conn, data={"q": "бариста"})
    vac(conn, profession="бариста <script>alert(1)</script>", employer='Кафе "Зерно" & Ко')
    mailer = MemoryMailer()
    run(conn, mailer)
    assert "<script>" not in mailer.outbox[0].html and "&lt;script&gt;" in mailer.outbox[0].html


def test_header_injection_is_refused(conn):
    import pytest

    from rabota.accounts.mailer import Mail, MailError

    with pytest.raises(MailError):
        build_message(Mail(to="a@example.test", subject="s", body="b", headers=(("X-Test", "a\r\nBcc: evil@example.test"),)), "n@example.test")


# ------------------------------------------------------------------ частота

def test_instant_waits_ten_minutes_between_mails(conn):
    sub_for(conn)
    vac(conn)
    mailer = MemoryMailer()
    t = NOW + timedelta(hours=1)
    assert run(conn, mailer, t)["sent"] == 1
    vac(conn)
    assert run(conn, mailer, t + timedelta(minutes=5))["sent"] == 0
    assert run(conn, mailer, t + timedelta(minutes=11))["sent"] == 1


def test_daily_goes_out_once_after_nine_in_donetsk(conn):
    from datetime import UTC, datetime

    sub_for(conn, frequency="daily")
    vac(conn)
    mailer = MemoryMailer()
    day = datetime(2030, 1, 15, tzinfo=UTC)
    # 09:00 в Донецке это 06:00 UTC; вчерашняя проверка была раньше сегодняшнего окна
    conn.execute("UPDATE alert_subscriptions SET last_checked_at=?, last_sent_at=NULL", ((day - timedelta(days=1)).replace(hour=7).isoformat(),))
    conn.commit()
    mailer = MemoryMailer()
    assert run(conn, mailer, day.replace(hour=5, minute=59))["sent"] == 0  # ещё нет девяти по Донецку
    assert run(conn, mailer, day.replace(hour=6, minute=1))["sent"] == 1
    vac(conn)
    assert run(conn, mailer, day.replace(hour=12))["sent"] == 0  # новое уйдёт завтра
    assert run(conn, mailer, day.replace(hour=6, minute=5) + timedelta(days=1))["sent"] == 1


# ------------------------------------------------------------------ адресаты

def test_blocked_or_unverified_users_get_nothing(conn):
    sub_for(conn, user(conn, "blocked@example.test", status="blocked"))
    _row, _ = service.subscribe_user(conn, user(conn, "ok@example.test"), SEARCH, frequency="instant", now=NOW)
    conn.execute("UPDATE users SET email_verified_at=NULL WHERE email='ok@example.test'")
    conn.commit()
    vac(conn)
    mailer = MemoryMailer()
    assert run(conn, mailer)["sent"] == 0 and mailer.outbox == []


def test_paused_and_unconfirmed_subscriptions_are_skipped(conn):
    me = user(conn)
    row = sub_for(conn, me)
    service.pause(conn, me, row["id"])
    service.subscribe_guest(conn, "guest@example.test", SEARCH, frequency="instant", secret=SECRET, now=NOW)
    vac(conn)
    mailer = MemoryMailer()
    assert run(conn, mailer)["sent"] == 0


def test_guest_gets_mail_after_confirmation(conn):
    row, code = service.subscribe_guest(conn, "guest@example.test", SEARCH, frequency="instant", secret=SECRET, now=NOW)
    service.check_code(conn, row["id"], code, SECRET, NOW)
    vac(conn)
    mailer = MemoryMailer()
    assert run(conn, mailer)["sent"] == 1 and mailer.outbox[0].to == "guest@example.test"


def test_account_email_change_is_followed(conn):
    me = user(conn)
    sub_for(conn, me)
    conn.execute("UPDATE users SET email='new@example.test' WHERE id=?", (me["id"],))
    conn.commit()
    vac(conn)
    mailer = MemoryMailer()
    run(conn, mailer)
    assert mailer.outbox[0].to == "new@example.test"


# ------------------------------------------------------------------ лимиты и сбои

def test_hourly_mail_cap_and_leftover_goes_next_hour(conn):
    for i in range(3):
        sub_for(conn, user(conn, f"u{i}@example.test"))
    vac(conn)
    setting(conn, "alerts_mail_per_hour", 2)
    mailer = MemoryMailer()
    t = NOW + timedelta(hours=1)
    res = run(conn, mailer, t)
    assert res["sent"] == 2 and res["limited"] is True
    assert run(conn, mailer, t + timedelta(minutes=20))["sent"] == 0  # окно часа ещё занято
    assert run(conn, mailer, t + timedelta(hours=1, minutes=1))["sent"] == 1
    assert len(mailer.outbox) == 3


def test_pause_between_mails(conn):
    for i in range(3):
        sub_for(conn, user(conn, f"u{i}@example.test"))
    vac(conn)
    setting(conn, "alerts_mail_pause", 7)
    pauses = []
    run(conn, MemoryMailer(), sleep=pauses.append)
    assert pauses == [7, 7, 7]


def test_failed_send_is_not_marked_and_is_retried(conn):
    sub = sub_for(conn)
    vac(conn)
    res = run(conn, MemoryMailer(fail=True))
    assert res["errors"] == 1 and res["sent"] == 0
    row = service.get(conn, sub["id"])
    assert row["error_count"] == 1 and "MailError" in row["last_error"] and row["last_sent_at"] is None
    assert conn.execute("SELECT COUNT(*) FROM alert_sent").fetchone()[0] == 0
    good = MemoryMailer()
    assert run(conn, good, NOW + timedelta(hours=2))["sent"] == 1
    assert service.get(conn, sub["id"])["error_count"] == 0 and service.get(conn, sub["id"])["last_error"] == ""


def test_cycle_stops_after_a_streak_of_failures(conn):
    for i in range(6):
        sub_for(conn, user(conn, f"u{i}@example.test"))
    vac(conn)
    res = run(conn, MemoryMailer(fail=True))
    assert res["errors"] == dispatch.CIRCUIT


def test_subscription_is_paused_after_repeated_failures(conn):
    sub = sub_for(conn)
    vac(conn)
    for i in range(dispatch.FAILS_TO_PAUSE):
        run(conn, MemoryMailer(fail=True), NOW + timedelta(hours=i + 1))
    row = service.get(conn, sub["id"])
    assert row["status"] == "paused" and row["error_count"] >= dispatch.FAILS_TO_PAUSE


def test_switched_off_in_admin_sends_nothing_and_marks_nothing(conn):
    sub_for(conn)
    vac(conn)
    setting(conn, "alerts_email_enabled", 0)
    mailer = MemoryMailer()
    res = run(conn, mailer)
    assert res["disabled"] is True and mailer.outbox == []
    assert conn.execute("SELECT COUNT(*) FROM alert_sent").fetchone()[0] == 0
    setting(conn, "alerts_email_enabled", 1)
    assert run(conn, mailer)["sent"] == 1


# ------------------------------------------------------------------ режим без почты

def test_mail_mode_detection(monkeypatch):
    assert conf.mail_enabled({"MAIL_BACKEND": "smtp", "SMTP_HOST": "127.0.0.1"}) is True
    assert conf.mail_enabled({"MAIL_BACKEND": "smtp"}) is False  # SMTP без сервера
    monkeypatch.setenv("ENVIRONMENT", "production")
    assert conf.mail_enabled({"MAIL_BACKEND": "file"}) is False  # на проде файловый режим письма не доставит
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert conf.mail_enabled({"MAIL_BACKEND": "file"}) is True
    assert conf.mail_enabled({"MAIL_BACKEND": "pigeon"}) is False


def test_worker_has_no_mail_job_without_mail(tmp_path, monkeypatch):
    from rabota.alerts.jobs import build_alert_jobs

    monkeypatch.setenv("ENVIRONMENT", "production")
    names = [j.name for j in build_alert_jobs(tmp_path / "x.db", {"MAIL_BACKEND": "file"})]
    assert "alerts-email" not in names
    names = [j.name for j in build_alert_jobs(tmp_path / "x.db", {"MAIL_BACKEND": "smtp", "SMTP_HOST": "127.0.0.1"})]
    assert names == ["alerts-email"]  # без токена бота задач Telegram нет


def test_email_job_runs_end_to_end(tmp_path):
    import asyncio

    from rabota.alerts.jobs import build_alert_jobs
    from rabota.web.queries import open_conn

    path = tmp_path / "job.db"
    c = open_conn(path)
    _row, _ = service.subscribe_user(c, user(c), SEARCH, frequency="instant", now=NOW)
    vac(c)
    c.close()
    mailer = MemoryMailer()
    jobs = build_alert_jobs(path, {}, mailer=mailer, clock=lambda: NOW + timedelta(hours=1), site_url=SITE)
    job = next(j for j in jobs if j.name == "alerts-email")
    asyncio.run(job.run())
    assert len(mailer.outbox) == 1
    c = open_conn(path)
    assert c.execute("SELECT last_ok_at FROM ingest_state WHERE source='job:alerts-email'").fetchone()[0]
    c.close()
