# -*- coding: utf-8 -*-
"""importance.py — структурная важность сюжета, считается БЕЗ обращения к LLM.

Зачем отдельный модуль. Оценка LLM (`articles.importance`) остаётся ГЕЙТОМ
релевантности: «по теме / не по теме» плюс грубая значимость. Ранжировать по
ней нельзя — она квантована: 96 % значений кратны пяти, и в окне одной страны
сотни статей делят одно и то же число (замер 27.07: у Китая 245 статей ровно
с 85). При TOP_N-отборе порядок внутри такой связки произволен.

Здесь важность считается по структуре корпуса и потому непрерывна — связок
не возникает вовсе. Три фактора:

    1. LexRank по графу косинусных схожестей (степенная итерация, аналог
       PageRank). Статья весома, если на неё похожи другие статьи, которые
       сами хорошо связаны с остальным окном. Считается по ЭМБЕДДИНГУ ТЕЛА
       статьи (`articles.embedding_body`), а не заголовка: по заголовкам
       LexRank мерил бы частоту перепечатки, а не тематическую центральность.

    2. Охват — сколько РАЗНЫХ доменов написали о сюжете, логарифмически.
       Линейная надбавка («+K за каждую статью») упирается в потолок: при
       базе 85 хватало четырёх перепечаток, дальше сюжет с 4 и сюжет с 40
       публикациями неразличимы. Логарифм насыщается, но не обнуляется и
       потолка не имеет: первая пара изданий даёт много, десятая почти
       ничего, но добавка продолжает расти.
       Домены дедуплицируются: одно издание, выкатившее пять версий текста,
       считается за один домен, а не за пять.

    3. Политический вес субъектов (entities.py) — как и раньше.

Свёртка трёх факторов домножается на свежесть (`freshness`): витрина держит
архив за две недели, и без затухания позавчерашний сюжет с сорока изданиями
стоял бы над сегодняшним вечно. Затухание половинное и с полом — крупная
старая новость опускается, но не исчезает.

Веса факторов — в settings.IMPORTANCE_W_*; выставить любой в 0 значит
выключить фактор, не трогая код. Затухание снимается
settings.IMPORTANCE_HALF_LIFE_DAYS = 0.
"""
import math
import re
from urllib.parse import urlsplit

import numpy as np

# www., m., amp. и подобные технические префиксы не делают издание другим.
_HOST_PREFIX = re.compile(r"^(?:www|m|amp|mobile|edition|en|ru)\.")


def domain(url: str) -> str:
    """URL -> нормализованный домен издания ('' если разобрать не удалось).

    Именно домен, а не URL, — единица охвата: пять версий одного текста на
    одном сайте не должны весить как пять изданий.
    """
    try:
        host = urlsplit(url or "").netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return _HOST_PREFIX.sub("", host)


def distinct_domains(urls) -> int:
    return len({d for d in (domain(u) for u in urls) if d})


def coverage_weight(n_domains: int, full_at: int) -> float:
    """Насыщающийся охват в [0, 1].

    n_domains=1 -> 0.0 (одно издание — охвата нет)
    n_domains=full_at -> 1.0
    дальше растёт, но обрезается единицей: разница между 40 и 80 изданиями
    уже не должна перевешивать всё остальное.
    """
    if full_at <= 1 or n_domains <= 1:
        return 0.0
    return min(1.0, math.log1p(n_domains - 1) / math.log1p(full_at - 1))


def _normalize_rows(embs):
    return embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-10)


def lexrank(embeddings, damping, max_iter, tol, domains=None):
    """LexRank-скоры (сумма = 1) по графу косинусных схожестей.

    domains (опц.): если задан, рёбра МЕЖДУ СТАТЬЯМИ ОДНОГО ДОМЕНА обнуляются.
    Без этого издание, опубликовавшее пять версий одного текста, образует
    плотную клику и накачивает само себя — ровно тот локальный шум, ради
    которого фактор и вводился.
    """
    E = np.asarray(embeddings, dtype=np.float32)
    n = E.shape[0]
    if n <= 1:
        return np.ones(n, dtype=np.float64)

    E = _normalize_rows(E)
    sim = (E @ E.T).astype(np.float64)
    np.fill_diagonal(sim, 0.0)
    np.clip(sim, 0.0, None, out=sim)

    if domains is not None:
        d = np.asarray(domains, dtype=object)
        same = (d[:, None] == d[None, :]) & (d[:, None] != "")
        sim[same] = 0.0

    row_sums = sim.sum(axis=1, keepdims=True)
    dangling = (row_sums.ravel() == 0.0)
    trans = sim / np.where(row_sums == 0.0, 1.0, row_sums)
    if dangling.any():
        # Узел без связей ни на кого не похож — раздаём его вес равномерно,
        # иначе он проглатывает скор и не возвращает его в граф.
        trans[dangling, :] = 1.0 / n

    scores = np.full(n, 1.0 / n, dtype=np.float64)
    teleport = (1.0 - damping) / n
    for _ in range(max_iter):
        nxt = teleport + damping * (trans.T @ scores)
        if np.abs(nxt - scores).sum() < tol:
            return nxt
        scores = nxt
    return scores


