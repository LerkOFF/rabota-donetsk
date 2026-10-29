"""Качество разбора на размеченном наборе (tests/fixtures/pipeline/labeled.json): порог 90 процентов по полям."""
import glob
import json
from pathlib import Path

import pytest

from rabota.models import POST_KINDS, RawPost
from rabota.pipeline.run import analyze, assign_duplicates, process

ROOT = Path(__file__).resolve().parents[2]
LABELED = json.loads((ROOT / "tests/fixtures/pipeline/labeled.json").read_text(encoding="utf-8"))
THRESHOLD = 0.90


def to_raw(ex):
    return RawPost(ex["source_type"], "src", ex["id"], "https://example.com/" + ex["id"], ex["text"])


def evaluate():
    kind_ok = kind_n = 0
    prof_ok = prof_n = 0
    pay_ok = pay_n = 0
    city_ok = city_n = 0
    for ex in LABELED:
        a = analyze(to_raw(ex))
        kind_n += 1
        kind_ok += a.kind == ex["kind"]
        if ex["kind"] != "vacancy" or ex.get("vacancies") is None:
            continue
        got = a.vacancies
        for exp in ex["vacancies"]:
            prof_n += 1
            prof_ok += exp["profession"] in [v.profession for v in got]
            if "pay_min" in exp:
                pay_n += 1
                same = [v for v in got if v.profession == exp["profession"]]
                want = (exp["pay_min"], exp["pay_max"])
                period = exp["pay_period"]
                pay_ok += any(
                    (v.pay_min, v.pay_max) == want and (period in (None, "*") or v.pay_period == period or want == (None, None))
                    for v in same
                )
        if "city" in ex:
            city_n += 1
            city_ok += any(v.city == ex["city"] for v in got)
    return {
        "kind": (kind_ok, kind_n), "profession": (prof_ok, prof_n), "pay": (pay_ok, pay_n), "city": (city_ok, city_n),
    }


def test_labeled_set_is_big_enough_and_clean():
    assert len(LABELED) >= 120
    assert {ex["kind"] for ex in LABELED} == set(POST_KINDS)
    assert len({ex["id"] for ex in LABELED}) == len(LABELED)
    blob = json.dumps(LABELED, ensure_ascii=False)
    assert "\u2014" not in blob
    assert "/Users/" not in blob


@pytest.mark.parametrize("field", ["kind", "profession", "pay"])
def test_accuracy_at_least_90_percent(field):
    ok, n = evaluate()[field]
    assert n >= 100 or field == "pay"
    assert ok / n >= THRESHOLD, f"{field}: {ok}/{n} = {ok / n:.3f}"


def test_city_accuracy():
    ok, n = evaluate()["city"]
    assert ok / n >= THRESHOLD


def test_every_vacancy_is_valid():
    from rabota.models import CATEGORIES, EMPLOYMENTS, PAY_PERIODS, WORK_FORMATS

    for ex in LABELED:
        for v in process(to_raw(ex)):
            assert v.profession
            assert v.category in CATEGORIES
            assert v.work_format in (None, *WORK_FORMATS)
            assert v.employment in (None, *EMPLOYMENTS)
            assert v.pay_period in (None, *PAY_PERIODS)
            assert v.summary and "\u2014" not in v.summary
            assert v.pay_min is None or v.pay_max is None or v.pay_min <= v.pay_max
            assert v.dedupe_key


def test_summary_is_clean_text():
    (v,) = process(to_raw({
        "id": "t", "source_type": "telegram",
        "text": "🔥 СРОЧНО!!! ТРЕБУЕТСЯ ПОВАР В КАФЕ #работа\nОплата 4000 за смену. Донецк https://t.me/x",
    }))
    assert "🔥" not in v.summary and "#" not in v.summary and "http" not in v.summary
    assert v.summary != v.summary.upper()
    assert len(v.summary) <= 400


def test_suspicious_fake_vacancy():
    text = (
        "АРТЛАЙФ ОТКРЫВАЕТ НАБОР В КОМАНДУ\nМенеджер по перепискам | Удалённая работа\nБез опыта, обучение. "
        "Доход от 80 000 руб. в месяц. Заинтересовала вакансия? Пишите в лс - @user"
    )
    (v,) = process(RawPost("max", "web", "1", "u", text))
    assert v.profession == "менеджер по перепискам"
    assert v.work_format == "удалённо"
    assert v.suspicious


def test_list_post_with_shift_pay_like_food_court():
    text = (
        "ФУД-КОРТ «ФАНТОМ» ПРИГЛАШАЕТ НА РАБОТУ!\n\nКАССИРЫ\nОплата: 3 000 ₽ за смену\n\nПОВАРА\nОплата: 5 000 ₽ за смену\n\n"
        "СУШИСТЫ\nОплата: 7 000 ₽ за смену\n\nМОЙЩИКИ ПОСУДЫ\nОплата: 2 500 ₽ за смену\n\nДонецк, +79490000000"
    )
    vs = process(RawPost("max", "web", "2", "u", text))
    got = {v.profession: (v.pay_min, v.pay_period) for v in vs}
    assert got == {
        "кассир": (3000, "смена"), "повар": (5000, "смена"), "сушист": (7000, "смена"), "мойщик посуды": (2500, "смена"),
    }


def test_samples_dumps_process_without_errors_and_with_valid_kinds():
    files = sorted(glob.glob(str(ROOT / "data/samples/*.json")))
    if not files:
        pytest.skip("нет выгрузок")
    vacancies = []
    for f in files:
        for d in json.loads(Path(f).read_text(encoding="utf-8")):
            if "source_type" not in d:
                continue
            raw = RawPost(**{k: d[k] for k in RawPost.__dataclass_fields__ if k in d})
            a = analyze(raw)
            assert a.kind in POST_KINDS
            assert (a.kind == "vacancy") == bool(a.vacancies)
            vacancies.extend(a.vacancies)
    assign_duplicates(vacancies)
    assert all(v.duplicate_of is None or 1 <= v.duplicate_of <= len(vacancies) for v in vacancies)
