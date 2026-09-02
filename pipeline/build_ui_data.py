"""Precompute the reference data the filter UI needs.

The UI must answer "how many companies match this?" while the user is
still clicking. Scanning the 517 MB RES export takes ~40 seconds, so
nothing interactive can be built on top of it directly.

This script runs once per RES refresh (twice a month) and produces:

    data/ui/nace.json      NACE tree: section - division - class, with counts
    data/ui/sizes.json     employee bands with counts
    data/ui/regions.json   regions and districts with counts
    data/ui/obce.json      municipality -> coordinates, for distance filtering
    data/ui/companies.db   SQLite index, one row per filterable company

Counts are computed over the baseline population - alive companies of
the two legal forms the ICP targets - not over the current filter. The
UI shows "how many exist", the query answers "how many match".

Run:
    python -m pipeline.build_ui_data
"""

import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

import requests

from pipeline.sources.codebooks import EMPLOYEE_CATEGORIES
from pipeline.sources.res_bulk import DEFAULT_PATH, ICP_FORMA, iter_rows

OUT_DIR = Path("data/ui")
NACE_CODEBOOK = Path("data/raw/nace_codebook.csv")

RUIAN_QUERY = (
    "https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer/{layer}/query"
    "?where=1%3D1&outFields={fields}"
    "&returnGeometry=true&maxAllowableOffset=0.02&outSR=4326&f=json"
)

# RES stores ICZUJ, the "basic territorial unit". For an ordinary town
# that is the municipality (layer 12), but statutory cities are split
# into districts and ICZUJ then holds the district code (layer 8).
# Praha alone accounts for ten of the twelve most common codes, so
# joining on layer 12 only loses every company in the largest cities.
OBEC_LAYER = (12, "kod,nazev,okres,nutslau")
CITY_DISTRICT_LAYER = (8, "kod,nazev,obec")
REGION_LAYER = 17

# Bands worth offering in the filter. A company of 1000+ is a
# corporation with its own IT department and would only pad the list.
#
# 000 IS OFFERED, AND IT USED TO SAY HERE THAT IT IS "NOT A SIZE". It is
# not a size, but leaving it out of the screen did not make those
# companies not exist - it made them invisible while the filter silently
# read them as the wrong size. In the ICP's own NACE divisions they are
# 67 129 live companies, 48 % of the field. Now they are a tier a person
# can tick or untick, which is the only honest place for that decision:
# see res_bulk.ICP_KATPO_UNKNOWN for what the pipeline then does with
# them, and what it deliberately does not.
USEFUL_SIZES = ("000", "120", "130", "210", "220", "230", "240", "310", "320", "330", "340")

# What each band is called on screen. Built here rather than in the page
# because "Neuvedeno" does not take the same sentence as "50-99": the
# interface used to append " zaměstnanců" to every label, which reads
# fine for a range and not at all for an absence.
SIZE_LABELS = {"000": "Velikost neuvedena v registru"}


