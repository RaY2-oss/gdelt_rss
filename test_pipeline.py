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

import db, feeds, pipeline, seen_store
from datetime import datetime, timedelta, timezone


def test_parse_scores():
    YES, NO = settings.RELEVANT_SCORE, 0
    # Промптом просим true/false, но в ротации десяток провайдеров
    assert pipeline._parse_scores('{"1": true, "2": false}', 2) == {1: YES, 2: NO}
    assert pipeline._parse_scores('```json\n{"1": "yes", "2": "NO"}\n```', 2) == {1: YES, 2: NO}
    # число — прежняя шкала 0..100: модель, продолжающая грейдить, не теряет батч
    assert pipeline._parse_scores('{"1": 80, "2": 3}', 2) == {1: YES, 2: NO}
    assert pipeline._parse_scores('junk {"1": true} tail', 3) == {1: YES}    # id вне диапазона отсекается
    assert pipeline._parse_scores('{"1": true, "9": true}', 3) == {1: YES}
    # неразобранное значение не выдаётся за отказ — статью никто не судил
    assert pipeline._parse_scores('{"1": "maybe", "2": false}', 2) == {2: NO}
    # «Extra data»: два склеенных объекта — раньше это роняло весь батч
    assert pipeline._parse_scores('{"1": true}{"2": true}', 2) == {1: YES}
    assert pipeline._parse_scores('Here you go:\n{"1": true}\nDone.', 2) == {1: YES}


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
        return json.dumps({"1": True})

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
    assert row("http://prefland/keep") == (settings.RELEVANT_SCORE, "llm")

    v = conn.execute("SELECT verdict, embedding FROM seen_urls WHERE url=?",
                     ("http://prefland/drop",)).fetchone()
    conn.close()
    assert v == ("rejected", v_drop.tobytes())


def test_score_reuses_verdict_from_previous_run():
    """Сюжет, осуждённый LLM час назад, не покупается у неё второй раз.

    Внутрипрогонный дедуп такое не ловит: статьи приезжают в РАЗНЫХ прогонах,
    и до этой стадии каждый час уходил повторный вызов на тот же сюжет
    (-9.9 % обращений на замере за 7 дней). Копия помечается scored_by='dup' —
    независимой меткой она не является и в обучение идти не должна.
    """
    from unittest.mock import patch
    import json

    db.init()
    conn = db.connect()
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=1)).isoformat()
    v_seen = np.zeros(settings.EMBEDDING_DIM, np.float32); v_seen[0] = 1
    v_new = np.zeros(settings.EMBEDDING_DIM, np.float32); v_new[1] = 1
    ins = ("INSERT INTO articles (url,country,fetched_at,title,text,language,"
           "title_hash,embedding,importance,scored_by) VALUES (?,?,?,?,?,?,?,?,?,?)")
    conn.execute(ins, ("http://dupland/old", "dupland", old, "Fab Story", "text",
                       "en", "dl1", v_seen.tobytes(), settings.RELEVANT_SCORE, "llm"))
    # тот же сюжет, другое издание, следующий прогон — косинус 1.0
    conn.execute(ins, ("http://dupland/again", "dupland", now.isoformat(),
                       "Fab Story reprint", "text", "en", "dl2",
                       v_seen.tobytes(), None, None))
    # чужой сюжет: ортогонален, копировать не с чего
    conn.execute(ins, ("http://dupland/other", "dupland", now.isoformat(),
                       "Other Story", "text", "en", "dl3",
                       v_new.tobytes(), None, None))
    conn.commit()

    calls = []

    def fake_openrouter(system, user, ref_url=None):
        calls.append(user)
        return json.dumps({"1": True})

    with patch.object(pipeline.prefilter, "is_ready", return_value=False), \
         patch.object(pipeline, "_call_openrouter_raw", side_effect=fake_openrouter):
        pipeline.score()

    assert len(calls) == 1, "повторный сюжет не должен уезжать в LLM"
    assert "Other Story" in calls[0] and "Fab Story" not in calls[0]

    row = lambda u: conn.execute(
        "SELECT importance, scored_by FROM articles WHERE url=?", (u,)).fetchone()
    assert row("http://dupland/again") == (settings.RELEVANT_SCORE, "dup")
    assert row("http://dupland/other") == (settings.RELEVANT_SCORE, "llm")

    # Копия не должна становиться обучающей меткой: train_prefilter берёт
    # только scored_by IS NULL или 'llm'.
    n = conn.execute("SELECT COUNT(*) FROM articles WHERE scored_by='dup' "
                     "AND (scored_by IS NULL OR scored_by='llm')").fetchone()[0]
    conn.close()
    assert n == 0


