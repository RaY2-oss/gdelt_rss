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
DIRECT = {
    "en": ("marian", "opus-en-ru", "Helsinki-NLP/opus-mt-en-ru"),
    "ar": ("marian", "opus-ar-ru", "Helsinki-NLP/opus-mt-ar-ru"),
    "ko": ("marian", "opus-ko-ru", "Helsinki-NLP/opus-mt-ko-ru"),
    "zh": ("t5", "t5-enruzh", "utrobinmv/t5_translate_en_ru_zh_small_1024"),
}
PIVOT = {
    "tr": ("opus-tr-en", "Helsinki-NLP/opus-mt-tr-en"),
    "id": ("opus-id-en", "Helsinki-NLP/opus-mt-id-en"),
    "vi": ("opus-vi-en", "Helsinki-NLP/opus-mt-vi-en"),
}

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
        res = self.tr.translate_batch(src, max_batch_size=BATCH, beam_size=1,
                                      max_decoding_length=256)
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
        res = self.tr.translate_batch(src, max_batch_size=BATCH, beam_size=1,
                                      max_decoding_length=256)
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
        return lang in self.tok.lang_code_to_token

    def __call__(self, sents, src_lang):
        self.tok.src_lang = src_lang
        src = [self.tok.convert_ids_to_tokens(
            self.tok.encode(s, truncation=True, max_length=256)) for s in sents]
        pre = [[self.tok.lang_code_to_token["ru"]]] * len(src)
        res = self.tr.translate_batch(src, target_prefix=pre, max_batch_size=BATCH,
                                      beam_size=1, max_decoding_length=256)
        return [self.tok.decode(self.tok.convert_tokens_to_ids(r.hypotheses[0][1:]),
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
    if not m.supports(lang):
        log.warning("translate_ru: язык %r не поддержан ни одной моделью", lang)
        return None, None
    return m(sents, lang), "m2m100"


def translate_doc(title, text, lang):
    """Заголовок и тело одним проходом модели: они на одном языке, а батч
    из одного предложения тратит загрузку впустую."""
    t_sents = split_sentences(title) or ([title.strip()] if title else [])
    x_sents = split_sentences(text)
    out, route = translate_sentences(t_sents + x_sents, lang)
    if out is None:
        return None, None, None
    n = len(t_sents)
    return (" ".join(out[:n]).strip() or None,
            " ".join(out[n:]).strip() or None, route)
