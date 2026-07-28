# -*- coding: utf-8 -*-
"""entity_ru.py — русские имена субъектов для блока «Кто в новостях».

Субъекты приходят из GKG латиницей (см. entities.py), и латинский столбик под
русской лентой читается как чужой текст. Порядок разрешения имени:

    1. Глоссарий (/opt/translate/glossary*.tsv). Совпадение считается только
       по ЦЕЛОЙ фразе: частичное («Ministry of Education» → «Министерство of
       Education») хуже честной латиницы. Режим keep оставляет оригинал
       намеренно — Samsung, Facebook, NASDAQ на кириллице читаются хуже.
    2. Кэш в таблице entity_ru: имена повторяются из сборки в сборку, платить
       за них сетевым запросом каждый час незачем.
    3. Google-переводчик, тот же deep_translator, что и у статей. Локальные
       marian-модели на именах собственных ломаются («Дип Сикс» из Deep Seek),
       Google их знает.

Ответ без кириллицы — это тоже ответ: Huawei и Xiaomi он возвращает как есть,
и записать латиницу в кэш правильнее, чем спрашивать про них каждый час.
Что не перевелось совсем — остаётся латиницей: пустая строка хуже оригинала.
"""
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

from deep_translator import GoogleTranslator

import glossary
import settings

log = logging.getLogger("gdelt_rss")

_CYR = re.compile(r"[А-Яа-яЁё]")
# Google вставляет в ответ нули ширины (U+200B) и неразрывные пробелы: на
# экране это лишние дыры внутри имени, а в кэше — мусор навсегда.
_JUNK = re.compile("[\u200b-\u200f\u2060\ufeff]")


def _clean(s):
    return _JUNK.sub("", s or "").replace("\xa0", " ").strip()

# Потолок сетевых запросов на сборку. Первый прогон разбирает весь накопленный
# список (порядка 600 имён на 89 стран), дальше почти всё берётся из кэша, и
# потолок не задевается вовсе. Недобранное покажется латиницей и переведётся
# следующим часовым прогоном.
BUDGET = int(os.environ.get("ENTITY_RU_BUDGET", "250"))
_spent = 0


def reset_budget():
    global _spent
    _spent = 0


def _ensure(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS entity_ru ("
                 "name TEXT PRIMARY KEY, ru TEXT NOT NULL, src TEXT)")


def _from_glossary(name):
    """Русская форма из словаря — только если словарь покрывает имя целиком."""
    key = glossary._norm(name)
    if not key:
        return None
    for _s, _e, hit, repl, mode in glossary.find_all(name):
        if hit == key:
            return name if mode == "keep" else repl
    return None


def _google(name):
    try:
        return _clean(GoogleTranslator(source="en", target="ru").translate(name)) or None
    except Exception as exc:
        log.debug("entity_ru: Google не ответил на %r: %s", name, exc)
        return None


def translate_names(conn, names):
    """names — латинские имена субъектов. Возвращает {имя: показывать_как}.

    Имя без перевода в словарь ответа всё равно попадает — латиницей, иначе
    блок молча потеряет строку.
    """
    names = [n for n in dict.fromkeys(n.strip() for n in names) if n]
    if not names:
        return {}
    global _spent

    out, unresolved = {}, []
    for name in names:
        hit = _from_glossary(name)
        if hit:
            out[name] = hit
        else:
            unresolved.append(name)
    if not unresolved:
        return out

    _ensure(conn)
    keys = [n.lower() for n in unresolved]
    cached = dict(conn.execute(
        "SELECT name, ru FROM entity_ru WHERE name IN (%s)" % ",".join("?" * len(keys)),
        keys))

    todo = []
    for name in unresolved:
        ru = cached.get(name.lower())
        if ru:
            out[name] = ru
        elif _spent < BUDGET:
            _spent += 1
            todo.append(name)
        else:
            out[name] = name

    if todo:
        # Title Case важен: Google распознаёт имя собственное по регистру и на
        # «narendra modi» отвечает охотнее нижним регистром, чем именем.
        with ThreadPoolExecutor(max_workers=settings.TRANSLATE_WORKERS) as pool:
            got = list(pool.map(lambda n: _google(n.title()), todo))
        rows = []
        for name, ru in zip(todo, got):
            # Ответ без кириллицы = бренд, который Google оставил латиницей.
            # Кэшируем и его, но показываем оригинал, а не выхлоп переводчика.
            keep = not ru or not _CYR.search(ru)
            out[name] = name if keep else ru
            rows.append((name.lower(), out[name], "keep" if keep else "google"))
        conn.executemany(
            "INSERT OR REPLACE INTO entity_ru (name, ru, src) VALUES (?,?,?)", rows)
        conn.commit()
        log.info("entity_ru: переведено %d имён, всего за сборку %d", len(rows), _spent)
    return out


def _selfcheck():
    """Проверяем ровно то, что может разъехаться молча: словарь бьётся по целой
    фразе, режим keep оставляет латиницу, ответ без кириллицы не показывается."""
    assert _from_glossary("narendra modi") == "Нарендра Моди"
    assert _from_glossary("samsung") == "samsung"          # keep — латиница
    assert _from_glossary("ministry of education") is None  # частичное — не берём
    assert _from_glossary("") is None
    assert _clean("Сонам ​​Вангчук") == "Сонам Вангчук"

    import sqlite3
    conn = sqlite3.connect(":memory:")
    # Google не зовём: имя есть в кэше, а второе имя упирается в потолок.
    _ensure(conn)
    conn.execute("INSERT INTO entity_ru (name, ru, src) VALUES ('dhaka university','Университет Дакки','google')")
    global _spent
    _spent = BUDGET
    got = translate_names(conn, ["Dhaka University", "Sanae Takaichi", "Samsung"])
    assert got["Dhaka University"] == "Университет Дакки", got
    assert got["Sanae Takaichi"] == "Sanae Takaichi", got   # бюджет исчерпан — латиница
    assert got["Samsung"] == "Samsung", got
    reset_budget()
    print("entity_ru selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
