"""Помощники тестов размещения вакансии гостем: форма, код из письма, подтверждение."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from rabota.accounts.mailer import MemoryMailer
from tests.accounts.harness import code_in

ORIGIN = {"Origin": "http://testserver"}
_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')

GUEST_EMAIL = "boss@example.test"


def csrf(client: TestClient, path: str = "/razmestit") -> str:
    m = _CSRF.search(client.get(path).text)
    assert m, f"на {path} нет csrf_token"
    return m.group(1)


@dataclass
class Poster:
    """Гость, который размещает вакансию: форма, затем код из письма."""

    client: TestClient
    mailer: MemoryMailer

    def submit(self, form: dict[str, str], email: str = GUEST_EMAIL, *, consent: bool = True, **over: Any):
        data = {"csrf_token": csrf(self.client), "account_email": email, **form}
        if consent:
            data["consent"] = "1"
        return self.client.post("/razmestit", data=data, headers=ORIGIN, follow_redirects=False, **over)

    def token_of(self, response) -> str:
        assert response.status_code == 303, response.text
        loc = response.headers["location"]
        assert loc.startswith("/razmestit/kod/"), loc
        return loc.rsplit("/", 1)[1]

    def confirm(self, token: str, code: str | None = None, email: str = GUEST_EMAIL):
        code = code or code_in(self.mailer.last(email))
        data = {"csrf_token": csrf(self.client, f"/razmestit/kod/{token}"), "code": code}
        return self.client.post(f"/razmestit/kod/{token}", data=data, headers=ORIGIN, follow_redirects=False)

    def publish(self, form: dict[str, str], email: str = GUEST_EMAIL):
        """Вся цепочка: форма, письмо, код. Возвращает последний ответ (перенаправление на страницу «Готово»)."""
        first = self.submit(form, email)
        return self.confirm(self.token_of(first), email=email)
