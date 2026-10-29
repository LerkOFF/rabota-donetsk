"""Командная строка проекта.

    python -m rabota collect [--limit 10] [--source telegram --source vk]   сбор сырых постов
    python -m rabota process                                                  разбор текста и картинок, склейка дублей
    python -m rabota expire                                                   скрыть вакансии старше 21 дня, удалить давно скрытые
    python -m rabota run [--limit 10]                                         всё сразу
    python -m rabota stats                                                    сводка по базе
    python -m rabota serve [--port 8000]                                      сайт
    python -m rabota worker                                                   приём данных в реальном времени
    python -m rabota make-admin <почта>                                       сделать пользователя администратором
    python -m rabota health [--json]                                          свежесть источников, код 1 при просрочке
    python -m rabota tg-login                                                 один раз: вход в Telegram для слушателя
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sqlite3
import sys
from pathlib import Path

from rabota import config
from rabota.db import connect, load_raw, save_raw, save_vacancy
from rabota.lifecycle import expire, purge
from rabota.models import RawPost

log = logging.getLogger("rabota")

COLLECTORS = ("telegram", "max", "vk", "trudvsem", "mczn", "hh", "avito")
SAMPLES_DIR = config.ROOT / "data" / "samples"


def _collector(name: str):
    return importlib.import_module(f"rabota.collectors.{name}")


def cmd_collect(conn: sqlite3.Connection, limit: int, sources: list[str]) -> dict[str, int]:
    """Запустить сборщики, сохранить сырые посты в базу и по выгрузке на источник в data/samples."""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    totals: dict[str, int] = {}
    for name in sources or COLLECTORS:
        try:
            posts: list[RawPost] = _collector(name).fetch(limit=limit)
        except Exception as exc:  # noqa: BLE001 - один источник не роняет остальные
            log.warning("%s: сбор не удался: %s", name, exc)
            print(f"{name}: ОШИБКА {type(exc).__name__}: {exc}")
            totals[name] = 0
            continue
        new = sum(save_raw(conn, p) for p in posts)
        (SAMPLES_DIR / f"{name}.json").write_text(
            json.dumps([p.to_dict() for p in posts], ensure_ascii=False, indent=1), encoding="utf-8"
        )
        totals[name] = len(posts)
        print(f"{name}: получено {len(posts)}, новых в базе {new}")
    return totals


def _vision_provider():
    from rabota.vision.providers import SidecarProvider, provider_from_env

    if config.get("LLM_PROVIDER") or config.get("LLM_API_KEY"):
        return provider_from_env()
    return SidecarProvider()  # локально: готовая разметка картинок из data/vision_cache


def cmd_process(conn: sqlite3.Connection) -> dict:
    """Разобрать текст, затем картинки там, где текста мало, склеить дубли."""
    from rabota.admin.stoplist import filter_vacancies
    from rabota.pipeline.run import process_db
    from rabota.realtime.ingest import new_image_vacancies

    stats = process_db(conn)
    provider = _vision_provider()
    from_images = 0
    for raw in load_raw(conn):
        if not raw.image_urls:
            continue
        have = {
            r["profession"]
            for r in conn.execute(
                "SELECT profession FROM vacancies WHERE source=? AND external_id=?", (raw.source, raw.external_id)
            )
        }
        kept, dropped = filter_vacancies(conn, new_image_vacancies(raw, provider, have))  # стоп-лист
        stats["stoplisted"] = stats.get("stoplisted", 0) + dropped
        for vac in kept:
            save_vacancy(conn, vac)
            from_images += 1
    if from_images:
        from rabota.pipeline.dedupe import dedupe

        stats["duplicates"] = dedupe(conn)
    stats["from_images"] = from_images
    stats["expired"] = expire(conn)
    return stats


def cmd_stats(conn: sqlite3.Connection) -> None:
    q = conn.execute
    print("сырых постов по источникам:")
    for r in q("SELECT source_type, COUNT(*) c FROM raw_posts GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {r['source_type']}: {r['c']}")
    print("вакансий по типу поста:", {r["kind"]: r["c"] for r in q("SELECT kind, COUNT(*) c FROM vacancies GROUP BY 1")})
    shown = q("SELECT COUNT(*) FROM vacancies WHERE kind='vacancy' AND status='active' AND duplicate_of IS NULL").fetchone()[0]
    dups = q("SELECT COUNT(*) FROM vacancies WHERE duplicate_of IS NOT NULL").fetchone()[0]
    print(f"на сайте: {shown}, склеено дублей: {dups}")


def cmd_make_admin(conn: sqlite3.Connection, email: str) -> int:
    from rabota.admin.cli import make_admin

    try:
        user_id, created = make_admin(conn, email)
    except ValueError as exc:
        print(f"Не получилось: {exc}", file=sys.stderr)
        return 1
    print(f"{email.strip().lower()}: {'аккаунт создан' if created else 'роль администратора выдана'} (id {user_id}).")
    if created:
        print("Пароль не задан. Откройте /forgot на сайте, введите эту почту и задайте пароль по ссылке из письма.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rabota")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("collect", "run"):
        p = sub.add_parser(name)
        p.add_argument("--limit", type=int, default=10, help="постов на канал или раздел")
        p.add_argument("--source", action="append", default=[], choices=COLLECTORS)
    sub.add_parser("process")
    sub.add_parser("expire", help="скрыть вакансии старше срока жизни и удалить давно скрытые")
    sub.add_parser("stats")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    sub.add_parser("worker", help="воркер реального времени: Telegram, VK, открытые базы, expire")
    health = sub.add_parser("health", help="свежесть источников; код возврата 1, если что-то просрочено")
    health.add_argument("--json", action="store_true")
    mk = sub.add_parser("make-admin", help="сделать пользователя администратором (пароль не передаётся в аргументах)")
    mk.add_argument("email")
    sub.add_parser("tg-login", help="интерактивный вход в Telegram (телефон, код, пароль), сессия в файл")
    args = parser.parse_args(argv)

    if args.cmd == "worker":
        from rabota.realtime.worker import main as worker_main

        return worker_main()
    if args.cmd == "health":
        from rabota.realtime.health import main as health_main

        return health_main(["--json"] if args.json else [])
    if args.cmd == "tg-login":
        from rabota.realtime.telegram_listener import login

        return login()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("rabota.web.app:app", host="127.0.0.1", port=args.port)
        return 0

    conn = connect(Path(config.DB_PATH))
    if args.cmd == "make-admin":
        return cmd_make_admin(conn, args.email)
    if args.cmd in ("collect", "run"):
        cmd_collect(conn, args.limit, args.source)
    if args.cmd in ("process", "run"):
        print("разбор:", cmd_process(conn))
    if args.cmd == "expire":
        print({"скрыто": expire(conn), "удалено": purge(conn)})
    if args.cmd in ("stats", "run"):
        cmd_stats(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
