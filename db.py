# -*- coding: utf-8 -*-
"""db.py — схема и соединение SQLite.

Одна таблица articles: URL — ключ дедупа/кэша, importance — оценка LLM (0..100,
NULL пока не оценено), embedding — e5 float32 blob (для e5-дедупа при сборке
фида, без повторной загрузки модели). seen_urls — журнал уже обработанных URL
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
    embedding    BLOB,             -- e5 float32
    entities     TEXT,              -- субъекты из GKG V1Persons/V1Organizations (см. entities.py)
    importance   INTEGER            -- оценка LLM 0..100 + поправка на политический вес; NULL = не оценено
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
        seen_store.ensure(conn)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init()
    print(f"[OK] БД инициализирована: {settings.DB_PATH}")
