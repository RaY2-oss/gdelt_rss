# -*- coding: utf-8 -*-
"""Снимок страницы из Web Archive — спасение текста БЕЗ браузера.

Половина «текста нет» — это не отсутствие статьи, а отказ чужого сервера:
403 щиту, таймаут, оборванное соединение. Браузер (camoufox) проходит первое
и не проходит остальное, стоит пятнадцать секунд на страницу и не всегда
поднимается вовсе. Архив отвечает на всё сразу: если снимок есть, он отдаётся
за пару секунд, и никакой щит на пути не стоит — страницу забрал робот
архива, а не мы.

Замер 08.08.2026 на тех же ста адресах, что и браузерный (все отброшены как
"short", спасением считается только настоящая статья — см. сторож заглушек):
    прямой запрос            26
    он же + favor_recall     36
    articleBody из JSON-LD   11   (+0 сверх recall — trafilatura уже читает
                                   структурные данные, отдельный разбор лишний)
    вариант адреса /amp      29   (+2 сверх предыдущих — за три лишних
                                   запроса на каждый неудавшийся адрес)
    архив                    39   (+24: столько не отдал вообще никто другой)
    вместе                   62   против 32 у браузера

Отсюда лестница: прямой запрос -> повтор разбора на полноту (бесплатно, тот
же HTML) -> архив -> и только потом браузер. Из двух API архива взят
availability: CDX не нашёл сверх него НИ ОДНОГО снимка из 39, а стоит секунд
десять на промахе.

Архив — чужой некоммерческий сервер, и грузить его пачкой в восемь потоков
нельзя: он начинает рвать соединения, и спасение проваливается целиком.
Поэтому здесь ни пула, ни повторов — вызывающий идёт по адресам подряд.
"""
import logging
import re

import requests

log = logging.getLogger(__name__)

AVAILABILITY = "https://archive.org/wayback/available"
HEADERS = {"User-Agent": "gdelt-rss/1.0 (news digest; contact via repo)"}

# Снимок отдаётся в двух видах: обычном — с баннером архива поверх страницы —
# и «сыром» (id_), это байт в байт то, что забрал робот. Нужен сырой: баннер
# подмешивает в разметку СВОЮ дату, а дату публикации мы читаем из разметки.
_TS = re.compile(r"/web/(\d{4,14})/")


def _raw_form(snapshot_url):
    """Ссылку на снимок -> её сырой вид (без баннера архива), https."""
    url = _TS.sub(lambda m: f"/web/{m.group(1)}id_/", snapshot_url, count=1)
    return url.replace("http://web.archive.org", "https://web.archive.org", 1)


def snapshot(url, timeout=25):
    """URL статьи -> байты последнего снимка из архива или None.

    Байты, а не str: без charset в заголовке requests угадывает ISO-8859-1 и
    калечит не-латинские страницы — а спасать чаще всего приходится именно их.
    """
    try:
        found = requests.get(AVAILABILITY, params={"url": url},
                             headers=HEADERS, timeout=timeout).json()
        closest = (found.get("archived_snapshots") or {}).get("closest") or {}
    except Exception as exc:
        log.debug("архив не ответил про %s: %s", url, exc)
        return None
    if not (closest.get("available") and closest.get("url")):
        return None
    try:
        resp = requests.get(_raw_form(closest["url"]),
                            headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        log.debug("снимок не забрался %s: %s", url, exc)
        return None
    return resp.content


def _selfcheck():
    assert _raw_form("http://web.archive.org/web/20260805163613/https://a.com/1") \
        == "https://web.archive.org/web/20260805163613id_/https://a.com/1"
    # Дата в самой ссылке на статью не должна приниматься за метку снимка.
    assert _raw_form("https://web.archive.org/web/20260101000000/https://a.com/web/2024/x") \
        == "https://web.archive.org/web/20260101000000id_/https://a.com/web/2024/x"
    assert snapshot("https://example.invalid/нет-такого") is None
    print("archive_fetch: ок")


if __name__ == "__main__":
    _selfcheck()
