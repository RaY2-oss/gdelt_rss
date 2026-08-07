"""Границы стран для карты витрины: Natural Earth 110m → data/borders.json.

Запускается руками и редко (границы меняются реже, чем новости):

    ./venv/bin/python make_borders.py

Сырой геоджейсон (0,8 МБ) в репозиторий не кладётся — скачивается на лету,
пережёвывается и выбрасывается. На выходе ~100 КБ путей SVG уже в системе
координат картинки, поэтому в странице лежит готовая геометрия, а не
библиотека карт.

Что здесь важно и чего раньше не было.

1. Проекция. Раньше долгота шла в x, широта в y напрямую — то есть карта была
   в равнопрямоугольной проекции, где Казахстан вдвое шире правды, а Нигерия
   вдвое уже. Теперь Equal Earth (Šavrič—Patterson—Jenny, 2018): равновеликая,
   то есть площадь страны на картинке пропорциональна настоящей, и при этом
   без «ушей» Моллвейде. Ровно то, что нужно карте охвата: заливка означает
   величину, и площадь под ней не должна врать.

2. Настоящая обрезка. Раньше всё, что вылезало за рамку, ПРИЖИМАЛОСЬ к её
   краю: у Казахстана срезался верх, у ЮАР — низ, и обе страны выглядели
   расплющенными о раму. Теперь рамка заведомо шире охвата (ни одна из наших
   стран её не касается), а соседи режутся по-честному, алгоритмом
   Сазерленда—Ходжмана. Держать соседей обрезанными всё равно надо: без этого
   Россия до Чукотки и обе Америки утащили бы в файл лишние сотни килобайт.

3. Сетка. Параллели и меридианы считаются той же проекцией и лежат в файле
   путями. Раньше их рисовал CSS полосками фона — прямыми и равномерными, то
   есть в проекции, которой на карте нет.
"""
import json
import math
import urllib.request

import settings

# Рамка. Шире охвата с запасом: западнее всех Кабо-Верде (−25,4°), восточнее
# всех Япония (145,8°), севернее всех Казахстан (55,4°), южнее всех ЮАР
# (−34,8°). Ни одна страна охвата рамки не касается — обрезка достаётся только
# соседям, а у них ровный срез по краю кадра и есть правильный вид.
GEO_BOX = (-27.0, 148.0, 57.0, -37.0)  # запад, восток, север, юг

# Ширина картинки в единицах viewBox; высота считается из проекции, чтобы
# площади не поехали. Тысяча восемьсот — это примерно десятая доля градуса на
# единицу по экватору, то есть та же точность, что и у 110m.
MAP_W = 1800

# Шаг сетки, градусы.
GRID_LON, GRID_LAT = 20, 15

SRC = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
       "master/geojson/ne_110m_admin_0_countries.geojson")

# Ключ фида → имя в Natural Earth там, где они расходятся. Остальные восемьдесят
# сходятся по «sri_lanka» → «Sri Lanka».
ALIAS = {
    "ivory_coast": "Ivory Coast", "east_timor": "East Timor",
    "uae": "United Arab Emirates", "dr_congo": "Democratic Republic of the Congo",
    "congo": "Republic of the Congo", "car": "Central African Republic",
    "cape_verde": "Cape Verde", "sao_tome": "São Tomé and Principe",
    "swaziland": "eSwatini", "eswatini": "eSwatini", "gambia": "The Gambia",
    "guinea_bissau": "Guinea-Bissau", "south_sudan": "South Sudan",
    "north_korea": "North Korea", "south_korea": "South Korea",
    "myanmar": "Myanmar", "laos": "Laos", "syria": "Syria", "iran": "Iran",
    "tanzania": "United Republic of Tanzania", "libya": "Libya",
    "equatorial_guinea": "Equatorial Guinea", "sierra_leone": "Sierra Leone",
    "burkina_faso": "Burkina Faso", "saudi_arabia": "Saudi Arabia",
    "south_africa": "South Africa", "western_sahara": "Western Sahara",
}

