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
текста в целом, а корзина здесь — это named entity в заголовке.

Зато слова годятся разметчиком. Строгий заход (тема названа в заголовке) почти
не врёт — на нём и учится one-vs-rest логистическая регрессия по вектору статьи,
а отвечает она там, где слово не мелькнуло (см. refine). Замер на том же корпусе
08.08.2026, 4516 строгих меток:

    вектор: заголовок и тело поровну макро-F1 0.71 по 17 корзинам
    он же по одному телу             0.66, по одному заголовку 0.68
    очная ставка на 40 сюжетах       классификатор точнее словаря на 17, слабее на 4
    порог 0.75                       «Прочее» 13.6 % против 15.1 % у словаря

То есть покрытие даже выросло, хотя классификатор молчит, когда не уверен, —
просто молчит он на других сюжетах, чем ошибался словарь. Стоит это дёшево:
вектора уже лежат в БД, 17 моделей на шести тысячах строк обучаются 8 с, и
кэшировать нечего.

Почему не темы GKG. Они у нас уже есть (settings.SCITECH_THEMES) и матчатся на
приёме, но в колонку не пишутся, и в них нет ни ИИ, ни квантов, ни чипов —
самых очевидных для читателя корзин. Разбирать SOC_EMERGINGTECH на подтемы всё
равно пришлось бы словами.

Границы. Тем у сюжета может быть несколько (чипы для ИИ — и то, и другое), и это
честно: фильтр по «ИИ» такой сюжет показать обязан. Без темы не остаётся никто:
раньше корзины покрывали треть ленты, и две трети сюжетов не показывал ни один
фильтр — то есть кнопки скрывали больше, чем находили. Что не разобрано, уходит
в «Прочее» — не украшение, а честная отметка «уверенной темы нет».

Два входа. Витрина зовёт refine: она даёт сюжеты пачкой, с векторами, и получает
словарь плюс классификатор. of — тот же отбор без векторов, одним сюжетом; там
вместо классификатора остаётся мягкий заход по одному слову, потому что учить
не на чем.
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
# Сколько строгих примеров нужно корзине, чтобы на ней стоило учить модель.
# Ниже этого регрессия запоминает выборку, а не тему, и раздаёт её всей ленте.
MIN_POS = 40
# С какой уверенностью классификатор имеет право назвать тему. Подобран замером
# (см. заголовок файла): ниже 0.75 он начинает вешать по полторы темы на сюжет,
# выше — молчит там, где прав.
THRESH = 0.75

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


def _text(title, body):
    """Заголовки и тело сюжета → две строки, в которых ищем слова.

    title — строка или список строк (у сюжета несколько заголовков-версий).
    """
    head = " ".join(p for p in ([title] if isinstance(title, str) else title or []) if p)
    tail = " ".join(p[:BODY_CHARS] for p in body if p)
    return head, tail


def _scan(head, tail):
    """Строгие попадания и мягкие догадки словаря порознь, уже отсортированные.

    Строгое — тема названа в заголовке или набрана в теле BODY_MIN разными
    словами; таких тем может быть несколько. Мягкое — в теле мелькнуло одно
    слово; это кандидаты, а не ответ, и распоряжаются ими of и refine по-разному.
    """
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
    hits.sort()
    weak.sort()
    return [slug for _t, _n, _i, slug in hits], [slug for _n, _i, slug in weak]


def of(title, *body) -> list:
    """Заголовок и тело одного сюжета → слаги тем, самая явная первой.

    Без вектора, а значит и без классификатора: строгий заход, потом мягкий —
    одна тема по одному мелькнувшему слову, — потом OTHER. Мягкий заход
    угадывает примерно в половине случаев, поэтому витрина ходит через refine;
    здесь он остаётся ради тех, у кого вектора нет вовсе.
    """
    head, tail = _text(title, body)
    if not (head or tail):
        return [OTHER]
    hits, weak = _scan(head, tail)
    return hits or weak[:1] or [OTHER]


