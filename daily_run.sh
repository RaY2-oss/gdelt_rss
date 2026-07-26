#!/bin/bash
# daily_run.sh — суточный дайджест, один раз в 00:00 UTC, после последнего
# часового прогона run.sh за день.
# cron: 0 0 * * * /opt/gdelt_rss/daily_run.sh >> /opt/gdelt_rss/logs/cron_digest.log 2>&1
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec flock -n "$BASE/.digest.run.lock" "$BASE/venv/bin/python" "$BASE/daily_digest.py"
