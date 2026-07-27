# -*- coding: utf-8 -*-
"""Самопроверка чистой логики: разбор оценок, дедуп, top-N, XML фида.
Сеть/LLM/e5 не трогаются. Запуск: venv/bin/python test_pipeline.py"""
import os
import tempfile
import numpy as np

import settings
settings.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
settings.OUTPUT_DIR = tempfile.mkdtemp()
settings.LOG_DIR = tempfile.mkdtemp()
settings.TOP_N = 3

import db, feeds, pipeline
from datetime import datetime, timezone


def test_parse_scores():
    assert pipeline._parse_scores('{"1": 80, "2": 15}', 2) == {1: 80, 2: 15}
    assert pipeline._parse_scores('```json\n{"1": 999, "2": -5}\n```', 2) == {1: 100, 2: 0}
    assert pipeline._parse_scores('junk {"1": 50} tail', 3) == {1: 50}       # id вне диапазона отсекается
    assert pipeline._parse_scores('{"1": 10, "9": 90}', 3) == {1: 10}
    # «Extra data»: два склеенных объекта — раньше это роняло весь батч
    assert pipeline._parse_scores('{"1": 40}{"2": 50}', 2) == {1: 40}
    assert pipeline._parse_scores('Here you go:\n{"1": 40}\nDone.', 2) == {1: 40}


def test_title_hash():
    assert pipeline._title_hash("Hello, World!") == pipeline._title_hash("hello   world")
    assert pipeline._title_hash("") is None


def test_group_for_scoring_dedups_before_llm():
    v = np.ones(settings.EMBEDDING_DIM, np.float32)
    w = np.zeros(settings.EMBEDDING_DIM, np.float32); w[0] = 1
    rows = [
        ("u1", "a",     "text a",     v.tobytes()),
        ("u2", "a dup", "text a dup", v.tobytes()),  # тот же сюжет, другое издание
        ("u3", "b",     "text b",     w.tobytes()),  # другой сюжет
    ]
    items, groups = pipeline._group_for_scoring(rows)
    assert len(items) == 2, "дубликаты должны схлопнуться в один сюжет перед LLM"
    assert sum(len(v) for v in groups.values()) == 3, "но оценка должна дойти до всех статей"


def _mk_gate_articles(conn, country):
    v_drop = np.zeros(settings.EMBEDDING_DIM, np.float32); v_drop[0] = 1
    v_keep = np.zeros(settings.EMBEDDING_DIM, np.float32); v_keep[1] = 1
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,title_hash,embedding) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (f"http://{country}/drop", country, "2026-07-20T00:00:00+00:00",
                  "Drop Story", "text", "en", f"{country}hd", v_drop.tobytes()))
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,title_hash,embedding) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (f"http://{country}/keep", country, "2026-07-20T00:01:00+00:00",
                  "Keep Story", "text", "en", f"{country}hk", v_keep.tobytes()))
    conn.commit()
    return v_drop, v_keep


def test_score_gate_drops_confident_rejects_without_llm():
    """Нижний порог гейта: сюжет получает importance=0 и вердикт 'rejected'
    без обращения к LLM, а строка помечается scored_by='gate' — чтобы не
    попасть в обучающую выборку следующего train_prefilter.py."""
    from unittest.mock import patch
    import json

    db.init()
    conn = db.connect()
    v_drop, v_keep = _mk_gate_articles(conn, "prefland")
    calls = []

    def fake_openrouter(system, user, ref_url=None):
        calls.append(user)
        return json.dumps({"1": 80})

    def fake_verdicts(path, embeddings, texts):
        # items идут в порядке rows (fetched_at DESC): keep первым, drop вторым
        return [pipeline.prefilter.ASK, pipeline.prefilter.DROP]

    with patch.object(pipeline.prefilter, "is_ready", return_value=True), \
         patch.object(pipeline.prefilter, "verdicts", side_effect=fake_verdicts), \
         patch.object(pipeline.random, "random", return_value=1.0), \
         patch.object(pipeline, "_call_openrouter_raw", side_effect=fake_openrouter):
        pipeline.score()

    assert len(calls) == 1, "гейт должен убрать отсеянный сюжет из LLM-батча"
    assert "Keep Story" in calls[0] and "Drop Story" not in calls[0]

    row = lambda u: conn.execute(
        "SELECT importance, scored_by FROM articles WHERE url=?", (u,)).fetchone()
    assert row("http://prefland/drop") == (0, "gate")
    assert row("http://prefland/keep") == (80, "llm")

    v = conn.execute("SELECT verdict, embedding FROM seen_urls WHERE url=?",
                     ("http://prefland/drop",)).fetchone()
    conn.close()
    assert v == ("rejected", v_drop.tobytes())


