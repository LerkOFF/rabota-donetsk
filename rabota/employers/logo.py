"""Логотип компании: загрузка с ограничением типа и размера, перекодирование через Pillow.

Принимаются только PNG, JPEG и WEBP (тип определяет сам Pillow по содержимому, расширение и заявленный тип
игнорируются). Файл целиком перекодируется в новый PNG не больше 256 пикселей: метаданные (EXIF, текстовые блоки,
профили), анимация и любое непонятное содержимое пропадают, исполняемому коду в файле негде остаться.
"""
from __future__ import annotations

import hashlib
import io
import re
import warnings
from email import policy
from email.parser import BytesParser
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MAX_BYTES = 2 * 1024 * 1024
MAX_SIDE = 4000
MAX_PIXELS = 16_000_000
MIN_SIDE = 32
OUT_SIDE = 256
ALLOWED_FORMATS = ("PNG", "JPEG", "WEBP")
NAME_RE = re.compile(r"^(\d{1,12})-([0-9a-f]{10})\.png$")


ERRORS = {
    "empty": "Файл пустой. Выберите картинку PNG, JPEG или WEBP.",
    "big_file": "Файл больше 2 МБ. Сожмите картинку или выберите другую.",
    "format": "Подходят только PNG, JPEG и WEBP. Другие форматы, в том числе SVG и GIF, не принимаются.",
    "big_side": f"Картинка слишком большая: сторона не больше {MAX_SIDE} пикселей.",
    "small_side": f"Картинка слишком маленькая: сторона не меньше {MIN_SIDE} пикселей.",
    "animated": "Анимированные картинки не принимаются. Загрузите обычное изображение.",
    "broken": "Не удалось прочитать картинку. Файл повреждён или это не изображение.",
    "no_file": "Выберите файл с логотипом.",
    "no_company": "Сначала сохраните карточку компании.",
    "form": "Форма отправлена неверно. Обновите страницу и повторите.",
}


def error_text(code: str) -> str:
    """Текст ошибки по коду из адреса; неизвестный код даёт общий текст."""
    return ERRORS.get(code, "Не удалось сохранить логотип.")


class LogoError(ValueError):
    """Файл не подошёл. code: ключ из ERRORS, text: сообщение для человека."""

    def __init__(self, code: str):
        self.code = code
        self.text = ERRORS.get(code, ERRORS["broken"])
        super().__init__(self.text)


def process_logo(data: bytes) -> bytes:
    """Проверить загруженный файл и вернуть новый PNG. LogoError с понятным текстом, если файл не годится."""
    if not data:
        raise LogoError("empty")
    if len(data) > MAX_BYTES:
        raise LogoError("big_file")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            fmt_name = img.format
            width, height = img.size
            if fmt_name not in ALLOWED_FORMATS:
                raise LogoError("format")
            if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise LogoError("big_side")
            if min(width, height) < MIN_SIDE:
                raise LogoError("small_side")
            if getattr(img, "n_frames", 1) > 1:
                raise LogoError("animated")
            img.load()
    except LogoError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning, MemoryError, EOFError):
        raise LogoError("broken") from None
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    clean = img.convert("RGBA" if has_alpha else "RGB")
    clean.thumbnail((OUT_SIDE, OUT_SIDE), Image.Resampling.LANCZOS)
    # новое изображение из чистых пикселей: исходные метаданные не переносятся
    fresh = Image.frombytes(clean.mode, clean.size, clean.tobytes())
    out = io.BytesIO()
    fresh.save(out, format="PNG", optimize=True)
    return out.getvalue()


def logo_dir(db_path: Path | str) -> Path:
    return Path(db_path).parent / "logos"


def file_name(company_id: int, png: bytes) -> str:
    return f"{int(company_id)}-{hashlib.sha256(png).hexdigest()[:10]}.png"


def safe_name(name: str | None, company_id: int | None = None) -> str | None:
    """Имя файла логотипа, если оно выглядит как наше (и принадлежит компании). Иначе None."""
    m = NAME_RE.match(name or "")
    if not m or (company_id is not None and int(m.group(1)) != int(company_id)):
        return None
    return str(name)


def save_logo(directory: Path, company_id: int, png: bytes) -> str:
    """Записать файл и удалить прежние логотипы этой компании. Возвращает имя файла."""
    directory.mkdir(parents=True, exist_ok=True)
    name = file_name(company_id, png)
    (directory / name).write_bytes(png)
    remove_logo(directory, company_id, keep=name)
    return name


def remove_logo(directory: Path, company_id: int, keep: str = "") -> None:
    if not directory.is_dir():
        return
    for path in directory.glob(f"{int(company_id)}-*.png"):
        if path.name != keep and safe_name(path.name, company_id):
            path.unlink(missing_ok=True)


def logo_url(company: dict, base: str = "") -> str:
    """Адрес логотипа с версией (имя файла содержит хеш содержимого). Пусто, если логотипа нет."""
    name = safe_name(company.get("logo_path"), company.get("id"))
    return f"{base}/company/{company['id']}/logo?v={name[len(str(company['id'])) + 1:-4]}" if name else ""


# ------------------------------------------------------------------ разбор multipart/form-data

def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Поля формы и файлы из тела multipart/form-data: ({имя: значение}, {имя: (имя файла, содержимое)})."""
    if not content_type.lower().startswith("multipart/form-data") or "boundary=" not in content_type.lower():
        raise LogoError("form")
    header = b"Content-Type: " + content_type.encode("latin-1", "replace") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    msg = BytesParser(policy=policy.HTTP).parsebytes(header + body)
    if not msg.is_multipart():
        raise LogoError("form")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name or len(fields) + len(files) > 20:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[str(name)] = (str(filename), payload)
        else:
            fields[str(name)] = payload.decode("utf-8", errors="replace")[:2000]
    return fields, files
