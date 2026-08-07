# -*- coding: utf-8 -*-
"""topics.py — подтема сюжета: ИИ отдельно, кванты отдельно, космос отдельно.

Зачем. Лента отвечает на вопрос «где» (страна, регион) и «когда» (полоса дат),
но не на «про что». А корпус разнородный: в одной ленте соседствуют закон о
данных, запуск спутника, вспышка вымогателей и приёмная кампания вуза. Читателю,
пришедшему за чипами, остальные девять десятых мешают.

Почему не классификатор. Пробовать стоило бы, если бы разметки не было вовсе, —
но она есть даром: тема названа в самом заголовке словами, которых в другой теме
не бывает («semiconductor», «qubit», «ransomware»). Словарь на таком корпусе
даёт то же, что модель, и при этом читается, чинится одной строкой и работает
задним числом — по всему архиву сразу, а не с момента установки. Обученная
модель (IPTC-классификатор, fastText) стоила бы полутора гигабайт памяти на
машине без AVX ради девяти корзин, которые и так различимы по словам.

Почему не темы GKG. Они у нас уже есть (settings.SCITECH_THEMES) и матчатся на
приёме, но в колонку не пишутся, и в них нет ни ИИ, ни квантов, ни чипов —
самых очевидных для читателя корзин. Разбирать SOC_EMERGINGTECH на подтемы всё
равно пришлось бы словами.

Границы. Тем у сюжета может быть несколько (чипы для ИИ — и то, и другое), и это
честно: фильтр по «ИИ» такой сюжет показать обязан. Тем может не быть ни одной —
тогда сюжет виден только без фильтра, и это тоже правильно: корзина «прочее»
никого не привлекает, а пустая корзина врёт.

# ponytail: словарь, а не модель — потолок в редких формулировках («large
# language model» без слова AI ловится, «трансформерная архитектура» — нет).
# Правится добавлением слова в KEYS; понадобится больше — на место of() встаёт
# классификатор с тем же интерфейсом.
"""
import re

