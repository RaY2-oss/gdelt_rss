"""Границы стран для карты витрины: Natural Earth 110m → data/borders.json.

Запускается руками и редко (границы меняются реже, чем новости):

    ./venv/bin/python make_borders.py

Сырой геоджейсон (0,8 МБ) в репозиторий не кладётся — скачивается на лету,
пережёвывается и выбрасывается. На выходе ~100 КБ путей SVG в системе координат
самой карты: x = (долгота - запад) * 10, y = (север - широта) * 10, целые
десятые градуса. Точнее не нужно: 110m и сама грубее.

Всё, что вылезает за рамку Азии и Африки, прижимается к её краю, а не
вырезается по-настоящему: viewBox всё равно обрежет, зато Россия с её
двадцатью тысячами точек до Чукотки схлопывается в пару отрезков по кромке.
"""
import json
import urllib.request

import settings

# Та же рамка, что у карты в site.py; там проверка сходимости в selfcheck.
GEO_BOX = (-21, 146, 52, -33)  # запад, восток, север, юг

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


def ring_to_path(ring, box, keep=1.0):
    """Кольцо координат → фрагмент d, прижатый к рамке и без повторов точек.

    keep — порог площади в квадратных десятых градуса: острова мельче него на
    110-м масштабе всё равно в один пиксель, а места в файле занимают много.
    """
    west, east, north, south = box
    pts, last = [], None
    for lon, lat in ring:
        x = round((min(max(lon, west), east) - west) * 10)
        y = round((north - min(max(lat, south), north)) * 10)
        if (x, y) != last:
            pts.append((x, y))
            last = (x, y)
    if len(pts) < 4:
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


def shape(feature, box):
    geom = feature["geometry"]
    rings = ([geom["coordinates"]] if geom["type"] == "Polygon"
             else geom["coordinates"])
    return "".join(ring_to_path(r[0], box) for r in rings)


def main():
    raw = json.loads(urllib.request.urlopen(SRC, timeout=60).read())
    west, east, north, south = GEO_BOX

    by_name = {}
    for f in raw["features"]:
        for k in ("ADMIN", "NAME", "NAME_LONG", "BRK_NAME", "GEOUNIT"):
            if f["properties"].get(k):
                by_name.setdefault(f["properties"][k], f)

    paths, missing = {}, []
    for key in settings.COUNTRIES:
        name = ALIAS.get(key) or key.replace("_", " ").title()
        f = by_name.get(name)
        d = shape(f, GEO_BOX) if f else ""
        if d:
            paths[key] = d
        else:
            missing.append(key)  # микрогосударства — точкой, как раньше

    # Соседи: без них Азия и Африка висят в пустоте. Только контур, без ссылок.
    ours = {ALIAS.get(k) or k.replace("_", " ").title() for k in settings.COUNTRIES}
    rest = []
    for f in raw["features"]:
        if f["properties"]["ADMIN"] in ours:
            continue
        lons = []
        lats = []
        geom = f["geometry"]
        polys = ([geom["coordinates"]] if geom["type"] == "Polygon"
                 else geom["coordinates"])
        for p in polys:
            for lon, lat in p[0]:
                lons.append(lon)
                lats.append(lat)
        if not lons or min(lons) > east or max(lons) < west \
                or min(lats) > north or max(lats) < south:
            continue
        d = shape(f, GEO_BOX)
        if d:
            rest.append(d)

    out = {"box": list(GEO_BOX), "paths": paths, "rest": "".join(rest),
           "dots": missing}
    with open("data/borders.json", "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print("границы:", len(paths), "стран,", len(missing), "точкой:", missing)
    print("соседей:", len(rest), "· файл",
          round(len(json.dumps(out)) / 1024), "КБ")


if __name__ == "__main__":
    main()
