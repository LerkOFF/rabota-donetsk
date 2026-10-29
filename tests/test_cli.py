import types

import pytest

from rabota import __main__ as cli
from rabota.db import connect, load_raw
from rabota.models import RawPost

TEXT = "Требуется бариста в кофейню, Донецк, Ворошиловский район. График 2/2, оплата 4000 за смену. Звоните +79490000001"


def _fake_collector(posts):
    return types.SimpleNamespace(NAME="fake", fetch=lambda limit=10, client=None: posts[:limit])


@pytest.fixture
def conn():
    return connect(":memory:")


def test_collect_saves_raw_and_samples(conn, monkeypatch, tmp_path):
    posts = [RawPost("telegram", "chan", str(i), f"https://t.me/chan/{i}", TEXT) for i in range(3)]
    monkeypatch.setattr(cli, "_collector", lambda name: _fake_collector(posts))
    monkeypatch.setattr(cli, "SAMPLES_DIR", tmp_path)
    totals = cli.cmd_collect(conn, limit=2, sources=["telegram"])
    assert totals == {"telegram": 2}
    assert len(load_raw(conn)) == 2
    assert (tmp_path / "telegram.json").exists()


def test_collect_survives_failing_source(conn, monkeypatch, tmp_path, capsys):
    def boom(name):
        raise RuntimeError("блокировка")

    monkeypatch.setattr(cli, "_collector", boom)
    monkeypatch.setattr(cli, "SAMPLES_DIR", tmp_path)
    assert cli.cmd_collect(conn, limit=10, sources=["avito"]) == {"avito": 0}
    assert "ОШИБКА" in capsys.readouterr().out


def test_process_makes_vacancy_and_merges_duplicates(conn, monkeypatch):
    for i, chan in enumerate(["a", "b"]):
        conn.execute(
            "INSERT INTO raw_posts(source_type,source,external_id,url,text,published_at) VALUES (?,?,?,?,?,?)",
            ("telegram", chan, str(i), f"https://t.me/{chan}/{i}", TEXT, f"2026-10-29T0{i}:00:00+00:00"),
        )
    conn.commit()
    monkeypatch.setattr(cli, "_vision_provider", lambda: None)
    stats = cli.cmd_process(conn)
    assert stats["vacancies"] == 2 and stats["duplicates"] == 1
    shown = conn.execute("SELECT COUNT(*) FROM vacancies WHERE duplicate_of IS NULL AND kind='vacancy'").fetchone()[0]
    assert shown == 1


def test_stats_prints(conn, capsys):
    cli.cmd_stats(conn)
    assert "на сайте: 0" in capsys.readouterr().out


def test_expire_command_hides_and_purges(conn, capsys, monkeypatch):
    from rabota.db import save_vacancy
    from rabota.models import Vacancy

    vid = save_vacancy(conn, Vacancy(profession="бариста", published_at="2020-01-01T00:00:00+00:00"))
    monkeypatch.setattr(cli, "connect", lambda path=None: conn)
    assert cli.main(["expire"]) == 0
    out = capsys.readouterr().out
    assert "удалено" in out
    assert conn.execute("SELECT COUNT(*) FROM vacancies WHERE id=?", (vid,)).fetchone()[0] == 0