def test_score_gate_accepts_without_llm():
    """Верхний порог: уверенный приём не покупается у LLM. Статья получает
    PREFILTER_ACCEPT_SCORE — этого хватает, чтобы пройти MIN_IMPORTANCE и
    попасть в кандидаты дайджеста, где порядок задаёт уже структурная
    важность (importance.py), а не это число."""
    from unittest.mock import patch

    db.init()
    conn = db.connect()
    _mk_gate_articles(conn, "acceptland")
    calls = []

    with patch.object(pipeline.prefilter, "is_ready", return_value=True), \
         patch.object(pipeline.prefilter, "verdicts",
                      side_effect=lambda p, e, t: [pipeline.prefilter.KEEP] * len(e)), \
         patch.object(pipeline.random, "random", return_value=1.0), \
         patch.object(pipeline, "_call_openrouter_raw",
                      side_effect=lambda *a, **k: calls.append(1) or "{}"):
        pipeline.score()

    assert not calls, "уверенный приём не должен тратить LLM-вызов"
    got = dict(conn.execute("SELECT url, importance FROM articles WHERE country='acceptland'"))
    sb = {r[0] for r in conn.execute("SELECT DISTINCT scored_by FROM articles WHERE country='acceptland'")}
    verdicts = {r[0] for r in conn.execute(
        "SELECT DISTINCT verdict FROM seen_urls WHERE url LIKE 'http://acceptland/%'")}
    conn.close()
    assert set(got.values()) == {settings.PREFILTER_ACCEPT_SCORE}, got
    assert got["http://acceptland/keep"] >= settings.MIN_IMPORTANCE
    assert sb == {"gate"}, "локальное решение не должно попасть в обучающую выборку"
    assert verdicts == {"accepted"}


def test_control_share_sends_gated_items_to_llm_anyway():
    """Контрольная струя: часть потока идёт в LLM в обход гейта, иначе новых
    независимых меток не появится и дрейф будет нечем заметить."""
    from unittest.mock import patch
    import json

    db.init()
    conn = db.connect()
    _mk_gate_articles(conn, "ctrlland")
    calls = []

    with patch.object(pipeline.prefilter, "is_ready", return_value=True), \
         patch.object(pipeline.prefilter, "verdicts",
                      side_effect=lambda p, e, t: [pipeline.prefilter.DROP] * len(e)), \
         patch.object(pipeline.random, "random", return_value=0.0), \
         patch.object(pipeline, "_call_openrouter_raw",
                      side_effect=lambda s_, u_, ref_url=None: calls.append(u_) or json.dumps({"1": 70, "2": 70})):
        pipeline.score()

    sb = {r[0] for r in conn.execute("SELECT DISTINCT scored_by FROM articles WHERE country='ctrlland'")}
    conn.close()
    assert len(calls) == 1, "при random()=0 гейт обязан пропустить всё в LLM"
    assert sb == {"llm"}


