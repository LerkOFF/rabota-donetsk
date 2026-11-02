"""Файл ключа IndexNow: /<ключ>.txt с самим ключом. Маршрут появляется только при заданном INDEXNOW_KEY."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


def install_indexnow(app: FastAPI, key: str) -> None:
    if not key:
        return

    @app.get(f"/{key}.txt", include_in_schema=False)
    def indexnow_key() -> PlainTextResponse:
        return PlainTextResponse(key, headers={"Cache-Control": "public, max-age=3600"})
