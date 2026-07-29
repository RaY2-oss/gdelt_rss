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

# Ключи не должны попадать в логи: исключения requests/httpx стрингуют полный
# URL и заголовки. Подмена фабрики записей глобальна — чистится каждая запись
# любого логгера в процессе. То же самое стоит в /opt/digest/config.py.
_SECRETS = [OPENROUTER_API_KEY, GROQ_API_KEY, GOOGLE_API_KEY]


def _install_secret_redaction() -> None:
    import logging

    secrets = [s for s in _SECRETS if s and len(s) >= 8]
    if not secrets:
        return
    original_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = original_factory(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            return record
        cleaned = message
        for secret in secrets:
            cleaned = cleaned.replace(secret, "***REDACTED***")
        if cleaned != message:
            record.msg, record.args = cleaned, ()
        return record

    logging.setLogRecordFactory(factory)


_install_secret_redaction()
