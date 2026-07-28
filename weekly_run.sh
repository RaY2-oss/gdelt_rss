#!/bin/bash
# weekly_run.sh — недельный дайджест, один раз в неделю (понедельник 00:10 UTC).
# cron: 10 0 * * 1 /opt/gdelt_rss/weekly_run.sh >> /opt/gdelt_rss/logs/cron_digest.log 2>&1
# Без flock — как и run.sh: прогон обязан состояться, даже если часовой идёт.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

"$BASE/venv/bin/python" "$BASE/weekly_digest.py" "$@"

# Как и в run.sh: дайджест собран — сразу отдаём его FreshRSS.
exec docker exec freshrss php /var/www/FreshRSS/app/actualize_script.php