def test_missing_ids_stay_unscored_instead_of_being_rejected():
    """Модель вернула не все id. Пропущенные НЕ получают ноль (это молчаливый
    вердикт «отклонено» по несуждённой статье, который ушёл бы в обучающую
    выборку) — остаются NULL и переоцениваются следующим прогоном."""
    from unittest.mock import patch
    import json

    db.init()
    conn = db.connect()
    _mk_gate_articles(conn, "partial")

    with patch.object(pipeline.prefilter, "is_ready", return_value=False), \
         patch.object(pipeline, "_call_openrouter_raw",
                      side_effect=lambda *a, **k: json.dumps({"1": 90})):
        pipeline.score()

    got = dict(conn.execute("SELECT url, importance FROM articles WHERE country='partial'"))
    conn.close()
    scored = [v for v in got.values() if v is not None]
    assert len(scored) == 1 and scored[0] == 90, got
    assert None in got.values(), "пропущенный моделью id обязан остаться NULL"


def test_score_propagates_verdict_to_duplicate_group_in_seen_urls():
    """Оценка одного представителя сюжета копируется на весь дубликат-кластер
    и в articles.importance, и в seen_urls (verdict по settings.RELEVANCE_CUTOFF)."""
    from unittest.mock import patch
    import json
    import re as re_mod

    db.init()
    conn = db.connect()
    v_dup = np.zeros(settings.EMBEDDING_DIM, np.float32); v_dup[0] = 1
    v_other = np.zeros(settings.EMBEDDING_DIM, np.float32); v_other[1] = 1
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,title_hash,embedding) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 ("http://d/1", "dupland", "2026-07-20T00:00:00+00:00",
                  "Dup Story A", "text", "en", "d1", v_dup.tobytes()))
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,title_hash,embedding) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 ("http://d/2", "dupland", "2026-07-20T00:01:00+00:00",
                  "Dup Story B", "text", "en", "d2", v_dup.tobytes()))
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,title_hash,embedding) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 ("http://d/3", "dupland", "2026-07-20T00:02:00+00:00",
                  "Other Story", "text", "en", "d3", v_other.tobytes()))
    conn.commit()

    def fake_openrouter(system, user, ref_url=None):
        scores = {}
        for m in re_mod.finditer(r"\[id=(\d+)\] (.*)", user):
            scores[m.group(1)] = 80 if m.group(2).startswith("Dup Story") else 2
        return json.dumps(scores)

    with patch.object(pipeline.prefilter, "is_ready", return_value=False), \
         patch.object(pipeline, "_call_openrouter_raw", side_effect=fake_openrouter):
        pipeline.score()

    verdicts = dict(conn.execute(
        "SELECT url, verdict FROM seen_urls WHERE url IN (?,?,?)",
        ("http://d/1", "http://d/2", "http://d/3")).fetchall())
    conn.close()
    assert verdicts["http://d/1"] == "accepted" and verdicts["http://d/2"] == "accepted", \
        "оба дубликата одного сюжета должны получить одинаковый вердикт"
    assert verdicts["http://d/3"] == "rejected"


def test_cluster_dedups_and_sorts_by_importance():
    # два одинаковых вектора (дубль) + два разных → 3 кластера; дубль схлопнут.
    v_dup = np.ones(settings.EMBEDDING_DIM, np.float32)
    v_b = np.zeros(settings.EMBEDDING_DIM, np.float32); v_b[0] = 1
    v_c = np.zeros(settings.EMBEDDING_DIM, np.float32); v_c[1] = 1
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        ("u1", "dup low",  "t", None, now, 10, v_dup.tobytes(), "en", None, None),
        ("u2", "dup high", "t", None, now, 90, v_dup.tobytes(), "en", None, None),
        ("u3", "b",        "t", None, now, 50, v_b.tobytes(), "en", None, None),
        ("u4", "c",        "t", None, now, 40, v_c.tobytes(), "en", None, None),
    ]
    top = feeds._top_items(rows, "india")
    urls = [r[0] for r in top]
    assert "u2" in urls and "u1" not in urls, "дубль: должен остаться более важный"
    assert len(top) == 3, "все статьи после дедупа, без лимита"
    assert [r[5] for r in top] == [90, 50, 40], "сортировка по важности"


