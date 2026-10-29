import importlib.util
import sys
from pathlib import Path

import pytest

from rabota.accounts import service
from rabota.db import connect

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_admin.py"


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("create_admin_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["create_admin_script"] = mod
    spec.loader.exec_module(mod)
    return mod


def answers(monkeypatch, *values):
    it = iter(values)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(it))


def test_script_creates_admin_without_printing_password(script, monkeypatch, tmp_path, capsys):
    db = tmp_path / "a.db"
    answers(monkeypatch, "admin-pass-phrase-5", "admin-pass-phrase-5")
    assert script.main(["Admin@Example.test", "--db", str(db)]) == 0
    out = capsys.readouterr()
    assert "admin@example.test" in out.out and "создан" in out.out
    assert "admin-pass-phrase-5" not in out.out + out.err
    conn = connect(db)
    row = service.get_user_by_email(conn, "admin@example.test")
    assert row["role"] == "admin" and row["email_verified_at"]
    assert service.authenticate(conn, "admin@example.test", "admin-pass-phrase-5").status == "ok"


def test_script_rejects_mismatch_and_weak_password(script, monkeypatch, tmp_path, capsys):
    db = tmp_path / "a.db"
    answers(monkeypatch, "admin-pass-phrase-5", "other")
    assert script.main(["a@example.test", "--db", str(db)]) == 1
    answers(monkeypatch, "12345678", "12345678")
    assert script.main(["a@example.test", "--db", str(db)]) == 1
    assert "простой" in capsys.readouterr().err
    assert connect(db).execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_script_updates_existing_account(script, monkeypatch, tmp_path, capsys):
    db = tmp_path / "a.db"
    conn = connect(db)
    from datetime import UTC, datetime
    service.register(conn, email="a@example.test", password="old-password-77", name="А", role="employer", consent=True, now=datetime.now(UTC))
    conn.close()
    answers(monkeypatch, "admin-pass-phrase-5", "admin-pass-phrase-5")
    assert script.main(["a@example.test", "--db", str(db)]) == 0
    assert "обновлён" in capsys.readouterr().out
    assert connect(db).execute("SELECT role FROM users").fetchone()[0] == "admin"