def test_score_gate_accepts_without_llm():
    """Верхний порог: уверенный приём не покупается у LLM. Статья получает
    PREFILTER_ACCEPT_SCORE — ту же отметку «принята», что и у решения LLM;
    порядок задаёт структурная важность (importance.structural), а не это
    число."""
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
    assert got["http://acceptland/keep"] > settings.RELEVANCE_CUTOFF
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
                      side_effect=lambda *a, **k: json.dumps({"1": True})):
        pipeline.score()

    got = dict(conn.execute("SELECT url, importance FROM articles WHERE country='partial'"))
    conn.close()
    scored = [v for v in got.values() if v is not None]
    assert len(scored) == 1 and scored[0] == settings.RELEVANT_SCORE, got
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
        # строка айтема: "[id=N] (Страна) Заголовок"
        for m in re_mod.finditer(r"\[id=(\d+)\] \([^)]*\) (.*)", user):
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


def test_score_batches_across_countries_in_one_request():
    """Сквозная очередь: сюжеты разных стран едут ОДНИМ запросом, пока их меньше
    LLM_BATCH, и каждый несёт свою страну в строке айтема. Пока батч резался по
    стране, три страны по одной статье стоили три запроса вместо одного — это и
    выжигало суточную квоту (см. logs 2026-07-28: 261 запрос из 647 нёс 1 сюжет)."""
    from unittest.mock import patch
    import json
    import re as re_mod

    db.init()
    conn = db.connect()
    for i, land in enumerate(("alphaland", "betaland", "gammaland")):
        v = np.zeros(settings.EMBEDDING_DIM, np.float32); v[i] = 1
        conn.execute("INSERT INTO articles (url,country,fetched_at,title,text,language,"
                     "title_hash,embedding) VALUES (?,?,?,?,?,?,?,?)",
                     (f"http://{land}/a", land, "2026-07-20T00:00:00+00:00",
                      f"Story {land}", "text", "en", f"{land}h", v.tobytes()))
    conn.commit()

    calls = []

    def fake_openrouter(system, user, ref_url=None):
        calls.append(user)
        ids = re_mod.findall(r"\[id=(\d+)\] ", user)
        return json.dumps({i: True for i in ids})

    with patch.object(pipeline.prefilter, "is_ready", return_value=False), \
         patch.object(pipeline, "_call_openrouter_raw", side_effect=fake_openrouter):
        pipeline.score()

    assert len(calls) == 1, f"три страны должны уехать одним запросом, а не {len(calls)}"
    for land in ("Alphaland", "Betaland", "Gammaland"):
        assert f"({land})" in calls[0], f"страна {land} обязана быть в строке айтема"
    # прежний запрос называл страну только в system-промпте — проверяем, что
    # привязка id -> страна доехала и вердикт лёг на все три
    got = dict(conn.execute(
        "SELECT url, importance FROM articles WHERE url LIKE 'http://%land/a'"))
    conn.close()
    assert set(got.values()) == {settings.RELEVANT_SCORE}, got


_NO_BODY = object()


def _feed_row(url, title, vec, fetched, entities_cell="", t_ru=None, x_ru=None,
              body=None):
    """Строка в форме feeds._SELECT (14 колонок).

    body по умолчанию равно vec: фид кластеризует ТОЛЬКО по телам
    (feeds.cluster_rows), и строка с пустым r[10] стала бы отдельным сюжетом.
    _NO_BODY — явная «тело ещё не досчитано»."""
    b = vec if body is None else body
    return (url, title, "t", None, fetched, settings.RELEVANT_SCORE, vec.tobytes(),
            "en", None, None, None if b is _NO_BODY else b.tobytes(),
            entities_cell, t_ru, x_ru)


