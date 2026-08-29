"""RES bulk export: the primary candidate list.

The whole Czech statistical register (RES) is published as one CSV,
refreshed twice a month. Filtering it locally costs zero HTTP requests,
which is why the pipeline starts here instead of querying an API:

    https://opendata.csu.gov.cz/soubory/od/od_org03/res_data.csv

About 3.5M rows, ~517 MB. Never loaded into memory as a whole - every
function here streams the file row by row.

Columns actually used (the file has 25):

    ICO       registration number, the key to everything downstream
    FIRMA     business name
    KATPO     employee count band, same CSU 579 codes as ARES
    NACE      economic activity, 5 digits ("25620")
    NACE2025  the same under the 2025 revision
    FORMA     legal form ("112" s.r.o., "121" a.s., "331" state-funded)
    OKRESLAU  district as a NUTS LAU code ("CZ0323" = Plzen-mesto)
    DDATZAN   termination date - non-empty means the subject is dead
    OBEC_TEXT municipality name, for human-readable output

This module selects candidates. It does not score them.

Run manually:
    python -m pipeline.sources.res_bulk --nace 25 28 --katpo 240 310
"""

import argparse
import csv
import sys
import urllib.request
from pathlib import Path


DEFAULT_PATH = Path("data/raw/res_data.csv")
URL = "https://opendata.csu.gov.cz/soubory/od/od_org03/res_data.csv"

# The published file is ~517 MB. Used as a floor for "did this actually
# finish", not as an exact expectation - CSU republishes twice a month
# and the size drifts.
MIN_BYTES = 400_000_000

# CSU 579 codes covering the ICP size range (50-199 employees).
# 220/230 are the lower boundary the ICP still accepts (from 20 people).
ICP_KATPO = ("240", "310")

# Legal forms the pipeline sells to. Everything else - state-funded
# organisations, sole traders, associations - is dropped: a school or a
# municipal office matches on size but can never buy this software.
ICP_FORMA = ("112", "121")

# NACE divisions kept as candidates.
#
# The ICP says the industry does not decide - the operational problem
# does: "firma musi v case rozvrhnout omezene fyzicke zdroje na zakazky".
# So the list is not "manufacturing", it is "has something to schedule".
# Its own five examples land in three different sections, which is why
# limiting this to section C would contradict the brief.
#
# Order-driven production with a shop floor:
#   16 wood            18 printing        22 rubber and plastics
#   23 building mat.   25 metal products  26 electronics
#   27 electrical eq.  28 machinery       31 furniture
#   32 other mfg.      33 repair and installation of machinery
#
# Dispatched work - the resource is a crew, a vehicle or a technician:
#   38 waste           41 building constr. 42 civil engineering
#   43 specialised constr. 49 land transport 77 rental and leasing
#   81 services to buildings and landscape  95 repair of goods
#
# NACE cannot tell make-to-order from serial production: a serial and a
# bespoke furniture maker share code 31. This narrows the field, it does
# not qualify anyone.
ICP_NACE = (
    "16", "18", "22", "23", "25", "26", "27", "28", "31", "32", "33",
    "38", "41", "42", "43", "49", "77", "81", "95",
)