# Порядок — редакционный: он же порядок кнопок на витрине и разрешение ничьей,
# когда сюжет попал сразу в несколько корзин.
#
# Правила подбора слов, выведенные из промахов на живом корпусе:
#   • слово-омоним берётся только в связке: «space» — это и место на диске, и
#     свободное место в общежитии, поэтому космос ловится по «spacecraft»,
#     «satellite», «orbit», а не по «space»;
#   • русские формы нужны рядом с английскими: в ленте и переводы, и оригиналы
#     на русском, а «спутник» из «satellite» регуляркой не выводится;
#   • корень вместо перечня форм («квант» покрывает и «квантовый», и
#     «квантовых»), но только там, где корень не даёт чужих слов.
KEYS = [
    ("ai", "ИИ", [
        r"artificial intelligence", r"\bA\.?I\.?\b", r"machine learning",
        r"deep learning", r"neural network", r"large language model", r"\bLLMs?\b",
        r"generative ai", r"chatbot", r"chatgpt", r"copilot", r"openai",
        r"anthropic", r"deepseek", r"ai model", r"ai chip", r"ai data cent",
        r"ai startup", r"ai polic", r"\bAI Act\b",
        # «ИИ» кириллицей — не украшение списка: заголовки на витрине русские,
        # и аббревиатура в них встречается чаще развёрнутого названия.
        r"\bИИ\b", r"\bИИ-\w+", r"искусственн\w+ интеллект", r"нейросет",
        r"нейронн\w+ сет", r"машинн\w+ обучен", r"языков\w+ модел", r"чат-?бот",
        r"генеративн\w+ (?:модел|ии|сет)",
    ]),
    ("chips", "Чипы", [
        r"semiconductor", r"microchip", r"\bchips?\b", r"chipmaker", r"foundry",
        r"\bwafers?\b", r"lithograph", r"fabless", r"chip fab", r"nanomet",
        r"integrated circuit", r"\bTSMC\b", r"\bASML\b",
        r"полупроводник", r"микросхем", r"микрочип", r"\bчип\w*", r"литограф",
    ]),
    ("space", "Космос", [
        r"satellite", r"spacecraft", r"space agency", r"space station",
        r"space mission", r"space programme", r"space program", r"launch vehicle",
        r"\borbit", r"rocket launch", r"astronaut", r"cosmonaut", r"\blunar\b",
        r"moon mission", r"mars mission", r"\bISRO\b", r"\bNASA\b", r"\bSpaceX\b",
        r"спутник", r"космическ", r"космонавт", r"космодром", r"\bМКС\b",
        r"ракет\w+ носител", r"орбит", r"лунн\w+ (?:мисси|станц|программ|модул)",
    ]),
    ("energy", "Энергетика", [
        r"renewable energy", r"solar power", r"solar plant", r"solar park",
        r"photovoltaic", r"wind power", r"wind farm",
        r"nuclear (?:power|plant|reactor|energy)", r"\breactors?\b",
        r"hydropower", r"hydrogen", r"geothermal", r"biofuel", r"power grid",
        r"electricity grid", r"transmission line", r"energy storage",
        r"battery (?:plant|storage|factory)", r"gigafactory", r"oil refinery",
        r"\bLNG\b", r"energy transition", r"megawatt", r"gigawatt",
        r"возобновляем", r"солнечн\w+ (?:электростанц|энерг|панел|систем|модул)",
        r"ветропарк", r"ветроэнергет", r"атомн\w+ (?:станц|энерг|реактор)",
        r"ядерн\w+ (?:станц|энерг|реактор)", r"\bАЭС\b", r"\bГЭС\b",
        r"гидроэлектро", r"электростанц", r"нефтеперерабат", r"энергосет",
        r"водородн", r"накопител\w+ энерги", r"мегаватт", r"гигаватт",
    ]),
    ("cyber", "Кибербезопасность", [
        r"cyberattack", r"cyber attack", r"cybersecurity", r"cyber security",
        r"ransomware", r"malware", r"spyware", r"data breach", r"data leak",
        r"hacking group", r"\bhackers?\b", r"phishing campaign", r"\bDDoS\b",
        r"zero-day", r"vulnerabilit", r"encryption",
        r"кибератак", r"кибербезопасн", r"вымогател", r"вредонос",
        r"утечк\w+ данных", r"\bвзлом\w*", r"шифрован", r"уязвимост",
    ]),
    ("quantum", "Кванты", [
        r"quantum", r"qubit", r"post-quantum", r"квант", r"кубит",
    ]),
    ("bio", "Биотех и медицина", [
        r"biotech", r"vaccine", r"clinical trial", r"gene editing", r"genome",
        r"genetic", r"\bCRISPR\b", r"stem cell", r"drug (?:trial|approval|discovery)",
        r"pharmaceutical", r"cancer research", r"antibiotic", r"pathogen", r"mRNA",
        r"биотех", r"вакцин", r"клиническ\w+ испытан", r"геном",
        r"ген\w+ редактир", r"стволов\w+ клет", r"фармацевт", r"антибиотик",
    ]),
    ("telecom", "Связь и интернет", [
        r"\b5G\b", r"\b6G\b", r"broadband", r"fibre optic", r"fiber optic",
        r"spectrum auction", r"telecom", r"mobile network", r"base station",
        r"undersea cable", r"submarine cable", r"data cent(?:re|er)",
        r"cloud region", r"internet shutdown", r"starlink",
        r"широкополос", r"оптоволок", r"сотов\w+ связ", r"базов\w+ станц",
        r"центр\w* обработки данных", r"\bЦОД\b", r"подводн\w+ кабел",
    ]),
    ("robots", "Роботы", [
        r"robotic", r"\brobots?\b", r"humanoid", r"cobot", r"drone deliver",
        r"autonomous vehicle", r"self-driving",
        r"робот", r"беспилотн\w+ (?:автомоб|транспорт|такси)",
        r"автономн\w+ (?:вожден|транспорт)",
    ]),
    ("physics", "Физика и материалы", [
        r"superconduct", r"graphene", r"nanomaterial", r"photonic",
        r"materials science", r"perovskite", r"particle accelerator",
        r"fusion (?:reactor|energy|experiment)", r"tokamak",
        # Голого «лазера» тут нет намеренно: в ленте это принтер, косметология и
        # лазерная указка чаще, чем оптика. Настоящая фотоника ловится соседями.
        r"laser (?:beam|pulse|cool|weapon|fusion|physic)", r"femtosecond",
        r"сверхпровод", r"графен", r"фотонн", r"наноматериал", r"материаловед",
        r"перовскит", r"ускорител\w+ частиц", r"термоядерн", r"токамак",
    ]),
    ("digital", "Цифровое государство", [
        r"digital government", r"e-government", r"digital public infrastructure",
        r"digital id\b", r"digital identity", r"data protection (?:law|bill|act|rules)",
        r"data localis", r"data localiz", r"digital economy", r"e-governance",
        # Цифровая грамотность сюда не входит: корзина про государство и его
        # реестры, а грамотность — это про школу, другая ось.
        r"цифров\w+ (?:государств|услуг|удостоверен|экономик)",
        r"электронн\w+ правительств", r"персональн\w+ данн", r"госуслуг",
    ]),
    ("edu", "Университеты", [
        r"universit", r"polytechnic", r"\bcampus\b", r"undergraduate",
        r"postgraduate", r"\bPhD\b", r"doctoral", r"scholarship",
        r"student enrol", r"research institute", r"academy of sciences",
        r"peer-reviewed", r"\bacademic\b", r"higher education",
        r"университет", r"\bвуз\w*", r"студент", r"аспирант", r"докторант",
        r"стипенди", r"академи\w+ наук", r"научн\w+ институт",
        r"высш\w+ образован",
    ]),
]