def test_cluster_dedups_and_ranks_by_coverage():
    """Дедуп + порядок по СТРУКТУРНОЙ важности.

    Оценка LLM теперь двоичная и порядка не задаёт вовсе, поэтому проверяем
    фактор охвата: сюжет, о котором написали три разных издания, обязан стоять
    выше одиночной заметки. Векторы взаимно ортогональны — LexRank у всех
    сюжетов одинаков, и решает именно охват.
    """
    v_dup = np.zeros(settings.EMBEDDING_DIM, np.float32); v_dup[2] = 1
    v_b = np.zeros(settings.EMBEDDING_DIM, np.float32); v_b[0] = 1
    v_c = np.zeros(settings.EMBEDDING_DIM, np.float32); v_c[1] = 1
    rows = [
        _feed_row("http://a.com/1", "dup old",  v_dup, "2026-07-20T10:00:00"),
        _feed_row("http://a.com/2", "dup new",  v_dup, "2026-07-20T12:00:00"),
        _feed_row("http://b1.com/x", "wide",    v_b, "2026-07-20T11:00:00"),
        _feed_row("http://b2.com/x", "wide",    v_b, "2026-07-20T11:00:00"),
        _feed_row("http://b3.com/x", "wide",    v_b, "2026-07-20T11:00:00"),
        _feed_row("http://c.com/x", "narrow",   v_c, "2026-07-20T11:00:00"),
    ]
    top = feeds._top_items(rows)
    urls = [r[0] for r in top]
    assert len(top) == 3, f"три сюжета после дедупа, без лимита: {urls}"
    assert "http://a.com/2" in urls and "http://a.com/1" not in urls, \
        "внутри кластера остаётся самая свежая статья"
    assert urls.index("http://b1.com/x") < urls.index("http://c.com/x"), \
        "сюжет с тремя изданиями должен стоять выше одиночного"


def _vec_at_cosine(cos, dim=settings.EMBEDDING_DIM, seed=0):
    """Единичный вектор с косинусом ровно `cos` к [1,0,0,...]."""
    rng = np.random.default_rng(seed)
    v = np.zeros(dim, np.float32); v[0] = 1.0
    orth = rng.standard_normal(dim).astype(np.float32); orth[0] = 0
    orth /= np.linalg.norm(orth)
    return (cos * v + np.sqrt(1 - cos**2) * orth).astype(np.float32)


def test_feed_clusters_by_bodies_not_titles():
    """Фид дедуплицирует ТОЛЬКО по телам, на своём пороге DEDUP_BODY_COSINE.

    Прод-случай: «India's Rs 4.78 Lakh Crore Energy Storage Plan» и «47 GW
    battery storage, 23 GW pumped storage projects in pipeline» — один
    документ, косинус тел 0.958, заголовков 0.859. Обратный прод-случай: сделка
    Bombardier и ядерная сделка США — Саудовская Аравия делят слова заголовка,
    но косинус тел у них 0.818 — разные сюжеты.

    Заголовочный канал убран не «за компанию»: на недельном окне он добавлял к
    34033 слияниям по телам ровно 33 своих. Проверяем, что заголовки больше не
    решают ничего — ни слить, ни развести."""
    same_body = _vec_at_cosine(1.0, seed=9)
    far_titles = [_vec_at_cosine(1.0, seed=1), _vec_at_cosine(0.70, seed=1)]
    rows = [_feed_row("http://a/1", "Energy Storage Plan", far_titles[0], "2026-07-20T10:00:00",
                      body=same_body),
            _feed_row("http://b/1", "47 GW battery storage", far_titles[1], "2026-07-20T11:00:00",
                      body=same_body)]
    assert len(feeds.cluster_rows(rows)) == 1, "совпавшие тела обязаны слить сюжет"

    # Заголовки-близнецы (косинус 1.0) при далёких телах — два разных сюжета.
    one_title = _vec_at_cosine(1.0, seed=1)
    rows = [_feed_row("http://a/2", "Saudi deal", one_title, "2026-07-20T10:00:00",
                      body=_vec_at_cosine(1.0, seed=9)),
            _feed_row("http://b/2", "Saudi deal", one_title, "2026-07-20T11:00:00",
                      body=_vec_at_cosine(0.82, seed=9))]
    assert len(feeds.cluster_rows(rows)) == 2, "тела 0.82 — разные сюжеты, заголовки не в счёт"


