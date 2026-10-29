from rabota.db import connect, load_raw, save_raw, save_vacancy, vacancy_row_to_dict
from rabota.models import RawPost, Vacancy


def test_raw_roundtrip_and_unique():
    c = connect(":memory:")
    p = RawPost("telegram", "x", "1", "https://t.me/x/1", "текст", image_urls=["a.jpg"], extra={"k": 1})
    assert save_raw(c, p) is True
    assert save_raw(c, p) is False
    assert load_raw(c)[0].image_urls == ["a.jpg"]


def test_vacancy_roundtrip():
    c = connect(":memory:")
    vid = save_vacancy(c, Vacancy(profession="бариста", contacts={"phones": ["+79490000000"]}, pay_daily=True))
    row = c.execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
    d = vacancy_row_to_dict(row)
    assert d["profession"] == "бариста" and d["pay_daily"] is True and d["contacts"]["phones"]