def minmax(v):
    """Приводит вектор к [0,1]; вырожденный случай -> 0.5 у всех."""
    v = np.asarray(v, dtype=np.float64)
    if v.size == 0:
        return v
    lo, hi = float(v.min()), float(v.max())
    if hi - lo <= 1e-12:
        return np.full_like(v, 0.5)
    return (v - lo) / (hi - lo)


def blend(lex, cov, ent, w_lex, w_cov, w_ent):
    """Свёртка трёх нормированных факторов в итоговую важность [0,1].

    Веса нормируются суммой, поэтому выключение фактора (вес 0) не требует
    пересчёта остальных вручную.
    """
    total = float(w_lex + w_cov + w_ent)
    if total <= 0:
        return np.asarray(lex, dtype=np.float64)
    return (w_lex * np.asarray(lex, dtype=np.float64)
            + w_cov * np.asarray(cov, dtype=np.float64)
            + w_ent * np.asarray(ent, dtype=np.float64)) / total


def freshness(age_days, half_life, floor):
    """Множитель свежести в [floor, 1]: половина веса за каждые half_life суток.

    Пол нужен, чтобы затухание не превращалось в удаление: сюжет, о котором
    написали сорок изданий, через две недели должен стоять ниже сегодняшних,
    но выше одиночной заметки — а не ниже всего на свете. half_life=0
    выключает затухание целиком.
    """
    if half_life <= 0:
        return 1.0
    return floor + (1.0 - floor) * 0.5 ** (max(0.0, float(age_days)) / half_life)


def body_emb(body_blob, title_blob):
    """Эмбеддинг ТЕЛА статьи; пока не досчитан — откат на заголовочный.

    Откат нужен на переходный период (см. pipeline.embed_bodies): у статей,
    собранных до появления колонки, тела ещё нет, и ронять из-за этого расчёт
    важности незачем.
    """
    return np.frombuffer(body_blob or title_blob, np.float32)


def structural(body_embs, urls, entity_cells, ent_df, ages=None):
    """Важность сюжетов ОДНОЙ страны в [0,1] — свёртка трёх факторов выше.

    Единственная точка, где считается важность: её зовут и дайджест, и фид
    «все статьи», и витрина. Раньше формула жила только в дайджесте, а фид с
    витриной ранжировали по оценке LLM — то есть по гейту релевантности,
    который для ранжирования не годится (см. шапку модуля).

    body_embs    — эмбеддинг ТЕЛА представителя каждого сюжета, (n, dim)
    urls         — urls[i]: url всех статей сюжета i (охват считается по ним)
    entity_cells — entity_cells[i]: ячейки entities всех статей сюжета i
    ent_df       — док-частота субъектов по окну этой страны (entities.document_freq);
                   пустая/незначимая -> фактор субъектов выключается сам
    ages         — ages[i]: возраст сюжета в сутках (None -> без затухания)

    Веса берутся из settings.IMPORTANCE_W_*, любой в 0 выключает фактор.
    """
    import entities
    import settings

    n = len(urls)
    if n == 0:
        return np.zeros(0, dtype=np.float64)

    # Граф по ВСЕМ сюжетам страны, без отсечки: замер 27.07 — все 89 стран за
    # 1.6 с, Китай (356 сюжетов) 0.47 с. Матрица n*n живёт внутри одного
    # вызова и умирает вместе с ним, страны считаются по очереди.
    lex = minmax(lexrank(np.vstack(body_embs),
                         settings.LEXRANK_DAMPING, settings.LEXRANK_MAX_ITER,
                         settings.LEXRANK_TOL,
                         domains=[domain(u[0]) if u else "" for u in urls]))

    cov = np.array([coverage_weight(distinct_domains(u), settings.COVERAGE_FULL_AT)
                    for u in urls], dtype=np.float64)

    # Словарь заметных субъектов самонастраивается по окну страны. Пока порога
    # не перешагнул никто, фактор выключается целиком — иначе он равномерно
    # опустил бы всю малую страну.
    if ent_df and any(v >= settings.ENTITY_MIN_DF for v in ent_df.values()):
        ent = np.array([max((entities.weight(
            entities.prominent(cell, ent_df, settings.ENTITY_MIN_DF),
            settings.ENTITY_FULL_AT) for cell in cells), default=0.0)
            for cells in entity_cells], dtype=np.float64)
    else:
        ent = np.zeros(n, dtype=np.float64)

    out = blend(lex, cov, ent, settings.IMPORTANCE_W_LEXRANK,
                settings.IMPORTANCE_W_COVERAGE, settings.IMPORTANCE_W_ENTITY)
    if ages is None:
        return out
    # Домножением, а не четвёртым слагаемым: свежесть — не заслуга сюжета, а
    # срок годности того, что у него уже есть. Слагаемым она поднимала бы
    # сегодняшнюю пустую заметку над вчерашней настоящей новостью.
    return out * np.array([freshness(a, settings.IMPORTANCE_HALF_LIFE_DAYS,
                                     settings.IMPORTANCE_AGE_FLOOR) for a in ages],
                          dtype=np.float64)


