# -*- coding: utf-8 -*-
"""translate_worker.py — фоновый перевод на русский того, что видит читатель.

Переводится НЕ весь бэклог, а окно витрины: статьи за ARCHIVE_DAYS суток,
прошедшие гейт релевантности. На 63 тысячи строк в базе это порядка 17%.

Раньше очередь набиралась из digest_sent плюс URL из собранных XML-фидов.
Недельного дайджеста больше нет, а фид — это сутки, то есть 460 ссылок против
десяти тысяч на витрине: читатель листает архив за две недели, и очередь по
фиду оставила бы тринадцать дней из четырнадцати непереведёнными. Витрина
теперь единственный потребитель, поэтому она же и задаёт очередь — тем же
запросом, что site.window_rows.

Маршрут ровно один и в два плеча: Google-переводчиком на английский
(_EN_TOPUP), затем с английского на русский локальной моделью
(translate_ru, opus-mt-tc-big-en-zle). Запасных путей нет: и прямые пары
opus X→ru, и Google в русскую сторону работали заметно хуже, а вреда от
плохого перевода больше, чем от его отсутствия. Нет английского — статья
ждёт следующего прогона.

Ставится отдельной строкой cron и намеренно уступает дорогу основному
конвейеру (см. run_translate.sh: nice, ionice, CPUWeight, свои потоки).
Бюджет по времени жёсткий: не успели — оставшееся возьмёт следующий запуск,
ничего не теряется, потому что очередь это просто "title_ru IS NULL".
"""
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import settings
import translate
import translate_ru as T

log = logging.getLogger("gdelt_rss")

BUDGET_S = int(os.environ.get("TRANSLATE_BUDGET_S", "600"))
LIMIT = int(os.environ.get("TRANSLATE_LIMIT", "400"))

# Английское плечо почти всегда уже посчитано и лежит в базе: его делает
# конвейер, когда отбирает статью в фид (translate.translate_missing). Чего
# нет — досчитываем здесь одним пакетом, до загрузки модели. Потолок на
# прогон: цена статьи — сетевой round-trip, а Google при пакетной сборке
# 89 стран троттлит.
_EN_TOPUP = int(os.environ.get("TRANSLATE_EN_TOPUP", "250"))

# Сколько раз ждём английское плечо, прежде чем убрать статью из очереди.
# Очередь сортируется по важности, поэтому горстка текстов, на которых Google
# спотыкается всегда, иначе навечно займёт её верх и заблокирует остальное.
_EN_TRIES = 3

# Глубина переперевода. Окну витрины (site.SITE_DAYS) НЕ следует: витрина
# растянута на две недели, а переперевод — это правка чужой работы старым
# движком, и удваивать ради неё нагрузку на бесплатный Google незачем.
# Свежая половина архива чинится, у старой остаётся перевод, с которым она
# и вышла.
_REDO_DAYS = int(os.environ.get("TRANSLATE_REDO_DAYS", "7"))

_COLS = "url, title, text, language, title_en, text_en"

# Метка нужного маршрута. Всё остальное в translated_by — наследие прошлых
# движков (opus-ar-ru, m2m100, t5-enruzh, google-en-ru), и оно идёт на
# переперевод.
_GOOD = "tcbig-en-ru"

# Статьи, ждущие английского: 'wait:en:1'..'wait:en:N'. Держим их в очереди
# именно так, а не по translated_by IS NULL, чтобы видеть номер попытки.
_WAIT = ["'wait:en:%d'" % i for i in range(1, _EN_TRIES)]


# Свежая очередь: перевода нет. Плюс те, кого прошлые прогоны отложили
# ('wait:en:N') или пометили неподдержанными — теперь маршрут другой, и почти
# всё из этого переводится.
_FRESH = ("a.title_ru IS NULL AND (a.translated_by IS NULL "
          "OR a.translated_by IN (%s) OR a.translated_by LIKE 'unsupported:%%')"
          % ",".join(_WAIT))


