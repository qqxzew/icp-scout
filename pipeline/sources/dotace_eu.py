"""EU subsidies: which candidate companies were granted money, and when.

Source: the monthly "Seznam operací (List of Operations) 2021-2027"
published by MMR at dotaceeu.cz. One XLSX, ~17 MB, 47 748 projects,
19 902 distinct recipients. Free, no key, keyed by ICO.

Columns that matter (verified live on the 2026-08 file):

    IČ příjemce                       the join key
    Datum podepsání právního aktu     when the grant was actually signed
    Název projektu                    free text - what the money is for
    Stav projektu                     lifecycle state
    Celkové náklady na operaci (CZK)  size of the project

Measured on the 3299 ICP candidates:

    692 companies (21 %) appear at all - three times the coverage of a
        leadership change, which makes this the widest NOW source found
    1365 projects for them, 100 % carrying a valid signing date
    median project 4.9 M CZK, largest 362 M
    323 of the 692 have more than one project (up to 10)

Four traps, each found by looking at the real file rather than assumed:

1. THE FILE IS MONTHLY, SO THIS CAN NEVER BE A 7-DAY SIGNAL. The newest
   signing date in the August file is 2026-07-23 - a month old on
   arrival. A weekly run asking "what happened in the last 7 days" gets
   zero from this source, every week, forever. It only works with a
   window wide enough to cover the publication lag, so the caller must
   pass one; there is no sensible default that hides this.

2. MOST ROWS ARE NOT NEWS. 504 of 1365 projects are already
   "finančně ukončen" and another 113 "ukončen ŘO" - finished, some
   years ago. A finished project is a fact about the company, not a
   reason to call this week. Only states meaning "signed / running" are
   treated as events.

3. DATE OF SIGNING, NOT DATE OF PUBLICATION. The date column is when
   the legal act was signed, which is what we want - but it means a
   project can appear in the file months after the money was agreed.
   Freshness has to be measured from the signing date, and the gap
   between that and "when we could first have known" is real.

4. ONE COMPANY, MANY PROJECTS. Half the recipients have several. Each
   is a separate dated event; collapsing them to "has subsidies" would
   throw away exactly the dating that makes this a NOW signal at all.

Run:
    python -m pipeline.sources.dotace_eu --refresh
    python -m pipeline.sources.dotace_eu 25507851
"""

import argparse
import json
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# The download link carries a GUID that changes when MMR republishes.
# Kept here rather than scraped so a run is reproducible; when it 404s,
# the landing page below has the current one.
SOURCE_PAGE = "https://www.dotaceeu.cz/cs/statistiky-a-analyzy/seznam-operaci-(prijemcu)"
URL = ("https://www.dotaceeu.cz/getmedia/e06f478c-d716-4dac-bfd7-48a8b6cc0d6b/"
       "2026_08_Seznam-operaci_List-of-Operations_21.xlsx.aspx")

XLSX = Path("data/raw/dotace_eu.xlsx")
CACHE = Path("data/raw/dotace_eu.jsonl")
CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")

USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"
TIMEOUT = 180

# Trap 2: only these states mean "money was granted and work is on".
# Everything else is a closed project - a fact about the company's past,
# not a reason to call.
#
# Trap 5, found while testing this module: the file writes these with a
# NON-BREAKING space after the one-letter preposition - "Projekt
# s\xa0právním aktem" - which is normal Czech typesetting and invisible
# on screen. Comparing against a plainly-typed string silently matched
# nothing: 122 of the 127 recently signed projects were dropped, and the
# signal looked like it simply did not exist. Exactly the trap already
# recorded for sbirka.py in the log (15.3), in a new place. Both sides
# of the comparison are normalised, never compared raw.
LIVE_STATES = {
    "projekt s pravnim aktem",
    "projekt v realizaci",
    "projekt fyzicky ukoncen",
}


def normalise_state(text):
    """Fold a state string so typesetting cannot break the comparison."""
    import unicodedata
    plain = (text or "").replace("\xa0", " ")
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFD", plain)
        if unicodedata.category(ch) != "Mn"
    )
    return " ".join(stripped.lower().split())


