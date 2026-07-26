# -*- coding: utf-8 -*-
"""train_prefilter.py — обучение локального предфильтра на вердиктах LLM.

Своя БД (settings.DB_PATH), свой артефакт (settings.PREFILTER_PATH) — модель
учится отличать релевантное для профиля "наука/технологии по странам" от
нерелевантного.

Источник разметки — ТОЛЬКО seen_urls (accepted/rejected с эмбеддингом),
которую пишет pipeline.score() после каждого LLM-вердикта.

Скрипт ОТКАЗЫВАЕТСЯ учиться, пока данных мало, и ничего не перезаписывает.

Запуск: venv/bin/python train_prefilter.py [--force]
"""
import os
import sys

import numpy as np

import settings
import db
import seen_store


def _load(conn):
    rows = conn.execute(
        "SELECT verdict, embedding FROM seen_urls "
        "WHERE verdict IN ('accepted','rejected') AND embedding IS NOT NULL"
    ).fetchall()
    if not rows:
        return None, None
    X = np.stack([np.frombuffer(e, dtype=np.float32) for _, e in rows])
    y = np.array([1 if v == "accepted" else 0 for v, _ in rows], dtype=np.int8)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-10)
    return X, y


def main():
    force = "--force" in sys.argv
    conn = db.connect()
    seen_store.ensure(conn)

    pos, neg = seen_store.label_counts(conn)
    total = pos + neg
    print(f"Размеченных примеров: {total} (принято {pos}, отклонено {neg})")

    if total < settings.PREFILTER_MIN_LABELS and not force:
        print(f"Мало данных: нужно минимум {settings.PREFILTER_MIN_LABELS}. "
              f"Пайплайн копит разметку сам, запусти позже. Артефакт не тронут.")
        return 1
    if min(pos, neg) < settings.PREFILTER_MIN_MINORITY and not force:
        print(f"Класс меньшинства слишком мал ({min(pos, neg)} < "
              f"{settings.PREFILTER_MIN_MINORITY}). Артефакт не тронут.")
        return 1

    X, y = _load(conn)
    if X is None:
        print("Нет данных с эмбеддингами.")
        return 1

    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    clf = LogisticRegression(max_iter=2000, class_weight="balanced")

    # Пороги калибруются по out-of-fold вероятностям: на обучающих
    # предсказаниях модель уверена в себе сильнее, чем на новых данных,
    # и порог получился бы оптимистично завышенным.
    n_splits = min(5, int(min(np.bincount(y))))
    if n_splits < 2:
        print("Слишком мало примеров одного из классов для кросс-валидации.")
        return 1
    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=0)
    oof = cross_val_predict(clf, X, y, cv=cv, method="predict_proba")[:, 1]

    target = settings.PREFILTER_TARGET_RECALL
    lo = float(np.percentile(oof[y == 1], (1.0 - target) * 100.0))

    kept = oof >= lo
    recall = float(kept[y == 1].mean())
    dropped_neg = float((~kept)[y == 0].mean())
    volume = float(kept.mean())

    print(f"\nПорог lo = {lo:.4f} (цель по полноте {target:.0%})")
    print(f"  сохраняем нужных : {recall:.1%}")
    print(f"  отсекаем мусора  : {dropped_neg:.1%}")
    print(f"  объём в LLM      : {volume:.1%} от нынешнего")

    if dropped_neg < settings.PREFILTER_MIN_GAIN and not force:
        print(f"\nВыигрыш меньше порога окупаемости "
              f"({settings.PREFILTER_MIN_GAIN:.0%}) — стадия не окупает себя. "
              f"Артефакт не тронут. Накопи больше разметки и повтори.")
        return 1

    clf.fit(X, y)
    import joblib
    os.makedirs(os.path.dirname(settings.PREFILTER_PATH), exist_ok=True)
    joblib.dump({"clf": clf, "lo": lo, "n_train": int(len(y)),
                 "recall": recall, "dropped_neg": dropped_neg,
                 "dim": int(X.shape[1]), "model": settings.EMBEDDING_MODEL},
                settings.PREFILTER_PATH)
    print(f"\nСохранено: {settings.PREFILTER_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
