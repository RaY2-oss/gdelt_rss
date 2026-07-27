# -*- coding: utf-8 -*-
"""feeds.py — сборка часового «все статьи» RSS-фида на страну для FreshRSS.

На страну: статьи за WINDOW_HOURS с importance>RELEVANCE_CUTOFF (то есть
вердикт LLM был accepted — см. pipeline.score), e5-косинусная кластеризация
дублей (один сюжет, разные издания/языки — берём представителя с наибольшей
важностью), сортировка по важности, без верхнего лимита (это фид «все
статьи» — отбор по важности с MMR/TOP_N делает digest.py для дневного
«важные статьи»-фида). Запись {country}.xml в OUTPUT_DIR (его отдаёт nginx
rss_proxy для FreshRSS).

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

import settings
import db
import importance as imp
import translate

log = logging.getLogger("gdelt_rss")

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


def _cluster(embs, threshold, titles=None, exclude=()):
    """Жадная кластеризация по косинусу, построчно (без матрицы n*n).

    titles (опц.): пары в [DEDUP_LEXICAL_FLOOR, threshold) — семантически
    близкие, но не обязательно один сюжет (см. DEDUP_LEXICAL_FLOOR) — мержим
    только если делят >= _MIN_SHARED_TOKENS "редких" для батча токена
    заголовка. Косинус >= threshold мержится безусловно."""
    n = len(embs)
    if n == 0:
        return np.zeros(0, int)
    E = np.asarray(embs, np.float32)
    E = E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-10)
    lex = _distinctive_tokens(titles, exclude) if titles else None
    labels = np.full(n, -1, int)
    nxt = 0
    for i in range(n):
        if labels[i] >= 0:
            continue
        sims = E @ E[i]
        match = sims >= threshold
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


def _top_items(rows, country):
    """rows: (url,title,text,publish_date,fetched_at,importance,embedding,
    language,title_en,text_en). -> все отобранные после дедупа, по убыванию
    важности (без TOP_N/MMR-лимита — это фид «все статьи»)."""
    if not rows:
        return []
    embs = [np.frombuffer(r[6], np.float32) for r in rows]
    # title_en, если уже закэширован с прошлого прогона (см. translate.py) —
    # мостит кросс-языковые дубли, которых сырой заголовок мостить не может
    # (разные алфавиты не делят токенов); сходится за пару часовых прогонов.
    titles = [r[8] or r[1] for r in rows]
    exclude = _TOKEN_RE.split(settings.country_display(country).lower())
    labels = _cluster(embs, settings.DEDUP_COSINE, titles=titles, exclude=exclude)
    best, urls = {}, {}
    for row, emb, lab in zip(rows, embs, labels):
        lab = int(lab)
        urls.setdefault(lab, []).append(row[0])
        if lab not in best or (row[5] or 0) > (best[lab][0][5] or 0):
            best[lab] = (row, emb)
    # Оценка LLM квантована (96 % значений кратны 5), поэтому одной ею сортировать
    # нельзя: в окне крупной страны сотни статей делят одно число и порядок внутри
    # связки произволен. Второй ключ — охват сюжета по РАЗНЫМ доменам. Полный
    # LexRank здесь не считается намеренно: это фид «все статьи» без TOP_N-отсечки,
    # ранжирование в нём косметическое, а стоимость графа — ежечасная на 89 стран.
    cov = {lab: imp.coverage_weight(imp.distinct_domains(u), settings.COVERAGE_FULL_AT)
           for lab, u in urls.items()}
    picked = sorted(best.items(),
                    key=lambda kv: ((kv[1][0][5] or 0), cov[kv[0]]), reverse=True)
    return [row for _lab, (row, _emb) in picked]


def build_country(conn, country):
    since = (datetime.now(timezone.utc) - timedelta(hours=settings.WINDOW_HOURS)).isoformat()
    # RELEVANCE_CUTOFF, не MIN_IMPORTANCE: это фид «все статьи» — граница та
    # же, что и вердикт accepted/rejected в pipeline.score (importance>CUTOFF).
    rows = conn.execute(
        "SELECT url,title,text,publish_date,fetched_at,importance,embedding,language,title_en,text_en "
        "FROM articles WHERE country=? AND importance>? AND fetched_at>=? "
        "ORDER BY importance DESC", (country, settings.RELEVANCE_CUTOFF, since)).fetchall()
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
    for url, title, text, pdate, fa, imp, _emb, _lang, _t_en, _x_en in items:
        t_en, x_en = translated.get(url, (None, None))
        fe = fg.add_entry()
        fe.id(url)
        fe.link(href=url)
        fe.title((t_en or title or url)[:300])
        fe.content(x_en or text or "", type="CDATA")
        fe.pubDate(_pubdate(pdate, fa))
    out = os.path.join(settings.OUTPUT_DIR, f"{country}.xml")
    fg.rss_file(out, pretty=True)
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