# Кому на 110-м масштабе фигуры не досталось. Гонконг и Макао разведены руками:
# в масштабе всей Евразии разница в полградуса — три пикселя, и точки слипались.
DOTS = {
    "singapore": (104.4, 0.4), "bahrain": (50.2, 26.6),
    "hong_kong": (115.6, 22.6), "macau": (112.0, 20.6),
    "maldives": (73.0, 3.5), "mauritius": (57.6, -20.3),
}

# ── Проекция ──────────────────────────────────────────────────────────────
# Equal Earth. Коэффициенты — из статьи авторов, те же, что в PROJ.
_A1, _A2, _A3, _A4 = 1.340264, -0.081106, 0.000893, 0.003796
_M = math.sqrt(3) / 2


def equal_earth(lon, lat):
    """Градусы → безразмерные координаты проекции (x вправо, y вверх)."""
    lam, phi = math.radians(lon), math.radians(lat)
    th = math.asin(_M * math.sin(phi))
    t2 = th * th
    t6 = t2 * t2 * t2
    x = lam * math.cos(th) / (_M * (_A1 + 3 * _A2 * t2 + t6 * (7 * _A3 + 9 * _A4 * t2)))
    y = th * (_A1 + _A2 * t2 + t6 * (_A3 + _A4 * t2))
    return x, y


def make_frame(box, width):
    """Рамка → (сдвиг, масштаб, высота картинки).

    Ширина берётся по экватору: у Equal Earth параллель тем короче, чем дальше
    от него, так что самая широкая — нулевая, и рамка по ней и строится.
    """
    west, east, north, south = box
    x0, _ = equal_earth(west, 0.0)
    x1, _ = equal_earth(east, 0.0)
    _, ytop = equal_earth(0.0, north)
    _, ybot = equal_earth(0.0, south)
    k = width / (x1 - x0)
    return (x0, ytop, k), (ytop - ybot) * k


def project(lon, lat, frame):
    x0, ytop, k = frame
    x, y = equal_earth(lon, lat)
    return ((x - x0) * k, (ytop - y) * k)


# ── Обрезка ───────────────────────────────────────────────────────────────
def clip(poly, w, h):
    """Сазерленд—Ходжман: многоугольник ∩ прямоугольник кадра.

    Именно обрезка, а не прижатие к краю: точка за кадром не переезжает на
    рамку (отчего страна и выглядела расплющенной), а заменяется на пересечение
    её ребра с рамкой.
    """
    for axis, limit, keep_low in ((0, 0.0, False), (0, w, True),
                                  (1, 0.0, False), (1, h, True)):
        if not poly:
            return []
        out = []
        for i in range(len(poly)):
            a, b = poly[i - 1], poly[i]
            ka = a[axis] <= limit if keep_low else a[axis] >= limit
            kb = b[axis] <= limit if keep_low else b[axis] >= limit
            if ka != kb:
                d = b[axis] - a[axis]
                t = 0.0 if d == 0 else (limit - a[axis]) / d
                cut = (limit, a[1] + (b[1] - a[1]) * t) if axis == 0 \
                    else (a[0] + (b[0] - a[0]) * t, limit)
            if kb:
                if not ka:
                    out.append(cut)
                out.append(b)
            elif ka:
                out.append(cut)
        poly = out
    return poly


def ring_to_path(ring, frame, w, h, keep=1.0):
    """Кольцо координат → фрагмент d, обрезанный кадром и без повторов точек.

    keep — порог площади в квадратных единицах картинки: остров мельче него
    всё равно ложится в один пиксель, а места в файле занимает много.
    """
    if len(ring) < 4:
        return ""
    lons = [p[0] for p in ring]
    if max(lons) - min(lons) > 180:
        return ""  # кольцо через 180-й меридиан: развернулось бы во всю карту
    poly = clip([project(lon, lat, frame) for lon, lat in ring], w, h)

    pts, last = [], None
    for x, y in poly:
        p = (round(x), round(y))
        if p != last:
            pts.append(p)
            last = p
    while len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    if len(pts) < 3:
        return ""

    area = abs(sum(pts[i][0] * pts[i - 1][1] - pts[i - 1][0] * pts[i][1]
                   for i in range(len(pts)))) / 2
    if area < keep:
        return ""

    head = pts[0]
    out = ["M%d %d" % head]
    for x, y in pts[1:]:
        out.append("l%d %d" % (x - head[0], y - head[1]))
        head = (x, y)
    return "".join(out) + "z"


