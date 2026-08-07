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
_tokenizer = None
_lock = threading.RLock()

# Куски, которые прячем целиком, если внутри нашлась буква вне словаря модели.
# Границы — пробелы и знаки конца фразы: запятые и точки модель должна видеть,
# иначе поедет структура предложения, а вот дефис и апостроф внутри имени
# разрывать нельзя («Erdoğan's», «2020–2025»).
_CHUNK = re.compile(r"[^\s.,;:!?()\[\]«»\"]+")
_POSS = re.compile(r"['’]s$")
_charok = {}

# Какую долю маркеров позволено потерять, прежде чем считать защиту
# провалившейся. Не ноль: модель иногда сама сливает соседние имена («Niğde
# Ömer Halisdemir University» → «Университет Халисдемир»), и требовать все
# маркеры значило бы из-за одного пропавшего имени откатиться на перевод, где
# искалечены все девять.
MARK_LOSS = float(os.environ.get("TRANSLATE_MARK_LOSS", "0.34"))


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


def tokenizer():
    """Токенизатор отдельно от переводчика: он стоит мегабайт против трёхсот,
    а нужен ещё и до перевода — решить, какие слова модель не выговорит."""
    global _tokenizer
    with _lock:
        if _tokenizer is None:
            from transformers import MarianTokenizer
            _tokenizer = MarianTokenizer.from_pretrained(
                HF_NAME, local_files_only=True, cache_dir=_HUB)
        return _tokenizer


def _known(ch):
    """Переживёт ли символ токенизатор.

    Спрашиваем саму модель, а не держим список: словарь SentencePiece — часть
    весов, и при смене модели список бы протух молча.
    """
    ok = _charok.get(ch)
    if ok is None:
        tok = tokenizer()
        ok = ch in tok.decode(tok.encode(ch), skip_special_tokens=True)
        _charok[ch] = ok
    return ok


def _protect_unknown(text, subs):
    """Спрятать под маркеры куски с буквами, которых нет в словаре модели.

    Символы вне словаря SentencePiece выпадают МОЛЧА — не в <unk>, а в ничто.
    В словаре tc-big-en-zle нет всей латиницы с диакритикой, и в дайджест ехало
    «TENMAK Aratrma Burs» вместо «Araştırma Bursu», «Nide mer Halisdemir»
    вместо «Niğde Ömer Halisdemir», «YK» вместо «YÖK». Одними турецкими буквами
    дело не ограничивается: так же теряются é è ñ å ø æ ß ł ń, то есть половина
    европейских фамилий, и знаки ° – ₺.

    Вход у модели всегда английский (первое плечо — Google), поэтому кусок с
    диакритикой — это имя собственное, которое переводчик и так оставил как
    есть. Прятать его под маркер ничего не стоит, а обратно он приходит
    невредимым. Механизм тот же, что у словаря, и нумерация маркеров общая:
    glossary.restore разбирает и те и другие одним проходом.
    """
    if not text:
        return text
    hits = [m.span() for m in _CHUNK.finditer(text)
            if any(not c.isascii() and not _known(c) for c in m.group())]
    if not hits:
        return text
    import glossary
    parts, prev = [], 0
    for i, (a, b) in enumerate(hits, len(subs) + 1):
        mk = glossary.mark(i)
        # Английский притяжательный падеж срезаем вместе с ним самим. Слово
        # под маркером модель просклонять не может по определению, и «сказал
        # Erdoğan's» доезжало до читателя как есть. В русском такого клитика
        # нет, а «офис Erdoğan» модель строит сама.
        chunk = _POSS.sub("", text[a:b])
        subs[mk] = chunk
        parts.append(text[prev:a]); parts.append(mk)
        prev = b
    parts.append(text[prev:])
    return "".join(parts)


class _Marian:
    def __init__(self):
        import ctranslate2
        self.tok = tokenizer()
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
    вывод поверх себя не переписывается — иначе поедут падежи.

    Под те же маркеры уходит всё, чего нет в словаре модели по буквам, —
    см. _protect_unknown. Порядок важен: сначала словарь, потом буквы, иначе
    «Erdoğan» ушёл бы под маркер как незнакомое слово и словарная форма к нему
    уже не применилась бы."""
    import glossary

    t_prot, t_subs = glossary.protect(title or "")
    x_prot, x_subs = glossary.protect(text or "")
    t_prot = _protect_unknown(t_prot, t_subs)
    x_prot = _protect_unknown(x_prot, x_subs)
    t_sents = split_sentences(t_prot) or ([t_prot.strip()] if t_prot.strip() else [])
    x_sents = split_sentences(x_prot)
    out, route = translate_sentences(t_sents + x_sents)
    if out is None:
        return None, None, None
    n = len(t_sents)
    t_ru = " ".join(out[:n]).strip()
    x_ru = " ".join(out[n:]).strip()

    t_ru, ok_t = glossary.restore(t_ru, t_subs, MARK_LOSS)
    x_ru, ok_x = glossary.restore(x_ru, x_subs, MARK_LOSS)
    if not (ok_t and ok_x):
        # Защита рассыпалась целиком. Переводим ещё раз без неё: лучше
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
    что модель на месте. Переводчик не поднимаем — это 300 МБ и полминуты,
    а вот токенизатор поднимаем: он мегабайт, и именно он решает, какие буквы
    доедут до читателя."""
    assert detect_src("ko", "한국 제목") == "ko"
    assert detect_src("ko", "中国 标题") == "zh", "метка врёт, письменность главнее"
    assert detect_src("en", "Русский заголовок целиком") == "ru"
    assert detect_src("zh-tw") == "zh" and detect_src("en") == "en"
    assert detect_src(None) is None and detect_src("") is None
    assert split_sentences("Раз. Два! Три?") == ["Раз.", "Два!", "Три?"]
    assert split_sentences("") == [] and split_sentences(None) == []
    assert available(), "CT2-сборка %s не найдена в %s" % (CT2_NAME, CT2_DIR)

    # Ради чего всё: буквы вне словаря модели выпадают молча, и без защиты
    # «Araştırma» доезжает как «Aratrma». Проверяем на живом токенизаторе —
    # список выпадающих букв не выписан нигде, он часть весов.
    assert not _known("ş") and not _known("ğ") and not _known("ø")
    assert _known("i") and _known("я") and _known("—")
    subs = {}
    got = _protect_unknown("Ørsted and Erdoğan's plan for Niğde in 2020–2025", subs)
    assert "Ørsted" not in got and "Niğde" not in got, got
    assert "and" in got and "plan for" in got, "обычные слова прятать незачем: " + got
    assert sorted(subs.values()) == ["2020–2025", "Erdoğan", "Niğde", "Ørsted"], subs
    assert _protect_unknown("A plain English sentence.", {}) == "A plain English sentence."
    # Маркеры словаря сами по себе под защиту попасть не должны: иначе первый
    # же keep-термин утащил бы за собой второй маркер поверх первого.
    import glossary
    marked = "the " + glossary.mark(1) + " report"
    assert _protect_unknown(marked, {}) == marked
    print("translate_ru selfcheck ok")


if __name__ == "__main__":
    _selfcheck()
