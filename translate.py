# -*- coding: utf-8 -*-
"""translate.py — перевод заголовка и текста на английский через бесплатный
Google Translate (deep-translator: просто HTTP GET к translate.google.com/m,
без ключа, без локальной модели — в отличие от LibreTranslate не грузит CPU
хоста).

Статьи на русском и английском не переводятся (уже читаемы). Переводятся
только статьи, уже отобранные в фид (top-N после дедупа в feeds.build_country)
— не весь оценённый бэклог.

Только на английский. Русское плечо целиком за translate_ru (локальная модель
opus-mt-tc-big-en-zle): Google в эту сторону переводит бодро, но плоско —
«38 убитых в столкновениях с автобусами» там, где было «38 погибших в
столкновении автобусов», — и мимо словаря имён (glossary.py), который к
модели подключён с двух сторон.
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from deep_translator import GoogleTranslator
from deep_translator.exceptions import LanguageNotSupportedException

import settings

log = logging.getLogger("gdelt_rss")

# langdetect -> код source у deep-translator. Совпадает почти всё (нижний
# регистр ISO 639-1); расходится только иврит (he -> iw). Нужен, когда язык
# определять по скрипту нечем (не-CJK).
_LANG_SRC = {"he": "iw"}

# Скрипт надёжнее langdetect на коротких CJK-заголовках: он регулярно метит
# традиционный китайский как ko/ja/zh-cn, а на auto-детекте Google молча
# возвращает традиционный китайский БЕЗ перевода (и мы кэшировали его как
# title_en). Хангыль => корейский, кана => японский; если их нет, а иероглифы
# есть — китайский. zh-CN переводит и упрощённый, и традиционный (проверено),
# так что упрощ./трад. различать не нужно.
_HANGUL = re.compile(r"[가-힣]")
_KANA = re.compile(r"[぀-ヿ]")
_HAN = re.compile(r"[一-鿿]")


def _src(lang, text=""):
    if text:
        if _HANGUL.search(text):
            return "ko"
        if _KANA.search(text):
            return "ja"
        if _HAN.search(text):
            return "zh-CN"
    return _LANG_SRC.get(lang, lang) if lang else "auto"

# Бюджет — длина ПОСЛЕ url-квотинга (запрос идёт GET-параметром), не сырых
# символов: не-латинские скрипты (бенгальский, китайский, тайский...) при
# %XX-квотинге раздуваются в 3-9 раз, а часто вообще не содержат ". ", так что
# посимвольный бюджет — единственный вариант, который не ловит гигантский URL
# и 400 от Google на весь текст разом.
_CHUNK = 1500

_SKIP_LANGS = ("ru", "en")


def _chunks(text):
    text = text.replace("\n", " ")
    parts, buf, buf_len = [], [], 0
    for ch in text:
        ch_len = len(quote(ch))
        if buf_len + ch_len > _CHUNK and buf:
            parts.append("".join(buf))
            buf, buf_len = [], 0
        buf.append(ch)
        buf_len += ch_len
    if buf:
        parts.append("".join(buf))
    return parts or [text]


def _translate(text, source="auto", target="en"):
    if not text:
        return None
    for attempt in range(2):
        try:
            tr = GoogleTranslator(source=source, target=target)
            return " ".join(tr.translate(c) for c in _chunks(text))
        except LanguageNotSupportedException:
            # неизвестный deep-translator код — откат на auto-детект
            return _translate(text, "auto", target) if source != "auto" else None
        except Exception as exc:
            # ponytail: один ретрай на троттлинг Google при пакетной сборке
            # (89 стран x TRANSLATE_WORKERS). Не помог — вернём None, NULL
            # добьётся на следующем часовом прогоне (translate_missing по NULL).
            if attempt == 0:
                time.sleep(1.5)
                continue
            log.debug("Google Translate fail (%s): %s", source, exc)
            return None


def translate_missing(conn, rows):
    """rows: (url, title, text, language, title_en, text_en) — уже отобранные в
    фид статьи (см. feeds.build_country: items после top-N/дедупа).
    Возвращает {url: (title_en, text_en)}; новые переводы сразу пишутся в БД."""
    result, todo = {}, []
    for url, title, text, lang, title_en, text_en in rows:
        if title_en or text_en:
            result[url] = (title_en, text_en)
        elif lang and lang not in _SKIP_LANGS:
            todo.append((url, title, text, lang))
    if not todo:
        return result
    # source берём по СКРИПТУ самого поля (см. _src): langdetect на коротких
    # заголовках путает китайский с ko/ja, а тело и заголовок один язык.
    with ThreadPoolExecutor(max_workers=settings.TRANSLATE_WORKERS) as pool:
        translated = list(pool.map(
            lambda r: (_translate(r[1], _src(r[3], r[1] or r[2])),
                       _translate(r[2], _src(r[3], r[2] or r[1]))), todo))
    for (url, _t, _x, _l), (t_en, x_en) in zip(todo, translated):
        if t_en or x_en:
            conn.execute("UPDATE articles SET title_en=?, text_en=? WHERE url=?", (t_en, x_en, url))
            result[url] = (t_en, x_en)
    conn.commit()
    return result
