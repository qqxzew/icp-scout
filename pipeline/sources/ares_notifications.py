"""ARES notification batches: which companies changed, without asking about each.

The weekly run needs fresh registry events, and the obvious way to get
them - re-enriching all 3299 candidates through ares.py - costs about
50 minutes and asks 13 200 questions to learn that almost nothing moved.
This service inverts that: ARES publishes a daily batch listing every
ICO whose record changed, and we intersect it with our base.

Measured live on 2026-08-29, seven days of the `vr` source:

    6 243   ICOs changed across the whole country
       30   of them are ours          <- 0.91 % of the base
    UPD 5234 · INS 984 · DEL 116

So a weekly refresh is 30 companies, not 3299 - and it is *fresher*
than a full re-run, because batch 710 was released the same day while
our last full enrichment file only reached 2026-08-25. This is the
"event finds the company, not the other way round" idea from the plan,
finally with a source behind it.

THE HISTORY IS SHORT AND THAT IS THE CATCH. Only about 30 days of
batches are retained (measured: 26 batches, 2026-07-31 to 2026-08-29).
Miss a month of runs and the incremental path cannot catch up - the
fallback is a full ares.py --all. Anything built on this module has to
survive that, so stale_since() below says plainly when the gap is too
wide rather than silently returning fewer changes than really happened.

THREE CHANGE TYPES, AND THEY ARE NOT INTERCHANGEABLE:

    UPD   an existing record changed - the case we act on
    INS   a company appeared in the register. 984 in one week, and by
          definition none of them are in our base yet. To use these we
          would have to run them through the ICP filter first, which is
          res_bulk.py's job, not this module's - so they are reported
          separately and not merged into the "ours" count.
    DEL   the record went away. Not a lead; worth knowing so a dead
          company stops being offered.

Closes an open question from the log (section 12): POST to ARES was
recorded as unreachable, 403 on CONNECT. That was the sandbox, not the
API - from a normal machine both POST endpoints answer fine.

Run:
    python -m pipeline.sources.ares_notifications --days 7
    python -m pipeline.sources.ares_notifications --days 7 --icos
    python -m pipeline.sources.ares_notifications --new
"""

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from pipeline.sources.ares import BACKOFF, MAX_ATTEMPTS, RETRY_STATUSES, TIMEOUT

BASE = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-notifikace"
CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")

# The registers ARES publishes batches for, confirmed live. `vr` is the
# one that matters here: the commercial register is where a director or
# owner change appears, which the ICP calls the cleanest NOW signal.
# `res` and `rzp` move far more (thousands a day) and carry nothing this
# project acts on yet.
SOURCES = ("vr", "res", "rzp", "ares")
DEFAULT_SOURCE = "vr"

# Measured, not assumed: 26 batches spanning 2026-07-31 to 2026-08-29.
# Used only to warn - the real retention is ARES's business and may
# change, so the code checks what actually came back rather than
# trusting this number.
HISTORY_DAYS = 30


def request(method, url, body=None):
    """One call to ARES with the same retry policy as ares.fetch().

    Imported rather than re-declared: a burst of requests gets answered
    with 403 and an HTML block page, and it lets go on its own (log
    13.5). Two modules disagreeing about how long to wait would be two
    places to fix when that behaviour changes.
    """
    for attempt in range(MAX_ATTEMPTS):
        response = requests.request(
            method, url, json=body, timeout=TIMEOUT,
            headers={"accept": "application/json"},
        )
        if response.status_code == 200:
            return response.json()
        if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
            delay = BACKOFF * (2 ** attempt)
            print(f"ares_notifications: HTTP {response.status_code}, "
                  f"retry {attempt + 1}/{MAX_ATTEMPTS - 1} in {delay}s", file=sys.stderr)
            time.sleep(delay)
            continue
        raise RuntimeError(
            f"ARES notifications {url}: HTTP {response.status_code} - {response.text[:200]}"
        )
    raise RuntimeError(f"ARES notifications {url}: failing after {MAX_ATTEMPTS} attempts")


def batches(source=DEFAULT_SOURCE):
    """Every retained notification batch for one register, oldest first.

    A batch is {cisloDavky, datovyZdroj, datumUvolneniDavky, pocetZmen}.
    More than one batch can share a date - ARES splits them, and `res`
    was seen capping at 10 000 changes per batch - so batches are always
    addressed by number and never by date alone.
    """
    payload = request("POST", f"{BASE}/vyhledat", {"datovyZdroj": source})
    found = payload.get("notifikacniDavky") or []
    return sorted(found, key=lambda b: b["cisloDavky"])


def batch(source, number):
    """One batch in full: [{"typZmeny": ..., "icoId": ...}, ...]."""
    payload = request("GET", f"{BASE}/datovy-zdroj/{source}/cislo-davky/{number}")
    return payload.get("seznamNotifikaci") or []


def stale_since(available, since):
    """True when `since` reaches further back than the retained history.

    The honest answer to "what changed since a date we no longer have
    batches for" is not a shorter list - it is that this module cannot
    answer, and the caller must fall back to a full ares.py --all.
    """
    if not available:
        return True
    earliest = min(b["datumUvolneniDavky"] for b in available)
    return since < earliest