def test_feed_body_threshold_boundaries():
    """Границы DEDUP_BODY_COSINE: 0.92 сливает, 0.88 — нет.

    Порог ниже заголовочного намеренно: тела разделяют сюжеты лучше (внутри
    сюжета p10 = 0.870 против p90 = 0.851 между сюжетами). Прежний общий 0.95
    глушил канал целиком — p99 косинусов тел всего 0.874."""
    assert settings.DEDUP_BODY_COSINE < settings.DEDUP_COSINE
    root = _vec_at_cosine(1.0, seed=9)

    def n_clusters(cos):
        rows = [_feed_row("http://a/x", "t", root, "2026-07-20T10:00:00", body=root),
                _feed_row("http://b/x", "t", root, "2026-07-20T11:00:00",
                          body=_vec_at_cosine(cos, seed=9))]
        return len(feeds.cluster_rows(rows))

    assert n_clusters(0.92) == 1, "косинус тел 0.92 — один сюжет, обязан слиться"
    assert n_clusters(0.88) == 2, "косинус тел 0.88 ниже порога — сливать нельзя"


def test_feed_rows_without_body_stay_separate():
    """Тело считается стадией позже (embed_bodies) и только у принятых, так что
    в окно попадают строки с пустым r[10]. Их нулевой вектор даёт косинус 0 ко
    всем — каждая обязана остаться отдельным сюжетом. Иначе все недосчитанные
    схлопнулись бы в один."""
    v = _vec_at_cosine(1.0, seed=1)
    rows = [_feed_row("http://a/n", "t", v, "2026-07-20T10:00:00", body=_NO_BODY),
            _feed_row("http://b/n", "t", v, "2026-07-20T11:00:00", body=_NO_BODY),
            _feed_row("http://c/n", "t", v, "2026-07-20T12:00:00", body=v)]
    assert len(feeds.cluster_rows(rows)) == 3, "нулевые вектора не должны сливать статьи"


def test_scoring_still_clusters_by_titles():
    """До гейта тел ещё нет — там дедуп по заголовкам, и это осознанно.

    Считать эмбеддинг тела для четырёх отказов из пяти на машине без AVX
    дороже, чем изредка спросить LLM про сюжет дважды. Недомерженное здесь
    схлопнется в фиде, где тела уже посчитаны."""
    v = _vec_at_cosine(1.0, seed=1)
    rows = [("http://a/s", "Same story", "text", v.tobytes()),
            ("http://b/s", "Same story", "text", _vec_at_cosine(0.97, seed=1).tobytes()),
            ("http://c/s", "Other story", "text", _vec_at_cosine(0.40, seed=2).tobytes())]
    items, groups = pipeline._group_for_scoring(rows)
    assert len(items) == 2, f"дубль заголовков — один вопрос к LLM: {items}"
    assert sorted(len(g) for g in groups.values()) == [1, 2]


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


