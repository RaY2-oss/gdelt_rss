# -*- coding: utf-8 -*-
"""glossary.py — словарь имён собственных, общий для обоих проектов.

Зачем: локальные модели фонетически коверкают всё, чего не знают. Живые
примеры с боевой базы — "DeepSeek's $71 Billion Boom" -> "Дип Сикс 71
миллиард Бум", "in Ctg" -> "в Цтге". Словарь это лечит.

ГЛАВНОЕ ОГРАНИЧЕНИЕ, ради которого модуль устроен именно так: русский язык
флективный. Наивная замена ПОВЕРХ русского вывода ломает падежи — "статья о
Дональд Трамп" вместо "о Дональде Трампе". Поэтому здесь НИКОГДА не
подменяется уже переведённый русский текст. Работаем только с латинскими
вхождениями:

  keep  — до перевода прячем термин под маркер, после подставляем обратно
          латиницей. Модель не успевает его исковеркать. Падежей нет —
          латинский блок в русском тексте и так не склоняется.
  ru    — если латинский термин всё же просочился в вывод непереведённым,
          заменяем его на принятую русскую форму (именительный падеж).
          Грамматически это ровно та же ситуация, что и оставленная латиница,
          хуже не делаем.

Для LLM (см. /opt/digest/sunday_processor_mmr.py) постобработка НЕ ПРИМЕНЯЕТСЯ
вообще: она сломает и падежи, и уже прописанное там правило «организации
остаются латиницей». Туда словарь идёт текстом в промпт — as_prompt_block().
"""
import logging
import os
import re
import threading

log = logging.getLogger("gdelt_rss")

# Все файлы glossary*.tsv из каталога: базовый плюс тематические
# (glossary_region.tsv — Турция, Центральная Азия, Южный Кавказ). Разделение
# по файлам чисто для удобства правки, на выходе один общий список.
GLOSSARY_DIR = os.environ.get("GLOSSARY_DIR", "/opt/translate")
# Маркер для режима keep. Латиница в верхнем регистре с цифрой: остаётся
# внутри словаря SentencePiece и переживает перевод чаще, чем скобки-юникод.
_MARK = "ZQX%dQ"
_MARK_RE = re.compile(r"ZQX(\d+)Q")

_lock = threading.Lock()
_cache = None      # {нормализованная фраза: (замена, режим)}
_maxw = 1          # максимум слов во фразе словаря
_mtime = None

# Раньше на каждый термин компилировалась своя регулярка, и поиск стоил
# O(размер словаря) на каждый текст. С 1269 терминов это ещё терпимо, с
# 20+ тысячами (вся Азия и Африка) — нет.
#
# Теперь наоборот: словарь это хеш-таблица, а по тексту идём один раз,
# пробуя на каждой позиции фразы от самой длинной к самой короткой. Стоимость
# O(длина текста x максимум слов во фразе) и от размера словаря не зависит.
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def _norm(phrase):
    """Ключ индекса: слова в нижнем регистре через один пробел. Дефисы и
    двойные пробелы схлопываются, поэтому 'Nagorno-Karabakh' и
    'nagorno karabakh' попадают в одну ячейку."""
    return " ".join(t.group(0).lower() for t in _TOKEN.finditer(phrase))


def _files():
    import glob
    return sorted(glob.glob(os.path.join(GLOSSARY_DIR, "glossary*.tsv")))


def _load():
    """Читаем при первом обращении и перечитываем, если любой файл поменялся."""
    global _cache, _mtime, _maxw
    with _lock:
        files = _files()
        try:
            m = tuple(os.path.getmtime(f) for f in files)
        except OSError:
            return _cache if _cache is not None else {}
        if _cache is not None and m == _mtime:
            return _cache
        idx, maxw = {}, 1
        for path in files:
            try:
                with open(path, encoding="utf-8") as fh:
                    for ln in fh:
                        ln = ln.rstrip("\n")
                        if not ln.strip() or ln.startswith("#"):
                            continue
                        parts = ln.split("\t")
                        if len(parts) < 3:
                            continue
                        src, dst, mode = (parts[0].strip(), parts[1].strip(),
                                          parts[2].strip())
                        key = _norm(src)
                        # Первый файл по алфавиту побеждает: ручные правки
                        # (glossary.tsv, glossary_region.tsv) перебивают
                        # авто-генерацию (glossary_z_*.tsv).
                        if not key or not dst or mode not in ("keep", "ru") or key in idx:
                            continue
                        idx[key] = (dst, mode)
                        maxw = max(maxw, key.count(" ") + 1)
            except OSError as exc:
                log.warning("glossary: не читается %s: %s", path, exc)
        _cache, _mtime, _maxw = idx, m, maxw
        log.info("glossary: %d терминов из %d файлов (до %d слов во фразе)",
                 len(idx), len(files), maxw)
        return _cache


