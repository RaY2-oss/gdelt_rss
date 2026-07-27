# -*- coding: utf-8 -*-
"""main.py — часовой прогон: сбор → оценка → сборка фидов → чистка.

    venv/bin/python /opt/gdelt_rss/main.py

Порядок важен: collect кладёт статьи с importance=NULL, score их оценивает,
build_all пишет фиды только по оценённым, prune чистит окно.
"""
import logging
import os
import sqlite3
import sys

import settings
import db
import pipeline
import feeds


def setup_logging():
    os.makedirs(settings.LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(os.path.join(settings.LOG_DIR, "gdelt_rss.log"),
                                      encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])
    return logging.getLogger("gdelt_rss")


def prune():
    conn = db.connect()
    try:
        cur = conn.execute(
            "DELETE FROM articles WHERE fetched_at < datetime('now', ?)",
            (f"-{settings.KEEP_HOURS} hours",))
        conn.execute(
            "DELETE FROM digest_sent WHERE digest_date < date('now', ?)",
            (f"-{settings.KEEP_HOURS // 24} days",))
        conn.commit()
        import seen_store
        seen_store.prune(conn, 30)
        try:
            conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            # VACUUM требует эксклюзивной блокировки — параллельный прогон
            # (flock снят, см. run.sh) её не даст. Место освободит следующий.
            logging.getLogger("gdelt_rss").warning("VACUUM пропущен: %s", exc)
        return cur.rowcount
    finally:
        conn.close()


def main():
    log = setup_logging()
    db.init()
    log.info("=== GDELT→FreshRSS прогон ===")
    pipeline.collect()
    pipeline.score()
    feeds.build_all()
    log.info("Очистка: удалено старых строк %d", prune())
    log.info("=== Готово ===")


if __name__ == "__main__":
    main()
