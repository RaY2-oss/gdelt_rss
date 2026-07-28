# -*- coding: utf-8 -*-
"""weekly_digest.py — конец недели: собрать недельный дайджест по всем странам.

    venv/bin/python /opt/gdelt_rss/weekly_digest.py

Запускать по cron ОДИН раз в неделю (collect/score туда не входят — это делает
main.py каждый час, дайджест только читает уже накопленную и оценённую БД).

Дневного выпуска больше нет: витрина ранжирует ленту по важности ежечасно,
и «главное за день» она показывает сама. Недельный срез даёт то, чего в
ленте нет, — сюжет, набиравший обороты несколько дней подряд, стоит выше
однодневной вспышки.

Аргумент — только метка выпуска (по умолчанию сегодня), окно всегда
последние settings.DIGEST_GRAPH_DAYS дней: weekly_digest.py 2026-07-27
"""
import sys
from datetime import datetime, timezone

import main as hourly  # переиспользуем setup_logging
import db
import digest


def main():
    day = (sys.argv[1] if len(sys.argv) > 1 else
           datetime.now(timezone.utc).date().isoformat())
    log = hourly.setup_logging()
    log.info("=== Недельный дайджест на %s ===", day)
    db.init()
    digest.build_all(day)
    log.info("=== Готово ===")


if __name__ == "__main__":
    main()
