# -*- coding: utf-8 -*-
"""feeds.py — сборка часового «все статьи» RSS-фида на страну для FreshRSS.

На страну: статьи за WINDOW_HOURS с importance>RELEVANCE_CUTOFF (то есть
вердикт LLM был accepted — см. pipeline.score), e5-косинусная кластеризация
дублей (один сюжет, разные издания/языки — берём самого свежего
представителя), сортировка по СТРУКТУРНОЙ важности (importance.structural:
LexRank по эмбеддингам тел + охват по доменам + вес субъектов), без верхнего
лимита (это фид «все статьи» — отбор с MMR/TOP_N делает digest.py для
дневного «важные статьи»-фида). Запись {country}.xml в OUTPUT_DIR (его отдаёт
nginx rss_proxy для FreshRSS).

По оценке LLM здесь НЕ ранжируют: она двоичная, «по теме или нет», и порядка
не задаёт. Граф считается заново на каждый прогон — на всех 89 странах это
1.6 с (замер 27.07: Китай, самый крупный, 1055 статей и 356 сюжетов — 0.47 с),
поэтому инкрементального обновления матрицы нет и не нужно.

Модель e5 здесь НЕ грузится: эмбеддинги уже лежат в БД blob'ом. Каждая страна
обрабатывается по очереди — пик RAM держится на размере одной страны.
"""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import numpy as np
from dateutil import parser as dtparser
from feedgen.feed import FeedGenerator
from lxml import etree

import settings
import db
import entities
import importance as imp
import translate

log = logging.getLogger("gdelt_rss")

# Общая форма строки для фида, витрины и дайджеста — индексы совпадают во
# всех трёх, иначе r[10]/r[11] означали бы в каждом своё.
_SELECT = ("SELECT url,title,text,publish_date,fetched_at,importance,embedding,"
           "language,title_en,text_en,embedding_body,entities FROM articles ")

_TOKEN_RE = re.compile(r"[^\w]+")
_TOKEN_MIN_LEN = 4
_MIN_SHARED_TOKENS = 2


def _distinctive_tokens(titles, exclude=()):
    """Токены заголовков за вычетом общей для батча лексики темы/страны
    (India, AI, energy...) — остаётся в основном специфика конкретного
    события (Bhargavastra, HCLTech, Odisha), общая для разных изданий об
    одном сюжете, но не между разными сюжетами.

    exclude: токены страны (country_display) — они частые в ЛЮБОМ батче
    этой страны по построению, так что не несут различительной силы и
    исключаются вне зависимости от DF (иначе ложно бриджуют случайные пары
    вроде "India"+"sparked" в двух вообще не связанных заголовках).

    cutoff растёт с размером батча (не наивный fixed-6 потолок): вирусный
    сюжет может доминировать сотнями пересказов на разных языках — тогда
    его же специфичная лексика (Pradhan, resigns...) становится "частой"
    относительно всего батча и раньше отсеивалась как общая, из-за чего
    дубликаты не мержились."""
    tokensets = [{t for t in _TOKEN_RE.split((title or "").lower()) if len(t) >= _TOKEN_MIN_LEN}
                 for title in titles]
    doc_freq = {}
    for toks in tokensets:
        for t in toks:
            doc_freq[t] = doc_freq.get(t, 0) + 1
    cutoff = max(2, round(len(titles) * 0.35))
    excl = {e.lower() for e in exclude}
    return [{t for t in toks if doc_freq[t] <= cutoff and t not in excl} for toks in tokensets]


def _cluster(embs, threshold, titles=None, exclude=(), bodies=None):
    """Жадная кластеризация по косинусу, построчно (без матрицы n*n).

    titles (опц.): пары в [DEDUP_LEXICAL_FLOOR, threshold) — семантически
    близкие, но не обязательно один сюжет (см. DEDUP_LEXICAL_FLOOR) — мержим
    только если делят >= _MIN_SHARED_TOKENS "редких" для батча токена
    заголовка. Косинус >= threshold мержится безусловно.

    bodies (опц.): эмбеддинги ТЕЛ, None у тех, где тело ещё не досчитано.
    Считаются ОТДЕЛЬНОЙ матрицей и объединяются по ИЛИ: тела ловят дубли,
    которые заголовки роняют («India's Rs 4.78 Lakh Crore Energy Storage Plan»
    и «47 GW battery storage, 23 GW pumped storage projects in pipeline» — один
    документ, косинус тел 0.958, косинус заголовков 0.859). Смешивать вектора
    заголовка и тела в ОДНОЙ матрице нельзя: это разные пространства, косинус
    между ними ~0.8 даже у точного дубля, и дедуп разваливается — тела
    досчитываются отдельной стадией, в суточном окне их пока 27 %. Строка без
    тела получает нулевой вектор: её косинус ко всем 0, порога не достигает."""
    n = len(embs)
    if n == 0:
        return np.zeros(0, int)

    def unit(vecs):
        E = np.asarray(vecs, np.float32)
        return E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-10)

    E = unit(embs)
    B = unit([b if b is not None else np.zeros(E.shape[1], np.float32)
              for b in bodies]) if bodies is not None else None
    lex = _distinctive_tokens(titles, exclude) if titles else None
    labels = np.full(n, -1, int)
    nxt = 0
    for i in range(n):
        if labels[i] >= 0:
            continue
        sims = E @ E[i]
        match = sims >= threshold
        if B is not None:
            match = match | (B @ B[i] >= threshold)
        if lex is not None and lex[i]:
            floor_ok = sims >= settings.DEDUP_LEXICAL_FLOOR
            shared = np.array([len(lex[i] & lex[j]) >= _MIN_SHARED_TOKENS for j in range(n)])
            match = match | (floor_ok & shared)
        labels[match & (labels < 0)] = nxt
        nxt += 1
    return labels


