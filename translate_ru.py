# -*- coding: utf-8 -*-
"""translate_ru.py — второе плечо перевода: английский → РУССКИЙ локальной
моделью opus-mt-tc-big-en-zle через CTranslate2 (int8).

Маршрут в системе ровно один и состоит из двух плеч:

    любой язык --Google--> английский --эта модель--> русский

Первое плечо делает translate.translate_missing (Google, кэш в
articles.title_en/text_en), второе — здесь. Запасных путей нет намеренно:
раньше их было четыре (прямые пары opus X→ru, пивоты, M2M100, MADLAD), и
каждый из них при живом Google только портил текст. Живые примеры с боевой
базы: opus-mt-ko-ru из «Юнгgiwon, кузница научных кадров» делал «ОДНАЖДЫ
НАУКА В ИСТОРИИ», t5-enruzh из «в первый вечер после листинга Changxin
заводские цеха сияли огнями» — «Произошла первая вечерняя ярмарка с
освещением на завод», m2m100 оставлял в русском тексте латиницу кусками.
Нет английского — статья ждёт следующего прогона, а не идёт кривым путём.

Почему CTranslate2 int8, а не torch: 13.9x на этом CPU (66.5 -> 4.8 c на
статью, замерено), качество на сверке совпадает дословно. У процессора нет
AVX, torch тут вообще падает по SIGILL.

Почему tc-big (230 млн), а не opus-mt-en-ru (77 млн): «38 погибших в
автобусном столкновении» против «38 убитых в столкновениях с автобусами»,
«христианско-мусульманских дебатов» против «христианских и мусульманских»,
и он не выдумывает «Цтге» из «Ctg».
"""
import logging
import os
import re
import threading

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
#   REP_PEN — штраф за повтор уже сказанного. Прямо бьёт по зацикливанию.
#   NO_REPEAT — запрет повторять n-грамму дословно.
BEAM = int(os.environ.get("TRANSLATE_BEAM", "4"))
REP_PEN = float(os.environ.get("TRANSLATE_REP_PENALTY", "1.1"))
NO_REPEAT = int(os.environ.get("TRANSLATE_NO_REPEAT_NGRAM", "4"))
_GEN = dict(beam_size=BEAM, repetition_penalty=REP_PEN,
            no_repeat_ngram_size=NO_REPEAT, max_decoding_length=256)

# settings.py уже выставил HF_HOME на кэш gdelt_rss, поэтому setdefault тут
# бесполезен: каталог моделей перевода передаём явным cache_dir.
_HUB = os.path.join(HF_HOME, "hub")

CT2_NAME = os.environ.get("TRANSLATE_CT2", "tcbig-en-ru")
HF_NAME = "Helsinki-NLP/opus-mt-tc-big-en-zle"

# zle — это ВОСТОЧНОСЛАВЯНСКИЕ: русский, украинский, белорусский. У
# многоцелевых Marian язык задаётся токеном в начале исходной строки, и без
# него модель выбирает сама. На «Ukraine's grain exports» она отвечала
# по-русски, но полагаться на угадывание незачем: токен есть в словаре
# (id 25502, не unk) и стоит один лишний символ на предложение.
TGT = ">>rus<< "

# Уже по-русски — не трогаем.
SKIP_LANGS = ("ru",)

_HANGUL = re.compile(r"[가-힯]")
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[一-鿿]")
_CYR = re.compile(r"[Ѐ-ӿ]")

_SPLIT = re.compile(r'(?<=[.!?。！？；;])\s*')
_engine = None
_lock = threading.RLock()


def detect_src(lang, text=""):
    """Код исходного языка. Письменность приоритетнее метки language.

    В базе 1296 статей из 1811 с меткой ko не содержат ни одного символа
    хангыля — это китайский, langdetect их перепутал. Маршрут теперь один на
    все языки, но метка всё ещё решает два вопроса: пропускать ли статью как
    уже русскую и нужно ли ей английское плечо.
    """
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


def split_sentences(text):
    t = re.sub(r"\s+", " ", (text or "")).strip()[:BODY_CHARS]
    return [s.strip() for s in _SPLIT.split(t) if len(s.strip()) > 1][:MAX_SENT]


