# -*- coding: utf-8 -*-
"""daily_digest.py — конец дня: собрать дневной дайджест по всем странам.

    venv/bin/python /opt/gdelt_rss/daily_digest.py

Запускать по cron ОДИН раз в сутки, после последнего часового прогона
main.py за день (collect/score туда не входят — это делает main.py каждый
час, дайджест только читает уже накопленную и оценённую БД).
"""
import main as hourly  # переиспользуем setup_logging
import db
import digest


def main():
    log = hourly.setup_logging()
    log.info("=== Дневной дайджест ===")
    db.init()
    digest.build_all()
    log.info("=== Готово ===")


if __name__ == "__main__":
    main()