def download(path=DEFAULT_PATH, url=URL, force=False):
    """Fetch the RES export, atomically. Returns the path.

    Written to a .part file and renamed only once the whole body has
    arrived. That is the entire point: a 517 MB download interrupted
    halfway would otherwise leave a file that exists, opens cleanly, and
    parses as valid CSV - just with a chunk of the register missing. The
    pipeline would then quietly produce a short candidate list, and the
    mistake would surface days later as "fewer companies than expected"
    with nothing pointing at the cause. A .part file that never got
    renamed is unmistakable.

    Streamed in chunks for the same reason nothing else here loads the
    file: half a gigabyte does not belong in memory.
    """
    path = Path(path)
    if path.exists() and path.stat().st_size >= MIN_BYTES and not force:
        print(f"res_bulk: {path} already present "
              f"({path.stat().st_size / 1e6:.0f} MB), skipping", file=sys.stderr)
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    print(f"res_bulk: downloading {url}", file=sys.stderr)

    request = urllib.request.Request(url, headers={"User-Agent": "icp-scout/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response:
        expected = int(response.headers.get("Content-Length") or 0)
        written = 0
        with open(partial, "wb") as sink:
            while chunk := response.read(1 << 20):
                sink.write(chunk)
                written += len(chunk)
                if expected:
                    print(f"\r  {written / 1e6:6.0f} / {expected / 1e6:.0f} MB "
                          f"({100 * written / expected:.0f}%)", end="", file=sys.stderr)
        print(file=sys.stderr)

    if written < MIN_BYTES:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            f"res_bulk: got only {written / 1e6:.0f} MB, expected at least "
            f"{MIN_BYTES / 1e6:.0f} - refusing to keep a truncated register"
        )

    partial.replace(path)
    print(f"res_bulk: saved {path} ({written / 1e6:.0f} MB)", file=sys.stderr)
    return path


def iter_rows(path=DEFAULT_PATH):
    """Stream the CSV row by row as dicts.

    A generator on purpose: the file is far too large to hold in memory,
    and every caller only ever needs one row at a time.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Download it:\n"
            f"  python -m pipeline.sources.res_bulk --download"
        )

    with open(path, encoding="utf-8", newline="") as handle:
        yield from csv.DictReader(handle)


def matches(row, nace=None, katpo=None, forma=None, okres=None, include_terminated=False):
    """Decide whether one row belongs in the candidate list.

    A pure function: no file, no network. Every criterion left as None
    is simply not applied, so the same predicate covers "all metal
    manufacturers" and "everything of the right size in one district".

    NACE is matched by prefix, so "25" accepts 25620 and 25110 alike -
    the register's 5-digit codes are too fine to list by hand.
    """
    if not include_terminated and row["DDATZAN"]:
        return False

    if katpo is not None and row["KATPO"] not in katpo:
        return False

    if forma is not None and row["FORMA"] not in forma:
        return False

    if okres is not None and row["OKRESLAU"] not in okres:
        return False

    if nace is not None:
        # NACE2025 is the classification in force since 2026-01-01 and is
        # what ARES reports too. The old NACE column is the fallback for
        # rows the new one has not reached yet.
        code = row["NACE2025"] or row["NACE"] or ""
        if not any(code.startswith(prefix) for prefix in nace):
            return False

    return True


def select(path=DEFAULT_PATH, **criteria):
    """Yield the rows of the export that satisfy the given criteria.

    Accepts the same keyword arguments as matches(). Streams, so the
    caller decides whether to count, print or collect the result.

    Criteria are the ICP defaults unless overridden. This matters: an
    unset criterion means "do not filter on this", so a bare select()
    used to return the whole live register - 2.9M rows instead of the
    3299 candidates. Pass an explicit None to switch a criterion off.
    """
    criteria.setdefault("nace", ICP_NACE)
    criteria.setdefault("katpo", ICP_KATPO)
    criteria.setdefault("forma", ICP_FORMA)

    for row in iter_rows(path):
        if matches(row, **criteria):
            yield row


def summarise(row):
    """Reduce a raw CSV row to the fields worth showing a human."""
    return {
        "ico": row["ICO"],
        "name": row["FIRMA"],
        "katpo": row["KATPO"],
        "nace": row["NACE"],
        "forma": row["FORMA"],
        "okres": row["OKRESLAU"],
        "city": row["OBEC_TEXT"],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Filter the RES bulk export into a candidate list."
    )
    parser.add_argument("--file", default=DEFAULT_PATH, help="path to res_data.csv")
    parser.add_argument("--nace", nargs="*", default=list(ICP_NACE), help="NACE prefixes, e.g. 25 28 10")
    parser.add_argument("--katpo", nargs="*", default=list(ICP_KATPO), help="employee band codes")
    parser.add_argument("--forma", nargs="*", default=list(ICP_FORMA), help="legal form codes")
    parser.add_argument("--okres", nargs="*", help="district LAU codes, e.g. CZ0323")
    parser.add_argument("--include-terminated", action="store_true", help="keep dead subjects")
    parser.add_argument("--count", action="store_true", help="print only how many matched")
    parser.add_argument("--limit", type=int, help="stop after N matches")
    parser.add_argument("--download", action="store_true",
                        help="fetch res_data.csv (~517 MB) and exit")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    args = parser.parse_args()

    if args.download:
        download(args.file, force=args.force)
        raise SystemExit

    criteria = {
        "nace": args.nace,
        "katpo": args.katpo,
        "forma": args.forma,
        "okres": args.okres,
        "include_terminated": args.include_terminated,
    }

    found = 0
    for row in select(args.file, **criteria):
        found += 1

        if not args.count:
            company = summarise(row)
            print(
                company["ico"],
                company["name"],
                "|", company["city"],
                "|", company["nace"],
                "|", company["katpo"],
            )

        if args.limit and found >= args.limit:
            break

    print(f"matched: {found}", file=sys.stderr)