# Одна регулярка на тему из перечня выше. Перечнем, а не одной строкой с «|»:
# буквальный пробел в «artificial intelligence» значащий, и режим re.X, в
# котором такой перечень можно было бы разложить по строкам, эти пробелы молча
# выбрасывает — фраза из двух слов не матчится вовсе, а ошибка тихая.
_RX = [(slug, name, re.compile("|".join(pats), re.I | re.U))
       for slug, name, pats in KEYS]

ALL = [(slug, name) for slug, name, _p in _RX]

# Сколько текста статьи смотрим. Тема названа в заголовке и первых абзацах; в
# подвале длинного материала найдётся любое слово, и «университет» из строки
# «автор — выпускник такого-то университета» записывал бы в вузы половину ленты.
BODY_CHARS = 700

# Сколько разных слов темы должно найтись в теле, если заголовок про неё молчит.
# Одного мало: на живом корпусе одиночное упоминание в теле — это землетрясение,
# где по пути назван реактор, и приговор конкурса, где назван университет
# докладчика. Двух разных слов случайно уже не набирается.
BODY_MIN = 2


def of(title, *body) -> list:
    """Заголовок и тело сюжета → слаги тем, самая явная первой.

    Заголовок весит больше тела, и это не тонкая настройка, а само правило
    отбора: новость называет свою тему в заголовке, а в теле любое слово может
    оказаться по пути. Слово из заголовка засчитывается сразу, слова из тела —
    только начиная с BODY_MIN разных.

    Разных, а не всего: пять раз «спутник» в одной новости — та же одна тема, а
    «спутник» рядом с «орбитой» и «космодромом» говорит, что сюжет и правда про
    космос. Ничья решается порядком KEYS.
    """
    head = " ".join(p for p in ([title] if isinstance(title, str) else title or []) if p)
    tail = " ".join(p[:BODY_CHARS] for p in body if p)
    if not (head or tail):
        return []
    hits = []
    for i, (slug, _name, rx) in enumerate(_RX):
        top = {m.group(0).lower() for m in rx.finditer(head)}
        deep = {m.group(0).lower() for m in rx.finditer(tail)} - top
        if top or len(deep) >= BODY_MIN:
            # Названная в заголовке тема идёт впереди любой набранной по телу,
            # сколько бы слов та ни набрала: заголовок говорит, о чём сюжет, а
            # тело — чего он по пути коснулось.
            hits.append((not top, -(len(top) + len(deep)), i, slug))
    hits.sort()
    return [slug for _t, _n, _i, slug in hits]


def mask(slugs) -> int:
    """Темы сюжета одним числом — так они едут в search.json. Список слагов на
    шесть тысяч сюжетов весит впятеро больше самого индекса."""
    bit = {slug: 1 << i for i, (slug, _n) in enumerate(ALL)}
    return sum(bit[s] for s in slugs if s in bit)


def _selfcheck():
    assert of("India launches new communications satellite from Sriharikota") == ["space"]
    assert of("Расширение сети 5G и строительство ЦОД") == ["telecom"]
    # Несколько тем — это норма, а не ошибка отбора; первой идёт та, о которой
    # в тексте больше разных слов.
    both = of("TSMC to build a semiconductor foundry for AI chips",
              "The chipmaker said the fab will supply artificial intelligence accelerators.")
    assert both[0] == "chips" and "ai" in both, both
    # Омонимы: свободное место и штатное расписание — не космос и не вузы.
    assert of("The ministry freed up office space for staff") == []
    assert of("Quantum computing lab opens with 50-qubit machine") == ["quantum"]
    assert of("Атомная станция вышла на проектную мощность") == ["energy"]
    assert of("Ransomware group hit the national registry, data leak confirmed") == ["cyber"]
    assert of("") == [] and of(None, "") == []
    # Тело одним словом тему не даёт — иначе землетрясение, по пути назвавшее
    # реактор, попадало бы в энергетику. Двумя разными — даёт.
    assert of("Magnitude 7.1 earthquake hits Kyushu",
              "The plant reactor was shut down as a precaution.") == []
    assert of("Magnitude 7.1 earthquake hits Kyushu",
              "The nuclear plant reactor shut down; the power grid held.") == ["energy"]
    # Заголовок весит больше тела: названная тема стоит выше набранной.
    assert of("Satellite launched from Sriharikota",
              "The chip and semiconductor industry watched the wafer supply.") == \
        ["space", "chips"]
    # Хвост длинной статьи не считается: тема названа в начале.
    assert of("Rice harvest report", "x" * BODY_CHARS + " university campus") == []

    assert mask(["ai"]) == 1
    assert mask(["ai", "chips"]) == 3
    assert mask(["нет такой темы"]) == 0
    assert len(ALL) <= 16, "маска в search.json рассчитана на 16 тем"
    print("topics: ok")


if __name__ == "__main__":
    _selfcheck()
