# -*- coding: utf-8 -*-
"""translate_ru.py — перевод заголовка и текста на РУССКИЙ локальными моделями
через CTranslate2 (int8), с маршрутизацией по языку.

Почему так, а не Google (см. прежний translate.py) и не одна большая модель:

1. CTranslate2 int8 против torch fp32 на этом CPU — 13.9x (66.5 -> 4.8 c на
   статью, замерено). Качество совпадает: на сверке предложения выходят
   дословно одинаковыми. Без этого полнотекстовый перевод здесь нереален.

2. Маршрутизация по языку. Мелкие Opus/T5 (77-111 млн параметров) быстрее
   универсальной M2M100 (418 млн) в 1.5-6 раз на КАЖДОМ проверенном языке:
       en  opus 3.54 c  против m2m 16.61
       ar  opus 3.83    против 23.84
       zh  t5   9.82    против 40.97
       tr  пивот 6.12   против 22.16
       vi  пивот 7.60   против 25.62
       id  пивот 9.41   против 13.92

3. Пивот через английский там, где прямой пары в русский нет. Кроме скорости
   он спасает от галлюцинаций: M2M100 на турецком воспроизводимо выдавал текст
   про «два законопроекта в Латвии» на статью о наборе преподавателей в
   университет Коджаэли. Английский промежуточный, наружу не идёт.

4. Язык берётся ПО ПИСЬМЕННОСТИ, а не из колонки language. В базе 1296 статей
   из 1811 с меткой ko не содержат ни одного символа хангыля — это китайский,
   langdetect их перепутал. Маршрутизация по метке слала бы их в корейскую
   модель. Скрипт — жёсткий гейт поверх метки.
"""
import logging
import os
import re
import threading

import settings

log = logging.getLogger("gdelt_rss")

CT2_DIR = os.environ.get("CT2_DIR", "/opt/translate/ct2")
HF_HOME = os.environ.get("TRANSLATE_HF_HOME", "/opt/translate/models")
THREADS = int(os.environ.get("TRANSLATE_THREADS", "3"))
MAX_SENT = int(os.environ.get("TRANSLATE_MAX_SENT", "60"))
BODY_CHARS = int(os.environ.get("TRANSLATE_BODY_CHARS", "6000"))
BATCH = int(os.environ.get("TRANSLATE_BATCH", "16"))

# Качество важнее скорости (решение 2026-07). Три ручки:
#   BEAM — лучевой поиск вместо жадного. Модель на каждом шаге держит N
#          гипотез и выбирает лучшую по всей фразе, а не по одному слову
#          вперёд. Главное лекарство от обрывов и отсебятины.
#   REP_PEN — штраф за повтор уже сказанного. Прямо бьёт по зацикливанию,
#          на котором M2M100 уходил в потолок декодирования (11.66 c на
#          турецкий заголовок вместо секунды).
#   NO_REPEAT — запрет повторять n-грамму дословно.
BEAM = int(os.environ.get("TRANSLATE_BEAM", "4"))
REP_PEN = float(os.environ.get("TRANSLATE_REP_PENALTY", "1.1"))
NO_REPEAT = int(os.environ.get("TRANSLATE_NO_REPEAT_NGRAM", "4"))
_GEN = dict(beam_size=BEAM, repetition_penalty=REP_PEN,
            no_repeat_ngram_size=NO_REPEAT, max_decoding_length=256)
# settings.py уже выставил HF_HOME на кэш gdelt_rss, поэтому setdefault тут
# бесполезен: каталог моделей перевода передаём явным cache_dir.
_HUB = os.path.join(HF_HOME, "hub")

# Уже по-русски — не трогаем. Английский теперь ПЕРЕВОДИМ (раньше был целью).
SKIP_LANGS = ("ru",)

_HANGUL = re.compile(r"[\uac00-\ud7af]")
_KANA = re.compile(r"[\u3040-\u30ff]")
_HAN = re.compile(r"[\u4e00-\u9fff]")
_CYR = re.compile(r"[\u0400-\u04ff]")