def load_nace_names():
    """Read the official CZ-NACE codebook into {level: {code: name}}.

    Levels: 1 = section (letter), 2 = division, 3 = group, 4 = class,
    5 = the five-digit code RES actually stores.
    """
    names = {}
    parents = {}

    with open(NACE_CODEBOOK, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            level = row["UROVEN"]
            code = row["CHODNOTA"]
            names.setdefault(level, {})[code] = row["TEXT"]
            if row["NADVAZ"]:
                parents[code] = row["NADVAZ"]

    return names, parents


def build_index(csv_path=DEFAULT_PATH):
    """Stream the RES export once, collecting everything downstream needs.

    One pass on purpose: the file is 3.5M rows, and reading it four times
    to build four files would cost four times the wait for no benefit.
    """
    companies = []
    nace_counts = Counter()
    size_counts = Counter()
    region_counts = Counter()
    district_counts = Counter()

    for row in iter_rows(csv_path):
        # Baseline population: alive, and a legal form that can buy
        # software at all. Sole traders and state-funded bodies are out
        # before anything is counted, or every facet total would be
        # dominated by subjects the salesperson can never call.
        if row["DDATZAN"] or row["FORMA"] not in ICP_FORMA:
            continue

        nace = row["NACE2025"] or row["NACE"] or ""
        katpo = row["KATPO"]
        district = row["OKRESLAU"]

        companies.append((
            row["ICO"],
            row["FIRMA"],
            katpo,
            nace,
            nace[:2],
            row["FORMA"],
            district,
            row["ICZUJ"],
            row["OBEC_TEXT"],
            row["PSC"],
        ))

        nace_counts[nace] += 1
        size_counts[katpo] += 1
        district_counts[district] += 1

    return companies, nace_counts, size_counts, district_counts


def lookup_name(names, code):
    """Find a code's name at whichever classification level holds it."""
    for level in ("5", "4", "3", "2"):
        name = names.get(level, {}).get(code)
        if name:
            return name
    return None


def write_nace(nace_counts, names, parents):
    """Write the NACE tree the UI expands: division -> its five-digit codes.

    Counts roll up: a division shows the sum of its children, so the
    user can pick a whole division or open it and drop single codes.
    """
    divisions = {}

    for code, count in nace_counts.items():
        if not code:
            continue
        division = code[:2]
        entry = divisions.setdefault(division, {
            "code": division,
            "name": names.get("2", {}).get(division),
            "section": None,
            "count": 0,
            "codes": [],
        })
        entry["count"] += count
        if len(code) > 2:
            entry["codes"].append({
                "code": code,
                # RES stores codes at whatever depth it happens to know:
                # five digits for most, but three- and four-digit groups
                # appear too. Look up every level rather than assuming.
                "name": lookup_name(names, code),
                "count": count,
            })
        else:
            # RES sometimes stores only the division, with no detail
            # below it. That is a real state, not a gap to hide: show
            # it as its own entry so the totals still add up.
            entry["codes"].append({
                "code": code,
                "name": names.get("2", {}).get(code),
                "count": count,
                "division_only": True,
            })

    for division in divisions.values():
        division["section"] = parents.get(division["code"])
        division["codes"].sort(key=lambda item: -item["count"])

    ordered = sorted(divisions.values(), key=lambda item: item["code"])

    sections = {
        code: names["1"][code]
        for code in names.get("1", {})
    }

    write_json("nace.json", {"sections": sections, "divisions": ordered})


def write_sizes(size_counts):
    """Write the employee bands worth filtering on, in codebook order."""
    bands = [
        {
            "code": code,
            "label": SIZE_LABELS.get(
                code, EMPLOYEE_CATEGORIES[code].replace("employees", "zaměstnanců")),
            "count": size_counts.get(code, 0),
        }
        for code in USEFUL_SIZES
    ]
    write_json("sizes.json", bands)


def fetch_region_names():
    """Map NUTS region codes to their Czech names."""
    url = RUIAN_QUERY.format(layer=REGION_LAYER, fields="kod,nazev,nutslau")
    url = url.replace("&returnGeometry=true&maxAllowableOffset=0.02", "&returnGeometry=false")
    response = requests.get(url, timeout=60)
    response.raise_for_status()

    return {
        feature["attributes"]["nutslau"]: feature["attributes"]["nazev"]
        for feature in response.json().get("features", [])
    }


def write_regions(district_counts):
    """Write regions with counts.

    District codes in RES are NUTS LAU ("CZ0323"); the region is the
    first five characters ("CZ032").
    """
    names = fetch_region_names()
    regions = {}

    for district, count in district_counts.items():
        if not district:
            continue
        region = district[:5]
        entry = regions.setdefault(region, {
            "code": region,
            "name": names.get(region),
            "count": 0,
        })
        entry["count"] += count

    ordered = sorted(regions.values(), key=lambda item: -item["count"])
    write_json("regions.json", ordered)


def fetch_layer(layer, fields):
    """Download one RUIAN layer and reduce each outline to a single point.

    The service returns polygons; the UI only needs somewhere to measure
    a radius from, so each outline collapses to the centre of its
    simplified ring. Good to a couple of kilometres, well inside the
    tolerance of a "150 km from Plzen" filter.
    """
    response = requests.get(RUIAN_QUERY.format(layer=layer, fields=fields), timeout=180)
    response.raise_for_status()

    places = {}
    for feature in response.json().get("features", []):
        attributes = feature.get("attributes", {})
        rings = feature.get("geometry", {}).get("rings")
        if not rings:
            continue

        points = [point for ring in rings for point in ring]
        places[str(attributes["kod"])] = {
            "kod": str(attributes["kod"]),
            "name": attributes.get("nazev"),
            "district_code": attributes.get("okres"),
            "nutslau": attributes.get("nutslau"),
            "lon": round(sum(p[0] for p in points) / len(points), 5),
            "lat": round(sum(p[1] for p in points) / len(points), 5),
        }

    return places


def fetch_obce():
    """Build the ICZUJ -> coordinates lookup from both RUIAN layers."""
    print("fetching municipalities from RUIAN...", file=sys.stderr)
    obce = fetch_layer(*OBEC_LAYER)
    print(f"  {len(obce)} municipalities", file=sys.stderr)

    districts = fetch_layer(*CITY_DISTRICT_LAYER)
    print(f"  {len(districts)} city districts", file=sys.stderr)

    # City districts win on collision: if RES stored a district code,
    # the district centre is the more precise answer of the two.
    obce.update(districts)
    return obce


def write_sqlite(companies, obce):
    """Write the queryable index the UI filters against.

    SQLite rather than JSON: the baseline population is far too large to
    ship to a browser, and an indexed query answers in milliseconds
    where a CSV scan takes forty seconds.

    Coordinates are attached per municipality, not per address. Exact
    address coordinates exist (see sources/coords.py) but cost one HTTP
    request each, which is fine for a shortlist and impossible here.
    """
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    db_path = OUT_DIR / "companies.db"
    db_path.unlink(missing_ok=True)

    connection = sqlite3.connect(db_path)
    connection.execute("""
        CREATE TABLE company (
            ico TEXT PRIMARY KEY,
            name TEXT,
            katpo TEXT,
            nace TEXT,
            nace_division TEXT,
            forma TEXT,
            district TEXT,
            obec_code TEXT,
            obec TEXT,
            psc TEXT,
            lat REAL,
            lon REAL
        )
    """)

    rows = []
    located = 0
    for company in companies:
        obec = obce.get(company[7])
        if obec:
            located += 1
        rows.append(company + (
            obec["lat"] if obec else None,
            obec["lon"] if obec else None,
        ))

    connection.executemany(
        "INSERT OR REPLACE INTO company VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    for column in ("katpo", "nace_division", "district", "obec_code"):
        connection.execute(f"CREATE INDEX idx_{column} ON company({column})")

    connection.commit()
    connection.close()

    print(f"  {len(rows)} companies, {located} with coordinates", file=sys.stderr)


def write_json(name, payload):
    """Write one UI file and report its size."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / name
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    print(f"  {path} ({path.stat().st_size // 1024} KB)", file=sys.stderr)


def main():
    names, parents = load_nace_names()

    obce = fetch_obce()
    write_json("obce.json", obce)

    print("scanning the RES export...", file=sys.stderr)
    companies, nace_counts, size_counts, district_counts = build_index()
    print(f"  baseline population: {len(companies)}", file=sys.stderr)

    write_nace(nace_counts, names, parents)
    write_sizes(size_counts)
    write_regions(district_counts)
    write_sqlite(companies, obce)


if __name__ == "__main__":
    main()