def changes(source=DEFAULT_SOURCE, days=7, today=None, known_batches=None):
    """{ico: change_type} for everything that moved in the window.

    Returns (changes, meta). `meta` carries what was actually covered -
    number of batches, the real date range, and whether the requested
    window ran past the retained history - because a caller deciding
    between an incremental and a full refresh needs that, and a bare
    dict of ICOs hides it.
    """
    today = today or date.today()
    since = (today - timedelta(days=days)).isoformat()

    available = known_batches if known_batches is not None else batches(source)
    wanted = [b for b in available if b["datumUvolneniDavky"] >= since]

    found = {}
    for entry in wanted:
        for record in batch(source, entry["cisloDavky"]):
            # Later batches win: a company that was updated twice in the
            # window is one company to re-fetch, and if it was deleted
            # after being updated, DEL is what matters.
            found[record["icoId"]] = record["typZmeny"]

    meta = {
        "source": source,
        "batches": len(wanted),
        "changes": len(found),
        "since": since,
        "covered_from": min((b["datumUvolneniDavky"] for b in wanted), default=None),
        "covered_to": max((b["datumUvolneniDavky"] for b in wanted), default=None),
        "history_too_short": stale_since(available, since),
    }
    return found, meta


def load_candidates(path=CANDIDATES):
    """The ICOs we already track, as a set."""
    if not Path(path).exists():
        return set()
    with open(path, encoding="utf-8") as handle:
        return {json.loads(line)["ico"] for line in handle if line.strip()}


def split(found, candidates):
    """Sort raw changes into what the pipeline can actually do with them.

    `refresh`  ours, still alive - re-fetch these through ares.py
    `gone`     ours, deleted from the register - stop offering them
    `new`      not ours. Overwhelmingly INS, ~1000 a week nationally,
               and useless until res_bulk.py's ICP filter has looked at
               them. Counted, not acted on.
    """
    refresh, gone, new = {}, {}, {}
    for ico, kind in found.items():
        if ico not in candidates:
            new[ico] = kind
        elif kind == "DEL":
            gone[ico] = kind
        else:
            refresh[ico] = kind
    return {"refresh": refresh, "gone": gone, "new": new}


def to_consider(days=7, source=DEFAULT_SOURCE, candidates_path=CANDIDATES, today=None):
    """(ours to re-fetch, changed ICOs not ours yet, meta).

    The second list is what split() calls `new` and used to only count.
    It is the entry the base has for a company nobody has enriched -
    above all the 67 129 whose headcount the register never recorded,
    which are too many to crawl and are perfectly reachable this way.
    The event finds the company (CLAUDE.md section 5); res_bulk.lookup()
    then decides whether the brief wants it.
    """
    found, meta = changes(source, days, today)
    groups = split(found, load_candidates(candidates_path))
    meta.update({k: len(v) for k, v in groups.items()})
    return sorted(groups["refresh"]), sorted(groups["new"]), meta


def to_refresh(days=7, source=DEFAULT_SOURCE, candidates_path=CANDIDATES, today=None):
    """The short list a weekly run actually needs. (icos, meta).

    This is the whole point of the module in one call: hand back the
    handful of our companies whose registry record moved, so run.py can
    re-fetch those instead of all 3299.
    """
    found, meta = changes(source, days, today)
    groups = split(found, load_candidates(candidates_path))
    meta.update({k: len(v) for k, v in groups.items()})
    return sorted(groups["refresh"]), meta


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ARES change notifications.")
    parser.add_argument("--source", default=DEFAULT_SOURCE, choices=SOURCES)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--batches", action="store_true", help="list retained batches only")
    parser.add_argument("--icos", action="store_true", help="print our changed ICOs, one per line")
    parser.add_argument("--new", action="store_true", help="print changed ICOs NOT in our base")
    args = parser.parse_args()

    if args.batches:
        found = batches(args.source)
        print(f"{len(found)} retained batches for {args.source}: "
              f"{found[0]['datumUvolneniDavky']} - {found[-1]['datumUvolneniDavky']}",
              file=sys.stderr)
        for entry in found:
            print(f"  {entry['cisloDavky']:>6}  {entry['datumUvolneniDavky']}  "
                  f"{entry['pocetZmen']:>6} changes")
        raise SystemExit

    found, meta = changes(args.source, args.days)
    groups = split(found, load_candidates())

    if args.icos:
        for ico in sorted(groups["refresh"]):
            print(ico)
        raise SystemExit
    if args.new:
        for ico in sorted(groups["new"]):
            print(ico)
        raise SystemExit

    print(json.dumps({**meta, **{k: len(v) for k, v in groups.items()}},
                     ensure_ascii=False, indent=2))
    if meta["history_too_short"]:
        print("\nwindow reaches past the retained history - "
              "an incremental refresh cannot cover it, run ares.py --all",
              file=sys.stderr)
