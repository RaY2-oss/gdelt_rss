# -*- coding: utf-8 -*-
"""daily_digest.py — конец дня: собрать дневной дайджест по всем странам.

    venv/bin/python /opt/gdelt_rss/daily_digest.py

Запускать по cron ОДИН раз в сутки, в 00:00 UTC (collect/score туда не
входят — это делает main.py каждый час, дайджест только читает уже
накопленную и оценённую БД).

Дайджест собирается за ПРЕДЫДУЩИЕ сутки: в 00:00 UTC сегодняшний день ещё
пуст (ни одного часового прогона не было), а вчерашний только что
закрылся — digest.build_country_digest берёт кандидатами только статьи
с датой == day, так что day=сегодня давал бы пустой дайджест каждый раз.
Дату можно передать аргументом: daily_digest.py 2026-07-25
"""
import sys
from datetime import datetime, timedelta, timezone

import main as hourly  # переиспользуем setup_logging
import db
import digest


def main():
    day = (sys.argv[1] if len(sys.argv) > 1 else
           (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())
    log = hourly.setup_logging()
    log.info("=== Дневной дайджест за %s ===", day)
    db.init()
    digest.build_all(day)
    log.info("=== Готово ===")


if __name__ == "__main__":
    main()
