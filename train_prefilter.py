# -*- coding: utf-8 -*-
"""train_prefilter.py — обучение локального гейта релевантности.

Источник разметки — таблица `articles`, а НЕ `seen_urls`. Журнал вердиктов
рассинхронизирован с фактическими оценками (ручная переоценка 25.07 обновила
articles.importance, не тронув verdict: 9 550 строк «accepted» при
importance<=5). На тех же признаках чистая метка даёт AUC 0.853 против 0.807 —
разница целиком объясняется мусором в метках, а не моделью.

Метка: importance > settings.RELEVANCE_CUTOFF. Промпт оценки устроен так, что
нерелевантному ставится 0-5, а релевантному выше, — то есть эта граница и есть
готовая метка «по теме», а не «значимо». Значимость здесь не при чём: её
считает importance.py по структуре корпуса.

Признаки — два блока, см. prefilter.py.

Скрипт ОТКАЗЫВАЕТСЯ учиться, пока данных мало, и ничего не перезаписывает,
пока выигрыш ниже порога окупаемости.

Запуск: venv/bin/python train_prefilter.py [--force]
"""
import os
import sys

import numpy as np

import settings
import db


def _load(conn):
    rows = conn.execute(
        "SELECT title, text, embedding, importance FROM articles "
        "WHERE importance IS NOT NULL AND embedding IS NOT NULL "
        # Только вердикты LLM: строки, решённые самим гейтом, — это его
        # собственные предсказания, и обучение на них замкнуло бы контур.
        # NULL — исторические строки, проставленные до появления колонки.
        "AND (scored_by IS NULL OR scored_by = 'llm') "
        # Порядок по времени нужен фолдам: см. cv в main().
        "ORDER BY fetched_at"
    ).fetchall()
    if not rows:
        return None, None, None
    X = np.stack([np.frombuffer(r[2], np.float32) for r in rows])
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-10)
    txt = [((r[0] or "") + " \n " + (r[1] or "")[:settings.PREFILTER_TEXT_CHARS])
           for r in rows]
    y = np.array([1 if r[3] > settings.RELEVANCE_CUTOFF else 0 for r in rows], np.int8)
    return X, txt, y