def test_browser_rescue_only_on_last_attempt():
    """Браузер дорог, поэтому достаётся только тем, кого иначе спишут навсегда.
    И вердикт у спасённого ровно один: «short» ставится ПОСЛЕ прохода спасения,
    иначе одна статья получала бы в журнале две отметки и лишнюю попытку."""
    import unittest.mock as mock

    db.init()
    conn = db.connect()
    seen_store.ensure(conn)
    # у 'tired' попытка уже была — эта последняя; 'fresh' встречен впервые
    seen_store.mark(conn, [("http://r/tired", 0, "short", None)], "2026-08-06")

    todo = {"http://r/tired": ("resqland", "2026-08-07", ""),
            "http://r/fresh": ("resqland", "2026-08-07", ""),
            "http://r/ok": ("resqland", "2026-08-07", "")}
    def page(title):        # заголовки разные: одинаковые схлопнет дедуп
        return ("текст статьи " * 40, title, "2026-08-07T10:00:00+00:00")
    asked = []

    def fake_rescue(urls):
        asked.extend(urls)
        return {u: page("Спасённая " + u) for u in urls}

    with mock.patch.object(pipeline, "collect_urls", return_value=todo), \
         mock.patch.object(pipeline, "fetch_and_extract",
                           side_effect=lambda u: page(u) if u.endswith("/ok")
                           else (None, None, None)), \
         mock.patch.object(pipeline, "_rescue", side_effect=fake_rescue), \
         mock.patch.object(pipeline, "_embed",
                           side_effect=lambda t: np.ones((len(t), settings.EMBEDDING_DIM),
                                                         np.float32)):
        pipeline.collect()

    verdicts = dict(conn.execute(
        "SELECT url, verdict FROM seen_urls WHERE url LIKE 'http://r/%'"))
    tries = seen_store.attempts(conn, todo)
    conn.execute("DELETE FROM articles WHERE country='resqland'")   # база общая
    conn.commit()
    conn.close()

    assert asked == ["http://r/tired"], asked      # 'fresh' получит ещё попытку сам
    assert verdicts["http://r/tired"] == "accepted", verdicts
    assert verdicts["http://r/fresh"] == "short", verdicts
    assert tries["http://r/fresh"] == 1, tries     # одна неудача — одна отметка


def test_extract_drops_antibot_shield():
    """Страница щита — не короткая: 300–500 символов человеческого текста, то
    есть ровно выше MIN_TEXT_LENGTH, и без сторожа она уходит в статьи. Особенно
    через _rescue: браузеру щит отвечает охотнее, чем скрипту (замер 08.08.2026
    на сотне трудных адресов — 13 «спасённых» из 45 оказались вот этим)."""
    shield = ('<html><head><title>Just a moment...</title></head><body><p>'
              'Enable JavaScript and cookies to continue. This website is using '
              'a security service to protect itself from online attacks and the '
              'action you just performed triggered the security solution.'
              '</p></body></html>').encode("utf-8")
    assert pipeline._extract(shield, "http://x/a") == (None, None)

    # Сторож смотрит на начало текста, а не на всю страницу: статья, где слово
    # «cloudflare» встретилось по делу, остаться должна.
    art = ('<html><head><title>Ускоритель в Аммане</title></head><body><article><p>'
           + 'Центр SESAME запустил новую линию синхротронного излучения. ' * 10
           + '</p></article></body></html>').encode("utf-8")
    text, title = pipeline._extract(art, "http://x/b")
    assert title == "Ускоритель в Аммане" and len(text) > 300, (title, text)


def test_stamp_reads_time_from_markup():
    """Дата без времени — вся суточная лента с меткой 00:00, и сортировать её
    нечем. Время у страницы почти всегда есть в её же разметке; проверяем, что
    берём именно его, и что чужой часовой пояс или «обновлено» не заводят дату
    в будущее."""
    seen = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    def st(html, hint=None):
        return pipeline._stamp(html.encode("utf-8"), "http://x/", hint, seen)

    # порядок доверия: <meta> раньше JSON-LD
    assert st('<meta property="article:published_time" content="2026-08-07T09:30:00Z">'
              '<script>{"datePublished":"2026-08-01T00:00:00Z"}</script>'
              ) == "2026-08-07T09:30:00+00:00"
    # content= перед именем — тот же тег, другой порядок атрибутов
    assert st('<meta content="2026-08-06T08:15:00+03:00" name="pubdate">'
              ).startswith("2026-08-06T08:15:00")
    # разметки нет — остаётся календарная дата от htmldate
    assert st("<html><body>ни даты, ни времени</body></html>",
              "2026-08-05") == "2026-08-05T00:00:00+00:00"
    # опубликовано позже, чем прочитано, не бывает: отбрасываем и падаем на hint
    assert st('<meta property="datePublished" content="2026-09-01T00:00:00Z">',
              "2026-08-05") == "2026-08-05T00:00:00+00:00"
    # год копирайта в разметке — не дата статьи
    assert st('<meta property="datePublished" content="1998-01-01T00:00:00Z">') is None
    assert st("<html></html>") is None          # взять неоткуда — решает вызывающий


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
