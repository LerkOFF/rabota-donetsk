"""Контакты из текста: телефоны (+7949..., +7856...), ники Telegram, MAX, почта."""
from __future__ import annotations

import re
from typing import Any

from rabota.pipeline.dictionaries import CHANNEL_HANDLES, norm
from rabota.pipeline.homoglyphs import fix_homoglyphs

# телефон: необязательный код страны, затем 10 цифр с любыми разделителями
_PHONE = re.compile(
    r"(?<![\w@/])(?P<cc>\+\s*7|\+\s*38|\b8|\b7)?[\s\-(.]*(?P<code>\d{3})[\s\-).]*(?P<a>\d{3})[\s\-.]*(?P<b>\d{2})[\s\-.]*(?P<c>\d{2})(?!\d)"
)
_PHONE_LOOSE_TAIL = re.compile(r"(?<![\w@/])\+\s*7[\s\-(]*(\d{3})[\s\-)]*(\d)[\s\-]*(\d)[\s\-]*(\d)[\s\-]*(\d)[\s\-]*(\d)[\s\-]*(\d)[\s\-]*(\d)(?!\d)")
_TG_HANDLE = re.compile(r"(?<![\w@.])@([A-Za-z][A-Za-z0-9_]{3,31})(?![\w@])")
_TG_LINK = re.compile(r"(?:https?://)?t\.me/(?!joinchat|\+)([A-Za-z][A-Za-z0-9_]{3,31})", re.IGNORECASE)
_EMAIL = re.compile(r"(?<![\w.])[A-Za-z0-9._%+\-]{2,}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_MAX_NEAR = re.compile(r"м[аa][хx]\b|макс\b|max\b", re.IGNORECASE)
_DNR_CODES = ("949", "856", "857", "858", "959", "71", "72")


def _normalize_phone(m: re.Match[str]) -> str | None:
    cc = (m.group("cc") or "").replace(" ", "")
    digits = m.group("code") + m.group("a") + m.group("b") + m.group("c")
    if cc.startswith("+38"):
        return None
    if not cc:
        # без кода страны берём только похожее на мобильный или ДНР: 9xx..., 856...
        if not (digits.startswith(("9", "856", "3"))):
            return None
        if digits.startswith("3"):
            return None
    return "+7" + digits


def extract_phones(text: str) -> list[str]:
    """Телефоны в формате +7XXXXXXXXXX, в порядке появления, без повторов."""
    text = fix_homoglyphs(text)
    found: list[tuple[int, str]] = []
    for m in _PHONE.finditer(text):
        p = _normalize_phone(m)
        if p:
            found.append((m.start(), p))
    # хвост, разорванный пробелами: "+7 949 73 70 308"
    for m in _PHONE_LOOSE_TAIL.finditer(text):
        p = "+7" + m.group(1) + "".join(m.group(i) for i in range(2, 9))
        if len(p) == 12:
            found.append((m.start(), p))
    found.sort()
    out: list[str] = []
    for _, p in found:
        if p not in out:
            out.append(p)
    return out


def extract_telegram(text: str, source: str = "") -> list[str]:
    """Ники Telegram (@name), без своих каналов площадки."""
    handles: list[tuple[int, str]] = []
    emails = [(m.start(), m.end()) for m in _EMAIL.finditer(text)]
    for m in _TG_HANDLE.finditer(text):
        if any(s <= m.start() < e for s, e in emails):
            continue
        handles.append((m.start(), m.group(1)))
    for m in _TG_LINK.finditer(text):
        handles.append((m.start(), m.group(1)))
    handles.sort()
    blocked = set(CHANNEL_HANDLES)
    if source:
        blocked.add(source.lower().lstrip("@"))
    out: list[str] = []
    for _, h in handles:
        if h.lower() in blocked:
            continue
        tag = "@" + h
        if tag not in out:
            out.append(tag)
    return out


def extract_email(text: str) -> str | None:
    m = _EMAIL.search(text)
    return m.group().lower() if m else None


def extract_max_phone(text: str, phones: list[str]) -> str | None:
    """Телефон, рядом с которым сказано про MAX ("Телефон и MAX: ...", "+7949... (Telegram, Max)")."""
    t = fix_homoglyphs(text)
    for m in _PHONE.finditer(t):
        p = _normalize_phone(m)
        if not p:
            continue
        ls = t.rfind("\n", 0, m.start()) + 1
        le = t.find("\n", m.end())
        window = t[ls: le if le != -1 else len(t)]
        if _MAX_NEAR.search(window):
            return p
    return None


def extract_contacts(text: str, source: str = "") -> dict[str, Any]:
    """Контакты для карточки: {"phones": [...], "telegram": "@x", "max": "+7...", "email": "..."}."""
    phones = extract_phones(text)
    contacts: dict[str, Any] = {}
    if phones:
        contacts["phones"] = phones
    tg = extract_telegram(text, source)
    if tg:
        contacts["telegram"] = tg[0]
        if len(tg) > 1:
            contacts["telegram_extra"] = tg[1:]
    mx = extract_max_phone(text, phones)
    if mx:
        contacts["max"] = mx
    email = extract_email(text)
    if email:
        contacts["email"] = email
    return contacts


def short_unusual(text: str) -> bool:
    """Есть ли в тексте только слова про связь без самого контакта (для классификации заглушек)."""
    return not extract_phones(text) and not extract_telegram(text) and "@" not in norm(text)
