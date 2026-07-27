#!/bin/bash
# run.sh — часовой прогон.
# cron: 7 * * * * /opt/gdelt_rss/run.sh >> /opt/gdelt_rss/logs/cron.log 2>&1
# Без flock: прогоны сознательно могут накладываться — ни один дамп GKG не
# должен быть пропущен только потому, что предыдущий прогон ещё идёт. Дедуп
# держат seen_urls (seen_store) и INSERT OR IGNORE, БД в WAL.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$BASE/venv/bin/python"

"$PY" "$BASE/main.py" || {
    # Страховка: main.py умер, не дойдя до feeds.build_all(). Фид «все статьи»
    # обязан обновляться каждый час независимо от причины — собираем его из
    # того, что уже лежит в БД (модель тут не нужна, только numpy).
    echo "run.sh: main.py вышел с кодом $? — пересобираю фиды из БД" >&2
    "$PY" -c "import main, db, feeds; main.setup_logging(); db.init(); feeds.build_all()"
}

# Фиды пересобраны — сразу просим FreshRSS их забрать, чтобы читалка не ждала
# своего расписания. У actualize_script.php есть собственный мьютекс, поэтому
# наложение с дайджестом (daily_run.sh) безопасно: второй запуск сам выйдет.
exec docker exec freshrss php /var/www/FreshRSS/app/actualize_script.php