def find_all(text):
    """Непересекающиеся совпадения словаря в тексте, длинные в приоритете.
    Возвращает [(начало, конец, ключ, замена, режим)]."""
    idx = _load()
    if not text or not idx:
        return []
    toks = [(mm.start(), mm.end(), mm.group(0).lower())
            for mm in _TOKEN.finditer(text)]
    out, i = [], 0
    while i < len(toks):
        hit = None
        for n in range(min(_maxw, len(toks) - i), 0, -1):
            key = " ".join(t[2] for t in toks[i:i + n])
            ent = idx.get(key)
            if ent:
                hit = (toks[i][0], toks[i + n - 1][1], key, ent[0], ent[1], n)
                break
        if hit:
            out.append(hit[:5])
            i += hit[5]
        else:
            i += 1
    return out


def lookup(phrase):
    """Словарная форма фразы целиком или None. Нужна тем, кто уже знает имя и
    хочет его каноническое написание, — в отличие от find_all, который ищет
    имена внутри текста."""
    return (_load().get(_norm(phrase)) or (None,))[0]


def extract_entities(text, limit=40):
    """Сущности, найденные в тексте. Годится и для колонки entities: в digest
    она пустая, GDELT их туда не доносит."""
    seen, out = set(), []
    for _s, _e, key, _dst, _mode in find_all(text or ""):
        if key not in seen:
            seen.add(key)
            out.append(key)
            if len(out) >= limit:
                break
    return out


def protect(text):
    """Прячем keep-термины под маркеры. Возвращает (текст, {маркер: оригинал})."""
    if not text:
        return text, {}
    hits = [h for h in find_all(text) if h[4] == "keep"]
    if not hits:
        return text, {}
    subs, parts, prev = {}, [], 0
    for i, (a, b, _key, dst, _mode) in enumerate(hits, 1):
        mark = _MARK % i
        subs[mark] = dst
        parts.append(text[prev:a]); parts.append(mark)
        prev = b
    parts.append(text[prev:])
    return "".join(parts), subs


def restore(text, subs):
    """Возвращаем оригиналы на место маркеров.

    Если модель потеряла или размножила маркеры, счётчик не сойдётся — тогда
    честнее отдать сигнал наверх, чем оставить в тексте мусор вида ZQX3Q.
    Вызывающий код в этом случае берёт неохраняемый перевод."""
    if not subs:
        return text, True
    ok = True
    found = set(_MARK_RE.findall(text or ""))
    if len(found) != len(subs):
        ok = False
    for mark, orig in subs.items():
        text = text.replace(mark, orig)
    # Подчищаем то, что модель испортила до неузнаваемости.
    if _MARK_RE.search(text or ""):
        text = _MARK_RE.sub("", text)
        ok = False
    return text, ok


def fix_leftovers(text):
    """Латинские термины, просочившиеся в русский вывод, -> принятая форма,
    СРАЗУ В НУЖНОМ ПАДЕЖЕ (см. morph.py).

    Идём справа налево: замена меняет длину строки, а позиции совпадений
    посчитаны по исходному тексту. Правая часть при этом не сдвигается.
    """
    if not text:
        return text
    hits = [h for h in find_all(text) if h[4] == "ru"]
    if not hits:
        return text
    try:
        import morph
    except Exception:
        morph = None
    for a, b, _key, dst, _mode in reversed(hits):
        form = dst
        if morph is not None:
            case = morph.case_before(text, a)
            if case:
                form = morph.inflect_phrase(dst, case)
        text = text[:a] + form + text[b:]
    return text


def as_prompt_block(text=None, entities=None, limit=60):
    """Блок для системного промпта LLM.

    Постобработку к выводу LLM применять НЕЛЬЗЯ — она ломает падежи; модель
    сама поставит нужную форму, если дать ей список. Словарь большой, а
    промпт платный по токенам, поэтому при переданном text в блок попадают
    только термины, реально встретившиеся в статье.
    """
    idx = _load()
    if not idx:
        return ""
    lines, seen = [], set()
    if text:
        for _a, _b, key, dst, mode in find_all(text):
            if key in seen:
                continue
            seen.add(key)
            lines.append('  "%s" -> %s' % (key, dst if mode == "ru"
                                           else "%s (keep Latin script)" % dst))
            if len(lines) >= limit:
                break
    else:
        want = None
        if entities:
            want = {_norm(e) for e in entities.split(";") if e.strip()}
        for key, (dst, mode) in idx.items():
            if want is not None and key not in want:
                continue
            lines.append('  "%s" -> %s' % (key, dst if mode == "ru"
                                           else "%s (keep Latin script)" % dst))
            if len(lines) >= limit:
                break
    if not lines:
        return ""
    return ("Established renderings — use exactly these forms, inflecting them "
            "for Russian grammar as needed:\n" + "\n".join(lines))
