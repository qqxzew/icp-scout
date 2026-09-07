"""MPSV vacancies: the free, dated, ICO-keyed record of who is hiring.

The whole current stock of Czech vacancies is published as one gzipped
JSON, refreshed daily:

    https://data.mpsv.cz/od/soubory/volna-mista/volna-mista.json.gz

17 MB compressed, ~39 000 vacancies, ~18 000 employers. Downloaded once
and kept, because every later stage reads it and re-fetching 186 MB per
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
import codecs
import gzip
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

URL = "https://data.mpsv.cz/od/soubory/volna-mista/volna-mista.json.gz"
CACHE = Path("data/raw/mpsv_vacancies.jsonl")
CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")

USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"
TIMEOUT = 300

# How much decompressed text to pull in at a time, and how far the read
# cursor may run into the buffer before the consumed head is dropped.
# One vacancy averages 4.8 KB, so a megabyte always holds a whole one,
# and trimming at half a megabyte keeps the buffer bounded without
# copying what is left of it after every single element.
CHUNK_BYTES = 1 << 20
TRIM_AFTER = 1 << 19


def refresh(only_icos=None, url=URL, path=CACHE, archive=None):
    """Download the export and keep the vacancies that matter.

    Filtered on the way in rather than stored whole: the full file is
    186 MB decompressed and 92 % of it is employers we will never look
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

    run_id = archive.start_run(note="mpsv vacancy refresh") if archive else None
    per_company = defaultdict(list)

    path.parent.mkdir(parents=True, exist_ok=True)
    seen = kept = 0
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response, \
            gzip.GzipFile(fileobj=response) as stream:
        with open(path, "w", encoding="utf-8") as sink:
            for item in stream_items(stream):
                seen += 1
                ico = employer_ico(item)
                if not ico:
                    continue
                if only_icos is not None and ico not in only_icos:
                    continue
                row = reshape(item, ico)
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                per_company[ico].append(row)
                kept += 1

    # Counted rather than announced up front: a generator has no length,
    # and the number is worth more after the fact anyway - it says how
    # many vacancies were actually walked, not how many a header claimed.
    print(f"  {seen} vacancies in the export", file=sys.stderr)

    if archive:
        for ico, rows in per_company.items():
            archive.store(ico, "mpsv", json.dumps(rows, ensure_ascii=False),
                          url=f"{url}#{ico}", run_id=run_id)
        archive.finish_run(run_id)
        print(f"  archived {len(per_company)} company vacancy sets", file=sys.stderr)

    print(f"  kept {kept} -> {path}", file=sys.stderr)
    return kept


def stream_items(fileobj, key="polozky"):
    """Yield the elements of {"polozky": [ ... ]} one at a time.

    Why not json.loads on the whole thing, measured 06.09.2026: the
    export is 186 MB of decompressed JSON, and parsing it whole peaks at
    804 MB while the 186 MB payload is still being held - about a
    gigabyte, to end up with the 28 MB that survives the filter below.
    On a laptop that is merely wasteful. On the deployment box (2 GB,
    shared with another site) it is a SIGKILL: the first server run died
    right here, four minutes in, and the interface's own run button hits
    the same wall because it spawns the run inside the serving container.
    Read one element at a time and the peak is a megabyte of buffer plus
    a single vacancy.

    json.JSONDecoder.raw_decode does the parsing, so nothing here has to
    know what a JSON string escape looks like: it decodes one value
    starting at an offset and reports where that value ended. What is
    left is finding the array and stepping over the commas.

    The one case this handles badly is a genuinely malformed document -
    it cannot tell "cut off by the chunk boundary" from "broken" except
    by reading further, so a corrupt export is read to its end before the
    error is raised. That is the wrong file arriving, not the normal path.
    """
    decoder = json.JSONDecoder()
    incremental = codecs.getincrementaldecoder("utf-8")()
    buffer = ""
    exhausted = False

    def pull():
        """Pull the next block into the buffer. False once spent."""
        nonlocal buffer, exhausted
        if exhausted:
            return False
        block = fileobj.read(CHUNK_BYTES)
        if not block:
            # final=True so a byte sequence left dangling at the end of
            # the stream is an error rather than silently dropped.
            buffer += incremental.decode(b"", final=True)
            exhausted = True
            return False
        buffer += incremental.decode(block)
        return True

    # The array opens at the first "[" after the key. Whatever precedes
    # it is the document's own preamble and nothing downstream reads it.
    marker = f'"{key}"'
    while marker not in buffer:
        if not pull():
            raise ValueError(f"{marker} not found in the export")
    position = buffer.index(marker) + len(marker)
    while buffer.find("[", position) < 0:
        if not pull():
            raise ValueError(f"{marker} is not followed by an array")
    position = buffer.index("[", position) + 1

    while True:
        # Between two elements there is whitespace, a comma, or the "]"
        # that ends the array.
        while True:
            while position < len(buffer) and buffer[position] in " \t\r\n,":
                position += 1
            if position < len(buffer):
                break
            if not pull():
                raise ValueError("the export ended inside the array")
        if buffer[position] == "]":
            return

        while True:
            try:
                item, position = decoder.raw_decode(buffer, position)
                break
            except ValueError:
                if not pull():
                    raise
        yield item

        # Drop the consumed head, but not after every element: the copy
        # is proportional to what is left, and doing it 39 000 times
        # would cost more than the parse.
        if position > TRIM_AFTER:
            buffer = buffer[position:]
            position = 0


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
