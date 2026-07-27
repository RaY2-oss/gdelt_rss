#!/bin/bash
# daily_run.sh — суточный дайджест за прошедшие сутки, один раз в 00:00 UTC.
# cron: 0 0 * * * /opt/gdelt_rss/daily_run.sh >> /opt/gdelt_rss/logs/cron_digest.log 2>&1
# Без flock — как и run.sh: прогон обязан состояться, даже если часовой идёт.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$BASE/venv/bin/python" "$BASE/daily_digest.py" "$@"
