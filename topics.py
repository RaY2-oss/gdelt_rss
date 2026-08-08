# -*- coding: utf-8 -*-
"""topics.py — подтема сюжета: ИИ отдельно, кванты отдельно, космос отдельно.

Зачем. Лента отвечает на вопрос «где» (страна, регион) и «когда» (полоса дат),
но не на «про что». А корпус разнородный: в одной ленте соседствуют закон о
данных, запуск спутника, вспышка вымогателей и приёмная кампания вуза. Читателю,
пришедшему за чипами, остальные девять десятых мешают.

Корзины — данные, а не код. Список лежит в topics.json: слаг, подпись, слова.
Завести тему — три строки в файле, кода не касаясь; порядок в файле задаёт и
порядок кнопок на витрине, и номера битов в маске. Маска пересчитывается каждой
сборкой, поэтому любая правка файла размечает архив задним числом целиком, а не
с момента правки.

Слова не выдумываются, а добываются: `python topics.py --suggest` берёт сюжеты,
которые корзина уже уверенно поймала заголовком, и показывает слова, чем они
отличаются от остального корпуса (log-odds с дирихле-приором, Monroe et al.).
Выхлоп — кандидаты в topics.json, а не готовая правка: рядом с «samsung» и
«hynix» для чипов приезжает «tesla» для космоса и «shtml» для квантов.

Почему не эмбеддинги. Проверено на живом корпусе 08.08.2026 (6513 сюжетов,
e5-small; вектора статей уже лежат в БД, так что модель стоила бы нуля):

    словарь                          тем/сюжет 1.27, «Прочее» 15 %
    косинус к описанию темы          совпадение со словарём 31–64 %
    он же с центроидом по корпусу    29–66 %, и корзины разъезжаются

Совпадение считалось против тем, названных прямо в заголовке, — то есть против
самого надёжного, что есть. Форсированная метка врёт заметно: «Измерение нашего
капитала знаний» → «Кванты», «Turbojet укрепляет оборонные амбиции Индии» →
«Роботы». Причина не в модели, а в задаче: e5 меряет тематическую близость
текста в целом, а корзина здесь — это named entity в заголовке. Поэтому
эмбеддинги остались там, где они уже работают (релевантность, дедуп, LexRank),
а темы раздаёт словарь.

Почему не темы GKG. Они у нас уже есть (settings.SCITECH_THEMES) и матчатся на
приёме, но в колонку не пишутся, и в них нет ни ИИ, ни квантов, ни чипов —
самых очевидных для читателя корзин. Разбирать SOC_EMERGINGTECH на подтемы всё
равно пришлось бы словами.

Границы. Тем у сюжета может быть несколько (чипы для ИИ — и то, и другое), и это
честно: фильтр по «ИИ» такой сюжет показать обязан. Без темы не остаётся никто:
раньше корзины покрывали треть ленты, и две трети сюжетов не показывал ни один
фильтр — то есть кнопки скрывали больше, чем находили. Отбор идёт в три захода,
от строгого к последнему (см. of); что не разобрал словарь, уходит в «Прочее» —
не украшение, а честная отметка «сюда словарь не дотянулся».

# ponytail: мягкий заход (одно слово в теле) угадывает примерно в половине
# случаев — замер на выборке 08.08.2026. Это цена покрытия: без него корзины
# теряют шестую часть ленты. Если понадобится точность, ворота ставятся в of()
# — но косинус на эту роль уже пробовали, он мимо (см. выше).
"""
import json
import os
import re

# Тело читаем не целиком: тема названа в начале, а в хвосте длинной статьи
# любое слово когда-нибудь встретится.
BODY_CHARS = 700
# Сколько РАЗНЫХ слов темы нужно в теле, чтобы тема считалась названной.
BODY_MIN = 2
# Слаг корзины-остатка держим именем, а не индексом: порядок в topics.json
# редакционный и меняется, а «остаток» — это роль, а не место в списке.
OTHER = "other"

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "topics.json")