def _candidates(conn):
    rows = conn.execute(
        "SELECT a.url, a.title, a.text, a.language, a.title_en, a.text_en "
        "FROM articles a WHERE " + _FRESH + " "
        "AND (a.title IS NOT NULL OR a.text IS NOT NULL) "
        # Окно и гейт — те же, что у витрины (site.window_rows). Повторяем
        # условие, а не импортируем site: воркер бегает раз в 20 минут под nice
        # и не должен тянуть за собой сборку страниц.
        "AND a.importance > ? AND a.fetched_at >= datetime('now', ?) "
        # Сначала важное, потом свежее. Локальная модель на этом железе делает
        # порядка сорока статей за прогон, а очередь витрины — тысячи: по одной
        # свежести первый сюжет витрины мог висеть непереведённым, пока
        # разбирается всё, что пришло позже него.
        "ORDER BY a.importance DESC, a.fetched_at DESC LIMIT ?",
        (settings.RELEVANCE_CUTOFF, "-%d hours" % (24 * settings.ARCHIVE_DAYS),
         LIMIT)).fetchall()
    # Переперевод чужого выхлопа: 660 статей от прежних движков (google-en-ru,
    # opus-ar-ru, m2m100, t5-enruzh...). Само оно не рассосётся — «Партия
    # тараканов Джанта» вместо «Джантар-Мантар» лежала бы в title_ru вечно.
    # Отдельным запросом и с четвертью квоты, чтобы очередь новых статей не
    # голодала; за окном витрины не трогаем — там этих переводов уже никто не
    # увидит.
    redo = LIMIT // 4
    if redo:
        rows += conn.execute(
            "SELECT " + _COLS + " FROM articles "
            "WHERE fetched_at >= datetime('now', ?) "
            "AND translated_by IS NOT NULL AND translated_by NOT LIKE ? "
            "AND translated_by NOT IN ('error') AND translated_by NOT LIKE 'skip:%' "
            "AND translated_by NOT LIKE 'unsupported:%' AND translated_by NOT LIKE 'wait:%' "
            "ORDER BY importance DESC LIMIT ?",
            ("-%d days" % _REDO_DAYS, "%" + _GOOD + "%", redo)).fetchall()
    return rows


def _topup_english(conn, rows):
    """Досчитать недостающее английское плечо Google-переводчиком.

    Тем же translate.translate_missing, что и конвейер, — и в тот же кэш
    articles.title_en/text_en, так что работа не повторяется ни здесь, ни в
    сборке фида. Недобранное возьмёт следующий запуск через 20 минут.
    """
    todo = [r for r in rows
            if not r[4] and (r[3] or "") not in translate._SKIP_LANGS][:_EN_TOPUP]
    if not todo:
        return rows
    got = translate.translate_missing(
        conn, [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in todo])
    if not got:
        return rows
    log.info("Перевод: английское плечо досчитано для %d из %d статей",
             len(got), len(todo))
    return [(r[0], r[1], r[2], r[3]) + got.get(r[0], (r[4], r[5])) for r in rows]


def _defer(conn, url, tag):
    """Английского нет — отложить до следующего прогона, посчитав попытки."""
    n = int(tag.rsplit(":", 1)[1]) + 1 if (tag or "").startswith("wait:en:") else 1
    conn.execute("UPDATE articles SET translated_by=? WHERE url=?",
                 ("wait:en:%d" % n if n < _EN_TRIES else "unsupported:no-en", url))


def run():
    deadline = time.monotonic() + BUDGET_S
    conn = db.connect()
    try:
        rows = _candidates(conn)
        if not rows:
            log.info("Перевод: очередь пуста")
            return 0
        tags = dict(conn.execute(
            "SELECT url, translated_by FROM articles WHERE url IN (%s)"
            % ",".join("?" * len(rows)), [r[0] for r in rows]))
        rows = _topup_english(conn, rows)

        queue, waiting = [], 0
        for url, title, text, lang, title_en, text_en in rows:
            src = T.detect_src(lang, (title or "") + " " + (text or "")[:400])
            if src in T.SKIP_LANGS:
                conn.execute("UPDATE articles SET translated_by='skip:ru' WHERE url=?", (url,))
            elif src == "en":
                queue.append((url, title, text, None))
            elif title_en:
                # Пивот: переводим английский кэш, а язык оригинала оставляем в
                # метке маршрута — по ней потом видно, где двойной перевод.
                queue.append((url, title_en, text_en or "", src))
            else:
                _defer(conn, url, tags.get(url))
                waiting += 1
        conn.commit()
        log.info("Перевод: в работе %d, ждут английского %d", len(queue), waiting)

        done = 0
        for url, title, text, pivot in queue:
            if time.monotonic() > deadline:
                log.info("Перевод: бюджет %d c исчерпан, остальное — следующим запуском",
                         BUDGET_S)
                break
            try:
                t_ru, x_ru, route = T.translate_doc(title, text)
            except Exception:
                log.exception("Перевод %s упал", url)
                conn.execute("UPDATE articles SET translated_by='error' WHERE url=?", (url,))
                conn.commit()
                continue
            if t_ru is None and x_ru is None:
                conn.execute("UPDATE articles SET translated_by='unsupported:empty' WHERE url=?",
                             (url,))
            else:
                tag = route or _GOOD
                conn.execute("UPDATE articles SET title_ru=?, text_ru=?, translated_by=? "
                             "WHERE url=?",
                             (t_ru, x_ru, "%s-en+%s" % (pivot, tag) if pivot else tag, url))
                done += 1
            conn.commit()
        T.unload_all()
        log.info("Перевод: готово %d статей", done)
        return done
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(0 if run() >= 0 else 1)
