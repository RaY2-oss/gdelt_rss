#!/bin/bash
# run.sh — часовой прогон.
# cron: 7 * * * * /opt/gdelt_rss/run.sh >> /opt/gdelt_rss/logs/cron.log 2>&1
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# flock -n: если предыдущий прогон ещё не завершился, новый просто выходит
# (не наслаиваемся, как было со старым google_news_rss.py).
exec flock -n "$BASE/.run.lock" "$BASE/venv/bin/python" "$BASE/main.py"
