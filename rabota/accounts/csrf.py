"""CSRF: двойная отправка с подписью. Случайное зерно лежит в cookie, в форму кладётся HMAC от зерна.

Подделать форму с чужого сайта нельзя: нужно знать и зерно, и серверный ключ. Cookie с чужого поддомена
тоже не помогает, потому что подпись проверяется ключом сервера.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from rabota import config

CSRF_COOKIE = "rabota_csrf"
FORM_FIELD = "csrf_token"
SEED_LEN = 43  # token_urlsafe(32)

log = logging.getLogger(__name__)


def new_seed() -> str:
    return secrets.token_urlsafe(32)


def valid_seed(seed: str | None) -> bool:
    return bool(seed) and len(seed) == SEED_LEN and all(c.isalnum() or c in "-_" for c in seed)


def sign(seed: str, secret: bytes) -> str:
    return hmac.new(secret, b"csrf:" + seed.encode("ascii"), hashlib.sha256).hexdigest()


def verify(seed: str | None, token: str | None, secret: bytes) -> bool:
    if not valid_seed(seed) or not token:
        return False
    return hmac.compare_digest(sign(seed or "", secret), token)


def same_origin(origin: str | None, host: str | None) -> bool:
    """Заголовок Origin, если он есть, должен указывать на этот же сайт."""
    if origin is None:
        return True
    if origin == "null" or not host:
        return False
    return urlparse(origin).netloc == host


def load_secret(explicit: str | bytes | None = None, data_dir: Path | None = None) -> bytes:
    """Ключ подписи: из аргумента, из SECRET_KEY или из файла data/.secret_key (создаётся при первом запуске)."""
    if explicit:
        return explicit.encode("utf-8") if isinstance(explicit, str) else explicit
    env = config.get("SECRET_KEY")
    if env:
        return env.encode("utf-8")
    directory = data_dir or config.DB_PATH.parent
    path = directory / ".secret_key"
    try:
        if path.exists():
            value = path.read_text().strip()
            if len(value) >= 32:
                return value.encode("utf-8")
        directory.mkdir(parents=True, exist_ok=True)
        value = secrets.token_urlsafe(48)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value)
        return value.encode("utf-8")
    except OSError:
        log.warning("не удалось сохранить ключ подписи форм, используется ключ на время работы процесса")
        return secrets.token_bytes(48)
