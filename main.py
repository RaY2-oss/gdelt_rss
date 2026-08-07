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


def do_collect(log):
    """Сбор: дёшево и часто. Интервал дампов задаёт курсор, поэтому чем чаще
    запуск, тем меньше отставание — и ни один тик всё равно не потеряется."""
    log.info("=== Сбор ===")
    pipeline.collect()
    log.info("=== Сбор завершён ===")


def do_score(log):
    """Оценка + эмбеддинг тел + сборка фидов + чистка.

    Отделено от сбора намеренно: оценка упирается в лимиты LLM-провайдеров и
    занимает большую часть прогона (замер 27.07: 29 мин из 55). Пока она идёт,
    сбор не должен простаивать — иначе прогон перестаёт помещаться в свой
    интервал и прогоны начинают накладываться друг на друга.
    """
    log.info("=== Оценка и сборка фидов ===")
    pipeline.score()
    # Тела эмбеддятся после оценки: только у статей, прошедших гейт
    # релевантности, — им же считать LexRank в дайджесте (см. importance.py).
    pipeline.embed_bodies()
    feeds.build_all()
    log.info("Очистка: удалено старых строк %d", prune())
    log.info("=== Оценка завершена ===")


STAGES = {"collect": do_collect, "score": do_score}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    stage = argv[0] if argv else "all"
    if stage not in STAGES and stage != "all":
        print(f"Использование: main.py [collect|score|all]  (получено: {stage})")
        return 2
    log = setup_logging()
    db.init()
    if stage == "all":
        log.info("=== GDELT→FreshRSS полный прогон ===")
        do_collect(log)
        do_score(log)
        log.info("=== Готово ===")
    else:
        STAGES[stage](log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
