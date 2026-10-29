"""Вид поста (vacancy/service/military/ad/other) и разбиение поста со списком должностей."""
import pytest

from rabota.models import RawPost
from rabota.pipeline.classify import classify, is_empty_stub
from rabota.pipeline.clean import clean_text
from rabota.pipeline.run import analyze, classify_post, process
from rabota.pipeline.split import split_post


def raw(text, source_type="telegram", **kw):
    return RawPost(source_type, "src", kw.pop("external_id", "1"), "https://example.com/1", text, **kw)


@pytest.mark.parametrize("text", [
    "Требуется бариста в кофейню. Оплата 3500 за смену. Тел. +79490000000",
    "Срочно нужен курьер! Оплата 4000 в день. Пишите @user",
    "В магазин требуется продавец. График 2/2, зарплата от 40 000 руб.",
    "ВАКАНСИЯ: БУХГАЛТЕР (г. Донецк)\nОбязанности:\n- отчетность\nТребования:\n- опыт от 1 года",
    "Приглашаем на работу грузчиков. Донецк, +79490000000",
])
def test_classify_vacancy(text):
    assert classify(clean_text(text)) == "vacancy"


@pytest.mark.parametrize("text", [
    "Услуги электрика. Недорого, гарантия. Звоните +79490000000",
    "Ремонт квартир под ключ, опыт 10 лет, гарантия качества. Пишите в личку",
    "Грузоперевозки по городу и области, недорого, звоните +79490000000",
])
def test_classify_service(text):
    assert classify(clean_text(text)) == "service"


@pytest.mark.parametrize("text", [
    "Служба по контракту в Вооруженных Силах РФ. Выплаты, жильё, полное сопровождение",
    "Контракт с МО РФ: доход от 6 до 18 миллионов рублей в год",
    "Требуются в полицию ДНР. Набор в полк патрульно-постовой службы, выплаты и жильё",
])
def test_classify_military(text):
    assert classify(clean_text(text)) == "military"


@pytest.mark.parametrize("text", [
    "Разместить",
    "Запись на собеседование\n+79490000000",
    "Разместить вакансию | Подписаться",
    "Подпишись на наш канал, лучшие вакансии ДНР! Реклама на канале",
    "Займ без отказа! Деньги на карту за 5 минут, любая кредитная история. Оформить заявку: +79490000001",
    "",
])
def test_classify_ad_and_stub(text):
    assert classify(clean_text(text)) == "ad"


@pytest.mark.parametrize("text", [
    "Ищу работу бухгалтером, опыт 5 лет. Резюме по запросу, +79490000000",
    "Ищу работу, возьмусь за любую подработку",
    "Сегодня в Донецке будет облачно, ночью до +5",
])
def test_classify_other(text):
    assert classify(clean_text(text)) == "other"


def test_resume_in_employer_post_is_not_a_seeker():
    text = "Требуется бухгалтер. Обязанности: отчетность. Требования: опыт. Присылайте резюме на почту"
    assert classify(clean_text(text)) == "vacancy"


def test_is_empty_stub():
    assert is_empty_stub("Запись на собеседование +79490000000")
    assert is_empty_stub("Телефон: +79490000000")
    assert not is_empty_stub("Требуется повар +79490000000")


def test_structured_source_is_always_vacancy():
    text = "Учитель\nРаботодатель: Школа\nЗарплата: от 27093 руб."
    assert classify(text, {"job-name": "Учитель", "salary_min": 27093}) == "vacancy"


def test_classify_post_uses_homoglyph_free_text():
    assert classify_post(raw("Тpeбуeтся бapистa в кофейню, оплата 3500 за смену")) == "vacancy"


def test_process_returns_empty_for_non_vacancies():
    for text in ["Услуги электрика, звоните +79490000000", "Служба по контракту в ВС РФ", "Разместить", "Ищу работу"]:
        assert process(raw(text)) == []


def test_process_single_vacancy():
    vs = process(raw("Требуется повар в кафе. Донецк. Оплата 4000 за смену. +79490000000"))
    assert len(vs) == 1
    assert vs[0].profession == "повар"
    assert vs[0].kind == "vacancy"
    assert vs[0].source_type == "telegram"


# ----------------------------------------------------------------------------- split

def professions(text):
    return [seg.profession for seg in split_post(clean_text(text))]


def test_split_numbered_list():
    text = "Требуются:\n1. Повар\n2. Официант\n3. Бармен\nГрафик 2/2"
    assert professions(text) == ["повар", "официант", "бармен"]


def test_split_inline_list_with_comma():
    assert professions("Требуются: бариста, повар") == ["бариста", "повар"]


def test_split_inline_list_with_slash_and_and():
    assert professions("Требуются: автомеханик / автослесарь / автомойщик") == ["автомеханик", "автослесарь", "автомойщик"]
    assert professions("Требуются повар и официант в кафе") == ["повар", "официант"]


def test_split_emoji_markers():
    text = "Ждём в команду:\n✅ Кассир - 3000 за смену\n✅ Повар - 5000 за смену\n✅ Мойщик посуды - 2500 за смену"
    assert professions(text) == ["кассир", "повар", "мойщик посуды"]


def test_split_paragraphs_with_own_profession():
    text = "Требуется пекарь. Оплата 5000 в смену.\n\nТребуется упаковщик. Оплата 4000 в смену."
    assert professions(text) == ["пекарь", "упаковщик"]


def test_split_single_vacancy_one_segment():
    segs = split_post(clean_text("Требуется повар в кафе. График 2/2. Оплата 4000"))
    assert len(segs) == 1
    assert segs[0].profession is None  # должность одиночного поста выбирается позже, по всему тексту


def test_split_inherits_common_fields():
    vs = process(raw("Срочно требуются:\n- Повар - 5000 за смену\n- Официант\nДонецк, график 2/2\nТел. +79490000000"))
    assert [v.profession for v in vs] == ["повар", "официант"]
    assert all(v.city == "Донецк" for v in vs)
    assert all(v.contacts["phones"] == ["+79490000000"] for v in vs)
    assert vs[0].pay_min == 5000 and vs[0].pay_period == "смена"


def test_list_post_each_item_has_own_pay():
    text = (
        "КАФЕ приглашает:\nКАССИРЫ\nОплата: 3 000 ₽ за смену\nПОВАРА\nОплата: 5 000 ₽ за смену\n"
        "СУШИСТЫ\nОплата: 7 000 ₽ за смену\nДонецк"
    )
    got = {v.profession: v.pay_min for v in process(raw(text, "max"))}
    assert got == {"кассир": 3000, "повар": 5000, "сушист": 7000}


def test_one_pay_for_the_whole_list_is_shared():
    vs = process(raw("Требуются повар, пекарь, кондитер. Оплата 5000 за смену. Донецк"))
    assert len(vs) == 3
    assert {(v.pay_min, v.pay_period) for v in vs} == {(5000, "смена")}


def test_analyze_keeps_cleaned_text_and_kind():
    a = analyze(raw("🔥 Требуется повар #работа"))
    assert a.kind == "vacancy" and "🔥" not in a.cleaned