def detect_src(lang, text=""):
    """Код исходного языка для маршрутизации. Письменность приоритетнее метки."""
    if text:
        if _HANGUL.search(text):
            return "ko"
        if _KANA.search(text):
            return "ja"
        if _HAN.search(text):
            return "zh"
        if _CYR.search(text) and len(_CYR.findall(text)) > len(text) * 0.3:
            return "ru"
    if not lang:
        return None
    lang = lang.lower()
    return {"zh-cn": "zh", "zh-tw": "zh", "iw": "he"}.get(lang, lang.split("-")[0])


# Маршруты: язык -> ("opus"|"t5"|"m2m"|"pivot", параметры).
# Порядок выбора — прямая мелкая модель, потом пивот, потом M2M100.
# en идёт через tc-big (230 млн) вместо opus-mt-en-ru (77 млн). Он в 1.6 раза
# медленнее (13.6 против 8.46 c на статью), но заметно точнее на том же
# материале: "38 погибших в автобусном столкновении" против "38 убитых в
# столкновениях с автобусами", "христианско-мусульманских дебатов" против
# "христианских и мусульманских", и он не выдумывает "Цтге" из "Ctg".
# Английский — это и 24.7% базы, и второе плечо КАЖДОГО пивота, поэтому
# апгрейд одной строки чинит сразу en, tr, id, vi.
DIRECT = {
    "en": ("marian", "tcbig-en-ru", "Helsinki-NLP/opus-mt-tc-big-en-zle"),
    "ar": ("marian", "opus-ar-ru", "Helsinki-NLP/opus-mt-ar-ru"),
    "ko": ("marian", "opus-ko-ru", "Helsinki-NLP/opus-mt-ko-ru"),
    "zh": ("t5", "t5-enruzh", "utrobinmv/t5_translate_en_ru_zh_small_1024"),
}
PIVOT = {
    "tr": ("tcbig-tr-en", "Helsinki-NLP/opus-mt-tc-big-tr-en"),
    "id": ("opus-id-en", "Helsinki-NLP/opus-mt-id-en"),
    "vi": ("opus-vi-en", "Helsinki-NLP/opus-mt-vi-en"),
}

# Последний рубеж. M2M100 знает 100 языков и покрывает 99.7% базы, но телугу
# (51 статья) в этот список не входит — а завтра прилетит что-нибудь ещё.
# opus-mt-mul-en переводит с почти любого языка в английский, дальше обычное
# второе плечо в русский. 296 МБ, Apache-2.0.
#
# Не MADLAD-400-3B, хотя он объективно лучше и тоже Apache-2.0: в int8 это
# ~3 ГБ резидентной памяти, а cgroup воркера ограничен 2.5 ГБ на восьми
# гигабайтах, где параллельно крутится сам конвейер. Включается переменной
# MADLAD_CT2 (путь к готовой CT2-сборке) — тогда он встаёт перед mul-en.
FALLBACK = ("opus-mul-en", "Helsinki-NLP/opus-mt-mul-en")
MADLAD_CT2 = os.environ.get("MADLAD_CT2", os.path.join(CT2_DIR, "madlad-3b"))

# Языки M2M100 статикой: раньше проверка "знает ли он этот код" поднимала
# весь движок на 471 МБ, хотя ответ нужен ДО решения о загрузке.
M2M_LANGS = frozenset("""af am ar ast az ba be bg bn br bs ca ceb cs cy da de el en es
et fa ff fi fr fy ga gd gl gu ha he hi hr ht hu hy id ig ilo is it ja jv ka kk km kn ko
lb lg ln lo lt lv mg mk ml mn mr ms my ne nl no ns oc or pa pl ps pt ro ru sd si sk sl so
sq sr ss su sv sw ta th tl tn tr uk ur uz vi wo xh yi yo zh zu""".split())

_SPLIT = re.compile(r'(?<=[.!?。！？；;])\s*')
_engines = {}
_lock = threading.RLock()


def split_sentences(text):
    t = re.sub(r"\s+", " ", (text or "")).strip()[:BODY_CHARS]
    return [s.strip() for s in _SPLIT.split(t) if len(s.strip()) > 1][:MAX_SENT]


