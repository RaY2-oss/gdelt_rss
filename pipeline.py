# -*- coding: utf-8 -*-
"""pipeline.py — сбор статей из GKG и оценка важности LLM.

Фаза сбора (collect):
    1. gkg_timestamps  — 15-мин дампы за COLLECT_LOOKBACK_HOURS (overlap
       перекрывает разрыв между часовыми прогонами; seen_store снимает повтор).
    2. каждый дамп качается ОДИН раз, gkg_filter.select прогоняется по каждой
       стране (одиночный FIPS в locations) → url помечается страной.
    3. seen_store отсекает URL с окончательным вердиктом (не качаем повторно).
    4. fetch_and_extract (trafilatura+htmldate) → текст/заголовок/дата.
    5. отсев коротких/не-новостных; hash-дедуп заголовка; e5-эмбеддинг.
    6. INSERT с importance=NULL; в seen_store — вердикт.

Фаза оценки (score):
    необработанные (importance IS NULL) группируются по стране и по сюжету
    (_group_for_scoring — один LLM-вызов на дубликат-группу). Представители
    сначала проходят через prefilter (если накоплено достаточно вердиктов в
    seen_urls, см. settings.PREFILTER_MIN_LABELS) — уверенные отказы не
    тратят LLM-вызов и сразу получают importance=0. Остальные батчами по
    LLM_BATCH уходят в _call_openrouter_raw (свой AI-стек, см. model_rotation.py),
    модель ставит 0..100 — это ГЕЙТ релевантности, а не шкала ранжирования.
    Политический вес субъектов сюда БОЛЬШЕ НЕ ПРИМЕШИВАЕТСЯ: он стал одним из
    факторов структурной важности (importance.py), и правка гейта тем же
    фактором давала бы двойной учёт; сбой провайдеров -> importance остаётся NULL
    (переоценка на следующем прогоне, как pending-вердикт). Каждый вердикт
    (accepted/rejected по settings.RELEVANCE_CUTOFF) пишется в seen_urls —
    это и есть обучающая выборка для prefilter'а следующего прогона.

RAM: дампы обрабатываются по одному; e5 грузится один раз и эмбеддит батчами;
вставка инкрементальная. Пик держится на O(размер одного дампа).
"""
import hashlib
import io
import json
import logging
import random
import re
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from htmldate import find_date
from langdetect import DetectorFactory, LangDetectException, detect
from trafilatura import bare_extraction

import settings
import db
import feeds                # локальный: переиспользуем _cluster для дедупа перед LLM
import gkg_filter
import seen_store
import prefilter           # классификатор-дистиллят вердиктов LLM,
                            # обучается на нашей же seen_urls (см. train_prefilter.py)
from model_rotation import _call_openrouter_raw

DetectorFactory.seed = 0
log = logging.getLogger("gdelt_rss")

GKG_URL_EN = "http://data.gdeltproject.org/gdeltv2/{ts}.gkg.csv.zip"
GKG_URL_TL = "http://data.gdeltproject.org/gdeltv2/{ts}.translation.gkg.csv.zip"
# 11 = V1Persons, 13 = V1Organizations — NER самого GDELT, наш «словарь
# политических субъектов» (см. entities.py). Порядок обязан быть возрастающим:
# pandas раздаёт names по позиции выбранных колонок в файле.
GKG_USECOLS = [1, 3, 4, 7, 8, 9, 10, 11, 13]
GKG_COLNAMES = ["date", "source", "url", "v1t", "v2t", "v1l", "v2l", "v1p", "v1o"]
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    "Accept-Language": "en,ru,ar,fr,zh,hi,fa,tr;q=0.9,*;q=0.5",
}
_EMBED_BATCH = 16
_model = None

