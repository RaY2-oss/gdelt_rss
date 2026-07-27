#!/bin/bash
# run_collect.sh — только сбор. Дёшево, поэтому часто (cron каждые 15 мин).
# Интервал дампов задаёт курсор pipeline_state.gkg_cursor: от последнего
# полностью обработанного тика до последнего опубликованного. Ни один дамп
# не теряется и ни один не качается дважды.
# Без flock: если предыдущий сбор ещё идёт, второй просто увидит тот же курсор
# и упрётся в seen_urls/INSERT OR IGNORE — потери данных это не даёт.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$BASE/venv/bin/python" "$BASE/main.py" collect