def outlines(feature):
    """Внешние кольца всех кусков фигуры. Дырки не берём: единственная на всю
    выборку — Лесото внутри ЮАР, и ради неё городить вычитание не стоит."""
    geom = feature["geometry"]
    polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
             else geom["coordinates"])
    return [p[0] for p in polys]


def shape(feature, frame, w, h):
    return "".join(ring_to_path(r, frame, w, h) for r in outlines(feature))


# ── Место для подписи ─────────────────────────────────────────────────────
def _inside(px, py, poly):
    """Луч вправо: нечётное число пересечений — точка внутри."""
    hit = False
    for i in range(len(poly)):
        (ax, ay), (bx, by) = poly[i - 1], poly[i]
        if (ay > py) != (by > py) and px < ax + (py - ay) / (by - ay) * (bx - ax):
            hit = not hit
    return hit


def _edge_dist(px, py, poly):
    """Расстояние до ближайшего ребра — сколько места вокруг точки."""
    best = float("inf")
    for i in range(len(poly)):
        (ax, ay), (bx, by) = poly[i - 1], poly[i]
        dx, dy = bx - ax, by - ay
        n = dx * dx + dy * dy
        t = 0.0 if n == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / n))
        best = min(best, math.hypot(px - ax - t * dx, py - ay - t * dy))
    return best


def label_spot(feature, frame, w, h):
    """Куда ставить имя страны: центр самого большого круга, влезающего внутрь.

    Не центр тяжести и не центр рамки: у Вьетнама и Сомали фигура вогнутая, и
    оба центра приходятся на соседа, а у Индонезии — на море. Круг ищем
    перебором по сетке с двумя уточнениями вокруг лучшей точки. Способ грубый,
    зато скрипт офлайновый и гоняется раз в год: сто тысяч проверок точки
    дешевле, чем очередь с приоритетом ради тех же координат.

    Возвращает (x, y, r) в координатах кадра, r — радиус этого круга. Ради него
    всё и затевалось: подписи разных стран лежат в непересекающихся кругах
    внутри непересекающихся фигур, поэтому наехать друг на друга не могут, и
    разбирать столкновения не приходится.
    """
    best = None
    for ring in outlines(feature):
        lons = [p[0] for p in ring]
        if max(lons) - min(lons) > 180:
            continue
        poly = clip([project(lon, lat, frame) for lon, lat in ring], w, h)
        if len(poly) < 3:
            continue
        area = abs(sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1]
                       for i in range(len(poly)))) / 2
        if best is None or area > best[0]:
            best = (area, poly)
    if best is None:
        return None

    poly = best[1]
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
    spot = None
    for _ in range(3):
        sx = (hi_x - lo_x) / 12 or 1.0
        sy = (hi_y - lo_y) / 12 or 1.0
        for i in range(13):
            for j in range(13):
                px, py = lo_x + i * sx, lo_y + j * sy
                if not _inside(px, py, poly):
                    continue
                r = _edge_dist(px, py, poly)
                if spot is None or r > spot[2]:
                    spot = (px, py, r)
        if spot is None:
            return None
        lo_x, hi_x = spot[0] - sx, spot[0] + sx
        lo_y, hi_y = spot[1] - sy, spot[1] + sy
    return (round(spot[0]), round(spot[1]), round(spot[2]))


