"""Провайдеры распознавания афиш и скриншотов.

Ответ любого провайдера приведён к одному виду (см. normalize_output):

    {"kind": "vacancy|service|military|ad|other",
     "vacancies": [{"profession", "pay_min", "pay_max", "pay_period", "city", "district",
                    "schedule", "requirements", "employer", "phones", "telegram", "extra_text"}],
     "raw_text": "весь прочитанный текст"}
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx

from rabota.config import get
from rabota.models import PAY_PERIODS, POST_KINDS
from rabota.vision.images import CACHE_DIR, ImageError, VisionCache, image_hash, open_image

log = logging.getLogger("rabota.vision")

DEFAULT_MODEL = "claude-haiku-5-5"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_API_SIDE = 1568
MAX_TOKENS = 2000

SIDECAR_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "vision" / "sidecar"

VACANCY_FIELDS = (
    "profession", "pay_min", "pay_max", "pay_period", "city", "district",
    "schedule", "requirements", "employer", "phones", "telegram", "extra_text",
)

PROMPT = """Ты разбираешь картинку из пабликов и каналов Донецка и ДНР: афишу или скриншот объявления.
Сначала определи, что это, потом достань поля. Ответ строго один JSON-объект, без пояснений и без markdown.

Шаг 1. Классификация, поле "kind":
- "vacancy": работодатель ищет сотрудника на работу или подработку;
- "service": мастер или бригада предлагает свои услуги (ремонт, отделка, перевозки, красота и т.п.);
- "military": набор на военную службу, контракт, мобилизационные объявления;
- "ad": обычная реклама товаров, магазинов, заведений, акции, заглушки вроде "Разместить вакансию";
- "other": всё остальное и всё нечитаемое.
Услуги мастеров и набор на службу НЕ вакансии. Если сомневаешься между vacancy и чем-то ещё, выбирай не vacancy.

Шаг 2. Только для kind="vacancy" заполни список "vacancies". Для остальных видов верни пустой список.
Одна афиша с несколькими должностями даёт несколько записей, по одной на должность. Общие для афиши
поля (работодатель, телефон, город, график) повтори в каждой записи. Если в строке две профессии
через запятую или "и" и у них одна оплата ("Продавец, кассир"), это одна запись.

Поля записи:
- "profession": должность так, как написана, в нормальном регистре ("Продавец, кассир", не капсом);
- "pay_min", "pay_max": целые числа в рублях без пробелов; одно число ("4000 р.") значит pay_min равно pay_max;
  вилка "от 48 500 - 56 000" значит 48500 и 56000; "до 100 000" значит pay_min null, pay_max 100000;
  оплаты нет или "по договорённости" значит оба null. Слова вроде "и выше" в числа не превращай;
- "pay_period": одно из "месяц", "смена", "день", "час", "задача" или null. Если период не назван, но
  оплата похожа на месячную (десятки тысяч), ставь "месяц", если сомневаешься, ставь null;
- "city", "district": город и район, только если они написаны на картинке, иначе null;
- "schedule": график работы как написано ("2/2", "5/2 с 9 до 18"), иначе null;
- "requirements": требования и условия кратко, иначе null;
- "employer": название организации или имя нанимателя, иначе null;
- "phones": список телефонов как на картинке. Короткие номера ("335") тоже телефоны, не отбрасывай;
- "telegram": ник или ссылка на Telegram, иначе null;
- "extra_text": остальное полезное для соискателя (льготы, оформление, адрес, условия звонка).

Поле "raw_text": весь прочитанный текст картинки как есть, строки через перевод строки.

Не выдумывай. Чего нет на картинке, того нет в ответе: ставь null или пустой список. Не додумывай телефон,
оплату и город по логотипу или фото. Если снизу приложен текст поста, он нужен только для понимания
контекста, поля бери с самой картинки.

