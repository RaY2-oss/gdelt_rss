# -*- coding: utf-8 -*-
"""translate_worker.py — фоновый перевод на русский того, что попало в фиды.

Переводится НЕ весь бэклог, а только статьи, реально ушедшие читателю: те,
что есть в digest_sent, плюс те, что лежат в текущих XML-фидах. На 19406
статей в базе это порядка 12%, разница по времени — сутки против часов.

Маршрут один на все языки и в два плеча: Google-переводчиком на английский
(см. _EN_TOPUP), затем с английского на русский — снова Google (_RU_GOOGLE).
Локальные модели остались запасным путём на случай, когда Google не ответил:
на процессоре без AVX они идут int8, выдумывают имена собственные и берут по
полминуты на статью. Модели работают группами по языку, каждая грузится один
раз на группу и выгружается перед следующей — иначе на 8 ГБ соберётся весь
зоопарк разом.

Ставится отдельной строкой cron и намеренно уступает дорогу основному
конвейеру (см. run_translate.sh: nice, ionice, CPUWeight, свои потоки).
Бюджет по времени жёсткий: не успели — оставшееся возьмёт следующий запуск,
ничего не теряется, потому что очередь это просто "title_ru IS NULL".
"""
import glob
import logging
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import settings
import translate
import translate_ru as T

log = logging.getLogger("gdelt_rss")

BUDGET_S = int(os.environ.get("TRANSLATE_BUDGET_S", "600"))
LIMIT = int(os.environ.get("TRANSLATE_LIMIT", "400"))
_URL_RE = re.compile(r"<link>([^<]+)</link>")


def _feed_urls():
    """URL из собранных XML-фидов. Читаем файлы, а не повторяем логику отбора
    из feeds.build_country: так перевод не разъедется с тем, что видит
    подписчик, даже если правила отбора поменяются."""
    urls = set()
    out = getattr(settings, "OUTPUT_DIR", None)
    if not out:
        return urls
    for path in glob.glob(os.path.join(out, "*.xml")):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                urls.update(m.group(1).strip() for m in _URL_RE.finditer(fh.read()))
        except OSError as exc:
            log.warning("translate: не читается фид %s: %s", path, exc)
    return urls


# Единственный маршрут перевода: X -> английский Google-переводчиком, затем
# английский -> русский локальной моделью (tc-big-en-zle). Прямые пары X->ru
# из opus-mt держались на честном слове: opus-mt-ko-ru из «Юнгgiwon, кузница
# научных кадров» делал «ОДНАЖДЫ НАУКА В ИСТОРИИ», t5-enruzh из «в первый
# вечер после листинга Changxin заводские цеха сияли огнями» — «Произошла
# первая вечерняя ярмарка с освещением на завод», а m2m100, на который падали
# хинди, бенгальский и тайский, оставлял в русском тексте латиницу целыми
# кусками. Двойной перевод теряет меньше, чем эти модели.
#
# Английское плечо почти всегда уже посчитано и лежит в базе: его делает
# конвейер, когда отбирает статью в фид (translate.translate_missing). Чего
# нет — досчитываем здесь одним пакетом, до загрузки любых моделей.
#
# Прямая модель остаётся запасным путём: если Google не ответил и английского
# так и нет, плохой перевод лучше отсутствующего (см. translate_ru.DIRECT).
_EN_TOPUP = int(os.environ.get("TRANSLATE_EN_TOPUP", "150"))

# Сколько статей за прогон уходит на русский через Google, а не через модель.
# Сто двадцать за двадцать минут — это 8-9 тысяч в сутки против четырёх тысяч
# приходящих: очередь разбирается с запасом. Остаток добирают модели, поэтому
# промах по потолку ничего не роняет.
_RU_GOOGLE = int(os.environ.get("TRANSLATE_RU_GOOGLE", "120"))

# Глубина переперевода — окно витрины (site.SITE_DAYS). Дальше вглубь
# исправлять нечего: тех статей на сайте уже нет.
_REDO_DAYS = int(os.environ.get("TRANSLATE_REDO_DAYS", "7"))

_COLS = "url, title, text, language, title_en, text_en"