def _load(path=_DATA):
    """topics.json → [(слаг, подпись, скомпилированная регулярка|None)].

    Ошибка в файле роняет импорт намеренно: сборка без тем выглядит как
    успешная и молча стирает подтемы у всего архива.
    """
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    seen = set()
    out = []
    for i, t in enumerate(raw):
        slug, name = t["slug"], t["name"]
        if slug in seen:
            raise ValueError("topics.json: слаг %r повторяется" % slug)
        seen.add(slug)
        words = t.get("words") or []
        out.append((slug, name, re.compile("|".join(words), re.I | re.U)
                    if words else None))
    if OTHER not in seen:
        raise ValueError("topics.json: нет корзины-остатка %r" % OTHER)
    # Маску читает app.js оператором «&», а он в JS 32-битный знаковый.
    if len(out) > 31:
        raise ValueError("topics.json: %d тем, маска в search.json держит 31"
                         % len(out))
    return out


_TOPICS = _load()

# Корзины со словами — в порядке файла; по ним и идёт поиск.
_RX = [(slug, name, rx) for slug, name, rx in _TOPICS if rx is not None]

# Для витрины: (слаг, подпись) на каждую кнопку, включая «Прочее».
ALL = [(slug, name) for slug, name, _rx in _TOPICS]


def of(title, *body) -> list:
    """Заголовок и тело сюжета → слаги тем, самая явная первой. Пустым не бывает.

    Три захода, от строгого к последнему:
      1. Строгий: тема названа в заголовке или набрана в теле BODY_MIN разными
         словами. Таких тем может быть несколько — все и возвращаются.
      2. Мягкий: ни одна тема строгий заход не прошла, но в теле мелькнуло одно
         слово. Берём одну тему — ту, где слов больше, при ничьей выше по файлу.
      3. Остаток: не нашлось и слова — OTHER.

    title — строка или список строк (у сюжета несколько заголовков-версий).
    """
    head = " ".join(p for p in ([title] if isinstance(title, str) else title or []) if p)
    tail = " ".join(p[:BODY_CHARS] for p in body if p)
    if not (head or tail):
        return [OTHER]
    hits, weak = [], []
    for i, (slug, _name, rx) in enumerate(_RX):
        top = {m.group(0).lower() for m in rx.finditer(head)}
        deep = {m.group(0).lower() for m in rx.finditer(tail)} - top
        if top or len(deep) >= BODY_MIN:
            # Названная в заголовке тема идёт впереди любой набранной по телу,
            # сколько бы слов та ни набрала: заголовок говорит, о чём сюжет, а
            # тело — чего он по пути коснулось.
            hits.append((not top, -(len(top) + len(deep)), i, slug))
        elif deep:
            weak.append((-len(deep), i, slug))
    if hits:
        hits.sort()
        return [slug for _t, _n, _i, slug in hits]
    if weak:
        return [min(weak)[2]]
    return [OTHER]


def mask(slugs) -> int:
    """Темы сюжета одним числом — так они едут в search.json. Список слагов на
    шесть тысяч сюжетов весит впятеро больше самого индекса."""
    bit = {slug: 1 << i for i, (slug, _n) in enumerate(ALL)}
    return sum(bit[s] for s in slugs if s in bit)