Формат ответа:
{"kind": "vacancy", "vacancies": [{"profession": "...", "pay_min": null, "pay_max": null,
"pay_period": null, "city": null, "district": null, "schedule": null, "requirements": null,
"employer": null, "phones": [], "telegram": null, "extra_text": null}], "raw_text": "..."}"""


class ProviderError(Exception):
    """Провайдер не смог дать ответ. Текст ошибки никогда не содержит ключ."""


class VisionProvider(ABC):
    """Интерфейс: байты картинки (и необязательный текст поста) в словарь ответа."""

    # можно ли хранить ответ в кэше как окончательный
    cacheable: bool = False

    @abstractmethod
    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        ...


def empty_result(kind: str = "other", raw_text: str = "") -> dict[str, Any]:
    return {"kind": kind, "vacancies": [], "raw_text": raw_text}


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def _to_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("null", "none"):
        return None
    return text


def _normalize_vacancy(item: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["profession"] = _to_str(item.get("profession")) or ""
    out["pay_min"] = _to_int(item.get("pay_min"))
    out["pay_max"] = _to_int(item.get("pay_max"))
    if out["pay_min"] is None and out["pay_max"] is None:
        period = None
    else:
        period = (_to_str(item.get("pay_period")) or "").lower() or None
        if period not in PAY_PERIODS:
            period = None
    out["pay_period"] = period
    for key in ("city", "district", "schedule", "requirements", "employer", "telegram", "extra_text"):
        out[key] = _to_str(item.get(key))
    phones = item.get("phones")
    if isinstance(phones, str):
        phones = [phones]
    out["phones"] = [p for p in (_to_str(x) for x in (phones or [])) if p]
    return out


def normalize_output(data: Any) -> dict[str, Any]:
    """Приводит ответ модели или разметки к строгой схеме. Лишнее отбрасывается, типы чинятся."""
    if not isinstance(data, dict):
        return empty_result()
    kind = data.get("kind")
    kind = kind if kind in POST_KINDS else "other"
    raw_text = data.get("raw_text")
    raw_text = raw_text if isinstance(raw_text, str) else ""
    vacancies: list[dict[str, Any]] = []
    if kind == "vacancy":
        for item in data.get("vacancies") or []:
            if isinstance(item, dict):
                vac = _normalize_vacancy(item)
                if vac["profession"]:
                    vacancies.append(vac)
        if not vacancies:
            kind = "other"
    return {"kind": kind, "vacancies": vacancies, "raw_text": raw_text}


def parse_model_json(text: str) -> dict[str, Any]:
    """Достаёт JSON-объект из ответа модели, даже если он обёрнут в ```json или в пояснение."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ProviderError("в ответе модели нет JSON")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise ProviderError("ответ модели не разбирается как JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError("ответ модели не объект JSON")
    return data


def prepare_image_for_api(image_bytes: bytes) -> tuple[bytes, str]:
    """Байты и media_type для API. Большие картинки уменьшаются и сжимаются в JPEG."""
    img = open_image(image_bytes)
    fmt = (img.format or "").upper()
    media = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}.get(fmt)
    if media and max(img.size) <= MAX_API_SIDE and len(image_bytes) <= 4 * 1024 * 1024 and fmt != "GIF":
        return image_bytes, media
    img = img.convert("RGB")
    img.thumbnail((MAX_API_SIDE, MAX_API_SIDE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue(), "image/jpeg"


class AnthropicVisionProvider(VisionProvider):
    """Messages API через httpx. Ключ и модель из LLM_API_KEY и LLM_MODEL_VISION."""

    cacheable = True

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        client: httpx.Client | None = None,
        timeout: float = 60.0,
        url: str = ANTHROPIC_URL,
    ):
        key = api_key if api_key is not None else get("LLM_API_KEY")
        if not key:
            raise ProviderError("LLM_API_KEY не задан, провайдер не создан")
        self._api_key = key
        self.model = model or get("LLM_MODEL_VISION") or DEFAULT_MODEL
        self.timeout = timeout
        self.url = url
        self._client = client

    def __repr__(self) -> str:
        return f"AnthropicVisionProvider(model={self.model!r})"

    def build_request(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        data, media_type = prepare_image_for_api(image_bytes)
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode()},
            }
        ]
        hint = hint_text.strip()
        if hint:
            content.append({"type": "text", "text": f"Текст поста (для контекста):\n{hint[:1500]}"})
        content.append({"type": "text", "text": "Разбери картинку и верни JSON."})
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": PROMPT,
            "messages": [{"role": "user", "content": content}],
        }

    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        body = self.build_request(image_bytes, hint_text)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            resp = client.post(self.url, json=body, headers=headers, timeout=self.timeout)
        except httpx.HTTPError as exc:
            # текст исключения httpx может содержать заголовки, пишем только тип
            raise ProviderError(f"запрос к API не удался: {type(exc).__name__}") from exc
        finally:
            if self._client is None:
                client.close()
        if resp.status_code != 200:
            raise ProviderError(f"API вернул HTTP {resp.status_code}")
        try:
            payload = resp.json()
            text = "".join(
                block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text"
            )
        except (ValueError, AttributeError, TypeError) as exc:
            raise ProviderError("неожиданная форма ответа API") from exc
        log.info("vision: модель %s ответила, %d символов", self.model, len(text))
        return normalize_output(parse_model_json(text))


class SidecarProvider(VisionProvider):
    """Готовый JSON вместо модели: для тестов, демо и ручной разметки.

    Ищет <hash>.json в переданных папках (по умолчанию data/vision_cache и фикстуры тестов).
    Хеш считается по картинке, поэтому уменьшенная или пережатая копия находит свою разметку.
    Метод extract_path читает ещё и файл рядом с картинкой: photo.jpg -> photo.json.
    """

    cacheable = False

    def __init__(self, directories: list[Path | str] | None = None, max_distance: int = 4):
        dirs = directories if directories is not None else [CACHE_DIR, SIDECAR_FIXTURES]
        self.caches = [VisionCache(d) for d in dirs]
        self.max_distance = max_distance

    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        try:
            h = image_hash(image_bytes)
        except ImageError as exc:
            raise ProviderError(str(exc)) from exc
        for cache in self.caches:
            data = cache.get(h, max_distance=self.max_distance)
            if data is not None:
                return normalize_output(data)
        raise ProviderError(f"нет разметки для картинки {h}")

    def extract_path(self, image_path: Path | str, hint_text: str = "") -> dict[str, Any]:
        path = Path(image_path)
        side = path.with_suffix(".json")
        if side.is_file():
            try:
                return normalize_output(json.loads(side.read_text(encoding="utf-8")))
            except ValueError as exc:
                raise ProviderError(f"разметка {side.name} не разбирается") from exc
        return self.extract(path.read_bytes(), hint_text)


class NullProvider(VisionProvider):
    """Ничего не распознаёт: kind="other". Безопасный вариант, когда ключа и разметки нет."""

    cacheable = False

    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        return empty_result("other")


def provider_from_env() -> VisionProvider:
    """LLM_PROVIDER: anthropic (нужен LLM_API_KEY), sidecar, null. Без ключа получаем NullProvider."""
    name = get("LLM_PROVIDER").strip().lower()
    if name == "sidecar":
        return SidecarProvider()
    if name in ("anthropic", "") and get("LLM_API_KEY"):
        return AnthropicVisionProvider()
    if name not in ("", "anthropic", "null", "none"):
        log.warning("vision: неизвестный LLM_PROVIDER=%r, распознавание картинок выключено", name)
    return NullProvider()