def _max_cosine(emb, others):
    if not others:
        return -1.0
    e = emb.astype(np.float32); e = e / (np.linalg.norm(e) + 1e-10)
    E = np.vstack(others).astype(np.float32)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-10)
    return float((E @ e).max())


def _mmr_select(candidates, n_pick, lam=None):
    """candidates: list of dict(..., imp, emb). Жадный MMR, O(n_pick*n) —
    кандидатов единицы-сотни, лишняя векторизация тут не нужна.

    Подстраховка сверху над _cluster: кросс-языковые дубли одного вирусного
    сюжета (разные алфавиты — не делят лексических токенов, см.
    _distinctive_tokens) не всегда мержатся в один кластер уже на этапе
    кластеризации — MMR не даёт им занять большую часть TOP_N, штрафуя
    кандидата за схожесть с уже отобранным."""
    lam = settings.DIGEST_MMR_LAMBDA if lam is None else lam
    if not candidates:
        return []
    n_pick = min(n_pick, len(candidates))
    imps = np.array([c["imp"] for c in candidates], dtype=np.float64)
    lo, hi = imps.min(), imps.max()
    rel = (imps - lo) / (hi - lo) if hi - lo > 1e-9 else np.full_like(imps, 0.5)

    selected, selected_embs = [], []
    remaining = list(range(len(candidates)))
    for _ in range(n_pick):
        best_i, best_score = None, -1e18
        for i in remaining:
            sim = _max_cosine(candidates[i]["emb"], selected_embs)
            score = lam * rel[i] - (1 - lam) * max(sim, 0.0)
            if score > best_score:
                best_score, best_i = score, i
        selected.append(candidates[best_i])
        selected_embs.append(candidates[best_i]["emb"])
        remaining.remove(best_i)
    return selected


def _parse_dt(v):
    try:
        d = dtparser.parse(v)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _pubdate(publish_date, fetched_at):
    """GKG отдаёт publish_date без времени ('2026-07-27'), dateutil разбирает
    его в полночь UTC — и вся суточная лента получает одну метку 00:00.

    Если статья встречена в тот же календарный день, время первой встречи
    ближе к правде: сбор идёт каждые 15 минут. День при этом не меняется, так
    что digest._row_date («опубликовано сегодня») работает как работал.
    """
    pd, fa = _parse_dt(publish_date), _parse_dt(fetched_at)
    if pd and fa and ":" not in str(publish_date) and fa.date() == pd.date():
        return fa
    return pd or fa or datetime.now(timezone.utc)


def cluster_rows(rows, country):
    """rows (12 колонок, см. _SELECT) -> {label: [строки сюжета]}.

    Общая для фида и витрины часть: одна и та же кластеризация должна давать
    одно и то же разбиение, иначе они показывают разные представители сюжета.
    """
    embs = [np.frombuffer(r[6], np.float32) for r in rows]
    # Тела — вторым сигналом, порог тот же: распределения косинусов почти
    # совпадают (медиана 0.806 против 0.800, p99 0.874 против 0.879), а дубли
    # тела ловят те, что заголовки роняют (см. _cluster).
    bodies = [np.frombuffer(r[10], np.float32) if r[10] else None for r in rows]
    # title_en, если уже закэширован с прошлого прогона (см. translate.py) —
    # мостит кросс-языковые дубли, которых сырой заголовок мостить не может
    # (разные алфавиты не делят токенов); сходится за пару часовых прогонов.
    titles = [r[8] or r[1] for r in rows]
    exclude = _TOKEN_RE.split(settings.country_display(country).lower())
    labels = _cluster(embs, settings.DEDUP_COSINE, titles=titles, exclude=exclude,
                      bodies=bodies)
    groups = {}
    for row, lab in zip(rows, labels):
        groups.setdefault(int(lab), []).append(row)
    return groups