# Trap 6, and the one that matters most: a subsidy is money for a
# specific project, not money in the company's pocket. Measured on the
# 1365 projects our candidates hold, only ~13 % are about production
# digitalisation at all; the rest are energy savings, staff training,
# trade fairs and construction. Treating "got funded" as one signal
# ranks a company that bought solar panels the same as one automating
# its shop floor, and they are not the same lead.
#
# Worse, the sign is not even consistently positive. Ten companies in
# the file are buying precisely what RTsoft sells - "Implementace ERP
# systému", "Komplexní podnikový ERP systém", "Digitalizace výroby a
# řízení" - for 2 to 15 M CZK. Those are a competitor's customers, and
# the first version of this module ranked them as strong leads. This is
# the same two-sided sign the log already records for NEN (12.2.1),
# reached here from a different direction.
PROJECT_KINDS = (
    # Already buying the thing we sell. Not a lead - a lost one, or at
    # best a company to revisit in three years.
    ("already_buying", (
        r"informacni system", r"\berp\b", r"\bmes\b",
        r"rizeni vyroby", r"planovani vyroby", r"podnikovy system",
        r"digitalizace vyroby", r"digitalizace a rizeni", r"rizeni podniku",
    )),
    # Automating the shop floor without naming a system: the scheduling
    # problem is getting harder and no software is named yet.
    ("production_digitalisation", (
        r"digitaliz", r"automatiz", r"robotiz", r"podnikove procesy",
        r"digitalni transformac",
    )),
    # Growing capacity - more units to schedule, which is ICP sign 1.
    ("capacity", (
        r"rozsireni.*kapacit", r"porizeni.*technologi", r"nova.*hala",
        r"vyrobni linka",
    )),
    # Genuinely unrelated to how the company plans its work.
    ("energy", (r"energetick", r"uspor", r"fotovoltai", r"tepeln", r"emis")),
    ("training", (r"vzdelavan", r"skoleni", r"kompetenc")),
    ("marketing", (r"veletr", r"vystav", r"zahranicn", r"export", r"marketing")),
    ("research", (r"vyzkum", r"\bvyvoj", r"inovac", r"prototyp")),
)

# Which project kinds are worth surfacing as a NOW event at all. The
# rest stay in the data - they are true, and a card may want to mention
# them - but they are not a reason to call this week.
SIGNAL_KINDS = {"production_digitalisation", "capacity"}


def classify_project(name):
    """What the money is for. First match wins, order is deliberate.

    `already_buying` is checked before everything else so that a project
    that names an ERP can never be mistaken for generic digitalisation -
    the two look alike in wording and mean opposite things for us.
    """
    import unicodedata
    plain = (name or "").replace("\xa0", " ")
    folded = "".join(
        ch for ch in unicodedata.normalize("NFD", plain)
        if unicodedata.category(ch) != "Mn"
    ).lower()
    for kind, patterns in PROJECT_KINDS:
        if any(re.search(pattern, folded) for pattern in patterns):
            return kind
    return "other"

# Trap 1: the file is monthly and lags. A 7-day window - the cadence of
# the weekly run - would match nothing, ever. This is the narrowest
# window at which the source can produce anything at all, and callers
# should treat it as "recently funded", not "funded this week".
MIN_USEFUL_WINDOW = 120


def download(url=URL, path=XLSX):
    print(f"downloading {url} ...", file=sys.stderr)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = response.read()
    if not data.startswith(b"PK"):
        raise RuntimeError(
            f"not an XLSX - the download link probably moved. "
            f"Check {SOURCE_PAGE} for the current one."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"  {len(data) // 1024 // 1024} MB -> {path}", file=sys.stderr)
    return path


def excel_date(serial):
    """Excel serial number to a date.

    The file stores dates as numbers, not text. Excel's epoch is
    1899-12-30 (not 12-31) because of its deliberate 1900 leap-year bug,
    which every reader has to reproduce to stay compatible.
    """
    try:
        number = float(serial)
    except (TypeError, ValueError):
        return None
    if number < 1:
        return None
    return date(1899, 12, 30) + timedelta(days=int(number))


CELL = re.compile(
    r'<c r="([A-Z]+)\d+"(?:[^>]*t="(\w+)")?[^>]*>'
    r'(?:<v>(.*?)</v>|<is><t[^>]*>(.*?)</t></is>)?</c>'
)


def parse(path=XLSX, only_icos=None):
    """Read the sheet into flat rows, one per project.

    Parsed straight from the XML rather than through a spreadsheet
    library: the file is one sheet of plain values with a shared-string
    table, and pulling in a dependency to read 47 000 rows of that would
    be the heavier choice, not the simpler one.
    """
    archive = zipfile.ZipFile(path)
    strings = re.findall(
        r"<t[^>]*>(.*?)</t>",
        archive.read("xl/sharedStrings.xml").decode("utf-8", "replace"), re.S
    )
    rows = re.findall(
        r"<row[^>]*>(.*?)</row>",
        archive.read("xl/worksheets/sheet1.xml").decode("utf-8", "replace"), re.S
    )

    def cells(fragment):
        out = {}
        for column, kind, value, inline in CELL.findall(fragment):
            if inline:
                out[column] = inline
            elif not value:
                out[column] = ""
            elif kind == "s":
                index = int(value)
                out[column] = strings[index] if index < len(strings) else ""
            else:
                out[column] = value
        return out

    # The header is not on row 1 - the file opens with a title and a
    # generation timestamp - so it is found rather than assumed.
    header_index = header = None
    for index, fragment in enumerate(rows[:20]):
        parsed = cells(fragment)
        if any("IČ příjemce" in str(v) for v in parsed.values()):
            header_index, header = index, parsed
            break
    if header is None:
        raise RuntimeError("header row not found - file layout changed")

    column_of = {name: letter for letter, name in header.items()}
    need = ("IČ příjemce", "Datum podepsání právního aktu", "Název projektu",
            "Popis projektu", "Stav projektu", "Celkové náklady na operaci (CZK)",
            "Název programu")
    missing = [name for name in need if name not in column_of]
    if missing:
        raise RuntimeError(f"columns missing, layout changed: {missing}")

    out = []
    for fragment in rows[header_index + 1:]:
        parsed = cells(fragment)
        ico = str(parsed.get(column_of["IČ příjemce"], "")).strip().zfill(8)
        if not ico.strip("0"):
            continue
        if only_icos is not None and ico not in only_icos:
            continue
        signed = excel_date(parsed.get(column_of["Datum podepsání právního aktu"]))
        out.append({
            "ico": ico,
            "signed": signed.isoformat() if signed else None,
            "project": parsed.get(column_of["Název projektu"], "").strip(),
            # Kept separately from the title because it is what tells
            # "already_buying" apart from "budget approved, vendor not
            # picked yet" - see classify_project()'s docstring. The
            # title alone cannot make that distinction; found by reading
            # BUSE s.r.o.'s actual description, which says "pořízení"
            # (future) and names no vendor, next to a title
            # indistinguishable from a finished implementation.
            "description": parsed.get(column_of["Popis projektu"], "").strip(),
            "programme": parsed.get(column_of["Název programu"], "").strip(),
            "state": parsed.get(column_of["Stav projektu"], "").strip(),
            "total_czk": parsed.get(column_of["Celkové náklady na operaci (CZK)"], ""),
        })
    return out