class _Marian:
    kind = "marian"

    def __init__(self, ct2_name, hf_name):
        import ctranslate2
        from transformers import MarianTokenizer
        self.tok = MarianTokenizer.from_pretrained(hf_name, local_files_only=True,
                                                   cache_dir=_HUB)
        self.tr = ctranslate2.Translator(os.path.join(CT2_DIR, ct2_name), device="cpu",
                                         compute_type="int8", inter_threads=1,
                                         intra_threads=THREADS)

    def __call__(self, sents):
        src = [self.tok.convert_ids_to_tokens(
            self.tok.encode(s, truncation=True, max_length=256)) for s in sents]
        res = self.tr.translate_batch(src, max_batch_size=BATCH, **_GEN)
        return [self.tok.decode(self.tok.convert_tokens_to_ids(r.hypotheses[0]),
                                skip_special_tokens=True) for r in res]


class _T5(_Marian):
    kind = "t5"

    def __init__(self, ct2_name, hf_name):
        import ctranslate2
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(hf_name, local_files_only=True,
                                                 cache_dir=_HUB)
        self.tr = ctranslate2.Translator(os.path.join(CT2_DIR, ct2_name), device="cpu",
                                         compute_type="int8", inter_threads=1,
                                         intra_threads=THREADS)

    def __call__(self, sents):
        src = [self.tok.convert_ids_to_tokens(
            self.tok.encode("translate to ru: " + s, truncation=True, max_length=256))
            for s in sents]
        res = self.tr.translate_batch(src, max_batch_size=BATCH, **_GEN)
        return [self.tok.decode(self.tok.convert_tokens_to_ids(r.hypotheses[0]),
                                skip_special_tokens=True) for r in res]


class _M2M:
    kind = "m2m"

    def __init__(self):
        import ctranslate2
        from transformers import M2M100Tokenizer
        self.tok = M2M100Tokenizer.from_pretrained("facebook/m2m100_418M",
                                                   local_files_only=True, cache_dir=_HUB)
        self.tr = ctranslate2.Translator(os.path.join(CT2_DIR, "m2m100"), device="cpu",
                                         compute_type="int8", inter_threads=1,
                                         intra_threads=THREADS)

    def supports(self, lang):
        return lang in M2M_LANGS

    def __call__(self, sents, src_lang):
        self.tok.src_lang = src_lang
        src = [self.tok.convert_ids_to_tokens(
            self.tok.encode(s, truncation=True, max_length=256)) for s in sents]
        pre = [[self.tok.lang_code_to_token["ru"]]] * len(src)
        res = self.tr.translate_batch(src, target_prefix=pre, max_batch_size=BATCH,
                                      **_GEN)
        return [self.tok.decode(self.tok.convert_tokens_to_ids(r.hypotheses[0][1:]),
                                skip_special_tokens=True) for r in res]


class _Madlad:
    """MADLAD-400-3B в готовой CT2-сборке int8. Целевой язык у него задаётся
    токеном <2ru> в НАЧАЛЕ ИСХОДНОГО текста, а не префиксом декодера."""
    kind = "madlad"

    def __init__(self, path):
        import ctranslate2
        from transformers import AutoTokenizer
        self.tok = AutoTokenizer.from_pretrained(path, local_files_only=True)
        self.tr = ctranslate2.Translator(path, device="cpu", compute_type="int8",
                                         inter_threads=1, intra_threads=THREADS)

    def __call__(self, sents):
        src = [self.tok.convert_ids_to_tokens(
            self.tok.encode("<2ru> " + s, truncation=True, max_length=256))
            for s in sents]
        res = self.tr.translate_batch(src, max_batch_size=4, **_GEN)
        return [self.tok.decode(self.tok.convert_tokens_to_ids(r.hypotheses[0]),
                                skip_special_tokens=True) for r in res]


def _engine(key, factory):
    with _lock:
        if key not in _engines:
            log.info("translate_ru: гружу %s", key)
            _engines[key] = factory()
        return _engines[key]


def unload_all():
    """Освободить модели. Очередь идёт группами по языку, между группами можно
    выгружать — иначе на 8 ГБ соберётся весь зоопарк разом."""
    with _lock:
        _engines.clear()


