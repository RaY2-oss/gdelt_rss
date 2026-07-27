# -*- coding: utf-8 -*-
"""prefilter.py — локальный гейт РЕЛЕВАНТНОСТИ перед обращением к LLM.

Разделение обязанностей, ради которого гейт и существует:

    релевантность  — «по теме или нет». Решается здесь, локально, по
                     эмбеддингу и лексике. Это то, чему LLM уже научила нас
                     тысячами своих вердиктов.
    важность       — «насколько это значимо». Не решается ни здесь, ни LLM:
                     считается структурно по корпусу (см. importance.py).

Признаки — два независимых блока, они дополняют друг друга (замер 27.07 на
28 258 статьях, метка = importance > RELEVANCE_CUTOFF):

    e5-заголовок                       AUC 0.8535   режет 33.5 % мусора
    TF-IDF заголовок+текст             AUC 0.8779   режет 44.6 %
    оба вместе                         AUC 0.8927   режет 47.2 %   <- используется

(доля мусора — при полноте 97 %, то есть теряя 3 % нужных статей.)

Два порога, а не один:

    score < lo  -> отказ без LLM (importance = 0)
    score > hi  -> приём без LLM (importance = PREFILTER_ACCEPT_SCORE)
    между       -> в LLM

Верхний порог здесь возможен, в отличие от /opt/digest: там тот же LLM-вызов
ставил статье региональную метку TR/CA/SC/MIX, и обойти его значило остаться
без метки. Здесь страна известна из GKG, вызов не несёт ничего, кроме
релевантности, — значит уверенный приём можно не покупать. Именно верхний
порог и даёт постепенный уход от API: доля потока мимо LLM растёт по мере
накопления разметки.

Контрольная струя (settings.PREFILTER_CONTROL_SHARE) — случайная доля потока,
которая уходит в LLM независимо от решения гейта. Без неё классификатор начнёт
учиться на собственных решениях и медленно уедет, а заметить это будет нечем.
Реализована на стороне вызывающего кода (pipeline.score), здесь только порог.

Пока артефакта нет — is_ready() False, и пайплайн работает как раньше.
"""
import logging
import os

import numpy as np

log = logging.getLogger("gdelt_rss")

_bundle = None
_tried = False

KEEP, DROP, ASK = "keep", "drop", "ask"


def _load(path):
    global _bundle, _tried
    if _tried:
        return _bundle
    _tried = True
    if not os.path.exists(path):
        log.info("Предфильтр: артефакта нет (%s) — стадия пропускается", path)
        return None
    try:
        import joblib
        b = joblib.load(path)
        _bundle = b
        log.info("Предфильтр загружен: обучен на %d примерах, пороги lo=%.3f hi=%.3f, "
                 "полнота %.1f%%, точность приёма %.1f%%",
                 b.get("n_train", 0), b["lo"], b["hi"],
                 100 * b.get("recall", float("nan")),
                 100 * b.get("precision_hi", float("nan")))
    except Exception as exc:
        log.warning("Предфильтр не загрузился (%s) — стадия пропускается", exc)
    return _bundle


def is_ready(path) -> bool:
    return _load(path) is not None


def _z(p, mu, sigma):
    return (np.asarray(p, dtype=np.float64) - mu) / (sigma if sigma > 1e-9 else 1.0)


def raw_score(bundle, embeddings, texts):
    """Комбинированный скор релевантности (чем больше, тем релевантнее).

    Два блока признаков сводятся в один скор через z-нормировку, параметры
    которой (mu/sigma) посчитаны при обучении на out-of-fold вероятностях —
    на обучающих модель уверена в себе сильнее, и нормировка получилась бы
    смещённой.
    """
    X = np.asarray(embeddings, dtype=np.float32)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-10)
    p_emb = bundle["clf_emb"].predict_proba(X)[:, 1]
    p_txt = bundle["pipe_txt"].predict_proba(list(texts))[:, 1]
    return (_z(p_emb, bundle["mu_emb"], bundle["sd_emb"])
            + _z(p_txt, bundle["mu_txt"], bundle["sd_txt"]))


def verdicts(path, embeddings, texts):
    """-> список из DROP / ASK / KEEP на каждый элемент.

    Сбой на предсказании не должен ронять прогон: при любой ошибке всё уходит
    в LLM, то есть система деградирует до прежнего поведения, а не встаёт.
    """
    n = len(embeddings)
    b = _load(path)
    if b is None or n == 0:
        return [ASK] * n
    try:
        sc = raw_score(b, embeddings, texts)
    except Exception as exc:
        log.warning("Предфильтр упал на предсказании (%s) — пропускаем всех в LLM", exc)
        return [ASK] * n
    return [DROP if v < b["lo"] else (KEEP if v > b["hi"] else ASK) for v in sc]
