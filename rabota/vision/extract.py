"""Картинка в вакансии: решение, нужна ли картинка, вызов провайдера, сборка Vacancy."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rabota.models import RawPost, Vacancy
from rabota.vision.images import ImageError, VisionCache, download_image, image_hash
from rabota.vision.providers import (
    ProviderError,
    VisionProvider,
    empty_result,
    normalize_output,
    provider_from_env,
)

log = logging.getLogger("rabota.vision")

# текст короче этого считается коротким: вакансия, скорее всего, на картинке
SHORT_TEXT_CHARS = 200

PHONE_RE = re.compile(r"(?<![\d])(?:\+?[78][\s\-()]*)?\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?![\d])")
PAY_WORDS_RE = re.compile(r"(?:₽|руб|\bр\.|\bр\b|грн|зарплат|оплат|ставк|з/?п\b|тыс|\d\s*к\b)", re.IGNORECASE)
ADDRESS_RE = re.compile(
    r"(?:\bул\.|\bул\b|улица|\bпр\.|проспект|бульвар|\bбул\.|переулок|\bпер\.|\bадрес|район|\bг\.\s?[А-ЯЁ]|"
    r"\bд\.\s?\d)",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"\d{1,3}(?:[  ]\d{3})+|\d+")
K_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:к\b|тыс)", re.IGNORECASE)
TG_RE = re.compile(r"(?:t\.me/|telegram\.me/|@)([A-Za-z0-9_]{3,32})")


def normalize_phone(raw: str) -> str | None:
    """+7949XXXXXXX для полных номеров, короткие номера (3-6 цифр) остаются цифрами, остальное None."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    if 3 <= len(digits) <= 6:
        return digits
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    if len(digits) == 10 and digits.startswith("062"):
        # старый городской код Донецка 062 теперь 856
        return "+7856" + digits[3:]
    if len(digits) == 10 and digits[:3] in ("856", "949", "959", "071"):
        return "+7" + digits
    return None


