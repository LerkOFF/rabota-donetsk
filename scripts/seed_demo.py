"""Наполняет демо-базу выдуманными вакансиями Донецка для разработки и скриншотов.

Все телефоны выдуманные (+79490000000 и похожие), работодатели и ссылки тоже. Реальные данные в эту
базу не попадают: сайт читает ту же таблицу vacancies, поэтому настоящий конвейер подключается
сменой пути базы (DB_PATH).

Запуск:
    .venv/bin/python scripts/seed_demo.py                # data/demo.db, таблицы очищаются
    .venv/bin/python scripts/seed_demo.py --db /tmp/x.db
    DB_PATH=data/demo.db .venv/bin/uvicorn rabota.web.app:app --port 8000
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rabota.db import connect, save_vacancy
from rabota.models import Vacancy

DEMO_DB = ROOT / "data" / "demo.db"

# Вакансии, которые в демо "разместили работодатели на сайте" (origin='direct'): внешний номер -> компания
DEMO_DIRECT = {
    "1004": "Торговая компания «Гермес»",
    "1005": "Склад «Оптовик»",
    "1010": "Салон красоты «Лотос»",
    "1012": "Магазин одежды «Стиль»",
    "1015": "Школа № 120",
}


def _phone(n: int) -> dict:
    return {"phones": [f"+794900000{n:02d}"]}


def demo_vacancies(now: datetime) -> list[tuple[Vacancy, str | None]]:
    """Список демо-вакансий и номера оригинала для дублей. ago - сколько минут назад опубликована."""
    items: list[tuple[int, Vacancy, str | None]] = []

    def add(ago: int, profession: str, category: str, dup_of: str | None = None, **kw) -> None:
        kw.setdefault("title_raw", profession.capitalize())
        kw.setdefault("city", "Донецк")
        kw.setdefault("source_type", "telegram")
        kw.setdefault("source", "@demo_rabota_dnr")
        v = Vacancy(profession=profession, category=category, **kw)
        n = len(items) + 1
        v.external_id = str(1000 + n)
        if v.source_type == "telegram":
            v.source_url = f"https://t.me/{v.source.lstrip('@')}/{1000 + n}"
        else:
            v.source_url = f"https://example.org/demo/{v.source_type}/{v.source}/{1000 + n}"
        v.published_at = (now - timedelta(minutes=ago)).isoformat(timespec="seconds")
        v.dedupe_key = f"demo-{n}"
        items.append((ago, v, dup_of))

    add(12, "бариста", "общепит", employer="Кофейня «Зерно»", district="Ворошиловский", schedule="2/2", work_format="точка",
        employment="сменная", pay_min=4000, pay_max=4000, pay_period="смена", pay_daily=True, experience="не нужен", age_min=18,
        contacts=_phone(1), summary="Готовить кофе и напитки, общаться с гостями. Обучим с нуля, форма выдаётся.")
    add(35, "кассир", "торговля", employer="Магазин «Продукты у дома»", district="Калининский", schedule="2/2, с 8:00 до 20:00", work_format="точка",
        employment="сменная", pay_min=3200, pay_max=3600, pay_period="смена", experience="не нужен", contacts=_phone(2),
        summary="Работа на кассе, приёмка товара, выкладка. Выплата два раза в месяц.")
    add(70, "бухгалтер", "офис", employer="ООО «Донбасс-Снаб»", district="Куйбышевский", schedule="5/2, с 9:00 до 18:00", work_format="офис",
        employment="постоянно", pay_min=48500, pay_max=56000, pay_period="месяц", experience="от 1 года", requirements="Опыт ведения учёта, знание 1С",
        contacts={**_phone(3), "email": "hr@demo-snab.example"}, summary="Первичная документация, расчёт зарплаты, отчётность в налоговую. Официальное оформление.")
    add(95, "оператор 1С", "офис", employer="Торговая компания «Гермес»", district="Ленинский", schedule="5/2", work_format="офис",
        employment="постоянно", pay_min=35000, pay_max=42000, pay_period="месяц", experience="от 1 года", contacts=_phone(4),
        summary="Ввод документов в 1С, сверка остатков, работа с накладными. Рядом остановка транспорта.")
    add(130, "грузчик", "склад", employer="Склад «Оптовик»", district="Петровский", schedule="на день", work_format="склад",
        employment="на сегодня", pay_min=3000, pay_max=3000, pay_period="день", pay_daily=True, experience="не нужен", age_min=18,
        contacts=_phone(5), summary="Разгрузка фуры, перенос товара. Выплата в конце дня наличными.")
    add(160, "водитель", "транспорт", employer="Служба доставки «Быстрый стол»", district="Буденновский", schedule="2/2", work_format="выезд",
        employment="постоянно", pay_min=40000, pay_max=55000, pay_period="месяц", experience="от 1 года", requirements="Права категории B, свой автомобиль",
        contacts=_phone(6), summary="Развозить заказы по городу на своём авто. Топливо компенсируется.")
    add(190, "экспедитор", "транспорт", employer="Оптовая база «Донторг»", district="Киевский", schedule="5/2", work_format="выезд",
        employment="постоянно", pay_min=30000, pay_max=38000, pay_period="месяц", experience="не нужен", contacts=_phone(7),
        summary="Сопровождать груз, оформлять накладные, принимать оплату от клиентов.")
    add(240, "повар", "общепит", employer="Столовая «Домашняя»", district="Ворошиловский", schedule="2/2, с 7:00 до 19:00", work_format="точка",
        employment="сменная", pay_min=3500, pay_max=4500, pay_period="смена", pay_daily=True, experience="от 1 года", requirements="Медицинская книжка",
        contacts=_phone(8), summary="Готовить первые и вторые блюда на 80 человек. Питание бесплатное.")
    add(300, "слесарь", "производство", employer="Завод «Донмаш»", district="Пролетарский", schedule="5/2", work_format="объект",
        employment="постоянно", pay_min=42000, pay_max=52000, pay_period="месяц", experience="от 3 лет", requirements="Разряд не ниже четвёртого",
        contacts={**_phone(9), "max": "demo_donmash"}, summary="Ремонт и обслуживание станочного оборудования. Оформление по трудовому договору.")
    add(360, "администратор", "услуги", employer="Салон красоты «Лотос»", district="Ворошиловский", schedule="2/2, с 9:00 до 21:00", work_format="точка",
        employment="сменная", pay_min=2800, pay_max=3200, pay_period="смена", experience="не нужен", age_min=18, contacts=_phone(10),
        summary="Встречать клиентов, вести запись, отвечать на звонки. Приятный коллектив.")
    add(420, "разнорабочий", "стройка", employer="Строительная бригада", district="Макеевский район", city="Макеевка", schedule="6/1", work_format="объект",
        employment="постоянно", pay_min=3500, pay_max=3500, pay_period="день", pay_daily=True, experience="не нужен", age_min=18, contacts=_phone(11),
        summary="Подсобные работы на строительстве частного дома. Оплата каждый вечер.")
    add(480, "оператор call-центра", "офис", employer="Сервисная компания «Линия»", schedule="5/2, удалённо", work_format="удалённо",
        employment="постоянно", pay_min=28000, pay_max=34000, pay_period="месяц", experience="не нужен", contacts={"telegram": "@demo_liniya_hr"},
        source_type="vk", source="club_demo_rabota", summary="Принимать звонки клиентов из дома. Нужны ноутбук и стабильный интернет.")
    add(540, "продавец-консультант", "торговля", employer="Магазин одежды «Стиль»", district="Куйбышевский", schedule="2/2", work_format="точка",
        employment="сменная", pay_min=3000, pay_max=3800, pay_period="смена", experience="не нужен", contacts=_phone(12),
        summary="Консультировать покупателей, следить за выкладкой. Проценты от продаж сверх оклада.")
    add(600, "курьер", "транспорт", employer="Служба доставки «Быстрый стол»", district="Калининский", schedule="свободный", work_format="выезд",
        employment="подработка", pay_min=2000, pay_max=3500, pay_period="день", pay_daily=True, experience="не нужен", age_min=18,
        contacts=_phone(13), summary="Развозить заказы пешком или на велосипеде. Работайте когда удобно, оплата каждый день.")
    add(660, "уборщица", "услуги", employer="Школа № 120", district="Киевский", schedule="5/2, с 7:00 до 12:00", work_format="точка",
        employment="постоянно", pay_min=15000, pay_max=15000, pay_period="месяц", experience="не нужен", contacts=_phone(14),
        summary="Уборка классов и коридоров в первой половине дня. Подходит для подработки.")
    add(720, "охранник", "услуги", employer="ЧОП «Щит»", district="Ленинский", schedule="сутки через трое", work_format="объект",
        employment="сменная", pay_min=28000, pay_max=34000, pay_period="месяц", experience="не нужен", age_min=21, requirements="Удостоверение охранника",
        contacts=_phone(15), summary="Охрана склада, контроль пропускного режима. Форма выдаётся.")
    add(780, "официант", "общепит", employer="Ресторан «Старый двор»", district="Ворошиловский", schedule="2/2, вечерние смены", work_format="точка",
        employment="сменная", pay_min=2500, pay_max=2500, pay_period="смена", pay_daily=True, experience="не нужен", age_min=18, contacts=_phone(16),
        summary="Обслуживать гостей, принимать заказы. Чаевые остаются вам.")
    add(840, "парикмахер", "услуги", employer="Студия «Локон»", district="Киевский", schedule="2/2", work_format="точка",
        employment="сменная", pay_min=40000, pay_max=70000, pay_period="месяц", experience="от 1 года", contacts=_phone(17),
        summary="Стрижки и окрашивание, процент с работы. Кресло и инструмент предоставляем.")
    add(900, "швея", "производство", employer="Швейная мастерская «Нить»", district="Калининский", schedule="5/2", work_format="объект",
        employment="постоянно", pay_min=25000, pay_max=32000, pay_period="месяц", experience="от 1 года", contacts=_phone(18),
        summary="Пошив спецодежды на промышленных машинах. Оплата сдельная, стабильный заказ.")
    add(960, "медсестра", "медицина", employer="Частная клиника «Здоровье»", district="Кировский", schedule="2/2", work_format="точка",
        employment="сменная", pay_min=30000, pay_max=38000, pay_period="месяц", experience="от 1 года", requirements="Диплом, сертификат",
        source_type="hh", source="hh.ru", contacts={}, summary="Процедурный кабинет, приём пациентов. Оформление по ТК.")
    add(1020, "учитель английского", "образование", employer="Языковая школа «Лингва»", district="Ворошиловский", schedule="вечерние группы", work_format="точка",
        employment="подработка", pay_min=1200, pay_max=1500, pay_period="час", experience="от 1 года", contacts=_phone(19),
        summary="Вести группы взрослых и подростков. Методические материалы готовы.")
    add(1100, "сборщик заказов", "склад", employer="Пункт выдачи «Посылка»", district="Петровский", schedule="2/2", work_format="склад",
        employment="сменная", pay_min=2700, pay_max=3200, pay_period="смена", pay_daily=True, experience="не нужен", age_min=18, contacts=_phone(20),
        summary="Собирать и упаковывать заказы интернет-магазина. Работа на ногах, склад отапливается.")
    add(1200, "оператор пункта выдачи", "торговля", employer="ПВЗ «Посылка»", district="Куйбышевский", schedule="2/2, с 9:00 до 21:00", work_format="точка",
        employment="сменная", pay_min=2600, pay_max=2600, pay_period="смена", experience="не нужен", age_min=18, contacts=_phone(21),
        summary="Выдавать посылки, проверять товар вместе с клиентом, вести учёт.")
    add(1300, "дизайнер", "офис", employer="Студия «Контур»", schedule="свободный, удалённо", work_format="удалённо",
        employment="подработка", pay_min=1000, pay_max=2000, pay_period="час", experience="от 1 года", contacts={"telegram": "@demo_kontur"},
        source_type="telegram", source="@demo_rabota_remote", summary="Макеты для соцсетей и печати. Портфолио обязательно.")
    add(1400, "маляр", "стройка", employer="Бригада ремонта", district="Пролетарский", schedule="по объему работ", work_format="объект",
        employment="подработка", pay_min=45000, pay_max=45000, pay_period="задача", experience="от 1 года", contacts=_phone(22),
        summary="Покраска фасада двухэтажного здания, около двух недель. Материалы заказчика.")
    add(1500, "кладовщик", "склад", employer="Оптовая база «Донторг»", district="Киевский", schedule="5/2", work_format="склад",
        employment="постоянно", pay_min=30000, pay_max=36000, pay_period="месяц", experience="от 1 года", requirements="1С, ответственность",
        contacts=_phone(23), summary="Приёмка и отгрузка товара, инвентаризация, ведение документов.")
    add(1600, "продавец овощей", "торговля", employer="Рынок «Центральный»", district="Ленинский", schedule="6/1, с 6:00 до 15:00", work_format="точка",
        employment="постоянно", pay_min=2200, pay_max=2200, pay_period="день", pay_daily=True, experience="не нужен", contacts=_phone(24),
        summary="Торговля овощами и фруктами на прилавке. Оплата каждый день.")
    add(1700, "тестировщик", "офис", employer="ИТ-компания «Байт»", schedule="5/2, удалённо", work_format="удалённо",
        employment="постоянно", pay_min=55000, pay_max=80000, pay_period="месяц", experience="от 1 года", contacts={"email": "jobs@demo-byte.example"},
        source_type="hh", source="hh.ru", summary="Ручное тестирование мобильных приложений. Гибкое начало дня.")
    add(1800, "сантехник", "стройка", employer="Управляющая компания «Дом»", district="Кировский", schedule="5/2", work_format="выезд",
        employment="постоянно", pay_min=35000, pay_max=45000, pay_period="месяц", experience="от 1 года", contacts=_phone(25),
        summary="Устранение аварий и плановый ремонт в жилых домах. Служебный транспорт.")
    add(1900, "кондитер", "общепит", employer="Пекарня «Хлебница»", district="Калининский", schedule="2/2, ночные смены", work_format="точка",
        employment="сменная", pay_min=3800, pay_max=4800, pay_period="смена", experience="от 1 года", requirements="Медицинская книжка",
        contacts=_phone(26), summary="Выпечка тортов и пирожных на заказ. Своя рецептура приветствуется.")
    add(2000, "фасовщик", "производство", employer="Фабрика «Донской пряник»", district="Пролетарский", schedule="2/2, с 8:00 до 20:00", work_format="объект",
        employment="сменная", pay_min=2400, pay_max=2800, pay_period="смена", pay_daily=True, experience="не нужен", age_min=18, contacts=_phone(27),
        summary="Фасовать и упаковывать готовую продукцию. Спецодежда и обед за счёт предприятия.")
    add(2200, "промоутер", "услуги", employer="Рекламная компания «Волна»", district="Ворошиловский", schedule="свободный", work_format="выезд",
        employment="на сегодня", pay_min=1800, pay_max=1800, pay_period="день", pay_daily=True, experience="не нужен", age_min=16, contacts=_phone(28),
        summary="Раздача листовок у торгового центра. Один день, оплата сразу после смены.")
    add(2400, "мастер маникюра", "услуги", employer="Студия «Лотос»", district="Ворошиловский", schedule="свободный", work_format="точка",
        employment="сменная", pay_min=50000, pay_max=90000, pay_period="месяц", experience="от 1 года", contacts=_phone(29),
        summary="Аренда кресла не нужна, процент от работ. Поток клиентов обеспечен.")
    add(2600, "дворник", "услуги", employer="Управляющая компания «Дом»", district="Киевский", schedule="5/2, с 6:00 до 11:00", work_format="объект",
        employment="постоянно", pay_min=14000, pay_max=14000, pay_period="месяц", experience="не нужен", contacts=_phone(30),
        summary="Уборка придомовой территории утром, остальное время свободно.")
    add(2900, "программист Python", "офис", employer="ИТ-компания «Байт»", schedule="5/2, удалённо", work_format="удалённо",
        employment="постоянно", pay_min=90000, pay_max=140000, pay_period="месяц", experience="от 3 лет", contacts={"email": "jobs@demo-byte.example"},
        source_type="hh", source="hh.ru", summary="Бэкенд на FastAPI и PostgreSQL. Команда из пяти человек, без бюрократии.")
    add(3100, "помощник воспитателя", "образование", employer="Детский сад № 37", district="Буденновский", schedule="5/2, с 7:30 до 17:30", work_format="точка",
        employment="постоянно", pay_min=18000, pay_max=21000, pay_period="месяц", experience="не нужен", requirements="Медицинская книжка",
        source_type="mczn", source="Центр занятости Донецка", contacts=_phone(31), summary="Помогать воспитателю, следить за порядком в группе, раздавать питание.")
    add(3400, "шиномонтажник", "транспорт", employer="Автосервис «Колесо»", city="Макеевка", district="Червоногвардейский район", schedule="6/1", work_format="точка",
        employment="постоянно", pay_min=45000, pay_max=60000, pay_period="месяц", experience="от 1 года", contacts=_phone(32),
        summary="Шиномонтаж и балансировка легковых и грузовых колёс. Оплата сдельная.")
    add(3700, "продавец-кассир", "торговля", employer="Магазин «Аптека-Сити»", city="Горловка", district="Центральный", schedule="2/2", work_format="точка",
        employment="сменная", pay_min=3000, pay_max=3000, pay_period="смена", experience="не нужен", contacts=_phone(33),
        summary="Продажа товаров, расчёт покупателей. Обучение в первую неделю.")
    add(4000, "сварщик", "производство", employer="Завод «Металлург»", city="Енакиево", schedule="5/2", work_format="объект",
        employment="постоянно", pay_min=55000, pay_max=75000, pay_period="месяц", experience="от 3 лет", requirements="Допуск НАКС",
        contacts=_phone(34), source_type="trudvsem", source="Работа России", summary="Сварка металлоконструкций, аргон и полуавтомат. Общежитие предоставляется.")
    add(4300, "садовник", "услуги", employer="Частный дом", district="Пролетарский", schedule="по договорённости", work_format="выезд",
        employment="подработка", pay_min=None, pay_max=None, pay_period=None, experience="не нужен", contacts=_phone(35),
        summary="Уход за садом, обрезка деревьев, покос. Оплату обсудим по телефону.")
    add(4700, "маркетолог", "офис", employer="Компания «Контур»", district="Ворошиловский", schedule="5/2", work_format="офис",
        employment="постоянно", pay_min=45000, pay_max=60000, pay_period="месяц", experience="от 1 года", contacts={"telegram": "@demo_kontur"},
        summary="Вести соцсети, запускать рекламу, считать отдачу. Нужен опыт с таргетом.")
    add(5200, "грузчик", "склад", employer="Мебельная фабрика «Дом»", district="Киевский", schedule="5/2", work_format="склад",
        employment="постоянно", pay_min=32000, pay_max=38000, pay_period="месяц", experience="не нужен", age_min=18, contacts=_phone(36),
        summary="Погрузка готовой мебели, сборка заказов. Официальное оформление.")
    add(5600, "бариста", "общепит", employer="Кофе-точка «Эспрессо»", district="Ленинский", schedule="2/2, с 8:00 до 20:00", work_format="точка",
        employment="сменная", pay_min=3500, pay_max=3500, pay_period="смена", experience="от 6 месяцев", contacts=_phone(37),
        summary="Работа на кофемашине, ведение кассы. Хороший проходной поток.")
    add(6200, "водитель-экспедитор", "транспорт", employer="Компания «Донторг»", city="Макеевка", schedule="5/2", work_format="выезд",
        employment="постоянно", pay_min=38000, pay_max=48000, pay_period="месяц", experience="от 1 года", requirements="Категория C",
        contacts=_phone(38), summary="Развозить товар по торговым точкам Макеевки и Донецка на служебном грузовике.")
    add(7000, "кассир", "торговля", employer="Магазин «Продукты у дома»", city="Макеевка", district="Центрально-Городской район", schedule="2/2", work_format="точка",
        employment="сменная", pay_min=3000, pay_max=3300, pay_period="смена", experience="не нужен", contacts=_phone(39),
        summary="Кассовая работа и приёмка. Обучение на месте.")
    add(8000, "администратор", "услуги", employer="Фитнес-клуб «Сила»", district="Калининский", schedule="2/2", work_format="точка",
        employment="сменная", pay_min=30000, pay_max=36000, pay_period="месяц", experience="не нужен", contacts=_phone(40),
        summary="Встречать посетителей, продавать абонементы, следить за чистотой.")
    add(10000, "повар-универсал", "общепит", employer="Кафе «Лето»", district="Ворошиловский", schedule="3/3", work_format="точка",
        employment="сменная", pay_min=3800, pay_max=3800, pay_period="смена", pay_daily=True, experience="от 1 года", contacts=_phone(41),
        summary="Горячий и холодный цех, заготовки. Питание за счёт заведения.")
    add(12000, "монтажник", "стройка", employer="Строительная компания «Фундамент»", city="Мариуполь", schedule="вахта 30/30", work_format="вахта",
        employment="постоянно", pay_min=80000, pay_max=110000, pay_period="месяц", experience="от 1 года", contacts=_phone(42),
        summary="Монтаж металлоконструкций, проживание и питание за счёт компании.")
    add(15000, "дизайнер интерьеров", "услуги", employer="Студия «Контур»", district="Ворошиловский", schedule="свободный", work_format="выезд",
        employment="подработка", pay_min=30000, pay_max=30000, pay_period="задача", experience="от 1 года", contacts={"telegram": "@demo_kontur"},
        summary="Проект квартиры 60 кв. м, замеры и визуализация. Срок три недели.")
    add(20000, "сиделка", "медицина", employer="Частный заказчик", district="Куйбышевский", schedule="сутки через двое", work_format="выезд",
        employment="сменная", pay_min=3500, pay_max=3500, pay_period="смена", experience="не нужен", age_min=25, contacts=_phone(43),
        summary="Уход за пожилым человеком: питание, прогулки, лекарства. Нужны аккуратность и терпение.")

    # Одна вакансия в нескольких каналах: главная карточка и её дубли из других источников.
    add(21, "бариста", "общепит", dup_of="1001", employer="Кофейня «Зерно»", district="Ворошиловский", contacts=_phone(1),
        pay_min=4000, pay_max=4000, pay_period="смена", source_type="vk", source="club_demo_kofe", summary="Тот же набор в группе ВКонтакте.")
    add(22, "бариста", "общепит", dup_of="1001", employer="Кофейня «Зерно»", district="Ворошиловский", contacts=_phone(1),
        pay_min=4000, pay_max=4000, pay_period="смена", source_type="max", source="demo_donetsk_rabota", summary="Тот же набор в канале MAX.")
    add(23, "бариста", "общепит", dup_of="1001", employer="Кофейня «Зерно»", district="Ворошиловский", contacts=_phone(1),
        pay_min=4000, pay_max=4000, pay_period="смена", source="@demo_rabota_barista", summary="Тот же набор в другом канале Telegram.")
    add(80, "бухгалтер", "офис", dup_of="1003", employer="ООО «Донбасс-Снаб»", district="Куйбышевский", contacts=_phone(3),
        pay_min=48500, pay_max=56000, pay_period="месяц", source_type="hh", source="hh.ru", summary="Та же вакансия на hh.ru.")
    add(170, "водитель", "транспорт", dup_of="1006", employer="Служба доставки «Быстрый стол»", district="Буденновский", contacts=_phone(6),
        pay_min=40000, pay_max=55000, pay_period="месяц", source_type="max", source="demo_donetsk_rabota", summary="Та же вакансия в MAX.")
    add(250, "повар", "общепит", dup_of="1008", employer="Столовая «Домашняя»", district="Ворошиловский", contacts=_phone(8),
        pay_min=3500, pay_max=4500, pay_period="смена", source_type="vk", source="club_demo_povar", summary="Та же вакансия во ВКонтакте.")

    # Записи, которые на сайте не показываются: нужны, чтобы проверять фильтрацию.
    add(20, "военная служба по контракту", "другое", kind="military", employer="Военкомат", contacts=_phone(90),
        title_raw="Набор на военную службу", summary="Набор на военную службу по контракту.")
    add(25, "кассир", "торговля", employer="Магазин «Продукты у дома»", district="Калининский", schedule="2/2", contacts=_phone(2),
        pay_min=3200, pay_max=3600, pay_period="смена", summary="Повтор той же вакансии из другого канала.", dup_of="1002")
    add(30, "менеджер по инвестициям", "офис", suspicious=True, contacts=_phone(91), pay_min=300000, pay_max=500000, pay_period="месяц",
        summary="Доход от трёхсот тысяч без опыта, нужен залог.")
    add(5000, "бариста", "общепит", status="closed", employer="Кофе-точка «Старт»", district="Ленинский", contacts=_phone(92),
        pay_min=3000, pay_max=3000, pay_period="смена", summary="Закрытая вакансия, набор завершён.")
    add(6000, "няня", "услуги", kind="service", contacts=_phone(93), summary="Услуги репетитора, а не вакансия.")

    items.sort(key=lambda t: -t[0])
    return [(v, dup) for _, v, dup in items]


def seed(db_path: Path, reset: bool = True, now: datetime | None = None) -> int:
    conn = connect(db_path)
    if reset:
        conn.execute("DELETE FROM complaints")
        conn.execute("DELETE FROM vacancies")
        conn.commit()
    ids: dict[str, int] = {}
    vacancies = demo_vacancies(now or datetime.now(UTC))
    for v, _ in vacancies:
        ids[v.external_id] = save_vacancy(conn, v)
    for v, dup in vacancies:
        if dup is not None:
            conn.execute("UPDATE vacancies SET duplicate_of = ? WHERE id = ?", (ids[dup], ids[v.external_id]))
    mark_direct(conn, ids)
    conn.commit()
    conn.close()
    return len(vacancies)


def mark_direct(conn, ids: dict[str, int]) -> None:
    """Часть вакансий помечается как размещённые работодателями: у них есть компания и кнопка отклика."""
    conn.execute("INSERT OR IGNORE INTO users(email, password_hash, role, name) VALUES ('demo-employer@example.test', '!', 'employer', 'Демо')")
    owner = conn.execute("SELECT id FROM users WHERE email = 'demo-employer@example.test'").fetchone()[0]
    conn.execute("DELETE FROM companies WHERE owner_user_id = ?", (owner,))
    for external_id, name in DEMO_DIRECT.items():
        if external_id not in ids:
            continue
        cur = conn.execute(
            "INSERT INTO companies(owner_user_id, name, description, city, verified) VALUES (?, ?, ?, 'Донецк', 1)",
            (owner, name, "Демонстрационный работодатель: описание выдумано для проверки оформления."),
        )
        conn.execute(
            "UPDATE vacancies SET origin = 'direct', company_id = ?, source_type = 'site', source = 'razmestit', source_url = '' WHERE id = ?",
            (cur.lastrowid, ids[external_id]),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Демо-вакансии Донецка (выдуманные)")
    parser.add_argument("--db", default=str(DEMO_DB), help="путь к демо-базе, по умолчанию data/demo.db")
    parser.add_argument("--keep", action="store_true", help="не очищать таблицы перед вставкой")
    args = parser.parse_args()
    total = seed(Path(args.db), reset=not args.keep)
    print(f"Записано {total} вакансий в {args.db}")


if __name__ == "__main__":
    main()
