# -*- coding: utf-8 -*-
"""Проверка _safe_url: в href витрины попадают только http(s)-адреса.

Запуск: /opt/gdelt_rss/venv/bin/python /opt/gdelt_rss/test_safe_url.py
(именно из venv — site.py тянет numpy через importance.py)
Падает, если "javascript:"/"data:" снова смогут доехать из GDELT в разметку.
"""
import importlib.util
import os

# Грузим по пути, а не `import site`: в стандартной библиотеке уже есть модуль
# с таким именем, и он выигрывает.
_spec = importlib.util.spec_from_file_location(
    "gdelt_site", os.path.join(os.path.dirname(os.path.abspath(__file__)), "site.py"))
_site = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_site)
_safe_url = _site._safe_url

OK = [
    "https://example.com/a?b=1#c",
    "http://example.com/",
    "HTTPS://EXAMPLE.COM/",
]
BAD = [
    "javascript:alert(1)",
    "  javascript:alert(1)",  # ведущие пробелы браузер отбрасывает
    "JaVaScRiPt:alert(1)",
    "data:text/html;base64,PHNjcmlwdD4=",
    "vbscript:msgbox(1)",
    "//evil.example.com",
    "/relative/path",
    "",
    None,
    12345,
]

for url in OK:
    assert _safe_url(url) == url, f"легальный адрес потерян: {url!r}"

for url in BAD:
    assert _safe_url(url) == "#", f"опасный адрес прошёл: {url!r} -> {_safe_url(url)!r}"

print("OK: в href уходят только http(s)")