class _Marian:
    def __init__(self):
        import ctranslate2
        from transformers import MarianTokenizer
        self.tok = MarianTokenizer.from_pretrained(HF_NAME, local_files_only=True,
                                                   cache_dir=_HUB)
        self.tr = ctranslate2.Translator(os.path.join(CT2_DIR, CT2_NAME), device="cpu",
                                         compute_type="int8", inter_threads=1,
                                         intra_threads=THREADS)

    def __call__(self, sents):
        src = [self.tok.convert_ids_to_tokens(
            self.tok.encode(TGT + s, truncation=True, max_length=256)) for s in sents]
        res = self.tr.translate_batch(src, max_batch_size=BATCH, **_GEN)
        return [self.tok.decode(self.tok.convert_tokens_to_ids(r.hypotheses[0]),
                                skip_special_tokens=True) for r in res]


def engine():
    global _engine
    with _lock:
        if _engine is None:
            log.info("translate_ru: гружу %s", CT2_NAME)
            _engine = _Marian()
        return _engine


def unload_all():
    """Освободить модель. Зовётся между пачками: 230 млн параметров в int8 это
    порядка 300 МБ, и держать их между запусками воркера незачем."""
    global _engine
    with _lock:
        _engine = None


def available():
    return os.path.isdir(os.path.join(CT2_DIR, CT2_NAME))


def translate_sentences(sents):
    """Английские предложения → русские. Вернуть (переводы, имя маршрута)."""
    if not sents:
        return [], None
    if not available():
        log.error("translate_ru: модель %s не установлена", CT2_NAME)
        return None, None
    return engine()(sents), CT2_NAME


def translate_doc(title, text):
    """Заголовок и тело АНГЛИЙСКОГО текста одним проходом модели: батч из
    одного предложения тратит загрузку впустую.

    Словарь (glossary.py) подключён с двух сторон: keep-термины прячутся под
    маркеры ДО перевода, чтобы модель не сделала из DeepSeek «Дип Сикс», а
    ru-термины чинятся ПОСЛЕ, если латиница просочилась в вывод. Русский
    вывод поверх себя не переписывается — иначе поедут падежи."""
    import glossary

    t_prot, t_subs = glossary.protect(title or "")
    x_prot, x_subs = glossary.protect(text or "")
    t_sents = split_sentences(t_prot) or ([t_prot.strip()] if t_prot.strip() else [])
    x_sents = split_sentences(x_prot)
    out, route = translate_sentences(t_sents + x_sents)
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
        log.info("glossary: маркеры не пережили перевод, повтор без защиты")
        t2 = split_sentences(title or "") or ([(title or "").strip()] if title else [])
        x2 = split_sentences(text or "")
        out2, route = translate_sentences(t2 + x2)
        if out2 is not None:
            n2 = len(t2)
            t_ru = " ".join(out2[:n2]).strip()
            x_ru = " ".join(out2[n2:]).strip()
            route = (route or "") + "/noglossary"

    t_ru = glossary.fix_leftovers(t_ru)
    x_ru = glossary.fix_leftovers(x_ru)
    return (t_ru or None, x_ru or None, route)


def _selfcheck():
    """Проверяем то, что ломается молча: маршрутизацию по письменности и то,
    что модель на месте. Саму модель не поднимаем — это 300 МБ и полминуты."""
    assert detect_src("ko", "한국 제목") == "ko"
    assert detect_src("ko", "中国 标题") == "zh", "метка врёт, письменность главнее"
    assert detect_src("en", "Русский заголовок целиком") == "ru"
    assert detect_src("zh-tw") == "zh" and detect_src("en") == "en"
    assert detect_src(None) is None and detect_src("") is None
    assert split_sentences("Раз. Два! Три?") == ["Раз.", "Два!", "Три?"]
    assert split_sentences("") == [] and split_sentences(None) == []
    assert available(), "CT2-сборка %s не найдена в %s" % (CT2_NAME, CT2_DIR)
    print("translate_ru selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
