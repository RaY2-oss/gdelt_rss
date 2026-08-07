# -*- coding: utf-8 -*-
"""entity_ru.py — русские имена субъектов для блока «Кто в новостях».

Субъекты приходят из GKG латиницей (см. entities.py), и латинский столбик под
русской лентой читается как чужой текст. Порядок разрешения имени:

    1. Глоссарий (/opt/translate/glossary*.tsv). Совпадение считается только
       по ЦЕЛОЙ фразе: частичное («Ministry of Education» → «Министерство of
       Education») хуже честной латиницы. Режим keep оставляет оригинал
       намеренно — Samsung, Facebook, NASDAQ на кириллице читаются хуже.
       Единственное исключение из «целой фразы» — бренд в НАЧАЛЕ имени: он
       держит латиницей всё имя. Иначе выходило «Samsung» рядом с «Самсунг
       Электроникс», то есть одна фирма двумя разными способами на одной
       странице.
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


def _latin(name):
    """Как показывать имя, оставшееся латиницей. Из GKG оно приходит в нижнем
    регистре, и «bloomberg» посреди русской строки читается как опечатка."""
    return name if _CYR.search(name) else name.title()


def _from_glossary(name):
    """Русская форма из словаря — только если словарь покрывает имя целиком."""
    key = glossary._norm(name)
    if not key:
        return None
    for start, end, hit, repl, mode in glossary.find_all(name):
        if hit == key:
            # keep — это «не кириллицей», а не «как пришло»: в словаре у такой
            # строки лежит каноническое латинское написание (NASDAQ, NVIDIA),
            # и оно лучше и нижнего регистра из GKG, и своего .title().
            return repl or name
        if start == 0 and mode == "keep":
            # Имя начинается с бренда, который мы условились не переводить, —
            # латиницей остаётся всё имя. Иначе на одной странице соседствуют
            # «Samsung» и «Самсунг Электроникс»: голова из словаря, хвост от
            # переводчика, и читается это как два разных предприятия.
            # Остальные словарные бренды внутри имени пишем канонически, а не
            # своим .title(): «Google DeepMind», не «Google Deepmind».
            out, pos = [], 0
            for s, e, _h, r, m in glossary.find_all(name):
                if m != "keep":
                    break
                out += [_latin(name[pos:s]), r]
                pos = e
            return " ".join(" ".join(out + [_latin(name[pos:])]).split())
        break   # словарь зацепил середину — целиком имя всё равно не наше
    return None


# «Не ответил» и «ответил латиницей» — разные вещи, и путать их дорого: ответ
# кэшируется навсегда, поэтому сорванный запрос кэшировать нельзя.
_FAILED = object()


def _google(name):
    try:
        return _clean(GoogleTranslator(source="en", target="ru").translate(name)) or ""
    except Exception as exc:
        log.debug("entity_ru: Google не ответил на %r: %s", name, exc)
        return _FAILED


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
            out[name] = _latin(ru)
        elif _spent < BUDGET:
            _spent += 1
            todo.append(name)
        else:
            out[name] = _latin(name)

    if todo:
        # Title Case важен: Google распознаёт имя собственное по регистру и на
        # «narendra modi» отвечает охотнее нижним регистром, чем именем.
        with ThreadPoolExecutor(max_workers=settings.TRANSLATE_WORKERS) as pool:
            got = list(pool.map(lambda n: _google(n.title()), todo))
        rows = []
        for name, ru in zip(todo, got):
            if ru is _FAILED:
                # Сорванный запрос в кэш не идёт. Иначе один неудачный прогон —
                # или потолок запросов на той стороне при разборе накопленного
                # списка — оставлял бы имя латиницей навсегда, и следующий
                # прогон даже не пробовал бы: в кэше ведь «ответ».
                out[name] = _latin(name)
                continue
            # Ответ без кириллицы = бренд, который Google оставил латиницей.
            # Кэшируем и его, но показываем оригинал, а не выхлоп переводчика.
            keep = not _CYR.search(ru)
            out[name] = _latin(name) if keep else ru
            rows.append((name.lower(), out[name], "keep" if keep else "google"))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO entity_ru (name, ru, src) VALUES (?,?,?)", rows)
            conn.commit()
        log.info("entity_ru: переведено %d имён из %d спрошенных, всего за сборку %d",
                 len(rows), len(todo), _spent)
    return out


def _selfcheck():
    """Проверяем ровно то, что может разъехаться молча: словарь бьётся по целой
    фразе, режим keep оставляет латиницу, ответ без кириллицы не показывается."""
    assert _from_glossary("narendra modi") == "Нарендра Моди"
    assert _from_glossary("nasdaq") == "NASDAQ"             # keep — но написание из словаря
    assert _from_glossary("ministry of education") is None  # частичное — не берём
    assert _from_glossary("") is None
    # Бренд в голове имени держит латиницей весь хвост, а в середине — нет:
    # «United States Army» словарь переводит целиком («Армия США»).
    assert _from_glossary("samsung electronics") == "Samsung Electronics"
    assert _from_glossary("huawei technologies co") == "Huawei Technologies Co"
    assert _from_glossary("united states army") is None
    assert _from_glossary("google deepmind") == "Google DeepMind"
    assert _clean("Сонам ​​Вангчук") == "Сонам Вангчук"
    assert _latin("whatsapp linkedin") == "Whatsapp Linkedin"
    assert _latin("Министерство образования") == "Министерство образования"

    import sqlite3
    conn = sqlite3.connect(":memory:")
    # Google не зовём: имена есть в кэше, а последнее упирается в потолок.
    _ensure(conn)
    conn.executemany("INSERT INTO entity_ru (name, ru, src) VALUES (?,?,?)", [
        ("dhaka university", "Университет Дакки", "google"),
        ("hexnode", "hexnode", "keep"),
    ])
    global _spent
    _spent = BUDGET
    got = translate_names(conn, ["Dhaka University", "Hexnode", "Sanae Takaichi", "Samsung"])
    assert got["Dhaka University"] == "Университет Дакки", got
    assert got["Hexnode"] == "Hexnode", got                # латиница из кэша — не нижним регистром
    assert got["Sanae Takaichi"] == "Sanae Takaichi", got   # бюджет исчерпан — латиница
    assert got["Samsung"] == "Samsung", got

    # Сорванный запрос показываем латиницей, но в кэш не пишем — иначе имя
    # останется латиницей навсегда.
    global _google
    real, _google = _google, lambda n: _FAILED
    try:
        reset_budget()
        got = translate_names(conn, ["Sanae Takaichi"])
        assert got["Sanae Takaichi"] == "Sanae Takaichi", got
        assert not conn.execute(
            "SELECT 1 FROM entity_ru WHERE name = 'sanae takaichi'").fetchone()
    finally:
        _google = real
    reset_budget()
    print("entity_ru selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
