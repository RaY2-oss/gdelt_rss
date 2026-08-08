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
from datetime import datetime, timedelta, timezone

import numpy as np
from dateutil import parser as dtparser
from feedgen.feed import FeedGenerator

import settings
import db
import entities
import importance as imp
import translate

log = logging.getLogger("gdelt_rss")

# Общая форма строки для фида, витрины и дайджеста — индексы совпадают во
# всех трёх, иначе r[10]/r[11] означали бы в каждом своё. Русский перевод
# (r[12]/r[13]) дописывает фоновый translate_worker; здесь он только читается,
# и на показ идёт первым — ru > en > оригинал.
_SELECT = ("SELECT url,title,text,publish_date,fetched_at,importance,embedding,"
           "language,title_en,text_en,embedding_body,entities,title_ru,text_ru "
           "FROM articles ")

def _cluster(embs, threshold, dim=settings.EMBEDDING_DIM):
    """Жадная кластеризация по косинусу, построчно (без матрицы n*n).

    embs: вектора ОДНОГО пространства — заголовочные либо телесные, но не
    вперемешку: косинус между заголовком и телом одной и той же статьи ~0.8,
    то есть на уровне шума между разными сюжетами, и дедуп разваливается.
    None вместо вектора (тело ещё не досчитано) даёт нулевой вектор: косинус
    ко всем 0, порога не достигает, строка остаётся отдельным сюжетом."""
    n = len(embs)
    if n == 0:
        return np.zeros(0, int)
    E = np.asarray([e if e is not None else np.zeros(dim, np.float32)
                    for e in embs], np.float32)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-10)
    labels = np.full(n, -1, int)
    nxt = 0
    for i in range(n):
        if labels[i] >= 0:
            continue
        match = (E @ E[i] >= threshold) & (labels < 0)
        match[i] = True   # нулевой вектор не совпадает даже сам с собой:
                          # без этого все строки без тела остались бы с меткой
                          # -1, то есть слиплись бы в один сюжет
        labels[match] = nxt
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
    сюжета не всегда мержатся в один кластер уже на этапе кластеризации — MMR
    не даёт им занять большую часть TOP_N, штрафуя кандидата за схожесть с уже
    отобранным."""
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


def cluster_rows(rows):
    """rows (14 колонок, см. _SELECT) -> {label: [строки сюжета]}.

    Общая для фида и витрины часть: одна и та же кластеризация должна давать
    одно и то же разбиение, иначе они показывают разные представители сюжета.

    Дедуп ТОЛЬКО по телам. Заголовочный канал (0.95) и лексический мостик по
    общим токенам заголовка отсюда убраны: на недельном окне (6452 статьи,
    у всех есть тело) тела дают 34033 слияния, заголовки добавляют к ним 33 —
    0.09 %, и все 33 сидят на косинусе тел 0.89-0.91, то есть у самого порога.
    Мостик же был откровенно вреден: он склеивал разные сюжеты по паре общих
    слов, а склейка прячет статью из фида целиком.
    """
    bodies = [np.frombuffer(r[10], np.float32) if r[10] else None for r in rows]
    labels = _cluster(bodies, settings.DEDUP_BODY_COSINE)
    groups = {}
    for row, lab in zip(rows, labels):
        groups.setdefault(int(lab), []).append(row)
    return groups


def rank(groups):
    """{label: [строки]} -> [(label, важность, возраст в сутках)] по убыванию.

    Важность структурная (importance.structural): LexRank по эмбеддингам тел +
    охват по доменам + вес субъектов. Оценкой LLM здесь не ранжируют — она
    двоичная, «по теме или нет», и порядка не задаёт вовсе.

    Важность в кортеже — БЕЗ затухания. Порядок задаёт произведение важности на
    свежесть, а сама важность едет наружу как есть: витрина меряет столбиком
    сюжет, а не его срок годности, и старая крупная новость должна опускаться
    в ленте, не теряя при этом своей длины (см. importance.fade).

    Возраст сюжета — возраст его САМОЙ СВЕЖЕЙ статьи: сюжет, о котором пишут
    сегодня, не старый, сколько бы дней назад он ни начался.
    """
    labels = list(groups)
    if not labels:
        return []
    reps = [max(groups[lab], key=lambda r: r[4] or "") for lab in labels]
    ent_df = entities.document_freq(r[11] for lab in labels for r in groups[lab])
    now = datetime.now(timezone.utc)
    ages = [min((now - _pubdate(r[3], r[4])).total_seconds()
                for r in groups[lab]) / 86400.0 for lab in labels]
    scores = imp.structural(
        [imp.body_emb(r[10], r[6]) for r in reps],
        [[r[0] for r in groups[lab]] for lab in labels],
        [[r[11] or "" for r in groups[lab]] for lab in labels],
        ent_df)
    return sorted(zip(labels, scores, ages),
                  key=lambda t: t[1] * imp.freshness(
                      t[2], settings.IMPORTANCE_HALF_LIFE_DAYS,
                      settings.IMPORTANCE_AGE_FLOOR),
                  reverse=True)


def _top_items(rows):
    """-> представители сюжетов по убыванию структурной важности (без
    TOP_N/MMR-лимита — это фид «все статьи»)."""
    if not rows:
        return []
    groups = cluster_rows(rows)
    # Представитель — самый важный по оценке… которой больше нет. Берём самый
    # свежий: внутри кластера это один и тот же сюжет (косинус тел >=
    # DEDUP_BODY_COSINE).
    return [max(groups[lab], key=lambda r: r[4] or "") for lab, *_ in rank(groups)]


def write_feed(fg, path):
    """XML для читалок — и только он.

    Раньше рядом писался HTML-двойник (feed.xsl + подмена в nginx по
    Sec-Fetch-Dest): человек, попавший на .xml из адресной строки, видел
    страницу вместо дерева тегов. Витрина сделала его лишним — у каждой страны
    есть своя страница, и вести туда правильнее, чем показывать фид под видом
    сайта. Сами .xml остались как были: на них смотрят 178 подписок FreshRSS.
    """
    with open(path, "wb") as f:
        f.write(fg.rss_str(pretty=True))


def build_country(conn, country):
    since = (datetime.now(timezone.utc) - timedelta(hours=settings.WINDOW_HOURS)).isoformat()
    # Порог один — граница приёма: вердикт accepted/rejected из pipeline.score.
    # Отдельного порога важности нет, важность считается графом (см. rank).
    rows = conn.execute(
        _SELECT + "WHERE country=? AND importance>? AND fetched_at>=?",
        (country, settings.RELEVANCE_CUTOFF, since)).fetchall()
    items = _top_items(rows)
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
        t_ru, x_ru = row[12], row[13]
        t_en, x_en = translated.get(url, (None, None))
        fe = fg.add_entry()
        fe.id(url)
        fe.link(href=url)
        fe.title((t_ru or t_en or title or url)[:300])
        fe.content(x_ru or x_en or text or "", type="CDATA")
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
