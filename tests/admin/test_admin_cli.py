"""python -m rabota make-admin <почта>: без пароля в аргументах."""
import pytest

from rabota import __main__ as cli
from rabota.admin.cli import make_admin
from rabota.db import connect


def test_make_admin_creates_unusable_password_account(conn):
    uid, created = make_admin(conn, "Boss@Example.test")
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    assert created and u["role"] == "admin" and u["email"] == "boss@example.test"
    assert u["email_verified_at"] and u["status"] == "active"
    from rabota.accounts import passwords

    assert not passwords.verify_password("", u["password_hash"])


def test_make_admin_promotes_existing_keeps_password(conn):
    conn.execute("INSERT INTO users(email, password_hash, role, email_verified_at) VALUES ('u@example.test','HASH','employer',NULL)")
    conn.commit()
    uid, created = make_admin(conn, "u@example.test")
    u = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    assert not created and u["role"] == "admin" and u["password_hash"] == "HASH" and u["email_verified_at"]


def test_make_admin_bad_email(conn):
    with pytest.raises(ValueError):
        make_admin(conn, "not-an-email")


def test_make_admin_audited(conn):
    from rabota.admin import audit

    make_admin(conn, "boss@example.test")
    assert audit.recent(conn)[0]["action"] == "admin.make"


def test_cli_command(tmp_path, monkeypatch, capsys):
    db = tmp_path / "cli.db"
    monkeypatch.setattr(cli.config, "DB_PATH", db)
    assert cli.main(["make-admin", "boss@example.test"]) == 0
    out = capsys.readouterr().out
    assert "boss@example.test" in out and "/forgot" in out
    c = connect(db)
    assert c.execute("SELECT role FROM users").fetchone()[0] == "admin"


def test_cli_rejects_password_argument(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "DB_PATH", tmp_path / "x.db")
    with pytest.raises(SystemExit):
        cli.main(["make-admin", "boss@example.test", "--password", "123"])


def test_cli_bad_email_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "DB_PATH", tmp_path / "x.db")
    assert cli.main(["make-admin", "nope"]) == 1