# Не-новостные материалы (интервью/колонки/мнения) — быстрый regex-отсев.
NON_EVENT = [r"\binterview\b", r"\bopinion\b", r"\beditorial\b", r"\bcolumn\b",
             r"\banalysis\b", r"\bcommentary\b", r"\bexplainer\b", r"\bpodcast\b",
             r"\bинтервью\b", r"\bмнение\b", r"\bколонка\b", r"\bمقابلة\b",
             r"\bرأي\b", r"\btribune\b", r"\béditorial\b"]


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info("Загрузка e5-модели %s ...", settings.EMBEDDING_MODEL)
        # backend="onnx", а не torch: у процессора этого VPS нет AVX, а MKL
        # внутри torch+cpu всё равно выполняет AVX-инструкцию и ядро убивает
        # процесс по SIGILL (см. README, «Известные проблемы»). onnxruntime
        # считает своей математикой (MLAS) с честной диспетчеризацией под
        # старый CPU; вектора совпадают с torch (cosine 1.0), поэтому уже
        # сохранённые в БД эмбеддинги остаются сравнимыми.
        # local_files_only: модель целиком в кэше, и ходить за ней в сеть незачем.
        # Без этого каждый прогон делает ~20 HEAD-запросов к huggingface.co —
        # лишняя задержка и зависимость конвейера от стороннего сервиса.
        # (HF_HUB_OFFLINE=1 здесь не годится: sentence-transformers всё равно
        # пытается получить листинг репозитория и падает.)
        _model = SentenceTransformer(settings.EMBEDDING_MODEL, backend="onnx",
                                     model_kwargs={"file_name": "onnx/model.onnx"},
                                     local_files_only=True)
    return _model


def _cfg(fips: str) -> dict:
    return {"themes": settings.SCITECH_THEMES, "locations": [fips],
            "max_theme_loc_gap": settings.MAX_THEME_LOC_GAP,
            "min_country_share": settings.MIN_COUNTRY_SHARE}


