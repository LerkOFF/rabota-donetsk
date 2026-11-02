"""Распознавание афиш без платной модели: Tesseract читает текст, дальше работает обычный разбор вакансий.

Включается LLM_PROVIDER=ocr. Нужны пакеты tesseract-ocr и tesseract-ocr-rus (команда tesseract в PATH).

Как читается афиша (афиши бывают на тёмном фоне, с крупным заголовком на плашке и мелким текстом блоков):

1. Серое изображение, автоконтраст, увеличение до ширины около 2400 px (LANCZOS).
2. Несколько вариантов картинки: оригинал, инверсия, выровненный фон, адаптивная бинаризация.
3. Несколько прогонов Tesseract с разными режимами `--psm` (3, 4, 6, 11, 12) и языком rus+eng.
4. Лучший прогон выбирается по качеству текста (доля слов из словаря вакансий и профессий, телефон, отсутствие
   мусорных символов, см. ocr_quality), а не по длине: шум длиннее настоящего текста.
5. Заголовок читается отдельно: если в лучшем прогоне нет должности или телефона, картинка режется на горизонтальные
   полосы, и строки, которых там нет, дописываются в начало текста (крупный текст в своей полосе читается лучше).
6. Если слов из словаря почти нет, текст считается нечитаемым: вакансия не создаётся, пост ждёт афишу (poster_reparse).

Осторожность вместо догадок: вакансия с картинки принимается, только если из текста получилась вакансия
и в ней есть контакт (телефон или ник Telegram). Без контакта карточка бесполезна, а шум распознавания
чаще всего и даёт такие обрывки. Текст афиши склеивается с текстом поста (там часто только подпись с телефоном).
Ответ кэшируется по хешу картинки и версии распознавания: одна картинка читается один раз, а после улучшения
распознавания (OCR_VERSION) читается заново.
"""
from __future__ import annotations

import io
import logging
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rabota.models import RawPost
from rabota.pipeline.dictionaries import GENERIC_LEMMAS, find_professions
from rabota.pipeline.quality import is_known_word
from rabota.vision.images import ImageError, open_image
from rabota.vision.ocr_quality import TextQuality, assess, readable
from rabota.vision.providers import ProviderError, VisionProvider, empty_result, normalize_output

log = logging.getLogger("rabota.vision")

OCR_VERSION = 2        # растёт при изменении алгоритма: кэш старых ответов перестаёт действовать
LANGS = "rus+eng"
TARGET_WIDTH = 2400    # афиши из Telegram узкие (500-800 px): буквы поднимаем до ~25-40 px
MAX_SIDE = 3600
MIN_SIDE = 1600        # совместимость: ниже этого размера картинку всегда увеличиваем
MIN_TEXT_CHARS = 30    # меньше: логотип или баннер без текста
TIMEOUT = 60           # один прогон Tesseract
BUDGET_SECONDS = 150.0  # весь разбор одной картинки: следующий прогон не начинается после этого срока
GOOD_ENOUGH_SHARE = 0.6  # лучший прогон не хуже: дальнейшие прогоны не нужны

# основной план: (вариант картинки, psm)
FULL_PLAN = (
    ("orig", 3), ("orig", 11), ("bin", 6), ("bin", 4), ("flat", 4), ("flatinv", 6), ("orig", 12), ("inv", 11),
)
STRIP_VARIANTS = ("orig", "inv")  # инверсия читает светлый текст на тёмном; второй вариант идёт, только если первого мало
STRIP_COUNT = 6
STRIP_OVERLAP = 0.4


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


# ------------------------------------------------------------ предобработка

def _gray(image_bytes: bytes):
    from PIL import Image

    try:
        img = open_image(image_bytes)
    except ImageError as exc:
        raise ProviderError(str(exc)) from exc
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        base = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        base.alpha_composite(rgba)
        img = base
    return img.convert("L")


def _upscale(img, width: int = TARGET_WIDTH):
    from PIL import Image

    scale = width / img.width
    if max(img.width, img.height) * scale > MAX_SIDE:
        scale = MAX_SIDE / max(img.width, img.height)
    if abs(scale - 1) < 0.02:
        return img
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)


def is_dark(img) -> bool:
    from PIL import ImageStat

    return ImageStat.Stat(img).mean[0] < 110


