# -*- coding: utf-8 -*-
"""digest.py — дневной дайджест: топ сюжетов дня по важности с учётом
7-дневного графа и защитой от повторов между дайджестами.

Идея: важность и MMR-диверсити считаются по окну DIGEST_GRAPH_DAYS (сюжет,
набирающий обороты несколько дней подряд, "дозревает" до дайджеста — даже
если в день события вышла всего одна статья), но кандидатом в САМ дайджест
дня может быть только сюжет, у которого есть статья, впервые увиденная
именно в этот день (иначе тот же сюжет попадал бы в дайджест каждый день,
пока держится в 7-дневном окне).

Шаги на страну:
    1. articles за 7 дней (importance IS NOT NULL) — граф сюжетов.
    2. кластеризация e5-косинусом (переиспользует feeds._cluster) — один
       кластер = один сюжет, растянутый на несколько дней.
    3. важность кластера = максимум importance статей + линейный буст за
       охват (число статей за неделю) — так пара заметок за два дня подряд
       обгоняет одиночную статью с той же LLM-оценкой.
    4. кластер участвует в дайджесте ТОЛЬКО если среди его статей есть хотя
       бы одна, вышедшая сегодня; представитель — самая важная из сегодняшних.
    5. кластеры, чей представитель косинусно совпадает с уже отправленным
       ранее (digest_sent), отбрасываются — не повторяем сюжет.
    6. MMR по оставшимся кандидатам (важность кластера vs похожесть на уже
       выбранное в этом дайджесте) — top-N, без дублей внутри самого дня.
    7. перевод отобранных статей на английский (кроме уже ru/en — см.
       translate.translate_missing), затем запись {country}_digest.xml +
       фиксация выбранного в digest_sent.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import numpy as np
from feedgen.feed import FeedGenerator

import settings
import db
import translate
from feeds import _cluster, _pubdate, _mmr_select, _max_cosine, _TOKEN_RE  # переиспользуем кластеризацию, MMR и парсинг даты

log = logging.getLogger("gdelt_rss")


def _row_date(row):
    """row: (url,title,text,publish_date,fetched_at,importance,embedding)."""
    return _pubdate(row[3], row[4]).date().isoformat()


def _cluster_importance(cluster_rows):
    base = max((r[5] or 0) for r in cluster_rows)
    boost = settings.DIGEST_COVERAGE_BOOST * (len(cluster_rows) - 1)
    return min(100, base + boost)


def build_country_digest(conn, country, day=None):
    day_str = day or datetime.now(timezone.utc).date().isoformat()
    graph_since = (datetime.now(timezone.utc) -
                   timedelta(days=settings.DIGEST_GRAPH_DAYS)).isoformat()

    rows = conn.execute(
        "SELECT url,title,text,publish_date,fetched_at,importance,embedding,"
        "language,title_en,text_en "
        "FROM articles WHERE country=? AND importance>=? AND fetched_at>=?",
        (country, settings.MIN_IMPORTANCE, graph_since)).fetchall()
    if not rows:
        return 0

    embs = [np.frombuffer(r[6], np.float32) for r in rows]
    exclude = _TOKEN_RE.split(settings.country_display(country).lower())
    labels = _cluster(embs, settings.DEDUP_COSINE, titles=[r[1] for r in rows], exclude=exclude)
    clusters = {}
    for row, lab in zip(rows, labels):
        clusters.setdefault(int(lab), []).append(row)

    sent_embs = [np.frombuffer(r[0], np.float32) for r in conn.execute(
        "SELECT embedding FROM digest_sent WHERE country=?", (country,))]

    candidates = []
    for cluster_rows in clusters.values():
        today_rows = [r for r in cluster_rows if _row_date(r) == day_str]
        if not today_rows:
            continue  # сюжет без публикаций сегодня — не в этот дайджест
        rep = max(today_rows, key=lambda r: r[5] or 0)
        rep_emb = np.frombuffer(rep[6], np.float32)
        if _max_cosine(rep_emb, sent_embs) >= settings.DEDUP_COSINE:
            continue  # уже был в одном из прошлых дайджестов
        candidates.append({
            "url": rep[0], "title": rep[1], "text": rep[2],
            "pdate": rep[3], "fa": rep[4],
            "imp": _cluster_importance(cluster_rows), "emb": rep_emb,
            "language": rep[7], "title_en": rep[8], "text_en": rep[9],
        })

    picked = _mmr_select(candidates, settings.TOP_N)
    picked.sort(key=lambda c: c["imp"], reverse=True)
    if picked:
        # переводим только то, что реально попало в дайджест — не весь
        # недельный граф (см. translate.py); ru/en пропускаются как есть.
        translated = translate.translate_missing(
            conn, [(c["url"], c["title"], c["text"], c["language"],
                    c["title_en"], c["text_en"]) for c in picked])
        _write_feed(country, day_str, picked, translated)
        _record_sent(conn, country, day_str, picked)
    return len(picked)


def _write_feed(country, day_str, picked, translated):
    disp = settings.country_display(country)
    fg = FeedGenerator()
    fg.title(f"GDELT Sci/Tech Daily Digest — {disp}")
    fg.link(href="https://data.gdeltproject.org/", rel="alternate")
    fg.description(f"Дневной дайджест {day_str}: топ-{settings.TOP_N} сюжетов по "
                    f"важности (7-дневный граф), без повторов с прошлыми дайджестами.")
    fg.language("mul")
    for c in picked:
        t_en, x_en = translated.get(c["url"], (None, None))
        fe = fg.add_entry()
        fe.id(c["url"])
        fe.link(href=c["url"])
        fe.title((t_en or c["title"] or c["url"])[:300])
        fe.content(x_en or c["text"] or "", type="CDATA")
        fe.pubDate(_pubdate(c["pdate"], c["fa"]))
    out = os.path.join(settings.OUTPUT_DIR, f"{country}_digest.xml")
    fg.rss_file(out, pretty=True)


def _record_sent(conn, country, day_str, picked):
    conn.executemany(
        "INSERT OR REPLACE INTO digest_sent (url,country,digest_date,embedding) "
        "VALUES (?,?,?,?)",
        [(c["url"], country, day_str, c["emb"].tobytes()) for c in picked])
    conn.commit()


def build_all(day=None):
    os.makedirs(settings.OUTPUT_DIR, exist_ok=True)
    conn = db.connect()
    try:
        total = 0
        for country in settings.COUNTRIES:
            n = build_country_digest(conn, country, day)
            total += n
            if n:
                log.info("  дайджест %s: %d сюжетов", country, n)
        log.info("Дневной дайджест собран: %d стран, %d сюжетов всего",
                 len(settings.COUNTRIES), total)
        return total
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_all()