def gkg_timestamps(hours):
    """Скользящее окно последних тиков. Оставлено для ручных прогонов;
    штатный интервал задаёт pending_timestamps() по курсору."""
    now = datetime.now(timezone.utc)
    a = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return [(a - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S")
            for i in range(int(hours * 4))]


def fetch_gkg_file(ts, translation=False):
    """-> (DataFrame|None, статус). Статус: "ok" | "missing" | "error".

    Различать обязательно: "missing" (404) — дамп не публиковался и не
    появится; "error" — сеть/парсинг, тик надо повторить. От этого зависит,
    можно ли двигать курсор дальше (см. collect_urls).
    """
    url = (GKG_URL_TL if translation else GKG_URL_EN).format(ts=ts)
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 404:
            return None, "missing"
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z, z.open(z.namelist()[0]) as f:
            return pd.read_csv(f, sep="\t", header=None, usecols=GKG_USECOLS,
                               names=GKG_COLNAMES, on_bad_lines="skip",
                               low_memory=False, dtype=str, encoding_errors="replace"), "ok"
    except Exception as exc:
        log.warning("GKG %s (tl=%s): %s", ts, translation, exc)
        return None, "error"


def latest_published_ts():
    """Последний опубликованный GDELT 15-минутный тик (из lastupdate.txt)."""
    try:
        r = requests.get(settings.GKG_LASTUPDATE_URL, timeout=30)
        r.raise_for_status()
        for line in r.text.splitlines():
            parts = line.split()
            if parts and parts[-1].endswith(".gkg.csv.zip"):
                return parts[-1].rsplit("/", 1)[-1].split(".")[0]
    except Exception as exc:
        log.warning("lastupdate.txt недоступен (%s) — считаем тик по часам", exc)
    return None


def _ts_to_dt(ts):
    return datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _floor_15(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def pending_timestamps(conn):
    """Тики от курсора (не включая его) до последнего опубликованного.

    Курсор — последний ПОЛНОСТЬЮ обработанный тик. Идём строго по возрастанию
    и двигаем курсор потиково, поэтому обрыв в середине не теряет остаток:
    следующий прогон продолжит с того же места.
    """
    latest = latest_published_ts()
    latest_dt = (_ts_to_dt(latest) if latest
                 else _floor_15(datetime.now(timezone.utc) - timedelta(minutes=15)))
    cursor = db.state_get(conn, "gkg_cursor")
    if cursor:
        start = _ts_to_dt(cursor) + timedelta(minutes=15)
    else:
        # Первый прогон: курсора нет, берём обычное окно.
        start = latest_dt - timedelta(hours=settings.COLLECT_LOOKBACK_HOURS)
    out = []
    cur = start
    while cur <= latest_dt:
        out.append(cur.strftime("%Y%m%d%H%M%S"))
        cur += timedelta(minutes=15)
    backlog = len(out)
    if backlog > settings.GKG_MAX_TICKS_PER_RUN:
        out = out[:settings.GKG_MAX_TICKS_PER_RUN]
        log.info("Отставание по дампам: %d тиков, берём %d — остальное догоним "
                 "следующими прогонами", backlog, len(out))
    return out


def collect_urls(conn, ts_list=None):
    """-> dict url -> (country_key, gkg_date, entities).

    Интервал тиков задаёт курсор (pending_timestamps), а не скользящее окно:
    каждый дамп обрабатывается ровно один раз и ни один не пропускается.
    Курсор двигается потиково и только после успешной обработки.
    """
    cfgs = {k: _cfg(fips) for k, fips in settings.COUNTRIES.items()}
    stats = {k: {} for k in cfgs}
    found = {}
    ts_list = pending_timestamps(conn) if ts_list is None else ts_list
    if not ts_list:
        log.info("GKG: новых дампов нет — курсор на последнем опубликованном тике")
        return found
    log.info("GKG: %d тиков x 2 потока x %d стран (с %s по %s)",
             len(ts_list), len(cfgs), ts_list[0], ts_list[-1])
    now = datetime.now(timezone.utc)
    for ts in ts_list:
        statuses = []
        for tl in (False, True):
            dfr, st = fetch_gkg_file(ts, translation=tl)
            statuses.append(st)
            if dfr is None or dfr.empty:
                continue
            for ck, cfg in cfgs.items():
                for url, gdate, ents in gkg_filter.select(dfr, cfg, stats[ck]):
                    found.setdefault(url, (ck, gdate, ents))  # первая страна выигрывает
        age_min = (now - _ts_to_dt(ts)).total_seconds() / 60.0
        if "ok" in statuses:
            db.state_set(conn, "gkg_cursor", ts)
        elif all(st == "missing" for st in statuses) and age_min >= settings.GKG_HOLE_AGE_MIN:
            # Дырка в публикации GDELT: дампа нет и уже не будет. Двигаем курсор,
            # иначе конвейер встанет на ней навсегда.
            log.warning("GKG %s: дампов нет (возраст %.0f мин) — считаем дыркой", ts, age_min)
            db.state_set(conn, "gkg_cursor", ts)
        else:
            # Свежий тик ещё не опубликован или сеть подвела — курсор не двигаем,
            # повторим на следующем прогоне.
            log.info("GKG %s: пока недоступен (%s) — остановка, продолжим позже",
                     ts, ",".join(statuses))
            break
        time.sleep(settings.GKG_FETCH_DELAY)
    passed = sum(s.get("passed", 0) for s in stats.values())
    log.info("GKG готово: уникальных URL %d (прошло строк по всем странам %d), "
             "курсор на %s", len(found), passed, db.state_get(conn, "gkg_cursor"))
    return found


def _title_hash(title):
    norm = re.sub(r"[^\w\s]", "", (title or "").lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    return hashlib.sha1(norm.encode()).hexdigest() if norm else None


def _detect_lang(text):
    s = re.sub(r"\s+", " ", (text or "").strip())[:2000]
    if len(s) < 80:
        return None
    try:
        return detect(s)
    except (LangDetectException, Exception):
        return None


def fetch_and_extract(url):
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        r.raise_for_status()
    except Exception as exc:
        log.debug("download fail %s: %s", url, exc)
        return None, None, None
    text = title = None
    try:
        # bytes, не r.text: без Content-Type charset requests угадывает
        # ISO-8859-1 и ломает не-латинские страницы (мойбейк); trafilatura
        # сама детектит кодировку по meta/BOM надёжнее.
        d = bare_extraction(r.content, url=url, with_metadata=True,
                            include_comments=False, favor_precision=True)
        if d:
            g = (lambda k: getattr(d, k, None)) if hasattr(d, "text") else d.get
            text = (g("text") or "").strip()
            title = (g("title") or "").strip()
    except Exception as exc:
        log.debug("trafilatura fail %s: %s", url, exc)
    pdate = None
    try:
        pdate = find_date(r.content, url=url, extensive_search=True, outputformat="%Y-%m-%d")
    except Exception:
        pass
    return text, title, pdate


def _is_non_event(title, text):
    hay = f"{(title or '').lower()} {(text or '')[:2000].lower()}"
    return any(re.search(p, hay) for p in NON_EVENT)


def collect():
    """Сбор + запись новых статей (importance=NULL)."""
    conn = db.connect()
    try:
        found = collect_urls(conn)
        if not found:
            return 0
        seen_store.ensure(conn)
        final = seen_store.final_urls(conn, found.keys())
        # title_hash'и, уже присутствующие в окне БД (дешёвый дедуп до LLM).
        known_hashes = {r[0] for r in conn.execute(
            "SELECT DISTINCT title_hash FROM articles WHERE title_hash IS NOT NULL")}
        todo = [(u, c, d, e) for u, (c, d, e) in found.items() if u not in final]
        log.info("К обработке новых URL: %d (пропущено по журналу %d)",
                 len(todo), len(found) - len(todo))

        now = datetime.now(timezone.utc).isoformat()
        pending, embed_texts, metas = [], [], []
        # Загрузка страниц — I/O-bound, поэтому в пуле потоков. Фильтрация,
        # hash-дедуп и запись остаются на главном потоке (known_hashes без
        # блокировки; какой из дублей выиграет — не важно).
        def _fetch(item):
            return item, fetch_and_extract(item[0])

        with ThreadPoolExecutor(max_workers=settings.FETCH_WORKERS) as ex:
            for (url, country, gdate, ents), (text, title, pdate) in \
                    ex.map(_fetch, todo):
                if not text or len(text) < settings.MIN_TEXT_LENGTH:
                    pending.append((url, None, "short", None)); continue
                if _is_non_event(title, text):
                    pending.append((url, None, "non_event", None)); continue
                th = _title_hash(title)
                if th and th in known_hashes:
                    pending.append((url, None, "rejected", None)); continue  # дубль заголовка
                if th:
                    known_hashes.add(th)
                # эмбедим заголовок, не тело: полный текст статьи размывает
                # сюжетное сходство (разная длина/цитаты/структура у разных
                # изданий) и топит реальные дубли ниже DEDUP_COSINE.
                embed_texts.append(title or text[:200])
                metas.append((url, country, now, pdate or gdate, title, text,
                              _detect_lang(text), th, ents))

        # e5-эмбеддинги батчами (один проход модели), затем вставка.
        inserted = 0
        embs = _embed(embed_texts) if embed_texts else []
        for meta, emb in zip(metas, embs):
            url, country, fa, pdate, title, text, lang, th, ents = meta
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO articles "
                    "(url,country,fetched_at,publish_date,title,text,language,"
                    "title_hash,embedding,entities,importance) VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                    (url, country, fa, pdate, title, text, lang, th, emb.tobytes(), ents))
                inserted += 1
                pending.append((url, country, "accepted", emb))
            except Exception as exc:
                log.warning("INSERT %s: %s", url, exc)
        conn.commit()
        _mark_seen(conn, pending)
        log.info("Сбор: вставлено %d, отсеяно %d", inserted, len(pending) - inserted)
        return inserted
    finally:
        conn.close()


def _embed(texts):
    if not texts:
        return np.zeros((0, settings.EMBEDDING_DIM), np.float32)
    return get_model().encode(["passage: " + t for t in texts],
                              batch_size=_EMBED_BATCH, convert_to_numpy=True,
                              show_progress_bar=False).astype(np.float32)


def _mark_seen(conn, pending):
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    seen_store.mark(conn, [(u, 0, v, e) for (u, _c, v, e) in pending], day)



def embed_bodies(limit=None):
    """Досчитывает embedding_body статьям, прошедшим гейт релевантности.

    Тело эмбеддится не всему потоку, а только тому, что реально попадёт в фид
    и в граф важности (importance > RELEVANCE_CUTOFF) — это ~20 % собранного,
    поэтому вторая колонка обходится впятеро дешевле первой. Заголовочный
    эмбеддинг остаётся у всех: он нужен для дедупа ещё до оценки.

    Ограничение на прогон (settings.EMBED_BODY_MAX_PER_RUN) не даёт стадии
    растянуть прогон на исторических хвостах; недосчитанное подберёт следующий.
    """
    limit = settings.EMBED_BODY_MAX_PER_RUN if limit is None else limit
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT url, text FROM articles "
            "WHERE embedding_body IS NULL AND text IS NOT NULL AND importance > ? "
            "ORDER BY fetched_at DESC LIMIT ?",
            (settings.RELEVANCE_CUTOFF, int(limit))).fetchall()
        if not rows:
            log.info("Эмбеддинг тел: всё посчитано")
            return 0
        embs = _embed([(r[1] or "")[:settings.EMBED_BODY_CHARS] for r in rows])
        conn.executemany("UPDATE articles SET embedding_body=? WHERE url=?",
                         [(e.tobytes(), r[0]) for r, e in zip(rows, embs)])
        conn.commit()
        left = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE embedding_body IS NULL "
            "AND text IS NOT NULL AND importance > ?",
            (settings.RELEVANCE_CUTOFF,)).fetchone()[0]
        log.info("Эмбеддинг тел: посчитано %d, осталось в очереди %d", len(rows), left)
        return len(rows)
    finally:
        conn.close()

# ── Фаза оценки важности ────────────────────────────────────────────────────

def _score_prompt(country_disp):
    return (
        f"You are a senior analyst tracking science, technology and higher-education "
        f"developments of NATIONAL significance for {country_disp}. For each news "
        f"item, FIRST decide topical relevance. It is ON-TOPIC only if its MAIN "
        f"subject is one of:\n"
        f"  A. Science / technology / higher-education POLICY — national strategy, "
        f"funding programmes, regulation, ministries, research budgets, R&D "
        f"agreements;\n"
        f"  B. A new technology, scientific discovery or R&D breakthrough;\n"
        f"  C. Major technological build-out — semiconductor fabs, data centres, "
        f"research labs, power/energy plants, space and launch facilities, large "
        f"engineering or industrial projects;\n"
        f"  D. Big technology corporations — investments, M&A, large deals, plant "
        f"or R&D expansion, major funding rounds;\n"
        f"  E. Universities and research institutions acting at national scale — "
        f"research funding, flagship programmes, international academic partnerships.\n"
        f"Score 0-5 (no matter how newsworthy it otherwise is) if the item is "
        f"instead: purely local/municipal/campus news, a single school or "
        f"science-fair event, a ceremony, minor award or routine press release; "
        f"individual crime (cyber-fraud, phishing, scam or hacking arrests); consumer "
        f"how-to or personal health/medical tips; routine stock-price or "
        f"single-company share moves; accidents, disasters or weather; sport, "
        f"culture, food, entertainment or human interest; general politics, diplomacy "
        f"or military news with no concrete science/technology substance; or an "
        f"opinion piece, interview or propaganda. The topic must be what the article "
        f"is ACTUALLY about — a passing mention does not count. Only if the item is "
        f"clearly ON-TOPIC, rate its STRATEGIC importance to {country_disp} on an "
        f"integer scale 0..100 (0=trivial/local, 100=major national strategic "
        f"significance). Judge by content, not language. Return ONLY a JSON object "
        f"mapping each item id to its integer score, e.g. "
        f'{{"1": 80, "2": 15}}. No prose, no markdown.')


def _parse_scores(content, n):
    """Ответ модели -> {id: оценка}. Возвращает ТОЛЬКО те id, по которым
    модель реально высказалась: пропущенные не подставляются нулём (это
    молча помечало бы статью отклонённой, хотя её никто не судил).

    Разбор через raw_decode, а не `re.search(r"{.*}")`: жадная регулярка при
    двух объектах в ответе хватала всё от первой { до последней } и падала с
    «Extra data» — 58 таких случаев за сутки, каждый терял батч до 20 сюжетов.
    raw_decode читает первый валидный объект и игнорирует хвост.
    """
    raw = (content or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    obj, start = None, raw.find("{")
    if start >= 0:
        try:
            obj, _end = json.JSONDecoder().raw_decode(raw[start:])
        except ValueError:
            obj = None
    if obj is None:
        obj = json.loads(raw)     # не разобралось — пусть падает, вызывающий залогирует
    out = {}
    for k, v in (obj.items() if isinstance(obj, dict) else []):
        try:
            i, s = int(k), int(v)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n:
            out[i] = max(0, min(100, s))
    return out


def _group_for_scoring(rows):
    """rows: [(url,title,text,embedding_bytes), ...] непосредственно одной страны.

    Схлопывает дубли одного сюжета (e5-косинус >= DEDUP_COSINE, разные издания
    об одном событии) в одну группу ДО обращения к LLM — оцениваем сюжет
    один раз, оценку копируем на все статьи-дубликаты. Экономит обращения к
    AI-API там, где GDELT тащит одно событие из десятка источников.

    -> (items, groups): items = [(label, (url,title,text,embedding)), ...] один
    представитель на сюжет (первый по fetched_at DESC, т.е. самый свежий),
    embedding — np.ndarray представителя (нужен prefilter'у в score());
    groups = {label: [url, ...]} все url внутри сюжета, включая представителя.
    """
    embs = [np.frombuffer(r[3], np.float32) for r in rows]
    labels = feeds._cluster(embs, settings.DEDUP_COSINE, titles=[r[1] for r in rows])
    groups, reps = {}, {}
    for r, emb, lab in zip(rows, embs, labels):   # r может нести лишние колонки
        url, title, text = r[0], r[1], r[2]
        lab = int(lab)
        groups.setdefault(lab, []).append(url)
        reps.setdefault(lab, (url, title, text, emb))
    return list(reps.items()), groups


def score():
    """Оценивает важность необработанных статей (importance IS NULL) по странам.
    Перед обращением к LLM статьи внутри страны схлопываются по сюжету
    (см. _group_for_scoring) — экономия вызовов AI-API на дублях. Представители
    сначала проходят prefilter.drop_mask (уверенные отказы получают importance=0
    без обращения к LLM — та же схема, что у digest/daily_collector.py). Каждый
    вердикт (accepted/rejected по settings.RELEVANCE_CUTOFF, пропагирован на весь
    сюжет-дубликат) пишется в seen_urls вместе с эмбеддингом — размеченная выборка
    для следующего train_prefilter.py."""
    conn = db.connect()
    try:
        countries = [r[0] for r in conn.execute(
            "SELECT DISTINCT country FROM articles WHERE importance IS NULL")]
        if not countries:
            log.info("Оценка: новых статей нет")
            return 0
        total = 0
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for country in countries:
            rows = conn.execute(
                "SELECT url,title,text,embedding,entities FROM articles "
                "WHERE importance IS NULL AND country=? ORDER BY fetched_at DESC",
                (country,)).fetchall()
            # url -> np.ndarray (seen_store.mark сам вызывает .tobytes(), сырой blob не подходит)
            url_emb = {r[0]: (np.frombuffer(r[3], np.float32) if r[3] else None) for r in rows}
            disp = settings.country_display(country)
            items, groups = _group_for_scoring(rows)

            to_llm = items
            if prefilter.is_ready(settings.PREFILTER_PATH):
                reps_embs = np.array([emb for _lab, (_u, _t, _x, emb) in items], np.float32)
                reps_txt = [((t or "") + " \n " + (x or "")[:settings.PREFILTER_TEXT_CHARS])
                            for _lab, (_u, t, x, _e) in items]
                verds = prefilter.verdicts(settings.PREFILTER_PATH, reps_embs, reps_txt)
                # Контрольная струя: доля потока уходит в LLM в обход гейта.
                # Иначе новых НЕЗАВИСИМЫХ меток не появится, гейт замкнётся на
                # собственных решениях, и заметить дрейф будет нечем.
                verds = [prefilter.ASK
                         if random.random() < settings.PREFILTER_CONTROL_SHARE else v
                         for v in verds]
                to_llm, ups, marks = [], [], []
                n_drop = n_keep = 0
                for lab_rep, v in zip(items, verds):
                    lab = lab_rep[0]
                    if v == prefilter.ASK:
                        to_llm.append(lab_rep)
                        continue
                    if v == prefilter.DROP:
                        sc, verdict = 0, "rejected"
                        n_drop += 1
                    else:
                        sc, verdict = settings.PREFILTER_ACCEPT_SCORE, "accepted"
                        n_keep += 1
                    for url in groups[lab]:
                        ups.append((sc, url))
                        marks.append((url, 0, verdict, url_emb.get(url)))
                if ups:
                    # scored_by='gate' — эти строки НЕ попадут в обучающую выборку
                    # следующего train_prefilter.py (см. его _load).
                    conn.executemany(
                        "UPDATE articles SET importance=?, scored_by='gate' WHERE url=?", ups)
                    seen_store.mark(conn, marks, day)
                    conn.commit()
                    total += len(ups)
                    log.info("  %s: гейт решил сам %d сюжетов из %d "
                             "(отказ %d, приём %d) — без LLM",
                             country, n_drop + n_keep, len(items), n_drop, n_keep)

            log.info("  %s: %d статей -> %d сюжетов на оценку LLM (сэкономлено %d обращений)",
                     country, len(rows), len(to_llm), len(rows) - len(to_llm))
            for s in range(0, len(to_llm), settings.LLM_BATCH):
                batch = to_llm[s:s + settings.LLM_BATCH]
                user = "\n".join(
                    f"[id={i}] {(t or '').strip()}\n{re.sub(r'\\s+',' ',(x or ''))[:800]}"
                    for i, (_lab, (u, t, x, _emb)) in enumerate(batch, 1))
                content = _call_openrouter_raw(_score_prompt(disp), user, ref_url=country)
                if not content:
                    log.warning("  %s: провайдеры молчат, батч отложен", country)
                    continue
                try:
                    scores = _parse_scores(content, len(batch))
                except Exception as exc:
                    log.warning("  %s: разбор оценок не удался (%s)", country, exc)
                    continue
                ups, marks, missing = [], [], 0
                for i, (lab, _rep) in enumerate(batch, 1):
                    if i not in scores:
                        # Модель не вернула оценку по этому id. Ноль тут ставить
                        # нельзя: это вердикт «отклонено» по статье, которую
                        # никто не судил, и он же уйдёт в обучающую выборку.
                        # Оставляем importance=NULL — следующий прогон переоценит.
                        missing += 1
                        continue
                    sc = scores[i]
                    verdict = "accepted" if sc > settings.RELEVANCE_CUTOFF else "rejected"
                    for url in groups[lab]:
                        ups.append((sc, url))
                        marks.append((url, 0, verdict, url_emb.get(url)))
                if missing:
                    log.warning("  %s: модель пропустила %d/%d id — отложены до "
                                "следующего прогона", country, missing, len(batch))
                conn.executemany(
                    "UPDATE articles SET importance=?, scored_by='llm' WHERE url=?", ups)
                seen_store.mark(conn, marks, day)
                conn.commit()
                total += len(ups)
            log.info("  %s: оценено %d", country, len(rows))
        log.info("Оценка готова: %d статей", total)
        return total
    finally:
        conn.close()
