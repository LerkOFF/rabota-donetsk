"""Проверка настроек и готовности к запуску.

    .venv/bin/python scripts/preflight.py [--env-file .env] [--strict] [--migrate] [--smoke]

--migrate  открыть боевую базу и довести схему до текущей (создаёт недостающие таблицы и колонки);
--smoke    собрать приложение на временной базе и проверить /healthz, главную и /robots.txt без сети.
Секреты не печатаются.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_env(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values

    values = {k: (v or "") for k, v in dotenv_values(path).items()} if path.is_file() else {}
    return {**values, **{k: v for k, v in os.environ.items() if k in values}}


def migrate(env: dict[str, str]) -> None:
    for key, value in env.items():
        os.environ.setdefault(key, value)
    from rabota import config
    from rabota.web.app import create_app

    path = Path(env.get("DB_PATH") or config.DB_PATH)
    create_app(path)  # подключение аккаунтов создаёт недостающие таблицы
    print(f"схема базы актуальна: {path}")


def smoke() -> None:
    from fastapi.testclient import TestClient

    from rabota.web.app import create_app

    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(Path(tmp) / "smoke.db", site_url="https://example.test")
        with TestClient(app, base_url="https://example.test") as client:
            for url, expect in (("/healthz", 200), ("/", 200), ("/vacancies", 200), ("/robots.txt", 200), ("/sitemap.xml", 200)):
                code = client.get(url).status_code
                if code != expect:
                    raise SystemExit(f"дымовая проверка: {url} вернул {code}, ожидалось {expect}")
    print("дымовая проверка приложения прошла")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--strict", action="store_true", help="предупреждения тоже считать ошибкой")
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)

    from rabota.ops.preflight import ERROR, check

    env = load_env(Path(args.env_file))
    found = check(env)
    for p in found:
        print(p)
    errors = [p for p in found if p.level == ERROR]
    bad = found if args.strict else errors
    if bad:
        print(f"Итог: проблем {len(bad)}, запуск остановлен.")
        return 1
    print("Итог: настройки в порядке." if not found else f"Итог: ошибок нет, предупреждений {len(found)}.")
    if args.smoke:
        smoke()
    if args.migrate:
        migrate(env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