def _selfcheck():
    assert domain("https://www.example.com/a/b?x=1") == "example.com"
    assert domain("http://m.gazeta.ru/news") == "gazeta.ru"
    assert domain("https://sub.example.co.uk:8443/x") == "sub.example.co.uk"
    assert domain("не-url") == ""
    assert distinct_domains(["https://a.com/1", "https://www.a.com/2",
                             "https://b.com/1"]) == 2

    # Охват: насыщается, но не упирается в потолок раньше времени.
    assert coverage_weight(1, 12) == 0.0
    assert coverage_weight(12, 12) == 1.0
    assert coverage_weight(40, 12) == 1.0
    c2, c4, c8 = (coverage_weight(k, 12) for k in (2, 4, 8))
    assert c2 < c4 < c8
    assert (c4 - c2) > (c8 - c4)          # прирост убывает — это и есть насыщение

    # LexRank: центральная статья весомее периферийной.
    core = np.array([1.0, 0.0], np.float32)
    embs = np.array([core, core * 0.99 + [0, 0.01], core, [0.0, 1.0]], np.float32)
    s = lexrank(embs, 0.85, 100, 1e-6)
    assert s[3] < s[0], s
    assert abs(s.sum() - 1.0) < 1e-6

    # Подавление внутридоменных рёбер: три клона с одного домена не должны
    # накачивать друг друга сильнее, чем при честном подсчёте.
    doms = ["a.com", "a.com", "a.com", "b.com"]
    s_dom = lexrank(embs, 0.85, 100, 1e-6, domains=doms)
    assert s_dom[0] < s[0], (s_dom, s)

    assert np.allclose(minmax(np.array([5.0, 5.0])), 0.5)
    assert np.allclose(minmax(np.array([0.0, 1.0, 2.0])), [0.0, 0.5, 1.0])
    b = blend([1.0, 0.0], [0.0, 1.0], [0.0, 0.0], 1.0, 1.0, 0.0)
    assert np.allclose(b, [0.5, 0.5])
    b0 = blend([1.0, 0.0], [0.0, 1.0], [0.0, 0.0], 1.0, 0.0, 0.0)
    assert np.allclose(b0, [1.0, 0.0])

    # Затухание: свежее не трогает, старое опускает, но не в ноль.
    hl, floor = 4.0, 0.35
    assert freshness(0, hl, floor) == 1.0
    assert abs(freshness(hl, hl, floor) - (floor + (1 - floor) / 2)) < 1e-12
    assert freshness(1, hl, floor) > 0.85       # сутки — окно фида, почти без потерь
    assert floor < freshness(14, hl, floor) < 0.5
    assert freshness(999, hl, floor) == floor   # упирается в пол, а не в ноль
    assert freshness(3, 0, floor) == 1.0        # выключено
    # Затухание перестраивает порядок только при близких весах: крупный старый
    # сюжет остаётся выше свежей мелочи.
    big_old, small_new = 0.90 * freshness(14, hl, floor), 0.20 * freshness(0, hl, floor)
    assert big_old > small_new, (big_old, small_new)
    print("importance: ok")


if __name__ == "__main__":
    _selfcheck()
