#!/usr/bin/env python3
"""
Build ward-level choropleths of Cleveland 311 service requests, and of plain
point-location datasets (e.g. trash can inventories) spatially joined to wards.

One script drives every map so they stay visually identical. Add a 311-style
dataset to DATASETS, or a bare address/lat/lon dataset to POINT_DATASETS, and
it gets the same treatment.

Inputs
------
  data/<dataset>.csv            311 service requests, one row per report
  data/<dataset>.csv            point datasets: a Geocodio export, one row per
                                thing, with "Geocodio Latitude"/"Longitude"
  data/<slug>-geocode-fixes.json  hand corrections for rows Geocodio misplaced
  assets/wards.geojson          City of Cleveland 2026 ward boundaries
  assets/template.html          shared CPD-styled HTML shell (311 datasets)
  assets/template-points.html   shared HTML shell (point datasets)

Output, per dataset
-------------------
  <slug>/index.html            self-contained interactive map
  <slug>/wordpress-embed.html  parent-page listener for the iframe
  <slug>/by_ward.csv           the underlying ward summary table
  <slug>/data.csv              the cleaned row-level table -- one row per
                               request (with its ward) for 311 datasets, one
                               row per location for point datasets

The method, in four steps:
  1. Parse ward polygons out of the local GeoJSON.
  2. Count reports (or spatially join points) per ward.
  3. Normalize -- raw counts favor populous wards, so also compute counts
     per 10,000 residents using ACS census population.
  4. Bin the wards into color classes and write a Leaflet map.
"""

import csv
import json
import math
import os
import re
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
ASSETS_DIR = os.path.join(HERE, "assets")
WARDS_GEOJSON_IN = os.path.join(ASSETS_DIR, "wards.geojson")
TEMPLATE_IN = os.path.join(ASSETS_DIR, "template.html")
TEMPLATE_POINTS_IN = os.path.join(ASSETS_DIR, "template-points.html")
EMBED_TPL_IN = os.path.join(ASSETS_DIR, "wordpress-embed-template.html")

DATASETS = [
    {
        "csv": "debris in street.csv",
        "slug": "debris-in-street",
        "heading": "Where Clevelanders report debris in the street",
        "noun": "\u201cDebris in Street\u201d service requests",
        "caveat": "311 data records reporting, not debris: wards where residents "
                  "call more often will rank higher.",
    },
    {
        "csv": "new container requests 2026-11.csv",
        "slug": "new-container-requests",
        # The city's export reaches back to April 2024, but the map covers
        # January 2025 onward. Requests before this are dropped before any
        # counting, so every figure on the map shares one window.
        "since": (2025, 1, 1),
        "heading": "Where Clevelanders ask for new public trash cans",
        "noun": "\u201cNew Public Street Receptacle\u201d service requests",
        "caveat": "311 data records requests, not need: a ward with an engaged, "
                  "web-savvy population will rank higher than an equally "
                  "under-served ward that does not call.",
    },
]

# Point-location datasets: a bare list of addresses/coordinates rather than
# pre-tallied 311 reports. Each gets a spatial join against the ward polygons
# (see ward_of_point below) so it still gets the same ward-level analysis,
# plus a marker layer for the actual locations.
POINT_DATASETS = [
    {
        "csv": "public receptacles.csv",
        "slug": "public-trashcan-locations",
        "heading": "Where Cleveland’s public trash cans are",
        "noun": "public street trash cans",
        "caveat": "Locations are geocoded from the street addresses and "
                  "intersections on the city\u2019s list and may be off by a few "
                  "dozen feet.",
    },
]

# Census population by 2026 ward.
# Source: City of Cleveland "Ward Profiles" (ward_characteristics) feature service,
# table B01001_001E (total population), ACS 5-year vintage 2020-2024 -- the newest
# available. Values are ACS *estimates*, aggregated from the census tracts assigned
# to each ward, so each carries a margin of error (B01001_001M, 90% confidence).
# Citywide these sum to 366,340.
WARD_POP = {
    1:  (22836, 1525),  2:  (20805, 1613),  3:  (22524, 2030),
    4:  (20490, 1457),  5:  (25905, 1655),  6:  (22030, 1208),
    7:  (28448, 1325),  8:  (22695, 1459),  9:  (24368, 1806),
    10: (25653, 1628),  11: (30081, 1842),  12: (24484, 1834),
    13: (26955, 1583),  14: (23173, 1573),  15: (25893, 1448),
}

