# -*- coding: utf-8 -*-
"""seen_store.py — журнал уже обработанных URL.

Зачем. url_exists() смотрел в таблицу articles, куда попадают ТОЛЬКО принятые
статьи. Отклонённая нигде не отмечалась, поэтому тот же URL возвращался в
24-часовом окне GKG и заново скачивался и заново уходил в LLM. Замер по логу:
51380 Accept-событий на 19662 уникальных URL — 61.7% работы дублировалось.

Вердикты:
    accepted   — прошла LLM, лежит в articles;
    rejected   — LLM явно ответила "нет";
    short      — текст короче MIN_TEXT_LENGTH или не извлёкся;
    non_event  — отсеяна regex-правилом (интервью/колонка/аналитика);
    pending    — решения НЕТ: провайдеры молчали или ответ не разобрался.

Окончательны первые четыре, но short — только со второй попытки (SHORT_TRIES):
он говорит не о статье, а о том, как в эту секунду ответил чужой сервер.
pending — это очередь, которую следующий прогон
разбирает ДО сканирования новых дампов. Именно поэтому исчерпанный лимит или
обрыв сети не превращают статью в вечный чёрный список: раньше такой сбой
молча помечал батч как отклонённый (прогон week_swap 21.07: 6006 кандидатов,
0 принятых), и статья спасалась только тем, что URL возвращался в окне GKG —
то есть ровно за счёт той переобработки, которую эта таблица убирает.

Эмбеддинг сохраняется для accepted/rejected — это готовая размеченная
выборка для train_prefilter.py. Для pending не сохраняем: текст всё равно
не хранится, следующий прогон скачает и посчитает заново.
"""
import sqlite3

FINAL = ("accepted", "rejected", "short", "non_event")

# Сколько раз пробуем скачать, прежде чем списать URL как «текста нет».
#
# «short» стоял в FINAL наравне с вердиктами LLM, и это была ошибка в самом
# их сравнении: «нет» от модели — суждение о статье, а «текст не извлёкся» —
# суждение о том, как в эту секунду ответил чужой сервер. Замер по журналу:
# 14 183 URL списаны как короткие, и у всех до единого ровно одна попытка —
# второй они не получали никогда. Проба сорока таких адресов показала, что
# тринадцать из семнадцати ответивших двухсоткой отдают уже полный текст:
# в тот раз это был таймаут, обрыв или лимит частоты у издания, а не отсутствие
# статьи. Отсюда вторая попытка — URL возвращается в 24-часовом окне GKG сам,
# и повтор ничего не стоит, кроме одной загрузки.
SHORT_TRIES = 2

DDL = """
CREATE TABLE IF NOT EXISTS seen_urls (
    url         TEXT PRIMARY KEY,
    query_index INTEGER,
    first_seen  TEXT,
    last_seen   TEXT,
    verdict     TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 1,
    embedding   BLOB
);
CREATE INDEX IF NOT EXISTS idx_seen_verdict ON seen_urls(verdict, query_index);
CREATE INDEX IF NOT EXISTS idx_seen_lastseen ON seen_urls(last_seen);
"""

# ponytail: чанк по 500 — упор в лимит SQLITE_MAX_VARIABLE_NUMBER (999),
# без вычисления точного лимита рантайма; запас двукратный.
_CHUNK = 500


def ensure(conn) -> None:
    conn.executescript(DDL)
    conn.commit()


def final_urls(conn, urls) -> set:
    """Подмножество urls, по которым решение уже принято окончательно.

    «short» окончателен не сразу, а после SHORT_TRIES попыток: см. комментарий
    к константе.
    """
    urls = list(urls)
    out = set()
    hard = tuple(v for v in FINAL if v != "short")
    for i in range(0, len(urls), _CHUNK):
        chunk = urls[i:i + _CHUNK]
        q = ("SELECT url FROM seen_urls WHERE url IN (%s) AND ("
             "  verdict IN (%s) OR (verdict='short' AND attempts >= ?))"
             % (",".join("?" * len(chunk)), ",".join("?" * len(hard))))
        out.update(r[0] for r in conn.execute(
            q, tuple(chunk) + hard + (SHORT_TRIES,)))
    return out