def main():
    force = "--force" in sys.argv
    conn = db.connect()
    try:
        X, txt, y = _load(conn)
    finally:
        conn.close()
    if X is None:
        print("Нет оценённых статей с эмбеддингами.")
        return 1

    pos, neg = int(y.sum()), int((1 - y).sum())
    total = pos + neg
    print(f"Размеченных примеров: {total} (релевантных {pos}, нет {neg}, "
          f"доля релевантных {pos / total:.1%})")

    if total < settings.PREFILTER_MIN_LABELS and not force:
        print(f"Мало данных: нужно минимум {settings.PREFILTER_MIN_LABELS}. "
              f"Пайплайн копит разметку сам. Артефакт не тронут.")
        return 1
    if min(pos, neg) < settings.PREFILTER_MIN_MINORITY and not force:
        print(f"Класс меньшинства слишком мал ({min(pos, neg)} < "
              f"{settings.PREFILTER_MIN_MINORITY}). Артефакт не тронут.")
        return 1

    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict
    from sklearn.pipeline import make_pipeline
    from sklearn.feature_extraction.text import TfidfVectorizer

    def mk_emb():
        return LogisticRegression(max_iter=3000, class_weight="balanced")

    def mk_txt():
        return make_pipeline(
            TfidfVectorizer(max_features=settings.PREFILTER_TFIDF_MAX_FEATURES,
                            min_df=settings.PREFILTER_TFIDF_MIN_DF,
                            ngram_range=(1, 2), sublinear_tf=True),
            LogisticRegression(max_iter=3000, class_weight="balanced"))

    n_splits = min(5, int(min(np.bincount(y))))
    if n_splits < 2:
        print("Слишком мало примеров одного из классов для кросс-валидации.")
        return 1
    # shuffle=False по выборке, отсортированной временем: фолд получает связный
    # отрезок времени. Со случайным перемешиванием перепечатки одного сюжета
    # (их у нас десятки на событие, все в пределах часов) расходились по train и
    # test, модель узнавала почти точную копию соседа — и AUC выходил 0.8927
    # против честных 0.8406 на временном сплите. Врал не столько AUC, сколько
    # калиброванные по нему lo/hi: на проде уверенность модели ниже, значит
    # заявленная полнота 97 % на деле не выдерживалась.
    cv = StratifiedKFold(n_splits=n_splits, shuffle=False)

    # Пороги и z-нормировка калибруются по out-of-fold вероятностям: на
    # обучающих предсказаниях модель уверена в себе сильнее, и пороги вышли бы
    # оптимистично завышенными.
    print("Кросс-валидация (эмбеддинги)...")
    oof_emb = cross_val_predict(mk_emb(), X, y, cv=cv, method="predict_proba")[:, 1]
    print("Кросс-валидация (TF-IDF)...")
    oof_txt = cross_val_predict(mk_txt(), txt, y, cv=cv, method="predict_proba")[:, 1]

    mu_e, sd_e = float(oof_emb.mean()), float(oof_emb.std())
    mu_t, sd_t = float(oof_txt.mean()), float(oof_txt.std())
    z = lambda p, mu, sd: (p - mu) / (sd if sd > 1e-9 else 1.0)
    oof = z(oof_emb, mu_e, sd_e) + z(oof_txt, mu_t, sd_t)

    from sklearn.metrics import roc_auc_score
    print(f"ROC-AUC: эмбеддинг {roc_auc_score(y, oof_emb):.4f} | "
          f"TF-IDF {roc_auc_score(y, oof_txt):.4f} | вместе {roc_auc_score(y, oof):.4f}")

    lo = float(np.percentile(oof[y == 1], (1.0 - settings.PREFILTER_TARGET_RECALL) * 100.0))
    kept = oof >= lo
    recall = float(kept[y == 1].mean())
    dropped_neg = float((~kept)[y == 0].mean())

    # Верхний порог — минимальный, при котором точность приёма ещё держится.
    hi, precision_hi, accept_share = float(oof.max()) + 1.0, 1.0, 0.0
    order = np.argsort(-oof)
    tp = 0
    for k, idx in enumerate(order, 1):
        tp += int(y[idx])
        prec = tp / k
        if prec >= settings.PREFILTER_TARGET_PRECISION:
            hi, precision_hi, accept_share = float(oof[idx]), prec, k / len(y)
    if hi <= lo:
        hi = float(oof.max()) + 1.0
        precision_hi, accept_share = 1.0, 0.0

    to_llm = float(((oof >= lo) & (oof <= hi)).mean())
    print(f"\nНижний порог lo = {lo:.3f} (цель по полноте {settings.PREFILTER_TARGET_RECALL:.0%})")
    print(f"  сохраняем нужных : {recall:.1%}")
    print(f"  отсекаем мусора  : {dropped_neg:.1%}")
    print(f"Верхний порог hi = {hi:.3f} (цель по точности {settings.PREFILTER_TARGET_PRECISION:.0%})")
    print(f"  принимаем без LLM: {accept_share:.1%} потока, точность {precision_hi:.1%}")
    print(f"ИТОГО в LLM        : {to_llm:.1%} потока "
          f"(+{settings.PREFILTER_CONTROL_SHARE:.0%} контрольной струи)")

    if dropped_neg < settings.PREFILTER_MIN_GAIN and not force:
        print(f"\nВыигрыш меньше порога окупаемости "
              f"({settings.PREFILTER_MIN_GAIN:.0%}) — стадия не окупает себя. "
              f"Артефакт не тронут. Накопи больше разметки и повтори.")
        return 1

    print("\nОбучение финальных моделей на всей выборке...")
    clf_emb = mk_emb().fit(X, y)
    pipe_txt = mk_txt().fit(txt, y)

    import joblib
    os.makedirs(os.path.dirname(settings.PREFILTER_PATH), exist_ok=True)
    joblib.dump({"clf_emb": clf_emb, "pipe_txt": pipe_txt,
                 "mu_emb": mu_e, "sd_emb": sd_e, "mu_txt": mu_t, "sd_txt": sd_t,
                 "lo": lo, "hi": hi, "n_train": int(len(y)),
                 "recall": recall, "dropped_neg": dropped_neg,
                 "precision_hi": precision_hi, "accept_share": accept_share,
                 "dim": int(X.shape[1]), "model": settings.EMBEDDING_MODEL},
                settings.PREFILTER_PATH)
    print(f"Сохранено: {settings.PREFILTER_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