WARD_INFO = {
    1:  ("Joseph T. Jones", "jjones@clevelandcitycouncil.gov", "216-664-4944"),
    2:  ("Kevin L. Bishop", "kbishop@clevelandcitycouncil.gov", "216-664-4945"),
    3:  ("Deborah Gray", "dgray@clevelandcitycouncil.gov", "216-664-4941"),
    4:  ("Kris Harsh", "kharsh@clevelandcitycouncil.gov", "216-664-2943"),
    5:  ("Richard A. Starr", "rstarr@clevelandcitycouncil.gov", "216-664-2309"),
    6:  ("Blaine A. Griffin", "bgriffin@clevelandcitycouncil.gov", "216-664-4234"),
    7:  ("Austin N. Davis", "adavis@clevelandcitycouncil.gov", "216-664-2691"),
    8:  ("Stephanie D. Howse-Jones", "showsejones@clevelandcitycouncil.gov", "216-664-2908"),
    9:  ("Kevin Conwell", "kconwell@clevelandcitycouncil.gov", "216-664-4252"),
    10: ("Michael D. Polensek", "mpolensek@clevelandcitycouncil.gov", "216-664-4236"),
    11: ("Nikki Hudson", "nhudson@clevelandcitycouncil.gov", "216-664-4235"),
    12: ("Tanmay Shah", "tshah@clevelandcitycouncil.gov", "216-664-4233"),
    13: ("Brian Kazy", "bkazy@clevelandcitycouncil.gov", "216-664-2942"),
    14: ("Jasmin Santana", "jsantana@clevelandcitycouncil.gov", "216-664-4238"),
    15: ("Charles J. Slife", "cslife@clevelandcitycouncil.gov", "216-664-4239"),
}


# ---------------------------------------------------------------- step 1: wards

def parse_wards(path):
    """Return {ward_number: {...ward info incl. polygon rings...}}."""
    with open(path, encoding="utf-8") as fh:
        geojson = json.load(fh)
    wards = {}

    for feature in geojson["features"]:
        ward_no = int(feature["properties"]["Ward"])
        council_member, email, phone = WARD_INFO[ward_no]

        rings = [
            [[round(lon, 6), round(lat, 6)] for lon, lat in ring]
            for ring in feature["geometry"]["coordinates"]
        ]

        wards[ward_no] = {
            "ward": ward_no,
            "name": f"Ward {ward_no}",
            "council_member": council_member,
            "email": email,
            "phone": phone,
            "population": WARD_POP[ward_no][0],
            "pop_moe": WARD_POP[ward_no][1],
            "rings": rings,
        }

    return wards


def simplify(points, tol_deg):
    """Ramer-Douglas-Peucker. Shrinks the HTML without visibly changing shapes."""
    if len(points) < 3:
        return points

    def perp(p, a, b):
        (x, y), (x1, y1), (x2, y2) = p, a, b
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            return math.hypot(x - x1, y - y1)
        t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
        return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))

    dmax, idx = 0.0, 0
    for i in range(1, len(points) - 1):
        d = perp(points[i], points[0], points[-1])
        if d > dmax:
            dmax, idx = d, i

    if dmax <= tol_deg:
        return [points[0], points[-1]]
    return simplify(points[: idx + 1], tol_deg)[:-1] + simplify(points[idx:], tol_deg)


# -------------------------------------------------------------- step 2: counts

