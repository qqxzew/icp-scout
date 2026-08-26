"""MPSV vacancies: the free, dated, ICO-keyed record of who is hiring.

The whole current stock of Czech vacancies is published as one gzipped
JSON, refreshed daily:

    https://data.mpsv.cz/od/soubory/volna-mista/volna-mista.json.gz

17 MB compressed, ~39 000 vacancies, ~18 000 employers. Downloaded once
and kept, because every later stage reads it and re-fetching 177 MB per
question is absurd.

What it is good for, measured on the 3294 ICP candidates:

    companies present at all        1195   36.3 %
    vacancies for them              3325
    with free text                    91 %   median 382 characters

What it is NOT good for, and this is the important half: **MPSV is a
blue-collar board**. The commonest titles among our candidates are welders,
drivers, warehouse staff and concrete workers. Managerial vacancies go
to jobs.cz and LinkedIn instead - "plánovač" appears once in 3325 ads.
So any signal built on "they are hiring a planner" will find nothing
here, and that is a property of the source, not of the companies.

What it is uniquely good for: **it is the only place a company names the
software it runs**, because that is a requirement on a candidate:

    "znalost práce v IS Helios iNuvio výhodou"
    "některém z informačních systémů QI, SAP, Helios"

44 of our companies name a system. No company website does.

Run:
    python -m pipeline.sources.mpsv --refresh
    python -m pipeline.sources.mpsv 26516189
"""

import argparse
import gzip
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

URL = "https://data.mpsv.cz/od/soubory/volna-mista/volna-mista.json.gz"
CACHE = Path("data/raw/mpsv_vacancies.jsonl")
CANDIDATES = Path("data/raw/ares_candidates.jsonl")

USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"
TIMEOUT = 300


def refresh(only_icos=None, url=URL, path=CACHE, archive=None):
    """Download the export and keep the vacancies that matter.

    Filtered on the way in rather than stored whole: the full file is
    177 MB decompressed and 92 % of it is employers we will never look
    at. Pass only_icos=None to keep everything.

    With an archive, each company's vacancies are also stored as one
    snapshot. A NOW claim like "posted a production planner role on
    2026-07-29" has to point at something, and the vacancy record is
    that something - grouped per company rather than per vacancy,
    because the claim is about the company and one snapshot per advert
    would mean thousands of near-identical rows.
    """
    print(f"downloading {url} ...", file=sys.stderr)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = gzip.GzipFile(fileobj=response).read()

    items = json.loads(payload)["polozky"]
    print(f"  {len(items)} vacancies in the export", file=sys.stderr)

    run_id = archive.start_run(note="mpsv vacancy refresh") if archive else None
    per_company = defaultdict(list)

    path.parent.mkdir(parents=True, exist_ok=True)
    kept = 0
    with open(path, "w", encoding="utf-8") as sink:
        for item in items:
            ico = employer_ico(item)
            if not ico:
                continue
            if only_icos is not None and ico not in only_icos:
                continue
            row = reshape(item, ico)
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
            per_company[ico].append(row)
            kept += 1

    if archive:
        for ico, rows in per_company.items():
            archive.store(ico, "mpsv", json.dumps(rows, ensure_ascii=False),
                          url=f"{url}#{ico}", run_id=run_id)
        archive.finish_run(run_id)
        print(f"  archived {len(per_company)} company vacancy sets", file=sys.stderr)

    print(f"  kept {kept} -> {path}", file=sys.stderr)
    return kept


def employer_ico(item):
    """ICO of the employer, zero-padded like everywhere else, or None."""
    ico = str((item.get("zamestnavatel") or {}).get("ico") or "").strip().zfill(8)
    return ico if ico.strip("0") else None


def reshape(item, ico=None):
    """Flatten one vacancy to the fields anything downstream reads.

    The export nests localised strings ({"cs": ...}) and reference ids
    that only resolve against separate codebooks. Everything kept here
    is either plain or an id we can compare without resolving.
    """
    return {
        "ico": ico or employer_ico(item),
        "id": item.get("id"),
        "title": (item.get("pozadovanaProfese") or {}).get("cs"),
        "text": (item.get("upresnujiciInformace") or {}).get("cs"),
        "isco": (item.get("profeseCzIsco") or {}).get("id"),
        "seats": item.get("pocetMist"),
        # Dates are what make a vacancy a NOW signal rather than a fact.
        "posted": (item.get("datumVlozeni") or "")[:10],
        "changed": (item.get("datumZmeny") or "")[:10],
        "starts": item.get("terminZahajeniPracovnihoPomeru"),
        "salary_from": item.get("mesicniMzdaOd"),
        "salary_to": item.get("mesicniMzdaDo"),
        "url": item.get("urlAdresa"),
        "retrieved_at": date.today().isoformat(),
    }


def load(path=CACHE):
    """ICO -> list of vacancies, from the local copy."""
    if not Path(path).exists():
        raise FileNotFoundError(
            f"{path} not found. Fetch it first:\n"
            f"  python -m pipeline.sources.mpsv --refresh"
        )
    index = defaultdict(list)
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            index[row["ico"]].append(row)
    return index


def texts(vacancies):
    """Title plus body of each vacancy, as (text, label) pairs.

    Title and body are joined because the mode is stated in either -
    "Montér - strojírenská zakázková výroba" is a job title, and
    "pestrá práce v zakázkové výrobě" is a body.
    """
    out = []
    for vacancy in vacancies:
        blob = " ".join(filter(None, [vacancy.get("title"), vacancy.get("text")]))
        if blob.strip():
            out.append((blob, vacancy.get("title") or vacancy.get("id")))
    return out


def candidate_icos(path=CANDIDATES):
    with open(path, encoding="utf-8") as handle:
        return {json.loads(line)["ico"] for line in handle}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MPSV vacancies by ICO.")
    parser.add_argument("ico", nargs="?", help="show what is on file for one ICO")
    parser.add_argument("--refresh", action="store_true", help="re-download the export")
    parser.add_argument("--all-employers", action="store_true",
                        help="keep every employer, not just ICP candidates")
    parser.add_argument("--archive", action="store_true",
                        help="store each company's vacancy set in the evidence store")
    args = parser.parse_args()

    if args.refresh:
        store = None
        if args.archive:
            from pipeline.evidence.archive import Archive
            store = Archive()
        refresh(None if args.all_employers else candidate_icos(), archive=store)
    elif args.ico:
        found = load().get(str(args.ico).zfill(8), [])
        print(json.dumps(found, ensure_ascii=False, indent=2))
    else:
        index = load()
        print(f"{sum(len(v) for v in index.values())} vacancies "
              f"for {len(index)} companies", file=sys.stderr)
