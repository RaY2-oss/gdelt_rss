# -*- coding: utf-8 -*-
"""site.py — статический сайт rss.bhutyan.online поверх того же feeds.db.

Читалка (FreshRSS) остаётся на freshrss.bhutyan.online, здесь — витрина:
главная с лентой дня, 10 регионов, страница на страну. Никакого рантайма:
после часового прогона (run_score.sh) генерируется HTML в SITE_OUT, дальше
всё делает nginx.

Отбор, дедуп и ранжирование НЕ дублируют логику фида, а переиспользуют её
целиком: `feeds.cluster_rows` (e5-косинус + лексический мостик) и `feeds.rank`
(структурная важность), то же окно `WINDOW_HOURS`, тот же порог
`RELEVANCE_CUTOFF`. Разница ровно одна: фид отдаёт представителя сюжета, а
сайту нужен ещё и размер кластера (сколько изданий подхватило сюжет) —
поэтому `stories` держит членов кластера, а не только победителя.

В БД сборка пишет ровно одно: переводы своих представителей кластеров, в тот
же кэш `articles.title_en/text_en`, которым пользуются фид и дайджест.
"""
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

from jinja2 import Environment, FileSystemLoader, select_autoescape

import db
import settings
import translate
import entities
import entity_ru
import glossary
import importance as imp
import feeds
import overview
import topics
from feeds import _pubdate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "site_templates")
STATIC_DIR = os.path.join(BASE_DIR, "site_static")
OUT_DIR = os.environ.get("GDELT_SITE_OUT", "/var/www/rss_site")

SITE_TITLE = "Bhutyan.online"
SITE_TAGLINE = "Наука и технологии 89 стран Азии и Африки"
FEED_BASE = "https://rss.bhutyan.online"