def translate_sentences(sents, lang):
    """Вернуть (переводы, имя_маршрута) или (None, None), если язык не покрыт."""
    if not sents:
        return [], None
    if lang in DIRECT:
        kind, ct2, hf = DIRECT[lang]
        cls = _T5 if kind == "t5" else _Marian
        return _engine(ct2, lambda: cls(ct2, hf))(sents), ct2
    if lang in PIVOT:
        ct2, hf = PIVOT[lang]
        en = _engine(ct2, lambda: _Marian(ct2, hf))(sents)
        ru = _engine("opus-en-ru", lambda: _Marian("opus-en-ru", DIRECT["en"][2]))(en)
        return ru, "%s+opus-en-ru" % ct2
    m = _engine("m2m100", _M2M)
    if m.supports(lang):
        return m(sents, lang), "m2m100"

    # Ни прямой пары, ни пивота, ни M2M100 — уходим на последний рубеж.
    log.info("translate_ru: %r не покрыт основными моделями, иду в фолбэк", lang)
    if MADLAD_CT2 and os.path.isdir(MADLAD_CT2):
        # Лучшая из доступных на редких языках и Apache-2.0, но ~3 ГБ в памяти
        # и заметно медленнее — поэтому только здесь, на 0.26% статей.
        try:
            return _engine("madlad", lambda: _Madlad(MADLAD_CT2))(sents), "madlad-3b"
        except Exception:
            log.exception("translate_ru: MADLAD упал, откатываюсь на mul-en")
            unload_all()
    ct2, hf = FALLBACK
    if not os.path.isdir(os.path.join(CT2_DIR, ct2)):
        log.warning("translate_ru: фолбэк %s не установлен", ct2)
        return None, None
    en = _engine(ct2, lambda: _Marian(ct2, hf))(sents)
    ru = _engine(DIRECT["en"][1],
                 lambda: _Marian(DIRECT["en"][1], DIRECT["en"][2]))(en)
    return ru, "%s+%s" % (ct2, DIRECT["en"][1])


def translate_doc(title, text, lang):
    """Заголовок и тело одним проходом модели: они на одном языке, а батч
    из одного предложения тратит загрузку впустую.

    Словарь (glossary.py) подключён с двух сторон: keep-термины прячутся под
    маркеры ДО перевода, чтобы модель не сделала из DeepSeek "Дип Сикс", а
    ru-термины чинятся ПОСЛЕ, если латиница просочилась в вывод. Русский
    вывод поверх себя не переписывается — иначе поедут падежи."""
    import glossary

    t_prot, t_subs = glossary.protect(title or "")
    x_prot, x_subs = glossary.protect(text or "")
    t_sents = split_sentences(t_prot) or ([t_prot.strip()] if t_prot.strip() else [])
    x_sents = split_sentences(x_prot)
    out, route = translate_sentences(t_sents + x_sents, lang)
    if out is None:
        return None, None, None
    n = len(t_sents)
    t_ru = " ".join(out[:n]).strip()
    x_ru = " ".join(out[n:]).strip()

    t_ru, ok_t = glossary.restore(t_ru, t_subs)
    x_ru, ok_x = glossary.restore(x_ru, x_subs)
    if not (ok_t and ok_x):
        # Маркеры не пережили перевод. Переводим ещё раз без защиты: лучше
        # исковерканное имя, чем дыра в тексте. Метку маршрута помечаем, чтобы
        # такие случаи можно было потом посчитать и подкрутить маркер.
        log.info("glossary: маркеры не пережили перевод (%s), повтор без защиты", lang)
        t2 = split_sentences(title or "") or ([(title or "").strip()] if title else [])
        x2 = split_sentences(text or "")
        out2, route = translate_sentences(t2 + x2, lang)
        if out2 is not None:
            n2 = len(t2)
            t_ru = " ".join(out2[:n2]).strip()
            x_ru = " ".join(out2[n2:]).strip()
            route = (route or "") + "/noglossary"

    t_ru = glossary.fix_leftovers(t_ru)
    x_ru = glossary.fix_leftovers(x_ru)
    return (t_ru or None, x_ru or None, route)
