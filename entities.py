# -*- coding: utf-8 -*-
"""entities.py — «политический вес» статьи по субъектам, которых GDELT уже
извлёк за нас.

Зачем. Важность у нас считается по структуре корпуса (LexRank в /opt/digest,
оценка LLM + размер кластера в /opt/gdelt_rss), и локальная заметка вроде
«сельский школьник сдал экзамен на максимум баллов» иногда набирает достаточно,
чтобы просочиться в дайджест: сюжет-то по теме, просто ничтожного масштаба.

Идея. В общенациональной новости фигурируют субъекты, о которых пишет и вся
остальная лента — министерства, агентства, корпорации, политики; в локальной —
имена, встречающиеся ровно один раз. Скачивать готовый «огромный словарь
политических имён и организаций» для этого не нужно: GKG отдаёт V1Persons и
V1Organizations на каждый URL (собственный NER GDELT, на всех языках, включая
translingual-дампы), а «заметность» субъекта берётся из нашего же корпуса — в
скольких статьях окна он встретился (document frequency). Словарь получается
самонастраивающимся: он сразу на всех языках, обновляется каждым прогоном и не
требует ни сопоставления сущностей, ни ручной поддержки.

Считаем НЕ число упоминаний, а число РАЗНЫХ заметных субъектов. Одно громкое
имя найдётся и в локальной заметке (профильное министерство упоминают всегда),
а вот три-пять разных — уже признак события национального масштаба.

# ponytail: DF считается по своему же корпусу — заметность зависит от того, что
# мы успели собрать; на пустой базе фактор просто равен нулю и ни на что не
# влияет (см. weight()). Понадобится независимый эталон — на место
# document_freq() подставляется внешний словарь, интерфейс не меняется.
"""
import re
from collections import Counter
from functools import lru_cache

MIN_NAME_LEN = 4        # короче — мусор NER («un», «ap», инициалы)
_WS = re.compile(r"\s+")

# Служебные слова, которыми NER обрывает имя: «xinhua news agency on». Внутри
# имени они законны («ministry of education»), по краям — всегда обрезок, и
# переводчик честно доводит обрезок до абсурда («агентство Синьхуа включено»).
_EDGE = frozenset("a an the of on in at to for and or by with from is was as that".split())

# Не субъекты, а обстановка страницы: кнопки «поделиться», подвал, шапка. GKG
# принимает их за организации, и в блоке «Кто в новостях» они попадаются чаще
# любого министерства — «whatsapp linkedin» стоял в 60 сюжетах из 6116.
_JUNK = frozenset([
    "whatsapp linkedin", "terms of service", "privacy policy", "cookie policy",
    "all rights reserved", "wikimedia commons", "getty images", "read more",
    "sdn bhd", "co ltd", "pvt ltd", "information technology",
    "company intelligence",
])


@lru_cache(maxsize=100_000)
def clean(name):
    """Нормализованное имя или "" — мусор.

    Одна точка и для записи, и для чтения: колонка articles.entities набита
    за месяцы вперёд, и чинить только parse() значило бы чинить лишь будущее.
    Кэш нужен потому, что чтение горячее — document_freq() зовёт clean() на
    каждое имя каждой статьи окна, а имена повторяются.
    """
    words = _WS.sub(" ", name or "").strip().lower().split(" ")
    while words and words[0] in _EDGE:
        words.pop(0)
    while words and words[-1] in _EDGE:
        words.pop()
    out = " ".join(words)
    return "" if len(out) < MIN_NAME_LEN or out in _JUNK else out


def parse(*cells) -> str:
    """Ячейки V1Persons / V1Organizations -> ';'-строка имён для колонки articles.entities.

    Пустая ячейка приходит из pandas как float('nan'), а не как "" — приводим явно.
    """
    names = []
    for cell in cells:
        if not isinstance(cell, str):
            continue
        for raw in cell.split(";"):
            name = clean(raw)
            if name and name not in names:
                names.append(name)
    return ";".join(names)


def split(cell):
    """Имена ячейки. Дедуп здесь не лишний: обрезка служебных слов сводит
    «xinhua news agency on» и «xinhua news agency» в одно имя."""
    out = []
    for raw in (cell or "").split(";"):
        name = clean(raw)
        if name and name not in out:
            out.append(name)
    return out


def document_freq(cells) -> Counter:
    """{имя: в скольких статьях окна встретилось}."""
    df = Counter()
    for cell in cells:
        df.update(set(split(cell)))
    return df


def prominent(cell, df, min_df) -> int:
    """Сколько РАЗНЫХ субъектов статьи встречаются минимум в min_df статьях окна."""
    return sum(1 for n in set(split(cell)) if df.get(n, 0) >= min_df)


def weight(n_prominent, full_at) -> float:
    """0.0 — ни одного заметного субъекта, 1.0 — их full_at и больше."""
    if full_at <= 0:
        return 0.0
    return min(1.0, n_prominent / full_at)


def _selfcheck():
    corpus = [
        parse("Ali Veli;ali  veli", "Ministry of Education"),          # локальная заметка
        parse(float("nan"), "Ministry of Education;TUBITAK;ASELSAN"),
        parse("Recep Tayyip Erdogan", "TUBITAK;Ministry of Industry;ASELSAN"),
        parse(None, "TUBITAK;ASELSAN"),
    ]
    assert corpus[0] == "ali veli;ministry of education", corpus[0]  # нормализация + дедуп
    assert parse(float("nan"), None) == ""

    # Служебное слово по краю срезается, внутри имени остаётся; обстановка
    # страницы не субъект; обрезок и целое имя сливаются в одно.
    assert clean("Xinhua News Agency on") == "xinhua news agency"
    assert clean("the World Bank") == "world bank"
    assert clean("ministry of education") == "ministry of education"
    assert clean("WhatsApp LinkedIn") == "" and clean("Terms of Service") == ""
    assert clean("of") == "" and clean(None) == ""
    assert split("xinhua news agency on;xinhua news agency") == ["xinhua news agency"]

    df = document_freq(corpus)
    assert df["tubitak"] == 3 and df["ali veli"] == 1 and df["ministry of education"] == 2

    # Порог заметности отделяет одиночное громкое имя от плотного набора:
    assert prominent(corpus[0], df, 3) == 0
    assert prominent(corpus[2], df, 3) == 2
    assert prominent(corpus[0], df, 2) == 1

    assert weight(0, 3) == 0.0 and weight(3, 3) == 1.0 and weight(9, 3) == 1.0
    assert weight(1, 3) < weight(2, 3)
    print("entities: ok")


if __name__ == "__main__":
    _selfcheck()