def normalize_phones(items: list[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        phone = normalize_phone(item)
        if phone and phone not in out:
            out.append(phone)
    return out


def phones_in_text(text: str) -> list[str]:
    return normalize_phones([m.group(0) for m in PHONE_RE.finditer(text or "")])


def normalize_telegram(value: str | None) -> str | None:
    if not value:
        return None
    m = TG_RE.search(value)
    if m:
        return "@" + m.group(1)
    bare = value.strip()
    return "@" + bare if re.fullmatch(r"[A-Za-z0-9_]{3,32}", bare) else None


def pay_numbers_in_text(text: str) -> set[int]:
    """Суммы, упомянутые в тексте. Телефоны вырезаются, чтобы не принять их за оплату."""
    clean = PHONE_RE.sub(" ", text or "")
    nums: set[int] = set()
    for m in K_RE.finditer(clean):
        nums.add(int(float(m.group(1).replace(",", ".")) * 1000))
    for m in NUMBER_RE.finditer(clean):
        value = int(re.sub(r"\D", "", m.group(0)))
        if 100 <= value <= 10_000_000:
            nums.add(value)
    return nums


def text_has_pay(text: str) -> bool:
    return bool(PAY_WORDS_RE.search(text or "")) and bool(pay_numbers_in_text(text))


def text_has_phone(text: str) -> bool:
    return bool(phones_in_text(text)) or bool(re.search(r"(?:тел|звон|whatsapp|вайбер|viber)", text or "", re.IGNORECASE))


def text_has_address(text: str) -> bool:
    return bool(ADDRESS_RE.search(text or ""))


def needs_image_recognition(text: str) -> bool:
    """Картинка идёт в распознавание, если текст короткий либо в нём нет оплаты, телефона или адреса.

    Если в посте уже есть все три, картинка ничего не добавит и запрос к модели не тратим.
    """
    text = (text or "").strip()
    if len(text) < SHORT_TEXT_CHARS:
        return True
    return not (text_has_pay(text) and text_has_phone(text) and text_has_address(text))


def _read_source(source: str | bytes | Path, fetch: Callable[[str], bytes]) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, Path):
        return source.read_bytes()
    return fetch(source)


def extract_from_image(
    url_or_bytes: str | bytes | Path,
    hint_text: str = "",
    provider: VisionProvider | None = None,
    *,
    cache: VisionCache | None = None,
    fetch: Callable[[str], bytes] | None = None,
    post_key: str | None = None,
) -> dict[str, Any]:
    """Распознаёт одну картинку. Возвращает ответ провайдера плюс служебные поля:

    image_hash, cached (ответ из кэша), skipped="banner" (повторяющийся баннер, провайдер не вызывали),
    error (картинку или провайдера не удалось использовать, ответ пустой kind="other").
    post_key нужен для учёта повторов: баннер это картинка из нескольких разных постов.
    """
    cache = cache or VisionCache()
    fetch = fetch or download_image
    try:
        data = _read_source(url_or_bytes, fetch)
        h = image_hash(data)
    except (ImageError, OSError) as exc:
        log.warning("vision: картинка недоступна: %s", exc)
        return {**empty_result(), "image_hash": None, "error": str(exc)}

    cached = cache.get(h, max_distance=2)
    if cached is not None:
        return {**normalize_output(cached), "image_hash": h, "cached": True}

    if post_key:
        cache.note_seen(h, post_key)
    if cache.is_banner(h):
        return {**empty_result("ad"), "image_hash": h, "skipped": "banner"}

    provider = provider or provider_from_env()
    try:
        result = provider.extract(data, hint_text)
    except ProviderError as exc:
        log.warning("vision: провайдер не дал ответ: %s", exc)
        return {**empty_result(), "image_hash": h, "error": str(exc)}
    result = normalize_output(result)
    if provider.cacheable:
        cache.put(h, result)
    return {**result, "image_hash": h}


def _summary(item: dict[str, Any], conflicts: list[str]) -> str:
    first = item["profession"]
    if item.get("employer"):
        first += f", {item['employer']}"
    parts = [first.rstrip(".") + "."]
    detail = item.get("extra_text") or item.get("requirements")
    if detail:
        detail = re.sub(r"\s+", " ", detail).strip()
        parts.append(detail[:220].rstrip(" ,;") + ("" if detail.endswith(".") else "."))
    if conflicts:
        parts.append("Картинка расходится с текстом поста (" + ", ".join(conflicts) + "), взяты данные из текста.")
    return " ".join(parts)


def build_vacancies(result: dict[str, Any], raw: RawPost) -> tuple[list[Vacancy], list[str]]:
    """Ответ провайдера в Vacancy. Второй элемент: расхождения картинки с текстом поста.

    При расхождении приоритет у текста: оплата с картинки отбрасывается, телефоны берутся из текста.
    """
    if result.get("kind") != "vacancy":
        return [], []
    text = raw.text or ""
    text_phones = [p for p in phones_in_text(text) if p.startswith("+")]
    text_numbers = pay_numbers_in_text(text) if text_has_pay(text) else set()

    conflicts: list[str] = []
    vacancies: list[Vacancy] = []
    for item in result.get("vacancies", []):
        item_conflicts: list[str] = []
        phones = normalize_phones(item.get("phones", []))
        full_img = [p for p in phones if p.startswith("+")]
        if text_phones and full_img and not set(text_phones) & set(full_img):
            item_conflicts.append("телефон")
            phones = list(text_phones)
        pay_min, pay_max, period = item.get("pay_min"), item.get("pay_max"), item.get("pay_period")
        img_pay = {v for v in (pay_min, pay_max) if v}
        if text_numbers and img_pay and not img_pay & text_numbers:
            item_conflicts.append("оплата")
            pay_min = pay_max = period = None
        contacts: dict[str, Any] = {}
        if phones:
            contacts["phones"] = phones
        tg = normalize_telegram(item.get("telegram"))
        if tg:
            contacts["telegram"] = tg
        vacancies.append(
            Vacancy(
                profession=item["profession"],
                title_raw=item["profession"],
                schedule=item.get("schedule"),
                pay_min=pay_min,
                pay_max=pay_max,
                pay_period=period,
                city=item.get("city"),
                district=item.get("district"),
                requirements=item.get("requirements"),
                employer=item.get("employer"),
                contacts=contacts,
                summary=_summary(item, item_conflicts),
                source_type=raw.source_type,
                source=raw.source,
                source_url=raw.url,
                external_id=raw.external_id,
                published_at=raw.published_at,
                kind="vacancy",
            )
        )
        for c in item_conflicts:
            if c not in conflicts:
                conflicts.append(c)
    return vacancies, conflicts


def vacancies_from_images(
    raw: RawPost,
    provider: VisionProvider | None = None,
    *,
    cache: VisionCache | None = None,
    fetch: Callable[[str], bytes] | None = None,
) -> list[Vacancy]:
    """Вакансии с картинок поста. Услуги, набор на службу, реклама и баннеры дают пустой список."""
    if not raw.image_urls or not needs_image_recognition(raw.text):
        return []
    cache = cache or VisionCache()
    post_key = f"{raw.source_type}:{raw.source}:{raw.external_id}"
    seen: set[str] = set()
    vacancies: list[Vacancy] = []
    for url in raw.image_urls:
        result = extract_from_image(url, raw.text, provider, cache=cache, fetch=fetch, post_key=post_key)
        h = result.get("image_hash")
        if h in seen:
            continue
        if h:
            seen.add(h)
        found, conflicts = build_vacancies(result, raw)
        if conflicts:
            log.info("vision: расхождение с текстом в %s: %s", raw.url, ", ".join(conflicts))
        vacancies.extend(found)
    return vacancies
