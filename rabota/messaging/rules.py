"""Правила откликов и сообщений: пределы длины, статусы, причины жалоб, очистка и проверка текста."""
from __future__ import annotations

import re

MAX_COVER = 2000  # сопроводительное сообщение
MAX_MESSAGE = 2000
MAX_REPORT_COMMENT = 500
MAX_LINKS = 2  # ссылок в одном тексте
MAX_UNANSWERED = 3  # сколько сообщений подряд можно отправить, пока собеседник не ответил
DUPLICATE_WINDOW_SECONDS = 60
THREAD_PAGE = 30
LIST_PAGE = 20

APP_STATUSES = ("new", "viewed", "invited", "rejected")
EMPLOYER_SET_STATUSES = ("viewed", "invited", "rejected")
STATUS_LABELS = {
    "new": "Новый",
    "viewed": "Просмотрен",
    "invited": "Приглашён",
    "rejected": "Отказ",
    "withdrawn": "Отозван",
}
# подписи статуса для самого соискателя: он не "новый", он отправил отклик
SEEKER_STATUS_LABELS = {**STATUS_LABELS, "new": "Отправлен", "viewed": "Просмотрен работодателем"}

REPORT_REASONS = (
    "Мошенничество или просьба заплатить",
    "Спам или реклама",
    "Оскорбления или угрозы",
    "Навязчивые или неуместные сообщения",
    "Другое",
)

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad]")
_SPACES = re.compile(r"[ \t\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
_BLANKS = re.compile(r"\n{3,}")
_LINK = re.compile(r"(?:https?://|www\.)\S+|\b[\w-]+\.(?:ru|рф|com|org|net|me|su|ua|biz|info|top|xyz)\b/?\S*", re.IGNORECASE)


class MessagingError(Exception):
    """Ожидаемая ошибка с понятным текстом для человека. code нужен коду и тестам."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


class NotFound(MessagingError):
    """Чужой или несуществующий объект: снаружи это всегда 404."""

    def __init__(self, what: str = "Не найдено"):
        super().__init__("not_found", what)


def clean_text(value: str | None) -> str:
    """Перевод строк к \\n, без управляющих и невидимых знаков, без лишних пробелов и пустых строк."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _INVISIBLE.sub("", _CONTROL.sub("", text))
    text = "\n".join(_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def count_links(text: str) -> int:
    return len(_LINK.findall(text))


def check_text(value: str | None, limit: int, *, required: bool = True, what: str = "Сообщение") -> str:
    """Очищенный текст или MessagingError: empty, too_long, too_many_links."""
    text = clean_text(value)
    if not text:
        if required:
            raise MessagingError("empty", "Напишите сообщение: пустое отправить нельзя.")
        return ""
    if len(text) > limit:
        raise MessagingError("too_long", f"{what} слишком длинное: не больше {limit} знаков, сейчас {len(text)}.")
    if count_links(text) > MAX_LINKS:
        raise MessagingError("too_many_links", f"Слишком много ссылок: не больше {MAX_LINKS}.")
    return text