def refresh(only_icos=None, archive=None):
    """Download, parse and keep the rows for the candidates we care about."""
    download()
    rows = parse(only_icos=only_icos)

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")

    if archive is not None:
        run_id = archive.start_run(note="eu subsidy refresh")
        grouped = defaultdict(list)
        for row in rows:
            grouped[row["ico"]].append(row)
        for ico, projects in grouped.items():
            archive.store(ico, "dotace_eu", json.dumps(projects, ensure_ascii=False),
                          url=URL, run_id=run_id)
        archive.finish_run(run_id)
        print(f"  archived {len(grouped)} company subsidy sets", file=sys.stderr)

    companies = {row["ico"] for row in rows}
    print(f"  kept {len(rows)} projects for {len(companies)} companies -> {CACHE}",
          file=sys.stderr)
    return len(rows)


def load(path=CACHE):
    """ICO -> list of subsidy projects, from the local copy."""
    index = defaultdict(list)
    if not Path(path).exists():
        return index
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            index[row["ico"]].append(row)
    return index


def funding_events(ico, subsidies, window_days, today=None):
    """Recently signed, still-live subsidy projects for one company.

    Trap 4 handled here: each project is its own event with its own
    date, because a company with three grants signed in three different
    months has three separate reasons to be called, not one.
    """
    today = today or date.today()
    events = []
    for project in subsidies.get(ico, []):
        if normalise_state(project["state"]) not in LIVE_STATES:
            continue                      # trap 2: finished is not news
        if not project["signed"]:
            continue
        signed = date.fromisoformat(project["signed"])
        age = (today - signed).days
        if age < 0 or age > window_days:
            continue
        purpose = classify_project(project["project"])
        events.append({
            "kind": "subsidy_signed",
            "purpose": purpose,
            # A signal only when the money is going into how the company
            # produces or how much it produces. Everything else is
            # carried so the card can show it, but flagged so scoring
            # does not mistake solar panels for a scheduling problem.
            "is_signal": purpose in SIGNAL_KINDS,
            "already_buying": purpose == "already_buying",
            "date": project["signed"],
            "age_days": age,
            "project": project["project"][:120],
            "programme": project["programme"],
            "state": project["state"],
            "total_czk": project["total_czk"],
        })
    events.sort(key=lambda e: e["age_days"])
    return events


def candidate_icos(path=CANDIDATES):
    with open(path, encoding="utf-8") as handle:
        return {json.loads(line)["ico"] for line in handle}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EU subsidies by ICO.")
    parser.add_argument("ico", nargs="?", help="show what is on file for one ICO")
    parser.add_argument("--refresh", action="store_true", help="re-download and parse")
    parser.add_argument("--archive", action="store_true",
                        help="store each company's subsidy set in the evidence store")
    parser.add_argument("--window", type=int, default=MIN_USEFUL_WINDOW,
                        help=f"freshness window in days (default {MIN_USEFUL_WINDOW})")
    args = parser.parse_args()

    if args.refresh:
        store = None
        if args.archive:
            from pipeline.evidence.archive import Archive
            store = Archive()
        refresh(candidate_icos(), archive=store)
    elif args.ico:
        subsidies = load()
        target = str(args.ico).zfill(8)
        print(json.dumps({
            "all_projects": subsidies.get(target, []),
            "events_in_window": funding_events(target, subsidies, args.window),
        }, ensure_ascii=False, indent=2))
    else:
        subsidies = load()
        print(f"{sum(len(v) for v in subsidies.values())} projects "
              f"for {len(subsidies)} companies", file=sys.stderr)
