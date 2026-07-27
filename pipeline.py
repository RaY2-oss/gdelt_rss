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
    модель ставит 0..100, к которым _adjust() прибавляет поправку на политический
    вес статьи (заметные персоны/организации из GKG, см. entities.py — так
    локальная заметка не добирается до порога дайджеста); сбой провайдеров -> importance остаётся NULL
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
import entities            # политический вес статьи по субъектам GKG (см. entities.py)
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
        _model = SentenceTransformer(settings.EMBEDDING_MODEL, backend="onnx",
                                     model_kwargs={"file_name": "onnx/model.onnx"})
    return _model


def _cfg(fips: str) -> dict:
    return {"themes": settings.SCITECH_THEMES, "locations": [fips],
            "max_theme_loc_gap": settings.MAX_THEME_LOC_GAP,
            "min_country_share": settings.MIN_COUNTRY_SHARE}


def gkg_timestamps(hours):
    now = datetime.now(timezone.utc)
    a = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return [(a - timedelta(minutes=15 * i)).strftime("%Y%m%d%H%M%S")
            for i in range(int(hours * 4))]


def fetch_gkg_file(ts, translation=False):
    url = (GKG_URL_TL if translation else GKG_URL_EN).format(ts=ts)
    try:
        r = requests.get(url, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z, z.open(z.namelist()[0]) as f:
            return pd.read_csv(f, sep="\t", header=None, usecols=GKG_USECOLS,
                               names=GKG_COLNAMES, on_bad_lines="skip",
                               low_memory=False, dtype=str, encoding_errors="replace")
    except Exception as exc:
        log.warning("GKG %s (tl=%s): %s", ts, translation, exc)
        return None


def collect_urls():
    """-> dict url -> (country_key, gkg_date, entities). Один проход по дампам, все страны."""
    cfgs = {k: _cfg(fips) for k, fips in settings.COUNTRIES.items()}
    stats = {k: {} for k in cfgs}
    found = {}
    ts_list = gkg_timestamps(settings.COLLECT_LOOKBACK_HOURS)
    log.info("GKG: %d тиков x 2 потока x %d стран", len(ts_list), len(cfgs))
    for i, ts in enumerate(ts_list, 1):
        for tl in (False, True):
            dfr = fetch_gkg_file(ts, translation=tl)
            if dfr is None or dfr.empty:
                continue
            for ck, cfg in cfgs.items():
                for url, gdate, ents in gkg_filter.select(dfr, cfg, stats[ck]):
                    found.setdefault(url, (ck, gdate, ents))  # первая страна выигрывает
        time.sleep(settings.GKG_FETCH_DELAY)
    passed = sum(s.get("passed", 0) for s in stats.values())
    log.info("GKG готово: уникальных URL %d (прошло строк по всем странам %d)",
             len(found), passed)
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
    found = collect_urls()
    if not found:
        return 0
    conn = db.connect()
    try:
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
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    obj = json.loads(m.group(0) if m else raw)
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


def _adjust(llm_score, ents, ent_df):
    """Оценка LLM + поправка на политический вес статьи (см. entities.py).

    Ниже RELEVANCE_CUTOFF штраф статью не опускает: вердикт accepted/rejected
    уже поставлен по СЫРОЙ оценке LLM (и он же — разметка для предфильтра),
    а из широкого фида _all статья вылетать не должна. Фактор нужен, чтобы
    локальная заметка не добиралась до MIN_IMPORTANCE дайджеста.

    ent_df пустой/None -> оценка не трогается: словаря по этой стране ещё нет
    (см. score())."""
    if not ent_df:
        return llm_score
    w = entities.weight(entities.prominent(ents, ent_df, settings.ENTITY_MIN_DF),
                        settings.ENTITY_FULL_AT)
    adj = llm_score + round(settings.ENTITY_BONUS * w
                            - settings.ENTITY_PENALTY * (1.0 - w))
    floor = settings.RELEVANCE_CUTOFF + 1 if llm_score > settings.RELEVANCE_CUTOFF else 0
    return max(floor, min(100, adj))


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
            url_ents = {r[0]: r[4] for r in rows}
            # Док-частота субъектов по окну ЭТОЙ страны (таблица и так хранит
            # только KEEP_HOURS, см. main.prune) — самонастраивающийся словарь
            # заметных персон/организаций для _adjust().
            ent_df = entities.document_freq(
                r[0] for r in conn.execute(
                    "SELECT entities FROM articles WHERE country=?", (country,)))
            # Пока в окне страны ни один субъект не перешагнул порог заметности,
            # словаря фактически нет: включённый фактор просто равномерно уронил
            # бы всю малую страну ниже MIN_IMPORTANCE. Выключаем его целиком.
            if not any(v >= settings.ENTITY_MIN_DF for v in ent_df.values()):
                ent_df = None
            disp = settings.country_display(country)
            items, groups = _group_for_scoring(rows)

            to_llm = items
            if prefilter.is_ready(settings.PREFILTER_PATH):
                reps_embs = np.array([emb for _lab, (_u, _t, _x, emb) in items], np.float32)
                mask = prefilter.drop_mask(settings.PREFILTER_PATH, reps_embs)
                to_llm = [it for it, drop in zip(items, mask) if not drop]
                dropped_labs = [lab for (lab, _rep), drop in zip(items, mask) if drop]
                if dropped_labs:
                    ups = [(0, url) for lab in dropped_labs for url in groups[lab]]
                    marks = [(url, 0, "rejected", url_emb.get(url)) for _sc, url in ups]
                    conn.executemany("UPDATE articles SET importance=? WHERE url=?", ups)
                    seen_store.mark(conn, marks, day)
                    conn.commit()
                    total += len(ups)
                    log.info("  %s: предфильтр отсеял %d/%d сюжетов без LLM",
                             country, len(dropped_labs), len(items))

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
                ups, marks = [], []
                for i, (lab, _rep) in enumerate(batch, 1):
                    sc = scores.get(i, 0)
                    verdict = "accepted" if sc > settings.RELEVANCE_CUTOFF else "rejected"
                    for url in groups[lab]:
                        ups.append((_adjust(sc, url_ents.get(url), ent_df), url))
                        marks.append((url, 0, verdict, url_emb.get(url)))
                conn.executemany("UPDATE articles SET importance=? WHERE url=?", ups)
                seen_store.mark(conn, marks, day)
                conn.commit()
                total += len(ups)
            log.info("  %s: оценено %d", country, len(rows))
        log.info("Оценка готова: %d статей", total)
        return total
    finally:
        conn.close()
