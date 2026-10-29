"""Пустое значение в .env (как в .env.example) не должно перекрывать значение по умолчанию."""
import importlib

from rabota import config


def test_empty_env_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("SOME_SETTING", "")
    assert config.get("SOME_SETTING", "запасное") == "запасное"


def test_set_env_value_wins(monkeypatch):
    monkeypatch.setenv("SOME_SETTING", "своё")
    assert config.get("SOME_SETTING", "запасное") == "своё"


def test_empty_db_path_keeps_default_location(monkeypatch):
    monkeypatch.setenv("DB_PATH", "")
    try:
        reloaded = importlib.reload(config)
        assert reloaded.DB_PATH.name == "rabota.db"
        assert reloaded.DB_PATH.parent.name == "data"
    finally:
        monkeypatch.delenv("DB_PATH", raising=False)
        importlib.reload(config)