def _candidates(conn):
    rows = conn.execute(
        "SELECT a.url, a.title, a.text, a.language, a.title_en, a.text_en "
        "FROM articles a "
        "WHERE a.title_ru IS NULL AND a.translated_by IS NULL "
        "AND (a.title IS NOT NULL OR a.text IS NOT NULL) "
        "AND a.url IN (SELECT url FROM digest_sent) "
        # Сначала важное, потом свежее. Локальная модель на этом железе делает
        # порядка тридцати статей за прогон, а очередь суток — тысячи: по
        # одной свежести первый сюжет витрины мог висеть непереведённым, пока
        # разбирается всё, что пришло позже него.
        "ORDER BY a.importance DESC, a.fetched_at DESC LIMIT ?", (LIMIT,)).fetchall()
    # Переперевод того, что успели сделать локальные модели. Их выхлоп никуда
    # не девается сам: «Партия тараканов Джанта» вместо «Джантар-Мантар»
    # лежала бы в title_ru вечно. Отдельным запросом и с четвертью квоты,
    # чтобы очередь новых статей не голодала; за окном витрины не трогаем —
    # там этих переводов уже никто не увидит.
    redo = LIMIT // 4
    if redo:
        rows += conn.execute(
            "SELECT " + _COLS + " FROM articles "
            "WHERE fetched_at >= datetime('now', ?) "
            "AND translated_by IS NOT NULL AND translated_by NOT LIKE '%google-en-ru%' "
            "AND translated_by NOT IN ('error') AND translated_by NOT LIKE 'skip:%' "
            "AND translated_by NOT LIKE 'unsupported:%' "
            "ORDER BY importance DESC LIMIT ?",
            ("-%d days" % _REDO_DAYS, redo)).fetchall()

    have = {r[0] for r in rows}
    extra = _feed_urls() - have
    if extra and len(rows) < LIMIT:
        qs = ",".join("?" * min(len(extra), 900))
        rows += conn.execute(
            "SELECT " + _COLS + " FROM articles "
            "WHERE title_ru IS NULL AND translated_by IS NULL AND url IN (%s) "
            "LIMIT ?" % qs, list(extra)[:900] + [LIMIT - len(rows)]).fetchall()
    return rows


def _topup_english(conn, rows):
    """Досчитать недостающее английское плечо Google-переводчиком.

    Тем же translate.translate_missing, что и конвейер, — и в тот же кэш
    articles.title_en/text_en, так что работа не повторяется ни здесь, ни в
    сборке фида. Потолок _EN_TOPUP на прогон: цена статьи — сетевой
    round-trip, а Google при пакетной сборке 89 стран троттлит. Недобранное
    возьмёт следующий запуск через 20 минут, очередь никуда не денется.
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


def run():
    deadline = time.monotonic() + BUDGET_S
    conn = db.connect()
    try:
        rows = _candidates(conn)
        if not rows:
            log.info("Перевод: очередь пуста")
            return 0
        rows = _topup_english(conn, rows)
        groups, pivoted = defaultdict(list), set()
        for url, title, text, lang, title_en, text_en in rows:
            src = T.detect_src(lang, (title or "") + " " + (text or "")[:400])
            if not src or src in T.SKIP_LANGS:
                conn.execute("UPDATE articles SET translated_by='skip:ru' WHERE url=?", (url,))
                continue
            # Пивот через английский кэш — если он есть; иначе прямая модель,
            # плохой перевод лучше отсутствующего.
            if title_en:
                pivoted.add(url)
                src, title, text = "en", title_en, text_en or text
            groups[src].append((url, title, text))
        conn.commit()
        log.info("Перевод: %d статей, языков %d (%s)", sum(len(v) for v in groups.values()),
                 len(groups), ", ".join("%s:%d" % (k, len(v)) for k, v in
                                        sorted(groups.items(), key=lambda x: -len(x[1]))))
        done, google_left = 0, _RU_GOOGLE
        # Крупные группы первыми: загрузка модели амортизируется лучше.
        for src, items in sorted(groups.items(), key=lambda x: -len(x[1])):
            if time.monotonic() > deadline:
                log.info("Перевод: бюджет %d c исчерпан, остальное — следующим запуском", BUDGET_S)
                break
            for url, title, text in items:
                if time.monotonic() > deadline:
                    break
                t_ru = x_ru = route = None
                # Основной маршрут для английского плеча — Google (см.
                # translate.to_ru). Потолок на прогон: он же переводит первое
                # плечо, и обе очереди делят одну квоту вежливости.
                if src == "en" and google_left:
                    t_ru, x_ru = translate.to_ru(title, text)
                    if t_ru or x_ru:
                        google_left -= 1
                        route = "google-en-ru"
                    else:
                        t_ru = x_ru = None
                if route is None:
                    try:
                        t_ru, x_ru, route = T.translate_doc(title, text, src)
                    except Exception:
                        log.exception("Перевод %s (%s) упал", url, src)
                        conn.execute("UPDATE articles SET translated_by='error' WHERE url=?", (url,))
                        conn.commit()
                        continue
                if t_ru is None and x_ru is None:
                    conn.execute("UPDATE articles SET translated_by=? WHERE url=?",
                                 ("unsupported:%s" % src, url))
                else:
                    tag = route or src
                    conn.execute("UPDATE articles SET title_ru=?, text_ru=?, translated_by=? "
                                 "WHERE url=?",
                                 (t_ru, x_ru, "encache+" + tag if url in pivoted else tag, url))
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