def flatten_background(img, radius: int = 25, gain: float = 2.0):
    """Вычитает размытый фон: ровный контраст на неравномерно освещённых и фоновых картинках (CLAHE-подобно)."""
    import numpy as np
    from PIL import Image, ImageFilter

    a = np.asarray(img, dtype=np.float32)
    blur = np.asarray(img.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
    return Image.fromarray(np.clip((a - blur) * gain + 128, 0, 255).astype(np.uint8))


def adaptive_binarize(img, radius: int = 25, offset: int = 8):
    """Адаптивный порог: пиксель светлый, если светлее локального среднего минус offset."""
    import numpy as np
    from PIL import Image, ImageFilter

    a = np.asarray(img, dtype=np.float32)
    blur = np.asarray(img.filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
    return Image.fromarray(((a > blur - offset).astype(np.uint8)) * 255)


def build_variants(image_bytes: bytes, width: int = TARGET_WIDTH) -> dict[str, Any]:
    """Варианты картинки: orig (серое с контрастом), flat (выровненный фон), bin (адаптивная бинаризация) и их инверсии."""
    from PIL import ImageOps

    img = ImageOps.autocontrast(_upscale(_gray(image_bytes), width), cutoff=1)
    flat, binary = flatten_background(img), adaptive_binarize(img)
    return {
        "orig": img,
        "inv": ImageOps.invert(img),
        "flat": flat,
        "flatinv": ImageOps.invert(flat),
        "bin": binary,
        "bininv": ImageOps.invert(binary),
    }


def prepare(image_bytes: bytes) -> bytes:
    """Основной вариант картинки в PNG: серое, увеличено до ~2400 px, автоконтраст."""
    out = io.BytesIO()
    build_variants(image_bytes)["orig"].save(out, format="PNG")
    return out.getvalue()


# ------------------------------------------------------------ Tesseract

def _tesseract(path: Path, psm: int) -> str:
    """Один прогон Tesseract. ProviderError при ошибке и таймауте. Единственное место запуска процесса."""
    try:
        proc = subprocess.run(
            ["tesseract", str(path), "stdout", "-l", LANGS, "--psm", str(psm)],
            capture_output=True, timeout=TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("tesseract не ответил за отведённое время") from exc
    if proc.returncode != 0:
        raise ProviderError(f"tesseract завершился с кодом {proc.returncode}")
    return proc.stdout.decode("utf-8", errors="replace")


@dataclass
class Run:
    variant: str
    psm: int
    text: str
    quality: TextQuality
    part: str = "full"


@dataclass
class Reading:
    text: str
    quality: TextQuality
    runs: list[Run] = field(default_factory=list)
    best: Run | None = None


def _run(img, variant: str, psm: int, tmp: Path, part: str = "full") -> Run:
    path = tmp / f"{part}-{variant}-{psm}.png"
    img.save(path, format="PNG")
    text = _tesseract(path, psm)
    return Run(variant, psm, text, assess(text), part)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[а-яёa-z]{4,}", text.lower().replace("ё", "е"))}


def _strip_boxes(height: int, count: int, overlap: float) -> list[tuple[int, int]]:
    step = height / (count - (count - 1) * overlap)
    boxes = []
    for i in range(count):
        top = round(i * step * (1 - overlap))
        boxes.append((max(0, top), min(height, round(top + step))))
    return boxes


def _known_words(line: str) -> list[str]:
    return [w.lower().replace("ё", "е") for w in re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", line) if is_known_word(w)]


def new_lines(base_text: str, other_text: str, have: set[str] | None = None) -> list[str]:
    """Строки другого прогона, в которых есть настоящие слова, отсутствующие в основном тексте."""
    have = set(have) if have is not None else _words(base_text)
    out: list[str] = []
    for line in other_text.splitlines():
        line = line.strip()
        words = re.findall(r"[А-Яа-яЁёA-Za-z]{4,}", line)
        known = _known_words(line)
        if not known or len(known) < len(words) * 0.6:
            continue
        if all(w in have for w in known):
            continue
        out.append(line)
        have |= set(known)
    return out


def headline_lines(variants: dict[str, Any], tmp: Path, base_text: str, deadline: float) -> list[str]:
    """Строки, прочитанные в горизонтальных полосах, которых нет в основном тексте: так находится крупный заголовок."""
    have = _words(base_text)
    found: list[str] = []
    for name in STRIP_VARIANTS:
        if found and name != STRIP_VARIANTS[0] and assess(base_text).has_profession:
            break  # заголовок уже найден или профессия есть в тексте: вторая проходка не нужна
        img = variants[name]
        for i, (top, bottom) in enumerate(_strip_boxes(img.height, STRIP_COUNT, STRIP_OVERLAP)):
            if time.monotonic() > deadline:
                return found
            try:
                run = _run(img.crop((0, top, img.width, bottom)), name, 6, tmp, part=f"strip{i}")
            except ProviderError:
                continue
            for line in new_lines("", run.text, have):
                found.append(line)
                have |= set(_known_words(line))
    return found


def read_image(image_bytes: bytes, *, budget: float = BUDGET_SECONDS) -> Reading:
    """Лучший текст картинки. ProviderError, если ни один прогон не удался."""
    deadline = time.monotonic() + budget
    variants = build_variants(image_bytes)
    runs: list[Run] = []
    best: Run | None = None
    last_error: ProviderError | None = None
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        for variant, psm in FULL_PLAN:
            if runs and time.monotonic() > deadline:
                break
            try:
                run = _run(variants[variant], variant, psm, tmp)
            except ProviderError as exc:
                last_error = exc
                continue
            runs.append(run)
            if best is None or run.quality.score > best.quality.score:
                best = run
            if len(runs) >= 3 and _good_enough(best.quality):
                break
        if best is None:
            raise last_error or ProviderError("tesseract не дал текста")
        text = best.text
        if best.quality.known_letters >= MIN_TEXT_CHARS // 2 and not _good_enough(best.quality):  # пустую картинку и уже понятный текст полосами не читаем
            headline = headline_lines(variants, tmp, text, deadline)
        else:
            headline = []
    extra_lines: list[str] = []
    have = _words(text) | {w for ln in headline for w in _known_words(ln)}
    for run in runs:
        if run is not best:
            for line in new_lines(text, run.text, have):
                extra_lines.append(line)
                have |= set(_known_words(line))
    merged = "\n".join([*headline, text.rstrip(), *extra_lines])
    quality = assess(merged)
    if quality.score < best.quality.score:
        merged, quality = text, best.quality
    merged = clean_poster_text(fix_latin_lookalikes(merged)).replace("\u2014", "-").replace("\u2013", "-")
    return Reading(merged, quality, runs, best)


def _good_enough(q: TextQuality) -> bool:
    return q.has_profession and q.phones >= 1 and q.known_share >= GOOD_ENOUGH_SHARE


def ocr_text(image_bytes: bytes) -> str:
    """Текст картинки. ProviderError, если Tesseract не установлен или не ответил."""
    if not tesseract_available():
        raise ProviderError("tesseract не установлен")
    return read_image(image_bytes).text


# ------------------------------------------------------------ текст в вакансию

_CONDITION_RE = re.compile(
    r"зарплат|оплат|оклад|процент|выработ|график|обучен|спецодежд|питани|проживан|оформлен|преми|бонус|смен",
    re.IGNORECASE,
)


def fix_latin_lookalikes(text: str) -> str:
    """В строке, где почти вся кириллица, слова из латинских двойников ("OT", "TO") становятся русскими, если выходит слово."""
    from rabota.pipeline.homoglyphs import LATIN_TO_CYR
    from rabota.pipeline.quality import STOPWORDS

    out: list[str] = []
    for line in text.splitlines():
        cyr, lat = len(re.findall(r"[А-Яа-яЁё]", line)), len(re.findall(r"[A-Za-z]", line))
        if lat and cyr >= 2 * lat:
            def fix(m: re.Match[str]) -> str:
                word = m.group(0)
                if not all(ch in LATIN_TO_CYR for ch in word):
                    return word
                fixed = "".join(LATIN_TO_CYR[ch] for ch in word)
                return fixed if fixed.lower() in STOPWORDS or is_known_word(fixed) else word

            line = re.sub(r"[A-Za-z]{2,5}", fix, line)
        out.append(line)
    return "\n".join(out)


def clean_poster_text(text: str) -> str:
    """Выбрасывает строки-шум (ни одного настоящего слова, обрывки значков) и пустые строки: текст короче и чище."""
    out: list[str] = []
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if len(line) < 3:
            continue
        q = assess(line)
        has_number = bool(re.search(r"\d{2,}", line))
        words_ok = q.known_letters >= 4 and q.known_share >= 0.4
        number_ok = has_number and q.junk <= 2 and q.known_letters >= 1
        if words_ok or number_ok or q.phones:
            out.append(line)
    return "\n".join(out)


def _clean_line(line: str) -> str:
    """Строка без одиночных обрывков распознавания: остаются слова из словаря, числа, знаки оплаты и графика."""
    keep: list[str] = []
    for token in re.split(r"\s+", line):
        word = re.sub(r"[^А-Яа-яЁёA-Za-z]", "", token)
        if re.search(r"\d|[+%/]", token) or (word and (is_known_word(word) or word.lower() in _SHORT_OK)):
            keep.append(token.strip("|=~*_[]{}"))
    return " ".join(t for t in keep if t)


_SHORT_OK = {"от", "до", "за", "на", "по", "или", "и", "с", "в", "не", "для", "ежедневно"}


_EXPERIENCE_RE = re.compile(r"опыт\w*.{0,80}?\bот\s*(\d{1,2})\s*(?:лет|года|год)\b", re.IGNORECASE | re.DOTALL)


def experience_years(text: str) -> int | None:
    """«Опытных автомойщиков (от 1 года)»: сколько лет опыта просят, если названо."""
    m = _EXPERIENCE_RE.search(text or "")
    return int(m.group(1)) if m and 0 < int(m.group(1)) < 30 else None


def conditions_text(text: str, limit: int = 240) -> str | None:
    """Строки афиши об условиях (оплата, график, обучение), прочитанные уверенно: попадают в описание карточки."""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = _clean_line(re.sub(r"\s+", " ", raw_line).strip(" -\u2013\u2014\u2022|=~*_[]{}")).rstrip(" +")
        if len(line) < 12 or not _CONDITION_RE.search(line) or assess(line).known_share < 0.6:
            continue
        words = _words(line)
        if any(words <= _words(other) for other in lines):
            continue
        lines = [other for other in lines if not _words(other) <= words]
        lines.append(line)
    years = experience_years(text)
    if years:
        lines.append("Опыт от 1 года" if years == 1 else f"Опыт от {years} лет")
    joined = "; ".join(lines)
    if not joined:
        return None
    return joined if len(joined) <= limit else joined[:limit].rsplit(" ", 1)[0].rstrip(" ,;")


def _vacancy_dict(v, conditions: str | None = None) -> dict[str, Any]:
    contacts = v.contacts or {}
    return {
        "profession": v.profession, "pay_min": v.pay_min, "pay_max": v.pay_max, "pay_period": v.pay_period,
        "city": v.city, "district": v.district, "schedule": v.schedule, "requirements": v.requirements,
        "employer": v.employer, "phones": list(contacts.get("phones") or []), "telegram": contacts.get("telegram"),
        "extra_text": conditions,
    }


def combine_texts(poster_text: str, post_text: str) -> str:
    """Текст афиши, затем текст поста (подпись с телефоном), если его там ещё нет."""
    poster, post = (poster_text or "").strip(), (post_text or "").strip()
    if not post:
        return poster
    digits = re.sub(r"\D", "", poster)
    post_digits = re.findall(r"\d{10,11}", re.sub(r"[\s\-()]", "", post))
    if post in poster or (post_digits and all(d[-10:] in digits for d in post_digits) and len(post) < 40):
        return poster
    return f"{poster}\n{post}"


_SHORT_PHONE_RE = re.compile(r"(?:звон\w*|позвон\w*|тел\w*|номер\w*)\D{0,20}?(?<!\d)(\d{3,6})(?!\d)", re.IGNORECASE)


def short_phone(text: str) -> str | None:
    """Короткий номер (3-6 цифр) после слов «звоните», «телефон»: единый номер сети, внутренний номер."""
    m = _SHORT_PHONE_RE.search(text or "")
    return m.group(1) if m else None


def result_from_text(text: str, hint_text: str = "") -> dict[str, Any]:
    """Ответ в формате провайдеров по распознанному тексту афиши и подписи поста."""
    from rabota.pipeline.run import analyze

    text = text.strip()
    if len(text) < MIN_TEXT_CHARS or not readable(text):
        return empty_result("other", text)  # шум распознавания: вакансии не будет, пост ждёт афишу
    analysis = analyze(RawPost("ocr", "ocr", "0", "", combine_texts(text, hint_text)))
    if analysis.kind != "vacancy":
        return empty_result(analysis.kind, text)
    conditions = conditions_text(text)
    vacancies = [_vacancy_dict(v, conditions) for v in analysis.vacancies]
    dictionary = [h.lemma for h in find_professions(text) if h.lemma not in GENERIC_LEMMAS]
    for v in vacancies:
        if dictionary and not find_professions(v["profession"]):
            v["profession"] = dictionary[0]  # заголовок не прочитан: берём должность из словаря по всему тексту афиши
    short = short_phone(text)
    for v in vacancies:
        if short and not v["phones"] and not v["telegram"]:
            v["phones"] = [short]  # "Звонок на 335 бесплатный": короткий номер без кода
    vacancies = [v for v in vacancies if v["phones"] or v["telegram"]]
    if not vacancies:
        return empty_result("other", text)
    return normalize_output({"kind": "vacancy", "vacancies": vacancies, "raw_text": text})


class OcrProvider(VisionProvider):
    cacheable = True
    version = OCR_VERSION

    def extract(self, image_bytes: bytes, hint_text: str = "") -> dict[str, Any]:
        return result_from_text(ocr_text(image_bytes), hint_text)
