# -*- coding: utf-8 -*-
"""restricted.py — издания, чьи материалы не уходят наружу.

Здесь только организации, признанные в РФ нежелательными (ФЗ-272). Прокуратура
считает распространением сам факт того, что материал лежит на открытой
странице, и дата публикации значения не имеет: список пополнился — старые
страницы уже нарушение. Поэтому фильтр стоит на выходе, а не на приёме.

Из баз и расчётов эти статьи НЕ выкидываются: важность считается графом по
всему корпусу, и вырезать из него источник значило бы менять оценку сюжета
из-за юридического обстоятельства, к сюжету отношения не имеющего. Меняется
только то, что видит посетитель.

Список ведётся руками и сверяется с реестром Минюста:
https://minjust.gov.ru/ru/activity/directions/msu/ — автоматики тут нет и
быть не может, машинного формата у реестра нет.
"""

# Домен пишется без www: imp.domain() его уже снял. Поддомены отсекаются
# суффиксным сравнением в is_restricted, перечислять их не нужно.
RESTRICTED_DOMAINS = frozenset({
    # Deutsche Welle — решение Генпрокуратуры 01.12.2025, реестр Минюста
    # 16.12.2025.
    "dw.com",
    "dw.de",
    # RFE/RL и языковые службы — нежелательная с 02.02.2024.
    "rferl.org",
    "svoboda.org",
    "currenttime.tv",
    "azattyq.org",
    "ozodi.org",
    "azattyk.org",
    # Radio Free Asia. Статус «нежелательной» подтвердить не удалось (в
    # отличие от RFE/RL), убрана по прямому решению владельца сайта.
    "rfa.org",
})


def is_restricted(domain: str) -> bool:
    """Домен сам в списке или его поддомен."""
    d = (domain or "").lower().strip().rstrip(".")
    if not d:
        return False
    return any(d == r or d.endswith("." + r) for r in RESTRICTED_DOMAINS)


def _selfcheck():
    assert is_restricted("dw.com")
    assert is_restricted("www.dw.com")          # на случай ненормализованного
    assert is_restricted("amp.dw.com")
    assert is_restricted("DW.COM")
    assert is_restricted("rfa.org")
    # Суффикс — по метке домена целиком, а не по строке: иначе бы сюда попал
    # любой сайт, чьё имя оканчивается на «dw.com».
    assert not is_restricted("bdw.com")
    assert not is_restricted("dw.com.br")
    assert not is_restricted("thehindu.com")
    assert not is_restricted("")
    assert not is_restricted(None)
    print("restricted: ok")


if __name__ == "__main__":
    _selfcheck()