# The city's 311 exports have changed their column spellings at least once:
# the older files use snake_case ("requested_datetime"), the November 2026
# export Title Case with spaces ("Requested Date and Time"). Read either, and
# say so plainly if a needed column is missing rather than silently counting
# every row as unassigned.
#
# Ward is read from the 2026 column, because the map draws 2026 boundaries.
COLUMN_ALIASES = {
    "ward":         ["ward_2026", "Ward (2026)", "ward", "Ward"],
    "status":       ["status_description", "Status"],
    "requested":    ["requested_datetime", "Requested Date and Time"],
    "neighborhood": ["neighborhood", "Neighborhood"],
    "address":      ["Address", "address"],
    "lat":          ["lat", "Latitude"],
    "lon":          ["long", "Longitude"],
}


def resolve_columns(fieldnames):
    """Map our internal field names onto whichever spelling this export uses."""
    present = {(f or "").strip(): f for f in (fieldnames or [])}
    cols, missing = {}, []
    for key, candidates in COLUMN_ALIASES.items():
        for cand in candidates:
            if cand in present:
                cols[key] = present[cand]
                break
        else:
            missing.append(key)
    if missing:
        raise SystemExit(
            "This CSV has no column for: " + ", ".join(missing)
            + "\n  columns seen: " + ", ".join(sorted(c for c in present if c))
            + "\n  add the new spelling to COLUMN_ALIASES in build_maps.py.")
    return cols


def parse_requested(s):
    """'10/31/2026 4:55:30 PM' -> (2026, 10, 31), or None."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return (int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def requested_key(s):
    """The same stamp down to the second, for sorting. Sorts unparseable last."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*([AP]M)?", s or "")
    if not m:
        return (9999, 99, 99, 99, 99, 99)
    mon, day, year, hh, mm, ss, ampm = m.groups()
    hh = int(hh) % 12 if ampm else int(hh)
    if ampm == "PM":
        hh += 12
    return (int(year), int(mon), int(day), hh, int(mm), int(ss))


