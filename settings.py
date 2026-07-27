# -*- coding: utf-8 -*-
"""settings.py — конфигурация GDELT→FreshRSS пайплайна.

Назван settings, а НЕ config, намеренно: `gkg_filter.py`/`seen_store.py`/
`model_rotation.py`/`prefilter.py` — собственные копии одноимённых модулей
из /opt/digest (независимый проект с той же архитектурой пайплайна), а
`model_rotation.py` делает `import config`, и наш модуль не должен
перехватывать это имя. Секреты (OpenRouter/Groq/Google) читаются из
config.py/.env этого же проекта, см. config.py.

Тематика: наука и технологии (+ смежные — энергетика, космос, биотех).
Один фид на страну, максимум TOP_N новостей за сутки, отсортированных по
важности; дубликаты схлопываются (hash при сборе + e5-косинус при сборке фида).
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GDELT_RSS_DB") or os.path.join(BASE_DIR, "data", "feeds.db")
LOG_DIR = os.path.join(BASE_DIR, "logs")
OUTPUT_DIR = os.environ.get("GDELT_RSS_OUT", "/var/www/rss_feeds")  # nginx rss_proxy

# e5 embedding model cache (own directory — no longer shared with digest).
os.environ.setdefault("HF_HOME", os.path.join(BASE_DIR, ".cache", "huggingface"))
# Модель уже в кэше — сеть при загрузке не нужна. Иначе каждый прогон делает
EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
EMBEDDING_DIM = 384

# Две колонки эмбеддингов, потому что задачи несовместимы:
#   embedding      — ЗАГОЛОВОК: дедуп и кластеризация сюжетов. Полный текст
#                    размывает сюжетное сходство (разная длина, цитаты,
#                    структура у разных изданий) и топит настоящие дубли
#                    ниже DEDUP_COSINE.
#   embedding_body — ТЕЛО статьи: релевантность и LexRank. По заголовкам
#                    LexRank мерил бы частоту перепечатки, а не тематическую
#                    центральность материала в окне.
# Тело эмбеддится ТОЛЬКО у статей, прошедших гейт релевантности (~20 % потока),
# поэтому вторая колонка стоит впятеро дешевле первой.
EMBED_BODY_CHARS = 2000        # сколько знаков тела подаётся в e5
EMBED_BODY_MAX_PER_RUN = 4000  # потолок на прогон, чтобы не растянуть его
# Второй потолок, по времени: стадия идёт перед пересборкой фидов, а прогон
# запускается раз в час. Потолка в статьях мало — цена статьи зависит от CPU
# и длины тела, и на этой машине 4000 тел в час не укладываются.
EMBED_BODY_BUDGET_S = 900

TOP_N = 20                 # максимум новостей в фиде за сутки / в дневном дайджесте
WINDOW_HOURS = 24          # окно фида (rolling)
COLLECT_LOOKBACK_HOURS = 2 # только для ПЕРВОГО прогона (курсора ещё нет) и для
                           # ручного запуска; в норме интервал задаёт курсор
                           # pipeline_state.gkg_cursor — от последнего полностью
                           # обработанного тика до последнего опубликованного.
                           # Так ни один дамп не теряется и ни один не качается
                           # дважды (прежние 2 ч при часовом cron давали ровно
                           # двукратную загрузку каждого тика).
GKG_LASTUPDATE_URL = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GKG_MAX_TICKS_PER_RUN = 24  # потолок на прогон, чтобы отставание не растянуло его
                            # на часы; курсор просто догоняет за несколько прогонов
GKG_HOLE_AGE_MIN = 90       # тик, которого нет старше этого возраста, считается
                            # дыркой в публикации GDELT: пропускаем и двигаем
                            # курсор, иначе конвейер встанет на ней навсегда
MIN_TEXT_LENGTH = 300      # короче — отсев до LLM
MIN_IMPORTANCE = 40        # пол по оценке LLM для фида/дайджеста (см. pipeline._score_prompt:
                            # не по теме или незначимо -> низкий score -> не попадает в топ-N).
                            # 40, не 20: сильные сюжеты берут 60-95, а слабо связанный
                            # с темой локальный шум (мелкие крим/финмошенничество, школьные
                            # мероприятия, бытовые советы, котировки) садится в 20-45.
DEDUP_COSINE = 0.95        # e5-косинус: выше — один сюжет, мержим безусловно
DEDUP_LEXICAL_FLOOR = 0.80 # ниже DEDUP_COSINE, но >= этого + общие "редкие" токены
                            # заголовка (см. feeds._distinctive_tokens) — тоже один
                            # сюжет. Калибровка на реальных дублях (Bhargavastra,
                            # HCLTech) показала: e5 на новостях одной страны/темы
                            # слабо разделяет РАЗНЫЕ сюжеты (cos до 0.90) от дублей
                            # ОДНОГО сюжета (cos от 0.86) — чистый порог косинуса
                            # либо пропускает дубли, либо мержет разное.
LLM_BATCH = 20             # сюжетов в одном запросе оценки важности (после дедупа, см. pipeline._group_for_scoring)
FETCH_WORKERS = 8          # потоков на загрузку страниц статей (I/O-bound)
TRANSLATE_WORKERS = 4      # потоков на перевод (Google Translate, бесплатный — не перегружать)
GKG_FETCH_DELAY = 0.5

# ── Предфильтр перед LLM (pipeline.score()) ──────────────────────────────────
# Локальная модель, дистиллирующая вердикты LLM из seen_urls (см.
# train_prefilter.py) — отсекает заведомый мусор без траты LLM-вызова.
# Значения зеркалят /opt/digest/config.py: тот же холодный старт — пока не
# накоплено PREFILTER_MIN_LABELS размеченных строк, prefilter.is_ready()
# возвращает False и стадия не влияет на пайплайн.
PREFILTER_PATH = os.path.join(BASE_DIR, "data", "prefilter.joblib")
PREFILTER_MIN_LABELS = 3000      # меньше — тренер отказывается учиться
PREFILTER_MIN_MINORITY = 500     # минимальный размер класса меньшинства
PREFILTER_TARGET_RECALL = 0.97   # какую долю нужных статей обязан сохранить
PREFILTER_MIN_GAIN = 0.30        # режет меньше мусора — стадия не окупается

# Источник разметки — articles.importance, а НЕ seen_urls.verdict. Журнал
# вердиктов рассинхронизировался с фактическими оценками при ручной
# переоценке 25.07 (9 550 строк «accepted» при importance<=5), и обучение по
# нему шло бы по мусору: AUC 0.807 против 0.853 на тех же признаках с чистой
# меткой. articles — единственный источник истины по оценке.
PREFILTER_TEXT_CHARS = 3000        # сколько знаков тела идёт в TF-IDF
PREFILTER_TFIDF_MAX_FEATURES = 150000
PREFILTER_TFIDF_MIN_DF = 3

# Верхний порог: уверенный приём без обращения к LLM. Калибруется по точности,
# а не по полноте — цена ошибки здесь пропуск мусора в фид, а не потеря статьи.
PREFILTER_TARGET_PRECISION = 0.90
# Какую importance выставить принятому локально. Это ГЕЙТ, а не шкала: порядок
# в дайджесте задаёт структурная важность (importance.py), поэтому значение
# нужно лишь для того, чтобы статья прошла MIN_IMPORTANCE и попала в кандидаты.
PREFILTER_ACCEPT_SCORE = MIN_IMPORTANCE

# Контрольная струя: доля потока, идущая в LLM в обход гейта. Без неё
# классификатор со временем начнёт учиться на собственных решениях, и заметить
# дрейф будет нечем — новых независимых меток просто не появится.
PREFILTER_CONTROL_SHARE = 0.05

# ── Политический вес (entities.py) ───────────────────────────────────────────
# Поправка к оценке LLM по числу РАЗНЫХ заметных субъектов статьи (министерства,
# агентства, корпорации, политики из GKG V1Persons/V1Organizations). Заметный =
# встречается минимум в ENTITY_MIN_DF статьях окна ЭТОЙ ЖЕ страны: локальная
# заметка обходится одним профильным министерством, общенациональный сюжет
# тянет за собой 3-5 разных субъектов.
ENTITY_MIN_DF = 3          # порог заметности субъекта (док-частота в окне страны)
ENTITY_FULL_AT = 4         # столько заметных субъектов = полный вес фактора
ENTITY_BONUS = 15          # очков важности при полном весе
ENTITY_PENALTY = 12        # штраф статье, где заметных субъектов нет вовсе.
                            # Ниже RELEVANCE_CUTOFF штраф не опускает (иначе
                            # статья вылетела бы из широкого фида _all, а не
                            # просто опустилась в рейтинге дайджеста).

# Граница "по теме / не по теме" из _score_prompt: промпт сам просит LLM
# ставить 0-5 нерелевантному и выше — настоящим кандидатам. Тем же порогом
# вердикт разбивается на accepted/rejected при записи в seen_urls, откуда
# его потом читает train_prefilter.py.
RELEVANCE_CUTOFF = 5

# ── Дневной дайджест (digest.py) ─────────────────────────────────────────────
# Важность и MMR-диверсити считаются по графу за DIGEST_GRAPH_DAYS дней (сюжет,
# набирающий обороты несколько дней подряд, "дозревает" до дайджеста), но в
# сам дайджест дня попадают только статьи, впервые увиденные в этот день.
DIGEST_GRAPH_DAYS = 7
DIGEST_MMR_LAMBDA = 0.5     # баланс важность/диверсити в MMR (0..1)

# ── Структурная важность (importance.py) ─────────────────────────────────────
# Оценка LLM (articles.importance) — ГЕЙТ релевантности, не шкала ранжирования:
# она квантована (96 % значений кратны 5, сотни статей делят одно число), и
# сортировка по ней внутри страны почти произвольна. Порядок в дайджесте задаёт
# непрерывная структурная важность: LexRank + охват + политический вес.
# Окно графа — ОДНА СТРАНА (в /opt/digest — один регион): межстрановое сравнение
# топило бы малые страны под объёмом Китая и Индии.
LEXRANK_DAMPING = 0.85
LEXRANK_MAX_ITER = 100
LEXRANK_TOL = 1e-6
# LexRank требует плотную матрицу N*N; окно страны за DIGEST_GRAPH_DAYS дней
# упирается в этот предел (Китай даёт ~5 тыс. статей за 5 суток). Сверх лимита
# берутся самые свежие — граф остаётся представительным, память ограничена.
LEXRANK_MAX_NODES = 1200

COVERAGE_FULL_AT = 12       # столько РАЗНЫХ доменов = полный вес охвата.
                            # Логарифм, не линейка: прежняя надбавка +5 за каждую
                            # статью упиралась в потолок 100 уже на четвёртой
                            # перепечатке, и сюжет с 4 публикациями становился
                            # неотличим от сюжета с 40. Считаются именно домены —
                            # пять версий текста на одном сайте это один домен.

IMPORTANCE_W_LEXRANK = 0.50   # вес фактора; 0 — выключить, не трогая код
IMPORTANCE_W_COVERAGE = 0.35
IMPORTANCE_W_ENTITY = 0.15

KEEP_HOURS = 24 * (DIGEST_GRAPH_DAYS + 1)  # 7-дневный граф + суточный запас, старше — удаляется из БД

# Пороги связи тема↔страна (символьные офсеты V2, см. digest/gkg_filter).
# Общие для всех стран; калибруются по перцентилям из лога прогона.
MAX_THEME_LOC_GAP = 400
MIN_COUNTRY_SHARE = 0.30

# ── Тематические GKG-темы: наука/технологии + смежные домены ────────────────
# Инвалидная тема просто никогда не сматчится (не ошибка). Набор калибруется
# по счётчикам прошедших строк, которые пишет каждый прогон (см. pipeline).
# Валидировано по живым дампам GKG (см. commit-заметку): отброшены слишком
# широкие WB_507_ENERGY_AND_EXTRACTIVES / MEDICAL / MANMADE_DISASTER / JOBS,
# оставлены прицельные sci/tech + энергетика/космос/кибер.
SCITECH_THEMES = [
    # Ядро науки и технологий
    "SCIENCE", "SOC_INNOVATION", "SOC_EMERGINGTECH", "SOC_TECHNOLOGYSECTOR",
    "TECH_AUTOMATION", "TECH_BIGDATA",
    "WB_133_INFORMATION_AND_COMMUNICATION_TECHNOLOGIES",
    "WB_678_DIGITAL_GOVERNMENT",
    "WB_376_INNOVATION_TECHNOLOGY_AND_ENTREPRENEURSHIP",
    "WB_377_FIRM_INNOVATION_PRODUCTIVITY_AND_GROWTH",
    "WB_380_FUNDING_INNOVATION",
    "WB_385_HUMAN_CAPITAL_FOR_INNOVATION_AND_ENTREPRENEURSHIP",
    "WB_1041_PATENTS", "WB_1084_TECHNOLOGY_TRANSFER_AND_DIFFUSION",
    "WB_286_TELECOMMUNICATIONS_AND_BROADBAND_ACCESS",
    "WB_2120_SATELLITES", "WB_1331_HEALTH_TECHNOLOGIES",
    "WB_2399_ICT_INNOVATION_AND_TRANSFORMATION",
    # Высшее образование / наука в вузах (не школьное EDUCATION — оно тащит
    # локальные школьные события; здесь только вузовский/исследовательский срез)
    "WB_2131_TERTIARY_EDUCATION", "SOC_POINTSOFINTEREST_UNIVERSITY",
    # Смежное: энергетика / космос / кибер
    "WB_525_RENEWABLE_ENERGY", "WB_509_NUCLEAR_ENERGY", "WB_528_SOLAR_ENERGY",
    "WB_529_WIND_ENERGY", "WB_533_ENERGY_EFFICIENCY",
    "ENV_SOLAR", "ENV_NUCLEARPOWER",
    # CYBER_ATTACK (гос/инфраструктурный уровень) оставлен; WB_2457_CYBER_CRIME и
    # ENV_CLIMATECHANGE убраны — тащили локальный крим (фишинг/мошенничество,
    # аресты) и погоду/климатическую политику, а не технологии.
    "CYBER_ATTACK",
]

# ── Гео-матрица: имя фида → FIPS 10-4 (GDELT V2EnhancedLocations CC) ─────────
# Азия + Африка, порт старого COUNTRIES. FIPS ≠ ISO (Turkey=TU, China=CH,
# Japan=JA, …). Один фид = одна страна.
COUNTRIES = {
    # Южная Азия
    "india": "IN", "pakistan": "PK", "bangladesh": "BG", "sri_lanka": "CE",
    "nepal": "NP", "bhutan": "BT", "maldives": "MV", "afghanistan": "AF",
    # Юго-Восточная Азия
    "indonesia": "ID", "malaysia": "MY", "singapore": "SN", "thailand": "TH",
    "vietnam": "VM", "philippines": "RP", "myanmar": "BM", "cambodia": "CB",
    "laos": "LA", "brunei": "BX", "east_timor": "TT",
    # Восточная Азия
    "china": "CH", "japan": "JA", "south_korea": "KS", "north_korea": "KN",
    "mongolia": "MG", "taiwan": "TW", "hong_kong": "HK", "macau": "MC",
    # Центральная Азия
    "kazakhstan": "KZ", "uzbekistan": "UZ", "turkmenistan": "TX",
    "kyrgyzstan": "KG", "tajikistan": "TI",
    # Западная Азия / Ближний Восток
    "turkey": "TU", "iran": "IR", "iraq": "IZ", "saudi_arabia": "SA",
    "uae": "AE", "israel": "IS", "qatar": "QA", "kuwait": "KU", "oman": "MU",
    "bahrain": "BA", "jordan": "JO", "lebanon": "LE", "syria": "SY", "yemen": "YM",
    # Северная Африка
    "egypt": "EG", "libya": "LY", "tunisia": "TS", "algeria": "AG",
    "morocco": "MO", "sudan": "SU",
    # Западная Африка
    "nigeria": "NI", "ghana": "GH", "senegal": "SG", "ivory_coast": "IV",
    "mali": "ML", "burkina_faso": "UV", "niger": "NG", "guinea": "GV",
    "benin": "BN", "togo": "TO", "sierra_leone": "SL", "liberia": "LI",
    "mauritania": "MR", "gambia": "GA",
    # Восточная Африка
    "kenya": "KE", "tanzania": "TZ", "uganda": "UG", "ethiopia": "ET",
    "rwanda": "RW", "somalia": "SO", "south_sudan": "OD", "eritrea": "ER",
    # Центральная Африка
    "cameroon": "CM", "dr_congo": "CG", "congo": "CF", "chad": "CD",
    "gabon": "GB", "angola": "AO",
    # Южная Африка
    "south_africa": "SF", "zimbabwe": "ZI", "zambia": "ZA", "mozambique": "MZ",
    "botswana": "BC", "namibia": "WA", "madagascar": "MA", "malawi": "MI",
    "mauritius": "MP",
}

# Человекочитаемое имя страны для промпта LLM (из ключа фида).
def country_display(key: str) -> str:
    return key.replace("_", " ").title()