def suggest(limit=14, min_docs=30, min_hits=8):
    """Слова-кандидаты в topics.json: чем корзина отличается от корпуса.

    Обучающая выборка корзины — сюжеты, где её слово стоит в ЗАГОЛОВКЕ: там
    словарь почти не ошибается. Дальше log-odds слова в выборке против всего
    корпуса, сглаженный дирихле-приором: частотность сама по себе выносит
    наверх «года» и «который», приор их придавливает.

    Печатает, а не правит: половина верхних слов — имена компаний и мусор
    доменов, отбирает человек.
    """
    import collections
    import math
    import sqlite3

    import settings

    conn = sqlite3.connect(settings.DB_PATH)
    rows = conn.execute("SELECT title, COALESCE(text,'') FROM articles "
                        "WHERE title IS NOT NULL").fetchall()
    conn.close()
    word = re.compile(r"[a-zA-Zа-яёА-ЯЁ][a-zA-Zа-яёА-ЯЁ-]{3,}")
    docs = [(t, collections.Counter(w.lower() for w in word.findall(t + " " + b[:BODY_CHARS])))
            for t, b in rows]
    total = collections.Counter()
    for _t, c in docs:
        total.update(c)
    n_all = sum(total.values())
    a0 = 0.01

    for slug, name, rx in _RX:
        pick = [c for t, c in docs if rx.search(t)]
        if len(pick) < min_docs:
            print("%-9s %-24s мало сюжетов (%d) — рано" % (slug, name, len(pick)))
            continue
        c = collections.Counter()
        for d in pick:
            c.update(d)
        n = sum(c.values())
        scored = []
        for w, k in c.items():
            if k < min_hits or total[w] < min_hits + 4 or rx.search(w):
                continue
            a = a0 * total[w]
            odds_in = (k + a) / (n + a0 * n_all - k - a)
            odds_out = ((total[w] - k + a)
                        / (n_all - n + a0 * n_all - (total[w] - k) - a))
            z = (math.log(odds_in / odds_out)
                 / math.sqrt(1.0 / (k + a) + 1.0 / (total[w] - k + a)))
            scored.append((z, w))
        scored.sort(reverse=True)
        print("%-9s %-24s (%d): %s" % (slug, name, len(pick),
                                       ", ".join(w for _z, w in scored[:limit])))


def _selfcheck():
    # Тесты называют слаги — значит, файл и код проверяются вместе: выпала
    # корзина из topics.json, сборка узнает об этом здесь, а не на витрине.
    assert of("India launches new communications satellite from Sriharikota") == ["space"]
    assert of("Расширение сети 5G и строительство ЦОД") == ["telecom"]
    # Несколько тем — это норма, а не ошибка отбора; первой идёт та, о которой
    # в тексте больше разных слов.
    both = of("TSMC to build a semiconductor foundry for AI chips",
              "The chipmaker said the fab will supply artificial intelligence accelerators.")
    assert both[0] == "chips" and "ai" in both, both
    # Омонимы: свободное место и штатное расписание — не космос и не вузы.
    assert of("The ministry freed up office space for staff") == [OTHER]
    assert of("Quantum computing lab opens with 50-qubit machine") == ["quantum"]
    assert of("Атомная станция вышла на проектную мощность") == ["energy"]
    # Реестр здесь настоящий — сюжет и правда про две корзины, но первой стоит
    # та, о которой слов больше.
    assert of("Ransomware group hit the national registry, data leak confirmed") == \
        ["cyber", "digital"]
    assert of("") == [OTHER] and of(None, "") == [OTHER]
    # Одно слово в теле — мягкий заход: тема одна и без права на компанию.
    assert of("Magnitude 7.1 earthquake hits Kyushu",
              "The plant reactor was shut down as a precaution.") == ["energy"]
    # Два разных слова — уже строгий, и тогда тема встаёт рядом с другими.
    assert of("Magnitude 7.1 earthquake hits Kyushu",
              "The nuclear plant reactor shut down; the power grid held.") == ["energy"]
    # Заголовок весит больше тела: названная тема стоит выше набранной.
    assert of("Satellite launched from Sriharikota",
              "The chip and semiconductor industry watched the wafer supply.") == \
        ["space", "chips"]
    # Хвост длинной статьи не считается: тема названа в начале.
    assert of("Дожди задержали уборку", "x" * BODY_CHARS + " университет кампус") == [OTHER]

    assert mask(["ai"]) == 1
    assert mask(["ai", "chips"]) == 3
    assert mask(["нет такой темы"]) == 0
    assert mask([OTHER]) == 1 << (len(ALL) - 1), "«Прочее» ждут последним битом"
    print("topics: ok, корзин %d (%d со словами)" % (len(ALL), len(_RX)))


if __name__ == "__main__":
    import sys
    if "--suggest" in sys.argv:
        suggest()
    else:
        _selfcheck()
