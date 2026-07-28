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
import entity_ru
import importance as imp
import feeds
from feeds import _pubdate

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "site_templates")
STATIC_DIR = os.path.join(BASE_DIR, "site_static")
OUT_DIR = os.environ.get("GDELT_SITE_OUT", "/var/www/rss_site")

SITE_TITLE = "Bhutyan.online"
SITE_TAGLINE = "Наука и технологии 89 стран Азии и Африки"
FEED_BASE = "https://rss.bhutyan.online"

# Окно витрины — вся неделя, что живёт в БД (settings.KEEP_HOURS), одно и то
# же на главной, регионе и стране. Раньше было суточное, а страна с парой
# заметок за сутки расширяла своё окно до недели — и лента главной вынужденно
# исключала такие страны, иначе неделя мешалась с сутками в одном списке.
# Теперь выбирать нечего: неделя лежит на странице целиком, а нужные даты
# читатель отмечает сам (см. .days в _story.html и app.js).
# Витрина показывает ровно неделю. KEEP_HOURS больше на сутки — этот запас
# нужен графу дайджеста, а не витрине: «восемь дней» в подписи под лентой,
# которая называется недельной, читается как опечатка.
SITE_DAYS = min(7, settings.KEEP_HOURS // 24)
SITE_HOURS = SITE_DAYS * 24

HOME_LIMIT = 100       # сюжетов в ленте главной
REGION_LIMIT = 120     # сюжетов на странице региона
COUNTRY_LIMIT = 150    # сюжетов на странице страны
ENTITY_TOP = 14        # субъектов в блоке «кто в новостях»

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


def _tier(score):
    return sum(score >= t for t in TIERS)


def _ago(dt, now):
    """Человекочитаемый возраст. Точность до часа — лента почасовая."""
    mins = max(0, int((now - dt).total_seconds() // 60))
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч назад"
    days = hours // 24
    return f"{days} дн назад" if days < 7 else dt.strftime("%d.%m")


def _plural(n, one, few, many):
    """Русские формы: 1 издание / 2 издания / 5 изданий."""
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


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
    groups = feeds.cluster_rows(rows, country)
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

    out = []
    for lab, weight in ranked:
        members = groups[lab]
        (url, title, text, pdate, fetched, _llm, _e, lang, t_en, x_en, _be, ents,
         t_ru, x_ru) = members[0]
        t_en, x_en = en.get(url, (t_en, x_en))
        sources, seen_domains = [], set()
        for m in members:
            d = imp.domain(m[0])
            if d and d not in seen_domains:
                seen_domains.add(d)
                sources.append({"domain": d, "url": m[0]})
        dt = _pubdate(pdate, fetched)
        # Структурная важность приходит в [0,1] (importance.structural); шкала
        # 0-100 — только для показа, столбик и ступень тона считают по ней.
        score = round(weight * 100)
        head = sources[0]["domain"] if sources else ""
        clean = _clean_title(t_ru or t_en or title, head) or url
        out.append({
            "url": url,
            "title": clean,
            # оригинал показываем, только если он реально другой (был перевод)
            "orig_title": _clean_title(title, head) if (t_ru or t_en) and title else "",
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
            "sources": sources[:8],
            "outlets": len(sources),
            "outlets_word": _plural(len(sources), "издание", "издания", "изданий"),
            "entities": [e for e in (ents or "").split(";") if e][:4],
            "country": country,
            "country_ru": RU_COUNTRY.get(country, settings.country_display(country)),
        })
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


DOW = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def days(now, have=(), span=None):
    """Полоса дат под ленту: последние span суток, свежая первой.

    Из окна выбрасываются дни, за которые не набралось ни одного сюжета, —
    но только с хвоста, чтобы полоса не рвалась дырами посередине. Витрина
    бывает моложе своего окна: после чистой базы неделя пустых столбиков
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
        by_country = {c: stories(conn, window_rows(conn, c, SITE_HOURS), c, now)
                      for c in settings.COUNTRIES}

        # Общая лента: сюжеты всех стран за то же окно. Кросс-страновой дедуп
        # не нужен — url это PRIMARY KEY, статья лежит ровно под одной страной.
        everything = [s for items in by_country.values() for s in items]
        everything.sort(key=lambda s: (s["score"], s["outlets"]), reverse=True)

        # Русские имена субъектов — одним пакетом на всю сборку, а не по стране:
        # имена повторяются между странами, и общий список даёт и кэшу, и пулу
        # потоков полную загрузку (см. entity_ru).
        tops = {c: top_entities(items) for c, items in by_country.items()}
        wanted = {n for pairs in tops.values() for n, _k in pairs}
        wanted.update(e.strip() for e in (everything[0]["entities"] if everything else []))
        names_ru = entity_ru.translate_names(conn, sorted(wanted))
    finally:
        conn.close()

    def ru_name(raw):
        raw = raw.strip()
        return names_ru.get(raw) or raw.title()

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
        "nav": json.dumps(
            [{"n": RU_COUNTRY.get(c, settings.country_display(c)),
              "u": f"/c/{c}.html", "r": name}
             for _s, name, cs in REGIONS for c in cs if c in by_country],
            ensure_ascii=False),
    }

    os.makedirs(out_dir, exist_ok=True)
    lead = everything[0] if everything else None
    _write(os.path.join(out_dir, "index.html"),
           env.get_template("index.html").render(
               lead=lead, lead_ents=[ru_name(e) for e in lead["entities"]] if lead else [],
               items=everything[1:HOME_LIMIT], total=len(everything), **ctx))

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
                   entities=[{"name": ru_name(n), "n": k} for n, k in tops[country]],
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

    assert _ago(now - timedelta(minutes=5), now) == "5 мин назад"
    assert _ago(now - timedelta(hours=3), now) == "3 ч назад"
    assert _ago(now - timedelta(days=2), now) == "2 дн назад"
    assert _ago(now + timedelta(hours=1), now) == "0 мин назад"  # будущая дата
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
    # молча не найдёт ни одной строки и покажет нули по всей неделе.
    assert s["day"] == now.strftime("%Y-%m-%d") == days(now)[0]["key"], s["day"]
    [s] = stories(None, [_row(None, None)], "south_korea", now)
    assert s["title"] == "Korean title" and s["lang_code"] == "en"

    entity_ru._selfcheck()
    print("site selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        print(build(sys.argv[1] if len(sys.argv) > 1 else OUT_DIR))