def refine(rows) -> list:
    """[(заголовки, тело, вектор)] на всю сборку → темы каждого сюжета.

    Заходы те же три, но средний считает не словарь, а классификатор: строгие
    попадания — обучающая выборка, вектор сюжета — признаки, и тема ставится
    только там, где модель уверена сильнее THRESH. Чем корпус больше, тем
    выборка полнее, так что корзину заводит по-прежнему topics.json, а
    дотягивается она сама.

    Вектор — единичной длины, заголовок и тело поровну (см. site._blend); None
    у сюжета допустим, он просто не попадёт ни в выборку, ни в опрос.
    """
    scan = [_scan(*_text(t, b if isinstance(b, (list, tuple)) else (b,)))
            for t, b, _v in rows]
    out = [list(hits) for hits, _weak in scan]
    for i, got in enumerate(_learned([r[2] for r in rows], out)):
        if got:
            out[i] = got
    return [tl or [OTHER] for tl in out]


def _learned(vecs, fixed) -> list:
    """Темы по векторам для сюжетов, где словарь смолчал. Список той же длины.

    Учим по одной модели на корзину (one-vs-rest): у сюжета тем может быть
    несколько, а мультиметку логистическая регрессия не знает. Учим только на
    строгих попаданиях — мягкие догадки словаря это как раз то, что мы здесь
    заменяем, и учиться на них значило бы переписать его ошибки в модель.

    Без sklearn или без выборки возвращает пустоту: витрина тогда соберётся по
    одному словарю, а не упадёт.
    """
    blank = [None] * len(vecs)
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
    except ImportError:
        return blank
    know = [i for i, v in enumerate(vecs) if v is not None and fixed[i]]
    ask = [i for i, v in enumerate(vecs) if v is not None and not fixed[i]]
    if len(know) < MIN_POS * 2 or not ask:
        return blank
    X = np.asarray([vecs[i] for i in know], np.float32)
    Q = np.asarray([vecs[i] for i in ask], np.float32)
    got = [[] for _ in ask]
    for slug, _name, _rx in _RX:
        y = np.fromiter((slug in fixed[i] for i in know), bool, len(know))
        # y.all() отдельно: одноклассовую выборку sklearn не учит, а такое
        # бывает на узком корпусе (одна страна, один день).
        if y.sum() < MIN_POS or y.all():
            continue
        # liblinear, а не lbfgs: тот же F1 0.71, но впятеро быстрее — 8 с на всю
        # сборку против сорока. class_weight — потому что корзина это 5-10 %
        # корпуса, и без него порог пришлось бы держать на уровне шума.
        m = LogisticRegression(C=4, max_iter=2000, solver="liblinear",
                               class_weight="balanced", random_state=0)
        m.fit(X, y)
        for k, p in enumerate(m.predict_proba(Q)[:, 1]):
            if p > THRESH:
                got[k].append((-p, slug))
    out = list(blank)
    for i, cand in zip(ask, got):
        if cand:
            out[i] = [slug for _p, slug in sorted(cand)]
    return out


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

    # refine: те же строгие попадания, но вместо мягкого захода — классификатор.
    # Корпус здесь синтетический (два полюса на плоскости), и проверяется не
    # качество модели, а провод: сюжет, в котором слова темы нет вовсе,
    # получает её по одному вектору, а не уходит в «Прочее».
    try:
        import numpy as np
    except ImportError:                      # pragma: no cover
        print("topics: numpy не поставлен, refine не проверен")
    else:
        rng = np.random.default_rng(0)

        def vec(pole):
            v = np.array(pole, float) + rng.normal(0, 0.05, 2)
            return v / np.linalg.norm(v)

        rows = []
        for _ in range(MIN_POS + 5):
            rows.append(("Quantum computing lab opens with 50-qubit machine",
                         "", vec((1, 0))))
            rows.append(("India launches new communications satellite", "",
                         vec((0, 1))))
        mute = len(rows)
        rows += [("Ministry signs a memorandum", "", vec((1, 0))),
                 ("Ministry signs a memorandum", "", vec((0, 1))),
                 ("Ministry signs a memorandum", "", None)]
        got = refine(rows)
        assert got[:2] == [["quantum"], ["space"]], got[:2]
        assert got[mute] == ["quantum"], got[mute]
        assert got[mute + 1] == ["space"], got[mute + 1]
        # Без вектора спрашивать нечего — и выдумывать тоже.
        assert got[mute + 2] == [OTHER], got[mute + 2]
        # Выборки нет — собираемся по словарю, а не падаем.
        assert refine([("Quantum computing lab", "", vec((1, 0))),
                       ("Ministry signs a memorandum", "", vec((0, 1)))]) == \
            [["quantum"], [OTHER]]
        assert refine([]) == []

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