def _vec_at_cosine(cos, dim=settings.EMBEDDING_DIM, seed=0):
    """Единичный вектор с косинусом ровно `cos` к [1,0,0,...]."""
    rng = np.random.default_rng(seed)
    v = np.zeros(dim, np.float32); v[0] = 1.0
    orth = rng.standard_normal(dim).astype(np.float32); orth[0] = 0
    orth /= np.linalg.norm(orth)
    return (cos * v + np.sqrt(1 - cos**2) * orth).astype(np.float32)


def test_cluster_lexical_confirm_merges_below_dedup_cosine():
    """Реальный кейс с калибровки: разные издания об одном сюжете (Bhargavastra)
    дают e5-косинус ~0.86-0.93 — ниже DEDUP_COSINE=0.95, но общие "редкие"
    токены заголовка (bhargavastra, swarm, counter...) подтверждают один
    сюжет выше DEDUP_LEXICAL_FLOOR."""
    titles = [
        "Solar Defence and Aerospace Ltd demonstrates indigenous 'Bhargavastra' counter-swarm drone system",
        "Bhargavastra: India's Indigenous Counter-Swarm Drone System",
        "India's turbojet breakthrough: 1st indigenous engine to power future missiles",
    ]
    embs = [
        _vec_at_cosine(1.0, seed=1),
        _vec_at_cosine(0.88, seed=1),   # тот же сюжет, разное издание (ниже 0.95)
        _vec_at_cosine(0.85, seed=2),   # другой сюжет, но семантически близкий (та же тема/страна)
    ]
    labels = feeds._cluster(embs, settings.DEDUP_COSINE, titles=titles)
    assert labels[0] == labels[1], "дубль одного сюжета должен схлопнуться по общим токенам"
    assert labels[2] != labels[0], "другой сюжет не должен слиться по одному лишь высокому косинусу"


def test_cluster_lexical_cutoff_scales_with_batch_size():
    """Регресс на баг из прод-инцидента 2026-07-25: вирусный сюжет (~90 из
    300 статей об одной новости на разных языках) — общая для дубликатов
    лексика (resigns, minister...) при старом fixed-6 потолке DF-cutoff
    ошибочно считалась "общей для батча" и переставала бриджевать дубли.
    Здесь батч намеренно большой (30), чтобы старый cutoff=6 не пропустил
    токен, встречающийся в 12 из 30 заголовков."""
    n_total, n_dup = 100, 20  # дубль в 20% батча: старый fixed-6 потолок это не тянет
    titles = [f"Minister resigns amid nationwide protests report {i}" for i in range(n_dup)]
    titles += [f"unrelated filler headline number {i}" for i in range(n_total - n_dup)]
    # все дубли в одной подплоскости (seed=1), как и "корень" (cos=1.0) — их
    # косинус ДРУГ К ДРУГУ через корень контролируем через cos, в отличие от
    # филлера (другой seed => ~ортогонален корню).
    embs = [_vec_at_cosine(1.0 if i == 0 else 0.85, seed=1) for i in range(n_dup)]
    embs += [_vec_at_cosine(0.05, seed=200 + i) for i in range(n_total - n_dup)]
    labels = feeds._cluster(embs, settings.DEDUP_COSINE, titles=titles)
    assert len(set(labels[:n_dup].tolist())) == 1, \
        "дубли одного вирусного сюжета должны схлопнуться даже в большом батче"