def rank(groups):
    """{label: [строки]} -> [(label, важность)] по убыванию.

    Важность структурная (importance.structural): LexRank по эмбеддингам тел +
    охват по доменам + вес субъектов. Оценкой LLM здесь не ранжируют — она
    двоичная, «по теме или нет», и порядка не задаёт вовсе.
    """
    labels = list(groups)
    if not labels:
        return []
    reps = [max(groups[lab], key=lambda r: r[4] or "") for lab in labels]
    ent_df = entities.document_freq(r[11] for lab in labels for r in groups[lab])
    scores = imp.structural(
        [imp.body_emb(r[10], r[6]) for r in reps],
        [[r[0] for r in groups[lab]] for lab in labels],
        [[r[11] or "" for r in groups[lab]] for lab in labels],
        ent_df)
    return sorted(zip(labels, scores), key=lambda kv: kv[1], reverse=True)


def _top_items(rows, country):
    """-> представители сюжетов по убыванию структурной важности (без
    TOP_N/MMR-лимита — это фид «все статьи»)."""
    if not rows:
        return []
    groups = cluster_rows(rows, country)
    # Представитель — самый важный по оценке… которой больше нет. Берём самый
    # свежий: внутри кластера это один и тот же сюжет (косинус >= DEDUP_COSINE).
    return [max(groups[lab], key=lambda r: r[4] or "") for lab, _s in rank(groups)]


_FEED_XSL = os.path.join(settings.BASE_DIR, "site_static", "feed.xsl")


def write_feed(fg, path):
    """XML для читалок плюс HTML-двойник для людей.

    По .xml браузер показывает голое дерево тегов, а <?xml-stylesheet?> дело
    больше не спасает: Chrome 151 выкинул XSLT совсем и вместо страницы рисует
    жёлтую плашку «browser does not support». Поэтому тот же feed.xsl
    применяется здесь, на сборке, а nginx подставляет готовый HTML тем, кто
    пришёл из адресной строки. Адрес не меняется — его можно скопировать и
    подписаться, — а читалки получают ровно тот же XML, что и раньше.
    """
    xml = fg.rss_str(pretty=True)
    with open(path, "wb") as f:
        f.write(xml)

    # Путь двойника считается ОТ пути XML, а не от своей настройки: тесты
    # подменяют settings.OUTPUT_DIR на временный каталог, и вторая настройка
    # мимо этой подмены писала бы прямо в боевой каталог (уже написала —
    # india_digest.html из test_digest подменил живую страницу).
    twin_dir = os.path.join(os.path.dirname(path), "html")
    os.makedirs(twin_dir, exist_ok=True)
    twin = os.path.join(twin_dir, os.path.basename(path)[:-len(".xml")] + ".html")
    xslt = etree.XSLT(etree.parse(_FEED_XSL))
    with open(twin, "wb") as f:
        f.write(bytes(xslt(etree.fromstring(xml))))


def build_country(conn, country):
    since = (datetime.now(timezone.utc) - timedelta(hours=settings.WINDOW_HOURS)).isoformat()
    # Порог один — граница приёма: вердикт accepted/rejected из pipeline.score.
    # Отдельного порога важности нет, важность считается графом (см. rank).
    rows = conn.execute(
        _SELECT + "WHERE country=? AND importance>? AND fetched_at>=?",
        (country, settings.RELEVANCE_CUTOFF, since)).fetchall()
    items = _top_items(rows, country)
    # переводим всё, что попало в фид (после дедупа, без лимита) — не весь
    # оценённый бэклог, см. translate.py.
    translated = translate.translate_missing(
        conn, [(r[0], r[1], r[2], r[7], r[8], r[9]) for r in items])
    disp = settings.country_display(country)

    fg = FeedGenerator()
    fg.title(f"GDELT Sci/Tech — {disp}")
    fg.link(href=f"https://data.gdeltproject.org/", rel="alternate")
    fg.description(f"Наука и технологии: {disp}. Все статьи за сутки по важности (GDELT GKG).")
    fg.language("mul")
    for row in items:
        url, title, text, pdate, fa = row[0], row[1], row[2], row[3], row[4]
        t_en, x_en = translated.get(url, (None, None))
        fe = fg.add_entry()
        fe.id(url)
        fe.link(href=url)
        fe.title((t_en or title or url)[:300])
        fe.content(x_en or text or "", type="CDATA")
        fe.pubDate(_pubdate(pdate, fa))
    write_feed(fg, os.path.join(settings.OUTPUT_DIR, f"{country}.xml"))
    return len(items)


def build_all():
    os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
    conn = db.connect()
    try:
        total = 0
        for country in settings.COUNTRIES:
            n = build_country(conn, country)
            total += n
            if n:
                log.info("  фид %s.xml: %d новостей", country, n)
        log.info("Фиды собраны: %d стран, %d новостей всего",
                 len(settings.COUNTRIES), total)
        return total
    finally:
        conn.close()
