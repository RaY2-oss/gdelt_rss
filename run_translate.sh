#!/bin/bash
# run_translate.sh — фоновый перевод на русский (cron, каждые 20 мин).
#
# Намеренно уступает дорогу основному конвейеру: собственный cgroup с низким
# CPUWeight, nice 19, idle-класс ввода-вывода и всего 2 потока из 4 ядер.
# Score и collect по своим строкам cron не должны замечать, что он идёт.
#
# Имя юнита фиксированное: если прошлый запуск ещё работает, systemd-run
# откажет и второй экземпляр не поднимется. Это не потеря — очередь хранится
# в БД как "title_ru IS NULL", следующий запуск возьмёт то же самое.
set -u
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if systemctl is-active --quiet gdelt-translate.service; then
    echo "$(date -Is) перевод уже идёт — пропускаю запуск"
    exit 0
fi

exec systemd-run --unit=gdelt-translate --collect --quiet \
    --nice=19 \
    --property=CPUWeight=20 \
    --property=IOWeight=20 \
    --property=MemoryMax=4G \
    --property=MemorySwapMax=0 \
    --setenv=TRANSLATE_THREADS="${TRANSLATE_THREADS:-2}" \
    --setenv=TRANSLATE_BUDGET_S="${TRANSLATE_BUDGET_S:-900}" \
    --setenv=TRANSLATE_LIMIT="${TRANSLATE_LIMIT:-400}" \
    --setenv=HOME=/root \
    "$BASE/venv/bin/python" "$BASE/translate_worker.py"