def test_cluster_excludes_country_token_from_lexical_bridge():
    """Без exclude двум НЕсвязанным статьям об Индии достаточно случайно
    разделить "india" + один общий глагол, чтобы ложно слиться в один
    кластер (прод-баг: NavIC история слилась с историей про протесты
    только по "india"+"sparked")."""
    titles = [
        "How 1999 Kargil GPS denial sparked India NavIC system",
        "Exam scandal in India sparked a student uprising",
    ]
    embs = [_vec_at_cosine(1.0, seed=1), _vec_at_cosine(0.83, seed=1)]
    exclude = feeds._TOKEN_RE.split("india")
    labels = feeds._cluster(embs, settings.DEDUP_COSINE, titles=titles, exclude=exclude)
    assert labels[0] != labels[1], "общие 'india'+'sparked' не должны в одиночку сливать разные сюжеты"


def test_build_country_writes_xml():
    db.init()
    conn = db.connect()
    now = datetime.now(timezone.utc).isoformat()
    emb = np.random.rand(settings.EMBEDDING_DIM).astype(np.float32).tobytes()
    # language="en": translate.translate_missing должен пропустить перевод —
    # без сети, без мока.
    conn.execute("INSERT INTO articles (url,country,fetched_at,publish_date,title,text,"
                 "language,title_hash,embedding,importance) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?)",
                 ("http://x/1", "india", now, "2026-07-23", "ISRO launch",
                  "full body text", "en", "h1", emb, 88))
    conn.commit()
    n = feeds.build_country(conn, "india")
    conn.close()
    path = os.path.join(settings.OUTPUT_DIR, "india.xml")
    assert n == 1 and os.path.exists(path)
    xml = open(path, encoding="utf-8").read()
    assert "ISRO launch" in xml and "full body text" in xml


def test_build_country_prefers_translated_fields():
    db.init()
    conn = db.connect()
    now = datetime.now(timezone.utc).isoformat()
    emb = np.random.rand(settings.EMBEDDING_DIM).astype(np.float32).tobytes()
    conn.execute("INSERT INTO articles (url,country,fetched_at,publish_date,title,text,"
                 "title_en,text_en,language,title_hash,embedding,importance) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("http://x/2", "china", now, "2026-07-24", "中文标题", "正文内容",
                  "English title", "English text", "zh-cn", "h2", emb, 77))
    conn.commit()
    n = feeds.build_country(conn, "china")
    conn.close()
    assert n == 1
    xml = open(os.path.join(settings.OUTPUT_DIR, "china.xml"), encoding="utf-8").read()
    assert "English title" in xml and "中文标题" not in xml
    assert "English text" in xml and "正文内容" not in xml


def test_build_country_applies_relevance_cutoff():
    db.init()
    conn = db.connect()
    now = datetime.now(timezone.utc).isoformat()
    emb = np.random.rand(settings.EMBEDDING_DIM).astype(np.float32).tobytes()
    conn.execute("INSERT INTO articles (url,country,fetched_at,publish_date,title,text,"
                 "language,title_hash,embedding,importance) VALUES "
                 "(?,?,?,?,?,?,?,?,?,?)",
                 ("http://x/5", "nigeria", now, "2026-07-23", "off-topic filler",
                  "text", "en", "h5", emb, settings.RELEVANCE_CUTOFF))
    conn.commit()
    n = feeds.build_country(conn, "nigeria")
    conn.close()
    assert n == 0, "статья с importance<=RELEVANCE_CUTOFF (rejected) не должна попасть в фид"


def test_translate_missing_skips_ru_and_en_stores_result():
    from unittest.mock import patch

    db.init()
    conn = db.connect()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,importance) "
                 "VALUES (?,?,?,?,?,?,?)",
                 ("http://x/3", "china", now, "你好", "世界", "zh-cn", 55))
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,importance) "
                 "VALUES (?,?,?,?,?,?,?)",
                 ("http://x/4", "india", now, "уже по-русски", "текст", "ru", 55))
    conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,importance) "
                 "VALUES (?,?,?,?,?,?,?)",
                 ("http://x/6", "india", now, "already english", "text", "en", 55))
    conn.commit()

    import translate as translate_mod
    with patch.object(translate_mod, "_translate", side_effect=lambda t, source="auto": f"[en] {t}"):
        rows = [
            ("http://x/3", "你好", "世界", "zh-cn", None, None),
            ("http://x/4", "уже по-русски", "текст", "ru", None, None),
            ("http://x/6", "already english", "text", "en", None, None),
        ]
        result = translate_mod.translate_missing(conn, rows)

    assert result["http://x/3"] == ("[en] 你好", "[en] 世界")
    assert "http://x/4" not in result, "статья уже на русском — перевод не нужен"
    assert "http://x/6" not in result, "статья уже на английском — перевод не нужен"
    zh_row = conn.execute("SELECT title_en, text_en FROM articles WHERE url=?",
                          ("http://x/3",)).fetchone()
    conn.close()
    assert zh_row == ("[en] 你好", "[en] 世界")


