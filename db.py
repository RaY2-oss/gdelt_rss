# -*- coding: utf-8 -*-
"""db.py — схема и соединение SQLite.

Одна таблица articles: URL — ключ дедупа/кэша, importance — оценка LLM (0..100,
NULL пока не оценено) — это ГЕЙТ релевантности, а не шкала ранжирования (она
квантована, см. importance.py). Эмбеддингов два: embedding по заголовку —
дедуп и кластеризация сюжетов; embedding_body по телу статьи — релевантность
и LexRank. seen_urls — журнал уже обработанных URL
(seen_store.py), чтобы не качать одно и то же между прогонами.
"""
import sqlite3

import settings
import seen_store

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    url          TEXT PRIMARY KEY,
    country      TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,     -- ISO UTC, окно фида считается по нему
    publish_date DATE,
    title        TEXT,
    text         TEXT,
    title_en     TEXT,             -- перевод заголовка (Google Translate, см. translate.py)
    text_en      TEXT,             -- перевод текста статьи
    language     TEXT,
    title_hash   TEXT,              -- дешёвый дедуп одинаковых заголовков
    embedding      BLOB,           -- e5 float32 по ЗАГОЛОВКУ: дедуп/кластеризация сюжетов
    embedding_body BLOB,           -- e5 float32 по ТЕЛУ статьи: релевантность и LexRank
    entities     TEXT,              -- субъекты из GKG V1Persons/V1Organizations (см. entities.py)
    importance   INTEGER,           -- гейт релевантности 0..100; NULL = не оценено
    scored_by    TEXT               -- 'llm' | 'gate': кто вынес вердикт. Обучающая
                                    -- выборка берётся ТОЛЬКО по 'llm', иначе гейт
                                    -- начнёт учиться на собственных решениях
);
CREATE INDEX IF NOT EXISTS idx_articles_country_time ON articles(country, fetched_at);
CREATE INDEX IF NOT EXISTS idx_articles_unscored ON articles(importance) WHERE importance IS NULL;
CREATE INDEX IF NOT EXISTS idx_articles_titlehash ON articles(title_hash);

-- Сюжеты (по эмбеддингу представителя), уже попавшие в дневной дайджест —
-- чтобы дайджест следующих дней не повторял их (см. digest.py).
CREATE TABLE IF NOT EXISTS digest_sent (
    url          TEXT PRIMARY KEY,
    country      TEXT NOT NULL,
    digest_date  DATE NOT NULL,
    embedding    BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digest_sent_country ON digest_sent(country);

-- Состояние конвейера. Ключ gkg_cursor — последний ПОЛНОСТЬЮ обработанный
-- 15-минутный тик GKG. Курсор двигается только после успешной обработки,
-- поэтому ни один дамп не теряется из-за сбоя, разрыва сети или того, что
-- предыдущий прогон ещё шёл.
CREATE TABLE IF NOT EXISTS pipeline_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init() -> None:
    import os
    os.makedirs(os.path.dirname(settings.DB_PATH), exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
        # ru->en: старые RU-переводы неприменимы к новой целевой колонке,
        # просто отбрасываем колонки и переводим заново под title_en/text_en.
        if "title_ru" in cols:
            conn.execute("ALTER TABLE articles DROP COLUMN title_ru")
        if "text_ru" in cols:
            conn.execute("ALTER TABLE articles DROP COLUMN text_ru")
        if "title_en" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN title_en TEXT")
        if "text_en" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN text_en TEXT")
        if "entities" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN entities TEXT")
        # Вторая колонка эмбеддингов (тело статьи). Досчитывается лениво
        # для уже прошедших гейт статей — см. pipeline.embed_bodies().
        if "embedding_body" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN embedding_body BLOB")
        if "scored_by" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN scored_by TEXT")
        seen_store.ensure(conn)
        conn.commit()
    finally:
        conn.close()




def state_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM pipeline_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def state_set(conn, key, value):
    conn.execute("INSERT INTO pipeline_state (key,value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))
    conn.commit()


if __name__ == "__main__":
    init()
    print(f"[OK] БД инициализирована: {settings.DB_PATH}")
