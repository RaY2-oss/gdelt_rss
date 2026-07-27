# -*- coding: utf-8 -*-
"""Самопроверка дневного дайджеста: буст по охвату, отсев не-сегодняшних
сюжетов, отсев повторов через digest_sent, MMR-диверсити.
Сеть/LLM/e5 не трогаются. Запуск: venv/bin/python test_digest.py"""
import os
import tempfile
import numpy as np

import settings
settings.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.LOG_DIR = tempfile.mkdtemp()
settings.TOP_N = 3

import db, digest, importance as imp
from datetime import datetime, timedelta, timezone

TODAY = datetime.now(timezone.utc).date().isoformat()
YDAY = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
NOW = datetime.now(timezone.utc).isoformat()


def _v(seed):
    v = np.zeros(settings.EMBEDDING_DIM, np.float32)
    v[seed] = 1
    return v


def _row(url, score, vec, ents=""):
    """12 колонок feeds._SELECT — форма строки у фида, витрины и дайджеста одна."""
    return (url, "t", "x", None, NOW, score, vec.tobytes(), "en", None, None,
            vec.tobytes(), ents)


def _cluster_imp(clusters, ent_df):
    """{label: важность} — то же, что делает feeds.rank, но с явным ent_df:
    формула живёт в importance.structural (одна на все три ленты), а тесты
    ниже проверяют именно её, а не подсчёт док-частоты."""
    labels = list(clusters)
    reps = [max(clusters[lab], key=lambda r: r[4] or "") for lab in labels]
    return dict(zip(labels, imp.structural(
        [imp.body_emb(r[10], r[6]) for r in reps],
        [[r[0] for r in clusters[lab]] for lab in labels],
        [[r[11] or "" for r in clusters[lab]] for lab in labels],
        ent_df, fresh=[r[4] for r in reps])))


def test_coverage_counts_domains_not_articles():
    """Пять версий текста на ОДНОМ домене — один голос охвата, а не пять."""
    v = _v(0)
    one_domain = {0: [_row(f"https://a.com/{i}", 40, v) for i in range(5)]}
    five_domains = {0: [_row(f"https://s{i}.com/x", 40, v) for i in range(5)]}
    a = _cluster_imp(one_domain, None)[0]
    b = _cluster_imp(five_domains, None)[0]
    assert b > a, (a, b)


def test_coverage_saturates_instead_of_capping():
    """Охват логарифмический: прирост убывает, но сюжет с 40 изданиями всё
    ещё отличим от сюжета с 4 — прежняя линейная надбавка упирала оба в потолок."""
    v = _v(0)
    def cov(n):
        cl = {0: [_row(f"https://s{i}.com/x", 40, v) for i in range(n)]}
        return _cluster_imp(cl, None)[0]
    # Сравнивать нужно РАВНЫЕ приращения числа изданий (2->4->6). На удвоениях
    # (2->4->8) логарифм по построению даёт одинаковый шаг, и убывание не видно.
    c2, c4, c6, c40 = cov(2), cov(4), cov(6), cov(40)
    assert c2 < c4 < c6 <= c40
    assert (c4 - c2) > (c6 - c4), "прирост обязан убывать — это и есть насыщение"


def test_importance_is_continuous_and_breaks_llm_ties():
    """Главное, ради чего всё затевалось: у сюжетов с ОДИНАКОВОЙ оценкой LLM
    структурная важность разная, поэтому TOP_N-отбор перестаёт быть жеребьёвкой."""
    clusters = {
        0: [_row("https://a.com/1", 85, _v(0)), _row("https://b.com/1", 85, _v(0)),
            _row("https://c.com/1", 85, _v(0))],
        1: [_row("https://d.com/1", 85, _v(1))],
        2: [_row("https://e.com/1", 85, _v(2)), _row("https://f.com/1", 85, _v(2))],
    }
    got = _cluster_imp(clusters, None)
    assert len(set(got.values())) == len(got), f"связки остались: {got}"
    assert got[0] > got[2] > got[1], got


def test_entity_factor_lifts_national_story():
    """Третий фактор — политический вес: сюжет с несколькими заметными
    субъектами обгоняет сюжет с тем же охватом, но без фигурантов."""
    v0, v1 = _v(0), _v(1)
    df = {"tubitak": 5, "aselsan": 5, "ministry of industry": 5, "ali veli": 1}
    clusters = {
        0: [_row("https://a.com/1", 40, v0, "tubitak;aselsan;ministry of industry"),
            _row("https://b.com/1", 40, v0, "tubitak;aselsan")],
        1: [_row("https://c.com/1", 40, v1, "ali veli"),
            _row("https://d.com/1", 40, v1, "ali veli")],
    }
    got = _cluster_imp(clusters, df)
    assert got[0] > got[1], got