def load_reports(path, since=None):
    """Return (rows, per-ward counters, per-neighborhood counter, notes).

    One row is one service request. `since` is a (year, month, day) floor on the
    requested date -- requests older than it are dropped before anything is
    counted, so every figure on the map reflects the same window. Each returned
    row carries the ward the city assigned it.
    """
    with open(path, encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cols = resolve_columns(reader.fieldnames)
        raw_rows = list(reader)

    rows = []
    before_window = 0
    by_ward = Counter()
    by_ward_open = Counter()
    by_ward_year = defaultdict(Counter)
    by_neighborhood = defaultdict(Counter)
    unassigned = 0

    for r in raw_rows:
        requested = (r.get(cols["requested"]) or "").strip()
        when = parse_requested(requested)
        if since and (when is None or when < since):
            before_window += 1
            continue

        ward_raw = (r.get(cols["ward"]) or "").strip()
        rec = {
            "requested": requested,
            "year": str(when[0]) if when else "",
            "address": " ".join((r.get(cols["address"]) or "").split()),
            "neighborhood": (r.get(cols["neighborhood"]) or "").strip(),
            "status": (r.get(cols["status"]) or "").strip(),
            "lat": (r.get(cols["lat"]) or "").strip(),
            "lon": (r.get(cols["lon"]) or "").strip(),
            "ward": int(float(ward_raw)) if ward_raw else None,
        }
        rows.append(rec)

        w = rec["ward"]
        if w is None:
            unassigned += 1
            continue
        by_ward[w] += 1
        if rec["status"].lower() == "open":
            by_ward_open[w] += 1
        if rec["year"]:
            by_ward_year[w][rec["year"]] += 1
        if rec["neighborhood"]:
            by_neighborhood[w][rec["neighborhood"]] += 1

    return (rows, by_ward, by_ward_open, by_ward_year, by_neighborhood,
            unassigned, before_window)


def tidy_address(raw):
    """'W 28th St & Detroit Ave,  Cleveland,  44113' -> 'W 28th St & Detroit Ave'.

    The city types these by hand, so spacing, the trailing city/ZIP and the
    occasional 'E.116TH' are all inconsistent. Only the display label is
    normalized here; the geocode itself is whatever Geocodio returned.
    """
    s = " ".join(raw.split())
    s = re.sub(r",\s*(cleveland|cle)\b", "", s, flags=re.I)
    s = re.sub(r",?\s*\bOH\b\.?", "", s, flags=re.I)
    s = re.sub(r",?\s*\b4[0-9]{4}\b", "", s)
    s = s.strip().strip(",").strip()
    # 'E.116TH' / 'E 116TH' -> 'E 116th St'
    s = re.sub(r"\b([ENSW])\.?\s*(\d+)(ST|ND|RD|TH)\b(?!\s*(St|Street))",
               lambda m: f"{m.group(1)} {m.group(2)}{m.group(3).lower()} St", s)
    return s


def load_points(path, fixes_path=None):
    """Read a Geocodio export into a list of {"address", "lat", "lon"}.

    One row is one receptacle, so a corner with four cans is four rows at (near
    enough) one coordinate. Rows named in the fixes file get the coordinate
    recorded there instead; a fix with a null coordinate drops the row, which is
    what an address like a bare "16800" gets.
    """
    fixes = {}
    if fixes_path and os.path.exists(fixes_path):
        with open(fixes_path, encoding="utf-8") as fh:
            # Keys starting with "_" are notes to the reader, not addresses.
            fixes = {k: v for k, v in json.load(fh).items() if not k.startswith("_")}

    points, dropped, repaired = [], [], 0
    with open(path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            raw = (r.get("Address") or "").strip()
            fix = fixes.get(raw)
            if fix is not None and fix.get("lat") is None:
                dropped.append(raw)
                continue
            if fix is not None:
                lat, lon = float(fix["lat"]), float(fix["lon"])
                repaired += 1
            else:
                slat = (r.get("Geocodio Latitude") or "").strip()
                slon = (r.get("Geocodio Longitude") or "").strip()
                if not slat or not slon:
                    dropped.append(raw or "(blank address)")
                    continue
                lat, lon = float(slat), float(slon)
            points.append({"address": tidy_address(raw) or raw, "lat": lat, "lon": lon})

    return points, dropped, repaired


def group_points(points):
    """Collapse cans that share a coordinate into one badged marker.

    Rounding to five decimals is about a meter -- fine enough that two genuinely
    different spots stay apart, coarse enough that two spellings of the same
    corner land together.
    """
    groups = {}
    for p in points:
        key = (round(p["lat"], 5), round(p["lon"], 5))
        g = groups.setdefault(key, {"lat": p["lat"], "lon": p["lon"],
                                    "n": 0, "labels": Counter()})
        g["n"] += 1
        g["labels"][p["address"]] += 1

    out = []
    for (lat, lon), g in groups.items():
        labels = [a for a, _ in g["labels"].most_common()]
        out.append({"lat": round(lat, 5), "lon": round(lon, 5), "n": g["n"],
                    "address": labels[0], "also": labels[1:4]})
    out.sort(key=lambda g: (-g["n"], g["address"]))
    return out


def point_in_ring(lon, lat, ring):
    """Ray casting: does (lon, lat) fall inside this ring?"""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def ward_of_point(lon, lat, wards):
    """Which ward, if any, contains this point."""
    for w, info in wards.items():
        for ring in info["rings"]:
            if point_in_ring(lon, lat, ring):
                return w
    return None


# --------------------------------------------------- step 3+4: classify & draw

# CPD teal ramp, palest -> primary. Four steps, because the style guide's token
# table defines exactly four teals; inventing a fifth would be off-palette. So the
# choropleth uses 4 quantile classes rather than the more common 5.
COLORS = ["#dbe7e3", "#a9d2cf", "#5fa896", "#23685b"]
N_CLASSES = len(COLORS)


def quantile_breaks(values, k=N_CLASSES):
    """Upper bound of each class, using quantiles so classes are evenly populated."""
    s = sorted(values)
    return [s[min(len(s) - 1, int(math.ceil((i + 1) / k * len(s))) - 1)] for i in range(k)]


def class_of(value, breaks):
    for i, b in enumerate(breaks):
        if value <= b:
            return i
    return len(breaks) - 1


def build(ds):
    """Build one map. `ds` is an entry from DATASETS."""
    outdir = os.path.join(HERE, ds["slug"])
    os.makedirs(outdir, exist_ok=True)
    html_out = os.path.join(outdir, "index.html")
    csv_out = os.path.join(outdir, "by_ward.csv")
    embed_out = os.path.join(outdir, "wordpress-embed.html")

    requests_out = os.path.join(outdir, "data.csv")

    wards = parse_wards(WARDS_GEOJSON_IN)
    (rows, by_ward, by_ward_open, by_ward_year, by_neighborhood,
     unassigned, before_window) = load_reports(
        os.path.join(DATA_DIR, ds["csv"]), ds.get("since"))

    years = sorted({y for c in by_ward_year.values() for y in c})

    summary = []
    for w, info in sorted(wards.items()):
        count = by_ward.get(w, 0)
        pop = info["population"]
        rate = count / pop * 10_000 if pop else 0.0
        # Propagate the ACS margin of error through the rate, so we can say how
        # much of a gap between two wards is actually meaningful.
        rate_moe = rate * (info["pop_moe"] / pop) if pop else 0.0
        top_nb = by_neighborhood[w].most_common(3)
        summary.append({
            "ward": w,
            "name": info["name"],
            "council_member": info["council_member"],
            "email": info["email"],
            "phone": info["phone"],
            "count": count,
            "open": by_ward_open.get(w, 0),
            "population": pop,
            "pop_moe": info["pop_moe"],
            "per_10k": round(rate, 1),
            "per_10k_moe": round(rate_moe, 1),
            "by_year": {y: by_ward_year[w].get(y, 0) for y in years},
            "top_neighborhoods": [{"name": n, "n": c} for n, c in top_nb],
        })

    count_breaks = quantile_breaks([s["count"] for s in summary])
    rate_breaks = quantile_breaks([s["per_10k"] for s in summary])

    features = []
    for s in summary:
        info = wards[s["ward"]]
        rings = [simplify(r, 0.00012) for r in info["rings"]]
        props = dict(s)
        props["count_class"] = class_of(s["count"], count_breaks)
        props["rate_class"] = class_of(s["per_10k"], rate_breaks)
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": rings},
        })

    geojson = {"type": "FeatureCollection", "features": features}

    # ---- summary CSV -----------------------------------------------------
    with open(csv_out, "w", newline="", encoding="utf-8") as fh:
        cols = ["ward", "name", "council_member", "count", "open",
                "population", "population_moe", "per_10k_residents", "per_10k_moe"] \
            + [f"reports_{y}" for y in years] + ["top_neighborhoods"]
        wr = csv.writer(fh)
        wr.writerow(cols)
        for s in sorted(summary, key=lambda r: -r["count"]):
            wr.writerow(
                [s["ward"], s["name"], s["council_member"], s["count"], s["open"],
                 s["population"], s["pop_moe"], s["per_10k"], s["per_10k_moe"]]
                + [s["by_year"][y] for y in years]
                + ["; ".join(f'{n["name"]} ({n["n"]})' for n in s["top_neighborhoods"])]
            )

    # ---- one row per request, with its ward ------------------------------
    with open(requests_out, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["requested", "ward", "neighborhood", "address", "status", "lat", "lon"])
        for r in sorted(rows, key=lambda r: requested_key(r["requested"])):
            wr.writerow([r["requested"], r["ward"] if r["ward"] is not None else "",
                         r["neighborhood"], r["address"], r["status"], r["lat"], r["lon"]])

    # ---- HTML map --------------------------------------------------------
    meta = {
        "total": len(rows),
        "mapped": sum(s["count"] for s in summary),
        "unassigned": unassigned,
        "years": years,
        "count_breaks": count_breaks,
        "rate_breaks": rate_breaks,
        "city_pop": sum(s["population"] for s in summary),
        "colors": COLORS,
        "heading": ds["heading"],
        "noun": ds["noun"],
        "caveat": ds["caveat"],
    }

    html = load_template().replace("__GEOJSON__", json.dumps(geojson, separators=(",", ":")))
    html = html.replace("__META__", json.dumps(meta, separators=(",", ":")))
    with open(html_out, "w", encoding="utf-8") as fh:
        fh.write(html)

    with open(EMBED_TPL_IN, encoding="utf-8") as fh:
        embed = fh.read()
    embed = embed.replace("__SLUG__", ds["slug"]).replace("__HEADING__", ds["heading"])
    with open(embed_out, "w", encoding="utf-8") as fh:
        fh.write(embed)

    print(f"=== {ds['slug']} ===")
    if ds.get("since"):
        y, m, d = ds["since"]
        print(f"window: requests on or after {y}-{m:02d}-{d:02d} "
              f"({before_window} earlier rows dropped)")
    print(f"reports total {len(rows)}, placed in a ward {meta['mapped']}, unassigned {unassigned}")
    print(f"years present {', '.join(years)}")
    print(f"citywide ACS population {meta['city_pop']:,}")
    print(f"count breaks   {count_breaks}")
    print(f"rate breaks    {rate_breaks}")
    print()
    print(f"{'ward':>5} {'count':>6} {'pop':>8} {'per 10k':>9} {'+/-':>6}  council member")
    for s in sorted(summary, key=lambda r: -r["per_10k"]):
        print(f'{s["ward"]:>5} {s["count"]:>6} {s["population"]:>8,} '
              f'{s["per_10k"]:>9.1f} {s["per_10k_moe"]:>6.1f}  {s["council_member"]}')
    print(f"\nwrote {html_out}")
    print(f"wrote {csv_out}")
    print(f"wrote {requests_out}")
    print(f"wrote {embed_out}\n")
    return summary