def test_translate_src_prefers_script_over_langdetect():
    """Скрипт важнее langdetect: он путает традиционный китайский с ko/ja, а
    Google auto молча не переводит трад. китайский. Хангыль->ko, кана->ja,
    иероглифы без них -> zh-CN (даже если langdetect сказал ko/ja/zh-tw)."""
    import translate as translate_mod
    # трад. китайский, ошибочно помеченный langdetect как ko -> всё равно zh-CN
    assert translate_mod._src("ko", "旺宏金矽獎AI作品參賽踴躍") == "zh-CN"
    assert translate_mod._src("zh-tw", "觀察：中國科研生態") == "zh-CN"
    # японский по кане, корейский по хангылю
    assert translate_mod._src("zh-cn", "空気のいらないタイヤ") == "ja"
    assert translate_mod._src("en", "안녕하세요 세계") == "ko"
    # не-CJK: иврит по карте, остальное как есть, пусто -> auto
    assert translate_mod._src("he", "שלום עולם") == "iw"
    assert translate_mod._src("tr", "Merhaba dünya") == "tr"
    assert translate_mod._src(None, "") == "auto"


def test_translate_chunks_long_text():
    from urllib.parse import quote
    import translate as translate_mod

    text = "First sentence. Second sentence. " * 200
    chunks = translate_mod._chunks(text)
    assert all(len(quote(c)) <= translate_mod._CHUNK for c in chunks)
    assert "".join(chunks) == text, "разбиение не теряет и не меняет текст"


def test_translate_chunks_multibyte_script_without_sentence_breaks():
    """Бенгальский/китайский текст часто не содержит ". " вовсе — старый
    посимвольный (не по байтам квотинга) чанкер собирал такой текст в один
    гигантский GET-запрос, Google молча отвечал ошибкой на весь текст."""
    from urllib.parse import quote
    import translate as translate_mod

    text = "কুর্মিটোলা হাসপাতালে বিনামূল্যে চিকিৎসাসেবা " * 60
    chunks = translate_mod._chunks(text)
    assert len(chunks) > 1
    assert all(len(quote(c)) <= translate_mod._CHUNK for c in chunks)
    assert "".join(chunks) == text


def test_fetch_and_extract_decodes_utf8_without_charset_header():
    """Сервер не шлёт charset -> requests угадывает ISO-8859-1 и калечит
    non-latin текст. fetch_and_extract должен брать r.content (байты), а не
    r.text, чтобы trafilatura/htmldate сами определили кодировку."""
    from unittest.mock import patch, MagicMock

    html = ("<html><head><meta charset='utf-8'><title>中文标题</title></head>"
            "<body><article><p>正文内容正文内容正文内容正文内容</p></article></body></html>")
    fake = MagicMock()
    fake.content = html.encode("utf-8")
    fake.text = fake.content.decode("iso-8859-1")  # так делает requests без charset в заголовке
    fake.raise_for_status = lambda: None

    with patch.object(pipeline.requests, "get", return_value=fake):
        text, title, _pdate = pipeline.fetch_and_extract("http://x/utf8")
    assert "中文标题" in (title or "") or "中文" in (text or "")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
    print("ALL PASS")