def test_only_todays_articles_are_candidates():
    db.init()
    conn = db.connect()
    v = _v(1)
    # сюжет: статья вчера + статья сегодня по тому же сюжету (importance выше
    # RELEVANCE_CUTOFF, иначе фильтр build_country_digest их не видит)
    conn.execute("INSERT INTO articles (url,country,fetched_at,publish_date,title,text,"
                 "language,title_hash,embedding,importance) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("http://x/y", "india", NOW, YDAY, "old", "body", "en", "h1",
                  v.tobytes(), 50))
    conn.execute("INSERT INTO articles (url,country,fetched_at,publish_date,title,text,"
                 "language,title_hash,embedding,importance) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("http://x/z", "india", NOW, TODAY, "new", "body2", "en", "h2",
                  v.tobytes(), 50))
    conn.commit()
    n = digest.build_country_digest(conn, "india", day=TODAY)
    conn.close()
    assert n == 1, "кластер без сегодняшней статьи не должен размножать дайджест"


def test_cross_day_no_repeat():
    db.init()
    conn = db.connect()
    v = _v(2)
    conn.execute("INSERT INTO digest_sent (url,country,digest_date,embedding) VALUES (?,?,?,?)",
                 ("http://old", "india", YDAY, v.tobytes()))
    conn.execute("INSERT INTO articles (url,country,fetched_at,publish_date,title,text,"
                 "language,title_hash,embedding,importance) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("http://x/new-day-same-story", "india", NOW, TODAY, "t", "body",
                  "en", "h3", v.tobytes(), 90))
    conn.commit()
    n = digest.build_country_digest(conn, "india", day=TODAY)
    conn.close()
    assert n == 0, "сюжет, уже отправленный вчера, не должен повториться"


def test_build_country_digest_translates_non_en_ru():
    """build_country_digest должен переводить отобранные статьи (кроме ru/en)
    и писать перевод в {country}_digest.xml — как это уже делает feeds.build_country."""
    from unittest.mock import patch
    import translate as translate_mod

    db.init()
    conn = db.connect()
    v = _v(4)
    conn.execute("INSERT INTO articles (url,country,fetched_at,publish_date,title,text,"
                 "language,title_hash,embedding,importance) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("http://x/zh", "china", NOW, TODAY, "中文标题", "正文内容",
                  "zh-cn", "hzh", v.tobytes(), 90))
    conn.commit()

    with patch.object(translate_mod, "_translate", side_effect=lambda t, source="auto": f"EN::{t}"):
        n = digest.build_country_digest(conn, "china", day=TODAY)
    conn.close()

    assert n == 1
    xml = open(os.path.join(settings.OUTPUT_DIR, "china_digest.xml"), encoding="utf-8").read()
    assert "EN::中文标题" in xml, "дайджест должен писать переведённый заголовок"
    assert "EN::正文内容" in xml, "дайджест должен писать переведённый текст"


def test_mmr_prefers_diverse_over_near_duplicate():
    a = {"url": "a", "title": "a", "text": "a", "pdate": None, "fa": NOW,
         "imp": 90, "emb": _v(0)}
    b = {"url": "b", "title": "b", "text": "b", "pdate": None, "fa": NOW,
         "imp": 89, "emb": _v(0)}  # почти дубль a по важности и вектору
    c = {"url": "c", "title": "c", "text": "c", "pdate": None, "fa": NOW,
         "imp": 50, "emb": _v(3)}  # другой сюжет, ниже важность
    picked = digest._mmr_select([a, b, c], 2)
    urls = [p["url"] for p in picked]
    assert urls == ["a", "c"], "MMR должен предпочесть разнообразие дублю с похожей важностью"


def test_daily_digest_defaults_to_previous_day():
    """Регресс на пустой _important: cron дёргает daily_digest.py в 00:00 UTC,
    когда сегодняшний день ещё пуст — день по умолчанию обязан быть вчерашним,
    иначе build_country_digest не находит ни одного кандидата (см. digest.py,
    шаг 4: в дайджест идут только статьи с датой == day)."""
    import subprocess, sys, os
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_digest.py")
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys, runpy; sys.argv=['daily_digest.py'];"
         "import digest; digest.build_all=lambda day=None: print('DAY', day);"
         "import db; db.init=lambda: None;"
         f"runpy.run_path({src!r}, run_name='__main__')"],
        capture_output=True, text=True, cwd=os.path.dirname(src))
    assert f"DAY {YDAY}" in out.stdout, out.stdout + out.stderr


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
    print("ALL PASS")
