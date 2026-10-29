"""Фикстуры тестов реального времени: чистое окружение, база во временной папке, подмена кэша картинок."""
from __future__ import annotations

import pytest
from rt_helpers import ImageProvider

from rabota import config
from rabota.realtime.ingest import Ingestor

ENV_NAMES = (
    "TG_API_ID", "TG_API_HASH", "TG_SESSION_PATH", "TG_PROXY", "TG_CHANNELS_FILE", "TG_SWEEP_SECONDS",
    "VK_ACCESS_TOKEN", "MEDIA_DIR", "HEARTBEAT_PATH", "WORKER_EXPIRE", "LLM_PROVIDER", "LLM_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setattr("rabota.vision.images.CACHE_DIR", tmp_path / "vision_cache")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "ROOT", tmp_path)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "t.db"


@pytest.fixture
def ingestor(db_path):
    return Ingestor(db_path, provider=ImageProvider())