# Окно витрины — весь архив, что живёт в БД (settings.ARCHIVE_DAYS), одно и то
# же на главной, регионе и стране. Раньше было суточное, а страна с парой
# заметок за сутки расширяла своё окно до недели — и лента главной вынужденно
# исключала такие страны, иначе разные окна мешались в одном списке.
# Теперь выбирать нечего: архив лежит на странице целиком, а нужные даты
# читатель отмечает сам (см. .days в _story.html и app.js).
# min — страховка: KEEP_HOURS больше ARCHIVE_DAYS на суточный запас, но если
# его когда-нибудь урежут, витрина не должна рисовать колонки за дни, которых
# в БД уже нет.
SITE_DAYS = min(settings.ARCHIVE_DAYS, settings.KEEP_HOURS // 24)
SITE_HOURS = SITE_DAYS * 24

# Сколько сюжетов попадает в саму разметку. Это НЕ глубина ленты: дальше
# листалка берёт сюжеты из search.json, где лежит весь корпус (см. pager в
# _story.html и app.js), так что читателю доступны все шесть тысяч. Сто —
# столько, сколько имеет смысл нести с собой: страница страны на ста пятидесяти
# карточках весила мегабайт с четвертью, и переход между документами всё это
# время держал уходящий кадр замёрзшим.
# Обзор в корпусе (search.json): на странице у сюжета до трёх предложений и
# 560 знаков, здесь вдвое меньше — файл грузится целиком на любое касание
# листалки, и каждая лишняя сотня знаков стоит полмегабайта на архив.
TAIL_OVER_LINES = 2
TAIL_OVER_CHARS = 300

HOME_LIMIT = 100       # сюжетов в разметке главной
REGION_LIMIT = 100     # сюжетов в разметке региона
COUNTRY_LIMIT = 100    # сюжетов в разметке страны

# Похожие сюжеты. Дедуп фида работает внутри страны — url лежит ровно под
# одной, — поэтому один и тот же запуск ракеты в индийской и японской подаче
# остаётся двумя сюжетами. Для ленты это правильно, а здесь ровно то, что и
# стоит показать: то же событие глазами соседа.
#
# Порог выбран по корпусу, а не на глаз: у e5 высокий пол сходства, лучший
# сосед сидит в узкой полосе (медиана 0.886, четверть корпуса выше 0.901).
# На 0.86 сосед находился у 93 % сюжетов, и это была одна отрасль, а не одно
# событие: «ИИ в налоговой Пакистана» ходило в паре с «законом о цифровом
# удостоверении». С 0.90 остаётся 27 % — зато это IPO CXMT в двух подачах и
# американские пошлины на поликремний глазами Китая и Тайваня. Блок, который
# есть у каждого четвёртого и всегда по делу, полезнее блока у каждого и
# наугад. Выше 0.93 начинаются почти дубли, но их и так мержит DEDUP_COSINE.
AKIN_TOP = 4           # сколько похожих держим у сюжета
AKIN_FLOOR = 0.90      # ниже — уже не «то же самое», а просто одна отрасль
ENTITY_TOP = 14        # субъектов в блоке «кто в новостях»
POPULAR_TOP = 12       # имён-подсказок под строкой поиска

# ── Гео-структура ───────────────────────────────────────────────────────────
# Порядок регионов = порядок в settings.COUNTRIES (там они размечены
# комментариями, но словарём не оформлены — нужны только здесь).
REGIONS = [
    ("south_asia", "Южная Азия",
     ["india", "pakistan", "bangladesh", "sri_lanka", "nepal", "bhutan",
      "maldives", "afghanistan"]),
    ("southeast_asia", "Юго-Восточная Азия",
     ["indonesia", "malaysia", "singapore", "thailand", "vietnam",
      "philippines", "myanmar", "cambodia", "laos", "brunei", "east_timor"]),
    ("east_asia", "Восточная Азия",
     ["china", "japan", "south_korea", "north_korea", "mongolia", "taiwan",
      "hong_kong", "macau"]),
    ("central_asia", "Центральная Азия",
     ["kazakhstan", "uzbekistan", "turkmenistan", "kyrgyzstan", "tajikistan"]),
    ("west_asia", "Западная Азия",
     ["turkey", "iran", "iraq", "saudi_arabia", "uae", "israel", "qatar",
      "kuwait", "oman", "bahrain", "jordan", "lebanon", "syria", "yemen"]),
    ("north_africa", "Северная Африка",
     ["egypt", "libya", "tunisia", "algeria", "morocco", "sudan"]),
    ("west_africa", "Западная Африка",
     ["nigeria", "ghana", "senegal", "ivory_coast", "mali", "burkina_faso",
      "niger", "guinea", "benin", "togo", "sierra_leone", "liberia",
      "mauritania", "gambia"]),
    ("east_africa", "Восточная Африка",
     ["kenya", "tanzania", "uganda", "ethiopia", "rwanda", "somalia",
      "south_sudan", "eritrea"]),
    ("central_africa", "Центральная Африка",
     ["cameroon", "dr_congo", "congo", "chad", "gabon", "angola"]),
    ("southern_africa", "Южная Африка",
     ["south_africa", "zimbabwe", "zambia", "mozambique", "botswana",
      "namibia", "madagascar", "malawi", "mauritius"]),
]

RU_COUNTRY = {
    "india": "Индия", "pakistan": "Пакистан", "bangladesh": "Бангладеш",
    "sri_lanka": "Шри-Ланка", "nepal": "Непал", "bhutan": "Бутан",
    "maldives": "Мальдивы", "afghanistan": "Афганистан",
    "indonesia": "Индонезия", "malaysia": "Малайзия", "singapore": "Сингапур",
    "thailand": "Таиланд", "vietnam": "Вьетнам", "philippines": "Филиппины",
    "myanmar": "Мьянма", "cambodia": "Камбоджа", "laos": "Лаос",
    "brunei": "Бруней", "east_timor": "Восточный Тимор",
    "china": "Китай", "japan": "Япония", "south_korea": "Южная Корея",
    "north_korea": "Северная Корея", "mongolia": "Монголия",
    "taiwan": "Тайвань", "hong_kong": "Гонконг", "macau": "Макао",
    "kazakhstan": "Казахстан", "uzbekistan": "Узбекистан",
    "turkmenistan": "Туркменистан", "kyrgyzstan": "Киргизия",
    "tajikistan": "Таджикистан",
    "turkey": "Турция", "iran": "Иран", "iraq": "Ирак",
    "saudi_arabia": "Саудовская Аравия", "uae": "ОАЭ", "israel": "Израиль",
    "qatar": "Катар", "kuwait": "Кувейт", "oman": "Оман", "bahrain": "Бахрейн",
    "jordan": "Иордания", "lebanon": "Ливан", "syria": "Сирия", "yemen": "Йемен",
    "egypt": "Египет", "libya": "Ливия", "tunisia": "Тунис",
    "algeria": "Алжир", "morocco": "Марокко", "sudan": "Судан",
    "nigeria": "Нигерия", "ghana": "Гана", "senegal": "Сенегал",
    "ivory_coast": "Кот-д’Ивуар", "mali": "Мали", "burkina_faso": "Буркина-Фасо",
    "niger": "Нигер", "guinea": "Гвинея", "benin": "Бенин", "togo": "Того",
    "sierra_leone": "Сьерра-Леоне", "liberia": "Либерия",
    "mauritania": "Мавритания", "gambia": "Гамбия",
    "kenya": "Кения", "tanzania": "Танзания", "uganda": "Уганда",
    "ethiopia": "Эфиопия", "rwanda": "Руанда", "somalia": "Сомали",
    "south_sudan": "Южный Судан", "eritrea": "Эритрея",
    "cameroon": "Камерун", "dr_congo": "ДР Конго", "congo": "Конго",
    "chad": "Чад", "gabon": "Габон", "angola": "Ангола",
    "south_africa": "ЮАР", "zimbabwe": "Зимбабве", "zambia": "Замбия",
    "mozambique": "Мозамбик", "botswana": "Ботсвана", "namibia": "Намибия",
    "madagascar": "Мадагаскар", "malawi": "Малави", "mauritius": "Маврикий",
}

RU_LANG = {
    "en": "англ.", "ru": "рус.", "zh-cn": "кит.", "zh-tw": "кит. трад.",
    "ko": "кор.", "ja": "яп.", "id": "индонез.", "ms": "малайск.",
    "tr": "тур.", "ar": "араб.", "vi": "вьетн.", "hi": "хинди",
    "fa": "перс.", "th": "тайск.", "he": "иврит", "ur": "урду",
    "bn": "бенг.", "ta": "тамильск.", "fr": "франц.", "pt": "португ.",
    "es": "исп.", "sw": "суахили", "ne": "непальск.", "si": "сингальск.",
    "mn": "монг.", "km": "кхмерск.", "uk": "укр.", "kk": "казахск.",
    "uz": "узбекск.", "az": "азерб.", "my": "бирманск.", "am": "амхарск.",
}

# Порог → ступень шкалы важности (5 ступеней, ОДИН тон светлый→тёмный, см.
# --sig-1..5 в style.css). Важность — величина, а не категория, поэтому
# шкала последовательная и одноцветная; радуга по величине читается неверно.
TIERS = (30, 50, 70, 85)

_SENT_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")
_WS = re.compile(r"\s+")
_ALNUM = re.compile(r"[^0-9a-zа-яё]+")
# хвост «… - The Zimbabwe Mail» / «… | News.az»
_TAIL = re.compile(r"\s*[|\-–—:·»]\s*([^|\-–—:·»]{2,40})\s*$")
_DUP_PROBE = 40   # длина зонда для поиска повторённого заголовка

# Перевод — работа фида (feeds.build_country переводит всё, что в него попало);
# здесь только добор по своим представителям, которых фид не покрыл. Два
# потолка: на страну — чтобы одна крупная не съела прогон целиком, и общий по
# времени — потому что цена статьи это сетевой round-trip, а сборка сидит в том
# же часовом прогоне, что и оценка. Недобранное подберёт следующий прогон.
TRANSLATE_BUDGET = 12
TRANSLATE_BUDGET_S = 120
_translate_until = 0.0

# Граница переключателя «Всё / Только важное» на шкале структурной важности
# (0-100). Не порог отбора: в ленте лежит всё, что прошло гейт релевантности,
# это фильтр на клиенте. Прежде тут стоял MIN_IMPORTANCE — порог по оценке
# LLM, которой больше нет.
# 50 по замеру распределения: медиана шкалы 38, порог 40 оставлял бы почти
# половину ленты, 50 оставляет верхние ~19 %.
IMPORTANT_AT = 50


_REACH = None


def reach(conn):
    """Вес издания — сколько статей оно дало за окно витрины.

    Готового рейтинга авторитетности взять негде: списки вроде Alexa платные,
    а индийские, нигерийские и индонезийские издания, которых тут половина,
    в них всё равно не входят. Зато есть собственный корпус: редакция, давшая
    за две недели триста разборов по нашим темам, — большая, давшая один —
    перепечатка агентства. Мера грубая, зато считается из того, что уже лежит
    в базе, и не устаревает вместе с чужим рейтингом.
    """
    global _REACH
    if _REACH is None:
        _REACH = {}
        if conn is None:            # самопроверка зовёт stories() без базы
            return _REACH
        for (url,) in conn.execute(
                "SELECT url FROM articles WHERE fetched_at > datetime('now', ?)",
                (f"-{SITE_HOURS} hours",)):
            d = imp.domain(url)
            if d:
                _REACH[d] = _REACH.get(d, 0) + 1
    return _REACH


def source_rank(text, domain, reach_map):
    """Место издания в списке источников: чем больше, тем выше.

    Две величины, обе из того, что уже есть. Детальность — длина разбора:
    двадцать тысяч знаков против пятисот это не оттенок, а разница между
    репортажем с места и заметкой «как сообщает агентство». Весомость — тот
    самый reach выше.

    Обе берутся логарифмом. Между 500 и 5000 знаками разница огромная, между
    20 000 и 25 000 её уже нет — без логарифма один случайный лонгрид с
    комментариями читателей встал бы выше нормального репортажа. Детальность
    весит больше: она про эту конкретную статью, а вес издания — про издание
    вообще, и мелкое издание с подробным разбором на месте событий полезнее
    крупного с четырьмя абзацами перепечатки.
    """
    detail = min(1.0, math.log1p(len(text or "")) / math.log1p(6000))
    weight = min(1.0, math.log1p(reach_map.get(domain, 1)) / math.log1p(300))
    return 0.62 * detail + 0.38 * weight


# Отбор заголовка сюжета. Цифры — то, чем один заголовок лучше другого;
# трогать их значит менять, какая строка встанет в ленту.
TITLE_MIN = 28         # короче — не заголовок, а рубрика: «Дайджест», «Наука»
TITLE_MAX = 170        # длиннее — это уже подводка, вставшая в поле <title>
TITLE_SWEET = (50, 112)


def _title_flaws(s):
    """Сколько снять с заголовка за то, за что цепляется глаз.

    Всё перечисленное — не вкусовщина, а то, что регулярно приезжает в поле
    <title> чужих страниц и одинаково плохо читается в ленте.
    """
    bad = 0.0
    letters = [c for c in s if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.55:
        bad += 0.30                       # КРИЧАЩИЙ ЗАГОЛОВОК ЦЕЛИКОМ
    if s.rstrip().endswith(("...", "…")):
        bad += 0.22                       # обрезан на середине мысли
    if s.count("|") or s.count(" - ") > 1:
        bad += 0.12                       # хвост рубрикатора издания
    if re.search(r"\b(видео|фото|подкаст|галерея|онлайн|прямая трансляция)\b",
                 s, re.I):
        bad += 0.15                       # это подпись к формату, а не событие
    if s.endswith("?"):
        bad += 0.08                       # вопрос вместо новости
    if not re.search(r"[а-яёa-z]", s, re.I):
        bad += 0.5                        # одни цифры и знаки
    return bad


def pick_title(cands, vecs):
    """Заголовок сюжета — лучший из заголовков его источников.

    Раньше в ленту шёл заголовок статьи-победителя, а она выигрывала отбор по
    читаемости и дате — вовсе не потому, что удачно называлась. Отсюда и
    брались строки вроде «Лаборатория встречает реальный мир на китайском
    роботе», при том что соседнее издание в том же сюжете называлось внятно.

    Отбор, а не пересказ, — по той же причине, что и в обзоре (см. overview):
    генеративная модель на этом железе переворачивает факты примерно в каждом
    пятом случае, а заголовок врать не имеет права вовсе. Побеждает тот, что
    ближе других к смысловому центру сюжета — то есть говорит об общем, а не
    о частности одного издания, — и меньше других цепляется за глаз.

    `cands` — готовые к показу строки, `vecs` — их эмбеддинги (или None).
    Возвращает индекс победителя; при пустом входе — None.
    """
    live = [i for i, s in enumerate(cands) if s and TITLE_MIN <= len(s) <= TITLE_MAX]
    if not live:
        # Все кандидаты за границами меры — берём первый непустой, чтобы
        # строка не осталась без заголовка вовсе.
        return next((i for i, s in enumerate(cands) if s), None)
    if len(live) == 1:
        return live[0]

    import numpy as np
    sim = {i: 0.0 for i in live}
    have = [i for i in live if vecs[i] is not None and vecs[i].size]
    if len(have) > 1:
        m = np.asarray([vecs[i] for i in have], np.float32)
        m /= np.linalg.norm(m, axis=1, keepdims=True).clip(1e-6)
        mid = m.mean(axis=0)
        mid /= max(float(np.linalg.norm(mid)), 1e-6)
        for i, v in zip(have, m @ mid):
            sim[i] = float(v)

    def score(i):
        s = cands[i]
        fit = 0.16 if TITLE_SWEET[0] <= len(s) <= TITLE_SWEET[1] else 0.0
        return sim[i] + fit - _title_flaws(s)

    return max(live, key=score)


def _readable(row):
    """Заголовок уже читается: перевод лежит в кэше (русский от
    translate_worker или английский) или язык оригинала такой, что переводить
    нечего (тот же список, по которому это решает translate)."""
    return bool(row[12] or row[8]) or (row[7] or "") in translate._SKIP_LANGS


# ── Данные ──────────────────────────────────────────────────────────────────
def connect():
    """Не read-only: сборка дописывает переводы для своих представителей
    кластеров (см. stories). Общий кэш с фидом, WAL держит параллельную
    запись пайплайна."""
    return db.connect()


def _clean_title(title, domain=""):
    """Заголовки приходят из <title> чужих страниц, поэтому в них регулярно
    сидит мусор двух видов: заголовок склеен сам с собой (издание печатает его
    дважды с разными хвостами) и приписка с именем издания.

    Приписка режется ТОЛЬКО если совпадает с доменом статьи — иначе легко
    потерять настоящую часть заголовка после тире. В фиде (feeds.py) заголовок
    намеренно остаётся сырым: правка сломала бы дедуп FreshRSS по уже
    прочитанным записям.
    """
    title = _WS.sub(" ", (title or "").strip())
    if not title:
        return ""

    # склейка: первые _DUP_PROBE знаков встречаются в строке ещё раз
    probe = title[:_DUP_PROBE]
    if len(title) > _DUP_PROBE * 2:
        again = title.find(probe, _DUP_PROBE)
        if again > 0:
            title = title[:again].strip()

    host = _ALNUM.sub("", (domain or "").lower())
    for _ in range(2):  # «… | Zimbabwe News - The Zimbabwe Mail» — два хвоста
        m = _TAIL.search(title)
        if not m:
            break
        tail = _ALNUM.sub("", m.group(1).lower())
        if not tail or not host or tail not in host:
            break
        title = title[:m.start()].strip()
    return title.strip(" |-–—:·»")


def _snippet(text, limit=260, lead=""):
    """Первые предложения текста, обрезанные по границе предложения/слова.

    `lead` — заголовок сюжета: тексты сплошь и рядом начинаются с него дословно,
    и тогда подводка под заголовком дублирует заголовок.
    """
    text = _WS.sub(" ", (text or "").strip())
    if lead and text[:len(lead)].lower() == lead.lower():
        text = text[len(lead):].lstrip(" .,:;—-–")
    if len(text) <= limit:
        return text
    out = ""
    for sent in _SENT_SPLIT.split(text):
        if out and len(out) + len(sent) > limit:
            break
        out = f"{out} {sent}".strip()
    # Первое предложение само может быть длиннее лимита (или границ предложений
    # нет вовсе — CJK без пробелов, лид одним абзацем): режем по слову.
    if len(out) > limit:
        out = text[:limit].rsplit(" ", 1)[0]
    return out.rstrip(" .,;:—-") + "…"


def tail_over(s):
    """Обзор сюжета для карточки из корпуса: [[предложение, издание], ...].

    Те же дословные предложения, что на странице, и с той же подписью — но
    две строки вместо трёх и вдвое короче: search.json грузится целиком, а
    полный обзор на шесть тысяч сюжетов весит лишних три мегабайта.

    Адреса строк сюда не едут: у карточки хвоста один выход наружу — издание,
    возглавившее сюжет, — а ещё шесть тысяч ссылок стоили бы столько же, во
    сколько обошёлся сам обзор. Пусто у сюжета бывает ровно тогда, когда
    источники ещё не переведены; тогда в ход идёт подводка головной статьи.
    """
    out, total = [], 0
    for o in s["overview"][:TAIL_OVER_LINES]:
        if out and total + len(o["text"]) > TAIL_OVER_CHARS:
            break
        out.append([o["text"], o["domain"]])
        total += len(o["text"])
    if out:
        return out
    text = _snippet(s["snippet"], limit=130)
    return [[text, s["domain"]]] if text else []


def _tier(score):
    return sum(score >= t for t in TIERS)


def _ago(dt, now):
    """Человекочитаемый возраст. Точность до часа — лента почасовая.

    Сокращений («3 ч назад», «2 дн назад») здесь больше нет: подпись читают
    глазами, а не парсят, и место под ней есть.
    """
    mins = max(0, int((now - dt).total_seconds() // 60))
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} {_plural(mins, 'минуту', 'минуты', 'минут')} назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} {_plural(hours, 'час', 'часа', 'часов')} назад"
    days = hours // 24
    if days >= 7:
        return dt.strftime("%d.%m")
    return f"{days} {_plural(days, 'день', 'дня', 'дней')} назад"


def _plural(n, one, few, many):
    """Русские формы: 1 издание / 2 издания / 5 изданий."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _safe_url(url):
    """Пропускает только http(s)-адреса, остальное схлопывает в "#".

    Адреса приходят из GDELT, то есть от третьей стороны, и уезжают прямо в
    href=. Jinja-автоэкранирование тут не помогает: "javascript:..." не ломает
    кавычек, но выполняется по клику. Чистим на входе в контекст рендера, а не
    в шаблонах — иначе проверку придётся повторять в каждом новом href."""
    if not isinstance(url, str):
        return "#"
    if url.strip().lower().startswith(("http://", "https://")):
        return url
    return "#"


_RU_DOC_COLS = "url,title,text,language,title_ru,text_ru"


def _ru_doc(row):
    """(url, русский текст, русский заголовок) статьи для обзора.

    Свой текст, если издание пишет по-русски, иначе перевод. Заголовок нужен
    отбору, чтобы срезать его же копию в начале статьи. Колонки — _RU_DOC_COLS,
    и оба места, откуда сюда приходят строки (сюжет и весь архив), обязаны
    подать их в этом порядке: разбор статьи кэшируется по адресу, и подай они
    разный текст — обзор собрался бы не по тому, что показано.
    """
    url, title, text, lang, title_ru, text_ru = row
    own = (lang or "").lower().startswith("ru")
    return (url,
            text if own and text else (text_ru or ""),
            title if own and title else (title_ru or ""))


def archive_docs(conn, hours):
    """Весь архив для overview.learn — то, по чему опознаётся вёрстка издания.

    Только статьи, у которых русский текст уже есть: остальным в обзоре всё
    равно нечего дать, а читать ради счёта ещё пятьдесят тысяч оригиналов —
    четверть гигабайта впустую.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        "SELECT %s FROM articles WHERE fetched_at>=? AND "
        "((text_ru IS NOT NULL AND text_ru<>'') OR language LIKE 'ru%%')"
        % _RU_DOC_COLS, (since,))
    return [_ru_doc(r) for r in rows]


def window_rows(conn, country, hours):
    """Те же колонки и тот же порог, что у `feeds.build_country`."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return conn.execute(
        feeds._SELECT + "WHERE country=? AND importance>? AND fetched_at>=?",
        (country, settings.RELEVANCE_CUTOFF, since)).fetchall()


def stories(conn, rows, country, now):
    """rows → сюжеты в том же порядке, что в фиде (структурная важность).

    Кластеризация и ранжирование берутся у фида целиком (feeds.cluster_rows /
    feeds.rank), но члены кластера здесь не выбрасываются: число разных
    изданий по сюжету — это то, чего в фиде не видно, и ровно оно отличает
    национальную новость от единичной заметки.
    """
    if not rows:
        return []
    groups = feeds.cluster_rows(rows)
    ranked = feeds.rank(groups)

    # Представитель — читаемый (перевод есть или язык en/ru), при прочих равных
    # самый свежий: тем же ключом свежести, что в feeds._top_items, чтобы фид и
    # витрина вели на один и тот же источник сюжета.
    #
    # Члены кластера — один и тот же сюжет (косинус >= DEDUP_COSINE), так что
    # показать вместо непереведённой головы переведённого соседа не стоит ни
    # одного обращения к переводчику. Сортировка целиком, а не max(): sources
    # ниже должны начинаться с того же издания, на которое ведёт заголовок.
    for members in groups.values():
        members.sort(key=lambda r: (_readable(r), r[4] or ""), reverse=True)

    # Что соседом не закрылось — переводим сами, в общий с фидом кэш. ranked уже
    # по убыванию важности, так что бюджет тратится сверху вниз.
    todo = [groups[lab][0] for lab, _w in ranked
            if not _readable(groups[lab][0])][:TRANSLATE_BUDGET] \
        if time.monotonic() < _translate_until else []
    en = translate.translate_missing(
        conn, [(r[0], r[1], r[2], r[7], r[8], r[9]) for r in todo])

    # Обзоры сюжетов — пачкой на страну: по строке из базы витрина бы не
    # собралась, сюжетов шесть тысяч. Ключ — набор адресов кластера, см.
    # overview: он переживает смену меток кластеризации между прогонами.
    keys = [overview.key_of([m[0] for m in groups[lab]]) for lab, _w in ranked]
    cached = overview.load(conn, keys)

    out = []
    for key, (lab, weight) in zip(keys, ranked):
        members = groups[lab]
        (url, title, text, pdate, fetched, _llm, _e, lang, t_en, x_en, _be, ents,
         t_ru, x_ru) = members[0]
        t_en, x_en = en.get(url, (t_en, x_en))
        # Источники: по одному на издание, в порядке «полнее и весомее — выше»
        # (см. source_rank). Раньше порядок повторял отбор головной статьи, то
        # есть читаемость и дату, и первой в списке могла стоять заметка в
        # четыре строки при развёрнутом репортаже третьим номером. Теперь это
        # единственный выход из сюжета наружу — заголовок больше не ссылка, —
        # и первая строка обязана вести туда, куда стоит идти.
        rmap = reach(conn)
        sources, seen_domains = [], set()
        for m in members:
            d = imp.domain(m[0])
            if d and d not in seen_domains:
                seen_domains.add(d)
                sources.append({"domain": d, "url": _safe_url(m[0]), "head": False,
                                "rank": source_rank(m[2], d, rmap)})
        sources.sort(key=lambda s: s["rank"], reverse=True)
        by_url = {s["url"]: s["domain"] for s in sources}

        # Заголовок — лучший среди заголовков всех источников сюжета, а не
        # заголовок статьи-победителя (см. pick_title). Кандидат годится, только
        # если его есть на чём показать: перевод в кэше или язык, который
        # переводить нечего. Непереведённый китайский заголовок в русской ленте
        # хуже любого неудачного русского.
        import numpy as np
        cands, tvecs = [], []
        for m in members:
            if m[0] == url:            # у головы перевод мог приехать только что
                raw = t_ru or t_en or (title if (lang or "") in translate._SKIP_LANGS else "")
            else:
                raw = m[12] or m[8] or (m[1] if (m[7] or "") in translate._SKIP_LANGS else "")
            cands.append(_clean_title(raw, imp.domain(m[0])) if raw else "")
            tvecs.append(np.frombuffer(m[6], np.float32) if m[6] else None)
        pick = pick_title(cands, tvecs)
        lines, updated = overview.resolve(
            conn, cached, key, [m[0] for m in members],
            [_ru_doc((m[0], m[1], m[2], m[7], m[12], m[13])) for m in members])
        # Издание у каждой строки обзора — не украшение: предложения взяты
        # дословно из разных источников, и без подписи абзац выглядел бы одним
        # авторским текстом, которым он не является.
        over = [{"text": s, "domain": by_url.get(_safe_url(u), imp.domain(u)),
                 "url": _safe_url(u)} for u, s in lines if s]
        tlist = topics.of([c for c in cands if c],
                          " ".join(o["text"] for o in over) or (x_ru or x_en or text or ""))
        dt = _pubdate(pdate, fetched)
        # Структурная важность приходит в [0,1] (importance.structural); шкала
        # 0-100 — только для показа, столбик и ступень тона считают по ней.
        score = round(weight * 100)
        head = sources[0]["domain"] if sources else ""
        clean = (cands[pick] if pick is not None else "") or url
        # Издание, чей заголовок выиграл отбор, помечается в списке источников:
        # заголовок больше не ссылка, и иначе неоткуда узнать, чьими словами
        # сюжет назван.
        if pick is not None:
            won = _safe_url(members[pick][0])
            for src in sources:
                if src["url"] == won:
                    src["head"] = True
                    break
        out.append({
            # Адрес сюжета — у того издания, что возглавило список источников,
            # а не у представителя кластера: рядом стоит `domain`, взятый
            # оттуда же, и разойтись они не должны — подпись говорила бы одно,
            # а ссылка вела в другое.
            "url": sources[0]["url"] if sources else _safe_url(url),
            "title": clean,
            # оригинал показываем, только если он реально другой (был перевод)
            "orig_title": _clean_title(title, head) if (t_ru or t_en) and title else "",
            # Обзор по всем изданиям сюжета; пока переводов нет — прежняя
            # подводка одной статьи, чтобы строка не осталась пустой.
            "overview": over,
            "updated": updated,
            "snippet": _snippet(x_ru or x_en or text, lead=clean),
            "lang": RU_LANG.get(lang, lang or ""),
            # для атрибута lang= — иначе скринридер и подбор шрифта считают
            # корейский заголовок русским текстом. Показываем язык ТОГО, что
            # реально в заголовке: русский перевод — язык страницы (пусто),
            # английский — en, и только без перевода — язык оригинала.
            "lang_code": "" if t_ru else ("en" if t_en else (lang or "").strip()),
            "translated": bool(t_ru or t_en),
            "score": score,
            "tier": _tier(score),
            "iso": dt.isoformat(),
            # день публикации в UTC — ключ, по которому строка попадает под
            # отметку в полосе дат (.days). Тот же формат, что у ключей days().
            "day": dt.strftime("%Y-%m-%d"),
            "ago": _ago(dt, now),
            "domain": sources[0]["domain"] if sources else "",
            # Все издания кластера, а не первые восемь: в строке выходных данных
            # стоит их полное число («24 издания»), и «Ещё 7 источников» под ним
            # читалось как противоречие — список молча обрывался на восьмом.
            "sources": sources,
            "outlets": len(sources),
            "outlets_word": _plural(len(sources), "издание", "издания", "изданий"),
            # Через entities.split, а не сырым split(";"): в колонке лежат
            # имена, записанные прежними прогонами, и обстановку страницы
            # («whatsapp linkedin») надо отсеять на чтении.
            "entities": entities.split(ents)[:4],
            # Подтемы сюжета. Заголовками идут ВСЕ издания кластера, а не один
            # выбранный: двенадцать заголовков про одно событие — двенадцать
            # попыток назвать его тему, и достаточно, чтобы слово «полупроводник»
            # нашлось хотя бы у одного. Телом — обзор и подводка, то есть уже
            # отобранные предложения, а не сырая статья с её подвалом.
            "topics": tlist,
            # То же маской: в разметке ею фильтрует app.js, в search.json она
            # едет вместо списка слагов (шесть тысяч строк — вчетверо легче).
            "tp": topics.mask(tlist),
            "country": country,
            "country_ru": RU_COUNTRY.get(country, settings.country_display(country)),
            # Эмбеддинг тела головной статьи — только чтобы посчитать похожие
            # (akin) в build; в шаблон и в JSON он не попадает.
            "vec": imp.body_emb(members[0][10], members[0][6])
            if (members[0][10] or members[0][6]) else None,
        })
    # Одна фиксация на страну, а не на сюжет: обзоров пишется несколько тысяч,
    # и транзакция на каждый стоила бы дороже самой сборки.
    if conn is not None:
        conn.commit()
    # Порядок уже задан ranked — пересортировывать нечем и незачем.
    return out


def top_entities(items, limit=ENTITY_TOP):
    """Кто чаще всего фигурирует в сюжетах. Субъекты извлечены GKG
    (`entities.py`) и лежат латиницей в нижнем регистре; русское имя им даёт
    entity_ru — здесь только счёт."""
    counter = Counter()
    for s in items:
        for e in s["entities"]:
            counter[e.strip()] += 1
    return [(name, n) for name, n in counter.most_common(limit) if name and n > 1]


def popular_names(items, limit=POPULAR_TOP):
    """Подсказки под строкой поиска: самые частые субъекты архива, у которых
    есть словарная форма.

    Фильтр по glossary.py тут не украшение, а единственный способ отделить имя
    от мусора: в сыром счёте GKG вперемешку с Нарендрой Моди идут «terms of
    service», «information technology» и «young». В словаре лежат только те,
    кого кто-то руками счёл достойным упоминания, — и заодно с правильным
    русским написанием (сам GKG отдаёт латиницу в нижнем регистре, а перевод
    «cockroach janta party» словарь чинит обратно в «Бхаратия джаната парти»).
    """
    counter = Counter()
    for s in items:
        for e in s["entities"]:
            counter[e.strip()] += 1
    out = []
    for name, n in counter.most_common():
        if n < 3 or len(out) >= limit:
            break
        ru = glossary.lookup(name)
        if ru:
            out.append([ru, name, n])
    return out


def akin(everything, top=AKIN_TOP, floor=AKIN_FLOOR):
    """Похожие сюжеты: для каждого — индексы ближайших по эмбеддингу тела.

    Соседи ищутся по всему корпусу, а не внутри страны: сюжет с высоким
    сходством из другой страны — это, как правило, то же событие в чужой
    подаче, и увести к нему читателя ценнее, чем к соседней строке той же
    ленты, которую он и так видит.

    Эмбеддинги забираются из сюжетов насовсем (pop): дальше они нигде не
    нужны, а держать шесть тысяч векторов до конца сборки незачем.
    """
    import numpy as np
    vecs = [s.pop("vec", None) for s in everything]
    out = [[] for _ in vecs]
    have = [i for i, v in enumerate(vecs) if v is not None and v.size]
    k = min(top, len(have) - 1)
    if k < 1:
        return out

    m = np.asarray([vecs[i] for i in have], np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True).clip(1e-6)
    # Кусками по 512 строк: вся матрица косинусов 6384×6384 — это 160 МБ ради
    # четырёх чисел на строку.
    for lo in range(0, len(have), 512):
        sim = m[lo:lo + 512] @ m.T
        for r, row in enumerate(sim):
            row[lo + r] = -1                       # сам себе не сосед
            near = np.argpartition(-row, k)[:k]
            near = near[np.argsort(-row[near])]
            out[have[lo + r]] = [have[j] for j in near if row[j] >= floor]
    return out


def search_index(everything, regions, near=None):
    """Индекс для поиска по всему корпусу — то, чего на странице нет.

    Каждая страница носит с собой только свою ленту, а искать читатель хочет
    по всему архиву сразу. Отдельный JSON грузится лениво, по первому касанию
    строки поиска, и потому не стоит ничего тем, кто просто читает.

    Формат — массивы, а не объекты с именами полей: имена полей на 2300
    записей весят больше самих данных. Страны и дни вынесены в словари и
    заменены индексами по той же причине.
    """
    days_list = sorted({s["day"] for s in everything}, reverse=True)
    di = {d: i for i, d in enumerate(days_list)}
    keys = sorted({s["country"] for s in everything})
    ci = {c: i for i, c in enumerate(keys)}
    # Русские написания только для тех имён, что есть в словаре: GKG отдаёт
    # латиницу в нижнем регистре, и «narendra modi» в списке фасетов рядом с
    # русскими заголовками выглядит как чужая строка. Остальные фасеты браузер
    # ставит с заглавных — этого хватает, а гнать 40 тысяч имён в JSON ради
    # красоты не стоит.
    names = {}
    for s in everything:
        for e in s["entities"]:
            e = e.strip()
            if e and e not in names:
                ru = glossary.lookup(e)
                if ru:
                    names[e] = ru
    return {
        "d": days_list,
        "n": names,
        "c": [[c, RU_COUNTRY.get(c, settings.country_display(c))] for c in keys],
        # регионы нужны, чтобы «объекты этой страницы» на странице региона
        # означали все его страны, а не одну
        "g": [[r["slug"], r["name"],
               [ci[c["key"]] for c in r["countries"] if c["key"] in ci]]
              for r in regions],
        # Обзор едет сюда укороченным (tail_over), но это тот же обзор, что на
        # странице, — собранный по всем источникам сюжета и с подписью издания
        # у каждого предложения. Раньше здесь лежала подводка головной статьи
        # в 130 знаков, и выходило, что сюжет пересказан только у первой сотни:
        # со второй страницы ленты витрина показывала обрывок одного издания, а
        # у непереведённых — обрывок по-английски. Темы едут маской
        # (topics.mask), а не списком слагов: полоса подтем фильтрует и
        # архивную часть ленты тоже, иначе «только про ИИ» резало бы первую
        # сотню в разметке и пропускало весь хвост из этого же файла.
        "s": [[s["title"], ci[s["country"]], di[s["day"]], s["score"],
               s["url"], s["domain"], ";".join(s["entities"]),
               tail_over(s), s["outlets"], s["tp"]]
              for s in everything],
        # Похожие сюжеты — индексы в том же массиве s. Лежат здесь, а не в
        # разметке: показываются по требованию, и корпус к этому моменту уже
        # загружен (см. akin в app.js).
        "k": near or [],
    }


DOW = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def _period_words(days):
    """Окно витрины прописью: («две недели», «двух недель») — винительный для
    «за ...», родительный для «верх ...».

    Подписи обязаны следовать за SITE_DAYS. Пока окно было неделей, слово
    «неделя» стояло в разметке два десятка раз — и на 14 днях сайт начал бы
    обещать неделю, показывая две."""
    weeks, rest = divmod(days, 7)
    forms = {1: ("неделю", "недели"), 2: ("две недели", "двух недель"),
             3: ("три недели", "трёх недель"), 4: ("четыре недели", "четырёх недель")}
    if not rest and weeks in forms:
        return forms[weeks]
    d = "%d %s" % (days, _plural(days, "день", "дня", "дней"))
    return d, d


def days(now, have=(), span=None):
    """Полоса дат под ленту: последние span суток, свежая первой.

    Из окна выбрасываются дни, за которые не набралось ни одного сюжета, —
    но только с хвоста, чтобы полоса не рвалась дырами посередине. Витрина
    бывает моложе своего окна: после чистой базы полоса пустых столбиков
    выглядит поломкой прибора, а не показанием.

    Даты публикации, вылетевшие за окно (в ленте попадаются статьи с датой
    2015 года — так их разметил источник), в полосу не попадают: столбик
    «одиннадцать лет назад» ничего не измеряет.
    """
    have = set(have)
    out = []
    for i in range(span or SITE_DAYS):
        d = (now - timedelta(days=i)).date()
        out.append({"key": d.isoformat(), "dow": DOW[d.weekday()],
                    "dm": d.strftime("%d.%m")})
    while len(out) > 1 and have and out[-1]["key"] not in have:
        out.pop()
    return out


def pipeline_status(conn, now):
    last = conn.execute("SELECT MAX(fetched_at) FROM articles").fetchone()[0]
    since = (now - timedelta(hours=settings.WINDOW_HOURS)).isoformat()
    scored, countries = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT country) FROM articles "
        "WHERE importance>? AND fetched_at>=?",
        (settings.RELEVANCE_CUTOFF, since)).fetchone()
    dt = _pubdate(None, last) if last else now
    age_h = (now - dt).total_seconds() / 3600
    return {
        "last_tick": _ago(dt, now),
        "last_tick_iso": dt.isoformat(),
        # сбор идёт каждые 15 мин, оценка раз в час: старше 3 ч — это застой
        "live": age_h < 3,
        # телеметрия конвейера — за сутки: это про «жив ли приём», а не про
        # глубину витрины. Глубина витрины — SITE_DAYS, она в days.
        "scored": scored,
        "countries": countries,
        "window": settings.WINDOW_HOURS,
        "days": SITE_DAYS,
    }


# ── Сборка ──────────────────────────────────────────────────────────────────
def _env():
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True, lstrip_blocks=True,
    )
    env.filters["plural"] = _plural
    env.filters["tier"] = _tier
    # именно global, а не переменная рендера: макросы в _story.html
    # импортируются без `with context` и переменных страницы не видят
    env.globals["important_at"] = IMPORTANT_AT
    # Список подтем — тоже global: полосу подтем ставит макрос из _story.html,
    # а он импортируется без контекста страницы.
    env.globals["topic_list"] = topics.ALL
    env.globals["period_acc"], env.globals["period_gen"] = _period_words(SITE_DAYS)
    return env


def _asset_v():
    """Версия для ?v= у css/js. Имён с хэшем у этих файлов нет, а Cloudflare
    держит /static/ час — без версии свежая разметка целый час встречалась бы
    у посетителя со старой таблицей стилей. Хэш по содержимому, а не по времени
    сборки: адрес меняется только когда меняется сам файл, поэтому часовой кэш
    продолжает работать между сборками, где статика не трогалась."""
    h = hashlib.sha1()
    for name in ("style.css", "app.js", "fonts.css"):
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:8]


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def build(out_dir=OUT_DIR):
    global _translate_until
    now = datetime.now(timezone.utc)
    _translate_until = time.monotonic() + TRANSLATE_BUDGET_S
    env = _env()
    entity_ru.reset_budget()
    conn = connect()
    try:
        status = pipeline_status(conn, now)
        overview.ensure(conn)
        # Вёрстку изданий считаем по всему архиву разом, до первой страны:
        # шаблон опознаётся тем, что стоит в статьях, ничем между собой не
        # связанных, — внутри одного сюжета его от новости не отличить.
        overview.learn(archive_docs(conn, SITE_HOURS))
        by_country = {c: stories(conn, window_rows(conn, c, SITE_HOURS), c, now)
                      for c in settings.COUNTRIES}

        # Общая лента: сюжеты всех стран за то же окно. Кросс-страновой дедуп
        # не нужен — url это PRIMARY KEY, статья лежит ровно под одной страной.
        everything = [s for items in by_country.values() for s in items]
        everything.sort(key=lambda s: (s["score"], s["outlets"]), reverse=True)

        # Похожие сюжеты считаются один раз на всю сборку и живут в search.json
        # индексами; сюжету остаётся только их число — по нему шаблон решает,
        # рисовать ли строку «Похожие сюжеты» вообще.
        near = akin(everything)
        for item, rel in zip(everything, near):
            item["akin"] = len(rel)

        # Русские имена субъектов — одним пакетом на всю сборку, а не по стране:
        # имена повторяются между странами, и общий список даёт и кэшу, и пулу
        # потоков полную загрузку (см. entity_ru).
        tops = {c: top_entities(items) for c, items in by_country.items()}
        wanted = {n for pairs in tops.values() for n, _k in pairs}
        # Объекты ВСЕХ сюжетов, а не только «кто в новостях» и лида. Кнопки
        # объектов стоят у каждой строки ленты, и латинское имя посреди русской
        # строки читается как недоделка: на главной лид был по-русски, а всё
        # ниже — латиницей. Дорого это не стоит: словарь и кэш отдают имя
        # бесплатно, в сеть entity_ru идёт только за новыми и только в пределах
        # своего потолка. Недобранное покажется латиницей и переведётся
        # следующим часовым прогоном — но уже навсегда.
        wanted.update(e.strip() for s in everything for e in s["entities"])
        names_ru = entity_ru.translate_names(conn, sorted(wanted))

        # Обзоры сюжетов, выпавших из архива. Своей даты у обзора нет, есть
        # только набор адресов, поэтому чистка идёт от обратного: пропали из
        # articles все адреса сюжета — пропал и обзор.
        overview.sweep(conn)
    finally:
        conn.close()

    def ru_name(raw):
        # Пакет выше покрывает все объекты сборки, так что сюда попадает разве
        # что имя, не добранное из-за потолка запросов. Словарь отдаёт
        # каноническое написание, а чего нет и там — латиницей с заглавных.
        raw = raw.strip()
        return names_ru.get(raw) or glossary.lookup(raw) or raw.title()

    # global, а не переменная рендера: макрос строки ленты импортируется без
    # контекста страницы (см. _env)
    env.globals["ru_name"] = ru_name

    regions = []
    for slug, name, countries in REGIONS:
        members = [c for c in countries if c in by_country]
        pool = [s for c in members for s in by_country[c]]
        pool.sort(key=lambda s: (s["score"], s["outlets"]), reverse=True)
        regions.append({
            # ключ НЕ "items": в шаблоне region.items резолвится в метод
            # словаря dict.items, а не в значение
            "slug": slug, "name": name, "stories": pool,
            # в шапке «Азия»/«Африка» повторяются десять раз и не влезают в
            # строку — там показываем часть света один раз, а рядом стороны
            "part": name.rsplit(" ", 1)[-1], "short": name.rsplit(" ", 1)[0],
            "countries": [{
                "key": c, "name": RU_COUNTRY.get(c, settings.country_display(c)),
                "n": len(by_country[c]),
                "top": by_country[c][0]["score"] if by_country[c] else 0,
            } for c in members],
            "n": sum(len(by_country[c]) for c in members),
        })

    ctx = {
        "site_title": SITE_TITLE, "tagline": SITE_TAGLINE,
        "feed_base": FEED_BASE, "regions": regions, "status": status,
        "built": now.strftime("%d.%m.%Y %H:%M UTC"),
        # полоса дат общая для всех страниц: колонки задаёт весь корпус, а
        # высоты столбиков app.js считает по ленте конкретной страницы
        "asset_v": _asset_v(), "days": days(now, {s["day"] for s in everything}),
        "popular": popular_names(everything),
    }

    os.makedirs(out_dir, exist_ok=True)
    # Индекс поиска: весь корпус архива, а не то, что попало на страницу.
    _write(os.path.join(out_dir, "search.json"),
           json.dumps(search_index(everything, regions, near),
                      ensure_ascii=False, separators=(",", ":")))

    # Лид больше не отдельная переменная: первый сюжет идёт первой строкой той
    # же ленты (см. index.html), а надпись над ним ставит feed().
    _write(os.path.join(out_dir, "index.html"),
           env.get_template("index.html").render(
               items=everything[:HOME_LIMIT], total=len(everything), **ctx))

    for region in regions:
        _write(os.path.join(out_dir, "r", f"{region['slug']}.html"),
               env.get_template("region.html").render(
                   region=region, items=region["stories"][:REGION_LIMIT], **ctx))

    for country, items in by_country.items():
        _write(os.path.join(out_dir, "c", f"{country}.html"),
               env.get_template("country.html").render(
                   country=country,
                   country_ru=RU_COUNTRY.get(country, settings.country_display(country)),
                   region=next((n for _s, n, cs in REGIONS if country in cs), ""),
                   region_slug=next((s for s, _n, cs in REGIONS if country in cs), ""),
                   total=len(items), items=items[:COUNTRY_LIMIT],
                   # key — сырой ключ GKG: по нему ищет фасет «объект», имя в
                   # него не годится (в индексе субъекты лежат латиницей)
                   entities=[{"name": ru_name(n), "key": n, "n": k}
                             for n, k in tops[country]],
                   **ctx))

    static_out = os.path.join(out_dir, "static")
    shutil.rmtree(static_out, ignore_errors=True)
    shutil.copytree(STATIC_DIR, static_out)
    return {"countries": len(by_country), "stories": len(everything),
            "regions": len(regions)}


def _selfcheck():
    """Проверяем то, что может сломаться молча: ступени шкалы, русские формы
    множественного и гео-матрицу против settings.COUNTRIES."""
    assert [_tier(i) for i in (0, 5, 29, 30, 60, 69, 70, 85, 100)] == \
           [0, 0, 0, 1, 2, 2, 3, 4, 4]

    forms = ("издание", "издания", "изданий")
    assert [_plural(n, *forms) for n in (1, 2, 5, 11, 14, 21, 22, 114)] == \
           ["издание", "издания", "изданий", "изданий", "изданий",
            "издание", "издания", "изданий"]

    mapped = [c for _s, _n, cs in REGIONS for c in cs]
    assert len(mapped) == len(set(mapped)), "страна попала в два региона"
    assert set(mapped) == set(settings.COUNTRIES), (
        "гео-матрица разошлась с settings.COUNTRIES: "
        f"лишние {sorted(set(mapped) - set(settings.COUNTRIES))}, "
        f"потерянные {sorted(set(settings.COUNTRIES) - set(mapped))}")
    assert set(RU_COUNTRY) == set(settings.COUNTRIES), "нет русского имени страны"

    assert _snippet("") == ""
    assert _snippet("Короткий текст.") == "Короткий текст."
    long_text = "Первое предложение. " * 40
    assert _snippet(long_text).endswith("…") and len(_snippet(long_text)) <= 262
    # текст без границ предложений всё равно должен обрезаться
    assert len(_snippet("длинноеслово " * 60)) <= 262
    assert not _snippet("Заголовок. Дальше.", lead="Заголовок").startswith("Заголовок")
    assert not _snippet(long_text + "x").endswith(".…")  # точка перед многоточием


    # заголовок: склейка сам с собой + хвост издания, режется только по домену
    dup = ("China memory chipmaker CXMT's shares soar in a blockbuster listing"
           " - The Zimbabwe Mail China memory chipmaker CXMT's shares soar in a"
           " blockbuster listing | Zimbabwe News - The Zimbabwe Mail")
    assert _clean_title(dup, "www.thezimbabwemail.com") == (
        "China memory chipmaker CXMT's shares soar in a blockbuster listing")
    assert _clean_title("Кто построит АЭС в Азии? | News.az", "news.az") == (
        "Кто построит АЭС в Азии?")
    # чужое издание в хвосте — не наш домен, не трогаем
    assert _clean_title("Обзор - The Economist", "kaztag.kz") == "Обзор - The Economist"
    # настоящая часть заголовка после тире не должна пропасть
    assert _clean_title("ИИ в 2026 - что изменилось", "vc.ru") == "ИИ в 2026 - что изменилось"
    assert _clean_title(None) == "" and _clean_title("  a  b ") == "a b"

    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    # Полоса дат: свежая колонка первой, без повторов, максимум SITE_DAYS.
    d = days(now)
    assert len(d) == SITE_DAYS and d[0]["key"] == "2026-07-27", d[0]
    assert d[0]["dow"] == "пн" and d[0]["dm"] == "27.07", d[0]
    assert [x["key"] for x in d] == sorted((x["key"] for x in d), reverse=True)
    assert len({x["key"] for x in d}) == SITE_DAYS
    # Пустой хвост отрезается, дыра посередине — нет: день с нулём сюжетов
    # внутри окна обязан остаться колонкой, иначе он исчезнет из фильтра.
    d = days(now, {"2026-07-27", "2026-07-25"})
    assert [x["key"] for x in d] == ["2026-07-27", "2026-07-26", "2026-07-25"], d
    # даты публикации вне окна полосу не удлиняют и не схлопывают её в ноль
    assert len(days(now, {"2015-05-07"})) == 1, days(now, {"2015-05-07"})

    assert _ago(now - timedelta(minutes=1), now) == "1 минуту назад"
    assert _ago(now - timedelta(minutes=5), now) == "5 минут назад"
    assert _ago(now - timedelta(hours=3), now) == "3 часа назад"
    assert _ago(now - timedelta(days=1), now) == "1 день назад"
    assert _ago(now - timedelta(days=2), now) == "2 дня назад"
    assert _ago(now - timedelta(days=9), now) == (now - timedelta(days=9)).strftime("%d.%m")
    assert _ago(now + timedelta(hours=1), now) == "только что"   # будущая дата
    # Версия статики должна быть стабильна между вызовами и меняться от
    # содержимого — иначе ?v= либо не сбросит кэш, либо сбросит его каждый час.
    v = _asset_v()
    assert re.fullmatch(r"[0-9a-f]{8}", v), v
    assert _asset_v() == v

    # Русский перевод от translate_worker главнее английского кэша: ru > en >
    # оригинал (тот же порядок в feeds.build_country и digest._write_feed).
    # conn не нужен: строка уже читаема, переводчик не зовётся.
    import numpy as np
    vec = np.zeros(settings.EMBEDDING_DIM, np.float32); vec[0] = 1
    def _row(t_ru, x_ru):
        return ("http://kbs.co.kr/1", "한국 제목", "한국 본문", None, now.isoformat(),
                settings.RELEVANT_SCORE, vec.tobytes(), "ko", "Korean title",
                "Korean body", None, "", t_ru, x_ru)
    [s] = stories(None, [_row("Корейский заголовок", "Корейский текст.")], "south_korea", now)
    assert s["title"] == "Корейский заголовок", s["title"]
    assert s["snippet"].startswith("Корейский текст"), s["snippet"]
    assert s["orig_title"] == "한국 제목" and s["translated"]
    assert s["lang_code"] == "", s["lang_code"]      # русский = язык страницы
    # day должен совпадать по формату с ключами days(), иначе полоса дат
    # молча не найдёт ни одной строки и покажет нули по всему архиву.
    assert s["day"] == now.strftime("%Y-%m-%d") == days(now)[0]["key"], s["day"]
    [s] = stories(None, [_row(None, None)], "south_korea", now)
    assert s["title"] == "Korean title" and s["lang_code"] == "en"

    # Похожие сюжеты: близкие по вектору находят друг друга, далёкий не
    # притягивается ни к кому, а сюжет без эмбеддинга не роняет расчёт.
    # Вектор из сюжета уходит насовсем — иначе он уехал бы в шаблон.
    def _v(a, b):
        w = np.zeros(settings.EMBEDDING_DIM, np.float32)
        w[0], w[1] = a, b
        return w
    pool = [{"vec": _v(1, 0)}, {"vec": _v(0.99, 0.14)}, {"vec": _v(0, 1)},
            {"vec": None}]
    rel = akin(pool, top=2)
    assert rel[0][:1] == [1] and rel[1][:1] == [0], rel
    assert rel[2] == [] and rel[3] == [], rel
    assert all("vec" not in p for p in pool), "вектор остался в сюжете"
    assert akin([{"vec": _v(1, 0)}]) == [[]], "один сюжет — соседей нет"

    # app.js — одна функция на весь файл, поэтому var из блока листалки и var
    # из блока подсказок живут в общей области. Совпади имена — второе
    # объявление молча забирает первое, и половина страницы перестаёт
    # отзываться без единой ошибки в консоли. Один раз так и вышло: draw из
    # подсказок отменил draw ленты, и листалка щёлкала вхолостую.
    js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "site_static", "app.js")).read()
    for chunk in js.split("\n(function () {"):
        seen = {}
        for ln, text in enumerate(chunk.split("\n"), 1):
            m = re.match(r"( {2,4})var ([A-Za-z_$][\w$]*)\s*=", text)
            if not m:
                continue
            name = m.group(2)
            assert name not in seen, (
                f"app.js: var {name} объявлен дважды в одной функции "
                f"(строки {seen[name]} и {ln} блока) — второй затрёт первый")
            seen[name] = ln

    entity_ru._selfcheck()
    print("site selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        print(build(sys.argv[1] if len(sys.argv) > 1 else OUT_DIR))