def attempts(conn, urls) -> dict:
    """url -> сколько раз мы его уже брали. Отсутствующие в журнале — ноль."""
    urls = list(urls)
    out = {}
    for i in range(0, len(urls), _CHUNK):
        chunk = urls[i:i + _CHUNK]
        out.update(conn.execute(
            "SELECT url, attempts FROM seen_urls WHERE url IN (%s)"
            % ",".join("?" * len(chunk)), chunk))
    return out


def pending_urls(conn, query_index) -> list:
    """Очередь нерешённых, самые старые и наименее пробованные — первыми."""
    return [r[0] for r in conn.execute(
        "SELECT url FROM seen_urls WHERE verdict='pending' AND query_index=? "
        "ORDER BY attempts ASC, first_seen ASC", (query_index,))]


def mark(conn, rows, day) -> None:
    """rows: iterable из (url, query_index, verdict, embedding|None)."""
    rows = list(rows)
    if not rows:
        return
    conn.executemany(
        "INSERT INTO seen_urls (url,query_index,first_seen,last_seen,verdict,attempts,embedding) "
        "VALUES (?,?,?,?,?,1,?) "
        "ON CONFLICT(url) DO UPDATE SET "
        "  last_seen = excluded.last_seen, "
        "  verdict   = excluded.verdict, "
        "  attempts  = seen_urls.attempts + 1, "
        "  embedding = COALESCE(excluded.embedding, seen_urls.embedding)",
        [(u, qi, day, day, v, (e.tobytes() if e is not None else None))
         for u, qi, v, e in rows])
    conn.commit()


def prune(conn, keep_days) -> int:
    """Чистка журнала. Строки с эмбеддингом — обучающая выборка, поэтому
    удаляются только служебные вердикты без вектора."""
    cur = conn.execute(
        "DELETE FROM seen_urls WHERE embedding IS NULL "
        "AND last_seen < date('now', ?)", (f"-{int(keep_days)} days",))
    conn.commit()
    return cur.rowcount


def stats(conn) -> dict:
    return dict(conn.execute("SELECT verdict, COUNT(*) FROM seen_urls GROUP BY 1"))


def label_counts(conn) -> tuple:
    """(положительных, отрицательных) с сохранённым эмбеддингом."""
    row = conn.execute(
        "SELECT SUM(verdict='accepted'), SUM(verdict='rejected') FROM seen_urls "
        "WHERE embedding IS NOT NULL").fetchone()
    return (row[0] or 0, row[1] or 0)


def _selfcheck():
    """Вторая попытка для «short» — ровно то, что молча ломается: одна лишняя
    строка в WHERE, и либо короткие снова списываются с первого раза, либо
    отказы LLM начинают скачиваться по кругу."""
    import numpy as np
    c = sqlite3.connect(":memory:")
    ensure(c)
    rows = [("u/acc", 0, "accepted", np.zeros(4, np.float32)),
            ("u/rej", 0, "rejected", None),
            ("u/non", 0, "non_event", None),
            ("u/short", 0, "short", None),
            ("u/pend", 0, "pending", None)]
    mark(c, rows, "2026-08-07")

    got = final_urls(c, [u for u, *_ in rows])
    assert got == {"u/acc", "u/rej", "u/non"}, got   # короткий ждёт второй попытки
    assert pending_urls(c, 0) == ["u/pend"]

    mark(c, [("u/short", 0, "short", None)], "2026-08-08")   # вторая — и всё
    assert "u/short" in final_urls(c, ["u/short"])

    assert attempts(c, ["u/short", "u/rej", "u/нет"]) == {"u/short": 2, "u/rej": 1}

    # окончательные вердикты вторых шансов не получают
    mark(c, [("u/rej", 0, "rejected", None)], "2026-08-08")
    assert final_urls(c, ["u/rej"]) == {"u/rej"}
    print("seen_store selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
