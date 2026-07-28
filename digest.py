# -*- coding: utf-8 -*-
"""digest.py — недельный дайджест: топ сюжетов недели по важности с защитой
от повторов между дайджестами.

Дневного дайджеста больше нет. Витрина сортирует ленту по структурной
важности сама и делает это ежечасно — второй раз отбирать «главное за день»
незачем. Осталось то, чего лента не даёт: срез за неделю, где сюжет,
набиравший обороты пять дней подряд, стоит выше однодневной вспышки.

Шаги на страну:
    1. articles за DIGEST_GRAPH_DAYS дней, прошедшие гейт релевантности.
    2. кластеризация e5-косинусом (feeds.cluster_rows) — один кластер = один
       сюжет, растянутый на несколько дней.
    3. важность кластера — структурная (feeds.rank -> importance.structural):
       LexRank по эмбеддингам тел + охват по РАЗНЫМ доменам + вес субъектов.
       Оценка LLM в ней не участвует: она двоичная, «по теме или нет».
    4. представитель кластера — самая свежая его статья. Гейта «есть статья
       именно за сегодня» больше нет: он существовал ровно для того, чтобы
       один сюжет не попадал в дайджест семь дней подряд, а у недельного
       выпуска эту работу делает дедуп по digest_sent.
    5. кластеры, чей представитель косинусно совпадает с уже отправленным
       ранее (digest_sent), отбрасываются — не повторяем сюжет.
    6. MMR по оставшимся кандидатам (важность кластера vs похожесть на уже
       выбранное в этом выпуске) — top-N, без дублей внутри самой недели.
    7. перевод отобранных статей на английский (кроме уже ru/en — см.
       translate.translate_missing), затем запись {country}_digest.xml +
       фиксация выбранного в digest_sent.

Имя файла осталось прежним ({country}_digest.xml): на него смотрят подписки
FreshRSS, и переименование стоило бы 89 переподписок ради нуля пользы.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import numpy as np
from feedgen.feed import FeedGenerator

import settings
import db
import feeds
import importance as imp
import translate
from feeds import _pubdate, _mmr_select, _max_cosine  # переиспользуем MMR и парсинг даты

log = logging.getLogger("gdelt_rss")

# Выпуск теперь недельный, а не суточный: settings.TOP_N рассчитан на день и
# на неделю даёт три сюжета в сутки. Полтора десятка сверху — не «побольше
# всего», а тот же охват при семикратном окне.
WEEK_TOP_N = settings.TOP_N + 15


def build_country_digest(conn, country, day=None):
    """day — дата выпуска (метка в описании фида и в digest_sent), окно всегда
    последние DIGEST_GRAPH_DAYS дней."""
    day_str = day or datetime.now(timezone.utc).date().isoformat()
    graph_since = (datetime.now(timezone.utc) -
                   timedelta(days=settings.DIGEST_GRAPH_DAYS)).isoformat()

    rows = conn.execute(
        # Граница приёма, а не отдельный порог важности: LLM решает только «по
        # теме или нет», отбор в дайджест делают TOP_N и MMR по структурной
        # важности. Прежний MIN_IMPORTANCE=40 резал по квантованной оценке и
        # выбрасывал сюжеты ещё до того, как граф успевал их взвесить.
        feeds._SELECT + "WHERE country=? AND importance>? AND fetched_at>=?",
        (country, settings.RELEVANCE_CUTOFF, graph_since)).fetchall()
    if not rows:
        _write_feed(country, day_str, [], {})
        return 0

    # Кластеризация и важность — те же, что у фида и витрины (feeds.py): три
    # ленты обязаны считать один и тот же сюжет одним и тем же, иначе дайджест
    # выносит наверх то, чего в фиде рядом нет.
    clusters = feeds.cluster_rows(rows, country)
    cluster_imp = dict(feeds.rank(clusters))

    # Сравнивать с уже отправленным надо тем же эмбеддингом, каким считается
    # представитель, иначе дедуп повторов слепнет. В digest_sent лежит тот, что
    # был на момент отправки (мог быть заголовочным — тело досчитывается позже
    # отдельной стадией), поэтому берём актуальное тело статьи, если она ещё в БД.
    sent_embs = [np.frombuffer(r[0], np.float32) for r in conn.execute(
        "SELECT COALESCE(a.embedding_body, s.embedding) FROM digest_sent s "
        "LEFT JOIN articles a ON a.url=s.url WHERE s.country=?", (country,))]

    candidates = []
    for lab, cluster_rows in clusters.items():
        # Внутри кластера это один сюжет, а оценка LLM теперь двоичная и
        # представителя не выбирает — берём самую свежую статью недели.
        rep = max(cluster_rows, key=lambda r: r[4] or "")
        rep_emb = imp.body_emb(rep[10], rep[6])
        if _max_cosine(rep_emb, sent_embs) >= settings.DEDUP_COSINE:
            continue  # уже был в одном из прошлых дайджестов
        candidates.append({
            "url": rep[0], "title": rep[1], "text": rep[2],
            "pdate": rep[3], "fa": rep[4],
            "imp": cluster_imp[lab], "emb": rep_emb,
            "language": rep[7], "title_en": rep[8], "text_en": rep[9],
            "title_ru": rep[12], "text_ru": rep[13],
        })

    picked = _mmr_select(candidates, WEEK_TOP_N)
    picked.sort(key=lambda c: c["imp"], reverse=True)
    translated = {}
    if picked:
        # переводим только то, что реально попало в дайджест — не весь
        # недельный граф (см. translate.py); ru/en пропускаются как есть.
        translated = translate.translate_missing(
            conn, [(c["url"], c["title"], c["text"], c["language"],
                    c["title_en"], c["text_en"]) for c in picked])
        _record_sent(conn, country, day_str, picked)

    # Пишем всегда, даже пустой. «За неделю в этой стране ничего не набралось» —
    # это ответ, и для подписки он выглядит как 200 с нулём записей. Раньше
    # файла просто не было, и 43 фида висели в FreshRSS в ошибке постоянно:
    # красный флаг переставал что-либо значить.
    _write_feed(country, day_str, picked, translated)
    return len(picked)


def _write_feed(country, day_str, picked, translated):
    disp = settings.country_display(country)
    fg = FeedGenerator()
    fg.title(f"GDELT Sci/Tech Weekly Digest — {disp}")
    fg.link(href="https://data.gdeltproject.org/", rel="alternate")
    fg.description(f"Недельный дайджест на {day_str}: топ-{WEEK_TOP_N} сюжетов по "
                    f"важности за {settings.DIGEST_GRAPH_DAYS} дней, без повторов "
                    f"с прошлыми выпусками.")
    fg.language("mul")
    for c in picked:
        t_en, x_en = translated.get(c["url"], (None, None))
        fe = fg.add_entry()
        fe.id(c["url"])
        fe.link(href=c["url"])
        fe.title((c["title_ru"] or t_en or c["title"] or c["url"])[:300])
        fe.content(c["text_ru"] or x_en or c["text"] or "", type="CDATA")
        fe.pubDate(_pubdate(c["pdate"], c["fa"]))
    feeds.write_feed(fg, os.path.join(settings.OUTPUT_DIR, f"{country}_digest.xml"))


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
        log.info("Недельный дайджест собран: %d стран, %d сюжетов всего",
                 len(settings.COUNTRIES), total)
        return total
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    build_all()
