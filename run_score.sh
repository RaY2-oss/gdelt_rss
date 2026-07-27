#!/bin/bash
# run_score.sh — оценка, эмбеддинг тел, пересборка фидов, чистка (cron ежечасно).
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$BASE/venv/bin/python"

"$PY" "$BASE/main.py" score || {
    # Страховка: стадия умерла, не дойдя до feeds.build_all(). Фид «все статьи»
    # обязан обновляться каждый час независимо от причины — собираем его из
    # того, что уже лежит в БД (модель для этого не нужна, только numpy).
    echo "run_score.sh: main.py score вышел с кодом $? — пересобираю фиды из БД" >&2
    "$PY" -c "import main, db, feeds; main.setup_logging(); db.init(); feeds.build_all()"
}

# Витрина rss.bhutyan.online — те же данные, что в фидах, только HTML. Своей
# БД у неё нет, читает ту же в режиме ro. Падение сборки не должно ронять
# актуализацию: страница максимум останется прошлогодней, фиды важнее.
"$PY" "$BASE/site.py" /var/www/rss_site >/dev/null ||
    echo "run_score.sh: site.py вышел с кодом $? — витрина осталась от прошлого прогона" >&2

# Фиды пересобраны — сразу просим FreshRSS их забрать. У actualize_script.php
# свой мьютекс, поэтому наложение с дайджестом безопасно.
exec docker exec freshrss php /var/www/FreshRSS/app/actualize_script.php