def graticule(box, frame, w, h):
    """Параллели и меридианы той же проекцией.

    Параллель у Equal Earth — прямая (y зависит только от широты), поэтому её
    хватает провести через весь кадр. Меридиан — кривая, и его приходится
    вести по точкам; сходятся они к полюсам сами, за кадр не выходят.
    """
    west, east, north, south = box
    out = []

    lat = math.ceil(south / GRID_LAT) * GRID_LAT
    while lat <= north:
        y = round(project(0.0, lat, frame)[1])
        out.append("M0 %dH%d" % (y, w))
        lat += GRID_LAT

    lon = math.ceil(west / GRID_LON) * GRID_LON
    while lon <= east:
        pts = [project(lon, south + t * (north - south) / 24.0, frame)
               for t in range(25)]
        out.append("M%d %d" % (round(pts[0][0]), round(pts[0][1]))
                   + "".join("L%d %d" % (round(x), round(y)) for x, y in pts[1:]))
        lon += GRID_LON
    return "".join(out)


def main():
    raw = json.loads(urllib.request.urlopen(SRC, timeout=60).read())
    west, east, north, south = GEO_BOX
    frame, height = make_frame(GEO_BOX, MAP_W)
    h = round(height)

    by_name = {}
    for f in raw["features"]:
        for k in ("ADMIN", "NAME", "NAME_LONG", "BRK_NAME", "GEOUNIT"):
            if f["properties"].get(k):
                by_name.setdefault(f["properties"][k], f)

    paths, spots, missing = {}, {}, []
    for key in settings.COUNTRIES:
        name = ALIAS.get(key) or key.replace("_", " ").title()
        f = by_name.get(name)
        d = shape(f, frame, MAP_W, h) if f else ""
        if d:
            paths[key] = d
            spot = label_spot(f, frame, MAP_W, h)
            if spot:
                spots[key] = list(spot)
        else:
            missing.append(key)
    assert set(missing) == set(DOTS), (
        "без фигуры и без точки: %s; лишние точки: %s"
        % (sorted(set(missing) - set(DOTS)), sorted(set(DOTS) - set(missing))))

    # Соседи: без них Азия и Африка висят в пустоте. Только контур, без ссылок.
    ours = {ALIAS.get(k) or k.replace("_", " ").title() for k in settings.COUNTRIES}
    rest = []
    for f in raw["features"]:
        if f["properties"]["ADMIN"] in ours:
            continue
        geom = f["geometry"]
        polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
                 else geom["coordinates"])
        pts = [p for poly in polys for p in poly[0]]
        if not pts:
            continue
        lons = [p[0] for p in pts]
        lats = [p[1] for p in pts]
        if min(lons) > east or max(lons) < west \
                or min(lats) > north or max(lats) < south:
            continue
        d = shape(f, frame, MAP_W, h)
        if d:
            rest.append(d)

    out = {
        "box": list(GEO_BOX),
        "w": MAP_W,
        "h": h,
        "paths": paths,
        # куда и каким кеглем ложится имя страны — см. label_spot
        "spots": spots,
        "rest": "".join(rest),
        "grid": graticule(GEO_BOX, frame, MAP_W, h),
        "dots": {k: [round(v) for v in project(lon, lat, frame)]
                 for k, (lon, lat) in DOTS.items()},
    }
    with open("data/borders.json", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("кадр: %d×%d, границы: %d стран, точкой: %d %s"
          % (MAP_W, h, len(paths), len(missing), sorted(missing)))
    print("соседей:", len(rest), "· файл",
          round(len(json.dumps(out)) / 1024), "КБ")


def _selfcheck():
    """Проверки проекции и обрезки. Сеть не трогают, потому и стоят здесь."""
    # Равновеликость — единственное, ради чего проекция и выбрана: площадь
    # клетки сетки на картинке должна относиться к её настоящей площади на шаре
    # одинаково на экваторе и на широте Казахстана.
    def flat(lon0, lat0, d=10.0, n=8):
        ring = ([(lon0 + d * i / n, lat0) for i in range(n)]
                + [(lon0 + d, lat0 + d * i / n) for i in range(n)]
                + [(lon0 + d - d * i / n, lat0 + d) for i in range(n)]
                + [(lon0, lat0 + d - d * i / n) for i in range(n)])
        p = [equal_earth(lo, la) for lo, la in ring]
        return abs(sum(p[i][0] * p[i - 1][1] - p[i - 1][0] * p[i][1]
                       for i in range(len(p)))) / 2

    def round_(lat0, d=10.0):
        return math.radians(d) * (math.sin(math.radians(lat0 + d))
                                  - math.sin(math.radians(lat0)))

    base = flat(0, 0) / round_(0)
    for lat0 in (20, 40, 50):
        assert abs(flat(0, lat0) / round_(lat0) / base - 1) < 0.02, \
            "проекция не равновеликая на широте %d" % lat0

    frame, height = make_frame(GEO_BOX, MAP_W)
    w, h = MAP_W, round(height)
    assert 1.3 < w / h < 1.7, "кадр перекосило: %d×%d" % (w, h)
    # ни одна страна охвата за рамку не выходит — иначе её опять расплющит
    for lon, lat in ((-25.4, 15), (145.8, 43), (67, 55.4), (24, -34.8)):
        x, y = project(lon, lat, frame)
        assert 0 < x < w and 0 < y < h, "край охвата вне кадра: %r" % ((lon, lat),)
    for lon, lat in DOTS.values():
        x, y = project(lon, lat, frame)
        assert 0 < x < w and 0 < y < h, "точка вне кадра"

    # обрезка: квадрат, наполовину вылезающий влево, должен упереться в x=0 и
    # сохранить обе правые вершины — прижатие дало бы вырожденный треугольник
    cut = clip([(-50.0, 10.0), (50.0, 10.0), (50.0, 90.0), (-50.0, 90.0)], 200, 200)
    assert min(p[0] for p in cut) == 0.0 and max(p[0] for p in cut) == 50.0
    assert len(cut) == 4, cut
    assert clip([(-9.0, -9.0), (-1.0, -9.0), (-1.0, -1.0)], 200, 200) == []

    ring = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    d = ring_to_path(ring, frame, w, h)
    assert d.startswith("M") and d.endswith("z") and "l" in d, d
    assert ring_to_path([(0, 0), (0.01, 0), (0.01, 0.01), (0, 0)], frame, w, h) == ""
    # кольцо через 180-й меридиан не должно развернуться во всю карту
    assert ring_to_path([(179, 60), (-179, 60), (-179, 61), (179, 61)],
                        frame, w, h) == ""

    # Место под подпись. Ради вогнутых фигур всё и написано, поэтому проверяем
    # на «уголке»: его центр тяжести лежит в вырезе, снаружи страны, и подпись
    # оттуда уехала бы на соседа. Настоящее место — в середине толстой полки.
    ell = [(0.0, 0.0), (30.0, 0.0), (30.0, 10.0), (10.0, 10.0),
           (10.0, 40.0), (0.0, 40.0)]
    x, y, r = label_spot({"geometry": {"type": "Polygon",
                                       "coordinates": [ell + [ell[0]]]}},
                         frame, w, h)
    poly = clip([project(lo, la, frame) for lo, la in ell], w, h)
    assert _inside(x, y, poly), "подпись легла вне фигуры: %r" % ((x, y),)
    assert _edge_dist(x, y, poly) >= r - 1, "круг подписи вылезает за край"
    # и он действительно самый большой из найденных, а не первый попавшийся
    assert r > _edge_dist(*project(5.0, 5.0, frame), poly), "круг не наибольший"

    g = graticule(GEO_BOX, frame, w, h)
    assert g.count("M") == len(range(-30, 57, GRID_LAT)) + len(range(-20, 149, GRID_LON))
    print("make_borders: проверки прошли")


if __name__ == "__main__":
    _selfcheck()
    main()
