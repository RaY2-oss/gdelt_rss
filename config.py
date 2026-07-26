# -*- coding: utf-8 -*-
"""config.py — секреты и сетевые настройки для model_rotation.py.

Отдельный от settings.py: model_rotation.py (общий с /opt/digest AI-стек)
делает `import config` и ждёт от него ровно эти четыре имени. settings.py
специально назван иначе, чтобы не путать "наши" параметры пайплайна с
секретами, читаемыми сторонним модулем.
"""
import os


def _load_dotenv(path: str) -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

PROXIES = None

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