def build_points(ds):
    """Build one point-location map. `ds` is an entry from POINT_DATASETS."""
    outdir = os.path.join(HERE, ds["slug"])
    os.makedirs(outdir, exist_ok=True)
    html_out = os.path.join(outdir, "index.html")
    csv_out = os.path.join(outdir, "by_ward.csv")
    embed_out = os.path.join(outdir, "wordpress-embed.html")

    locations_out = os.path.join(outdir, "data.csv")

    wards = parse_wards(WARDS_GEOJSON_IN)
    fixes_in = os.path.join(DATA_DIR, f'{ds["slug"]}-geocode-fixes.json')
    points, dropped, repaired = load_points(os.path.join(DATA_DIR, ds["csv"]), fixes_in)

    by_ward = Counter()
    unassigned = 0
    for p in points:
        w = ward_of_point(p["lon"], p["lat"], wards)
        p["ward"] = w
        if w is None:
            unassigned += 1
        else:
            by_ward[w] += 1

    grouped = group_points(points)
    for g in grouped:
        g["ward"] = ward_of_point(g["lon"], g["lat"], wards)

    summary = []
    for w, info in sorted(wards.items()):
        count = by_ward.get(w, 0)
        pop = info["population"]
        rate = count / pop * 10_000 if pop else 0.0
        rate_moe = rate * (info["pop_moe"] / pop) if pop else 0.0
        summary.append({
            "ward": w,
            "name": info["name"],
            "council_member": info["council_member"],
            "email": info["email"],
            "phone": info["phone"],
            "count": count,
            "population": pop,
            "pop_moe": info["pop_moe"],
            "per_10k": round(rate, 1),
            "per_10k_moe": round(rate_moe, 1),
        })

    count_breaks = quantile_breaks([s["count"] for s in summary])
    rate_breaks = quantile_breaks([s["per_10k"] for s in summary])

    features = []
    for s in summary:
        info = wards[s["ward"]]
        rings = [simplify(r, 0.00012) for r in info["rings"]]
        props = dict(s)
        props["count_class"] = class_of(s["count"], count_breaks)
        props["rate_class"] = class_of(s["per_10k"], rate_breaks)
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "Polygon", "coordinates": rings},
        })

    geojson = {"type": "FeatureCollection", "features": features}
    point_features = [{"address": g["address"], "lat": g["lat"], "lon": g["lon"],
                       "n": g["n"], "also": g["also"]} for g in grouped]

    # ---- summary CSV -----------------------------------------------------
    with open(csv_out, "w", newline="", encoding="utf-8") as fh:
        cols = ["ward", "name", "council_member", "count",
                "population", "population_moe", "per_10k_residents", "per_10k_moe"]
        wr = csv.writer(fh)
        wr.writerow(cols)
        for s in sorted(summary, key=lambda r: -r["count"]):
            wr.writerow(
                [s["ward"], s["name"], s["council_member"], s["count"],
                 s["population"], s["pop_moe"], s["per_10k"], s["per_10k_moe"]]
            )

    # ---- cleaned location table ------------------------------------------
    with open(locations_out, "w", newline="", encoding="utf-8") as fh:
        wr = csv.writer(fh)
        wr.writerow(["address", "lat", "lon", "cans", "ward", "also_listed_as"])
        for g in grouped:
            wr.writerow([g["address"], g["lat"], g["lon"], g["n"],
                         g["ward"] if g["ward"] is not None else "",
                         "; ".join(g["also"])])

    # ---- HTML map --------------------------------------------------------
    meta = {
        "total": len(points),
        "mapped": len(points) - unassigned,
        "unassigned": unassigned,
        "count_breaks": count_breaks,
        "rate_breaks": rate_breaks,
        "city_pop": sum(s["population"] for s in summary),
        "colors": COLORS,
        "heading": ds["heading"],
        "noun": ds["noun"],
        "caveat": ds["caveat"],
    }

    html = load_points_template().replace("__GEOJSON__", json.dumps(geojson, separators=(",", ":")))
    html = html.replace("__POINTS__", json.dumps(point_features, separators=(",", ":")))
    html = html.replace("__META__", json.dumps(meta, separators=(",", ":")))
    with open(html_out, "w", encoding="utf-8") as fh:
        fh.write(html)

    with open(EMBED_TPL_IN, encoding="utf-8") as fh:
        embed = fh.read()
    embed = embed.replace("__SLUG__", ds["slug"]).replace("__HEADING__", ds["heading"])
    with open(embed_out, "w", encoding="utf-8") as fh:
        fh.write(embed)

    print(f"=== {ds['slug']} ===")
    print(f"trash cans total {len(points)}, placed in a ward {meta['mapped']}, unassigned {unassigned}")
    print(f"distinct locations {len(grouped)} "
          f"(max cans at one spot {max(g['n'] for g in grouped) if grouped else 0})")
    print(f"re-geocoded by hand {repaired}, dropped as unusable {len(dropped)}"
          + (f": {dropped}" if dropped else ""))
    print(f"citywide ACS population {meta['city_pop']:,}")
    print(f"count breaks   {count_breaks}")
    print(f"rate breaks    {rate_breaks}")
    print()
    print(f"{'ward':>5} {'cans':>6} {'pop':>8} {'per 10k':>9} {'+/-':>6}  council member")
    for s in sorted(summary, key=lambda r: -r["per_10k"]):
        print(f'{s["ward"]:>5} {s["count"]:>6} {s["population"]:>8,} '
              f'{s["per_10k"]:>9.1f} {s["per_10k_moe"]:>6.1f}  {s["council_member"]}')
    print(f"\nwrote {html_out}")
    print(f"wrote {csv_out}")
    print(f"wrote {locations_out}")
    print(f"wrote {embed_out}\n")
    return summary


def main():
    """Build every dataset whose input CSV is actually present.

    Not every checkout carries every dataset -- the public repo for the trash
    can map ships only that one -- so a missing CSV is a skip, not a crash.
    """
    built = 0
    for ds, fn in [(d, build) for d in DATASETS] + [(d, build_points) for d in POINT_DATASETS]:
        if not os.path.exists(os.path.join(DATA_DIR, ds["csv"])):
            print(f'skipping {ds["slug"]}: data/{ds["csv"]} is not in this checkout\n')
            continue
        fn(ds)
        built += 1
    if not built:
        print("nothing built -- no dataset CSV found in", DATA_DIR)


def load_template():
    """The HTML lives in template.html so it stays editable as real HTML/CSS."""
    with open(TEMPLATE_IN, encoding="utf-8") as fh:
        return fh.read()


def load_points_template():
    """The HTML lives in template-points.html so it stays editable as real HTML/CSS."""
    with open(TEMPLATE_POINTS_IN, encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    main()
 