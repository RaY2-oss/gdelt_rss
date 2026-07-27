#!/bin/bash
# run.sh — часовой прогон.
# cron: 7 * * * * /opt/gdelt_rss/run.sh >> /opt/gdelt_rss/logs/cron.log 2>&1
# Без flock: прогоны сознательно могут накладываться — ни один дамп GKG не
# должен быть пропущен только потому, что предыдущий прогон ещё идёт. Дедуп
# держат seen_urls (seen_store) и INSERT OR IGNORE, БД в WAL.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$BASE/venv/bin/python"

"$PY" "$BASE/main.py" && exit 0

# main.py умер, не дойдя до feeds.build_all(). Обычная причина на этом VPS —
# SIGILL (132) внутри libtorch_cpu: у QEMU-процессора нет AVX, а MKL-ядро в
# колесе torch+cpu его использует (см. README, «Известные проблемы»). Фид
# «все статьи» обязан обновляться каждый час независимо от этого — собираем
# его из того, что уже лежит в БД (модель тут не нужна, только numpy).
echo "run.sh: main.py вышел с кодом $? — пересобираю фиды из БД" >&2
exec "$PY" -c "import main, db, feeds; main.setup_logging(); db.init(); feeds.build_all()"
