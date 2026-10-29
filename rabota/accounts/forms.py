"""Разбор тела формы application/x-www-form-urlencoded с ограничением размера."""
from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import Request
from starlette.exceptions import HTTPException

MAX_BODY = 16 * 1024


async def read_form(request: Request) -> dict[str, str]:
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise HTTPException(413, "Слишком большой запрос")
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True, max_num_fields=40)
    return {k: v[0] for k, v in parsed.items()}
