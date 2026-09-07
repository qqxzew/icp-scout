"""One weekly run, start to finish - and a preflight so a cold machine says
what it needs instead of crashing three stages in.

THE SHAPE OF A RUN, AND WHY IT IS NOT "REFRESH EVERYTHING FIRST".

The stages have wildly different costs, and the expensive one is
pull-based rather than pushed over the whole base (log 22.8):

    1 icp       read the salesperson's brief, record it on the run
    2 refresh   only what actually moved: ARES notification batches say
                which of our companies changed (~30 a week, measured
                0.91%), so 30 get re-fetched instead of 3299. Plus MPSV
                vacancies and the monthly subsidy file.
    3 gate      the saved brief (industry, size, region), the negative
                filters, then NOW - a dated event or the company stops
                here
    4 enrich    site, contacts, certificates FOR THE GATED FEW ONLY.
                This is the ~90-minute stage over the full base, and it
                is the reason the gate comes first: ten companies is
                minutes.
    5 agents    the LLM pass, again only on the survivors
    6 select    rank by what was actually proved
    7 cards     render, and record what was delivered

Refreshing everything up front would invert 4 and 3 and make a weekly
run cost hours. The gate is not an optimisation bolted on afterwards -
it is what makes the rest affordable.

FEWER THAN FIVE IS A RESULT, NOT A FAILURE. The log settles this
(22.3): "better to hand over less than to lie". No widening of the
window to fill the quota, no padding from a waiting list - the run
reports the number it honestly found.

PREFLIGHT. data/ is gitignored in full, so a fresh clone has nothing.
Rather than failing at whichever stage first touches a missing file,
`check()` declares every prerequisite up front: what it is, which stage
needs it, and how to get it. Anything this code can build itself,
`--bootstrap` builds, in dependency order. Anything it cannot - the
517 MB registry export, the API key - it names precisely instead of
guessing.

Run:
    python -m pipeline.run --check
    python -m pipeline.run --bootstrap
    python -m pipeline.run
    python -m pipeline.run --stage gate --stage select
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from pipeline.evidence.archive import Archive

ICP_FILE = Path("web/icp.json")
RES_BULK = Path("data/raw/res_data.csv")
CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")
WEBSITES = Path("data/raw/websites.jsonl")
CONTACTS = Path("data/raw/contacts.jsonl")
MPSV_VACANCIES = Path("data/raw/mpsv_vacancies.jsonl")
MPSV_HISTORY = Path("data/raw/mpsv_history.jsonl")
SUBSIDIES = Path("data/raw/dotace_eu.jsonl")
TENDERS = Path("data/raw/nen.jsonl")
NACE_CODEBOOK = Path("data/raw/nace_codebook.csv")
# What build_ui_data.py writes last, and therefore what says the whole of
# data/ui is there: companies.db plus nace/sizes/regions/obce.json.
UI_DB = Path("data/ui/companies.db")
ENV_FILE = Path(".env")
# What the run leaves behind for web/week/ to read, and what api/main.py
# serves at /api/results. Written by the run itself rather than exported
# by hand: the page that shows the week has to show THIS week, and a
# fixture that once looked right is the same failure as a stale card.
RESULTS = Path("data/ui/results/latest.json")

STAGES = ("icp", "refresh", "gate", "enrich", "agents", "select", "cards")

DEFAULT_WINDOW = 7
DEFAULT_TOP = 5


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class Need:
    """One prerequisite: what it is, who needs it, how to get it.

    `build` is a command this module may run unattended. When it is None
    the thing cannot be produced here - a 517 MB download or a secret -
    and `hint` is printed for a human instead. The distinction is the
    whole point: a bootstrap that silently half-works is worse than one
    that stops and says which step is yours.
    """

    def __init__(self, path, what, stages, build=None, hint=None, minutes=None, min_mb=0):
        self.path = Path(path)
        self.what = what
        self.stages = stages
        self.build = build
        self.hint = hint
        self.minutes = minutes
        self.min_mb = min_mb

    @property
    def size_mb(self):
        return self.path.stat().st_size / 1e6 if self.path.exists() else 0

    @property
    def present(self):
        """Exists AND is plausibly complete.

        "Exists" alone is not enough for the big ones. A 517 MB download
        that dies halfway leaves a file that passes every existence check
        and then silently yields a truncated candidate list - the failure
        would surface as "fewer companies than expected" days later,
        which is exactly the kind of quiet wrongness this project keeps
        having to dig out. The floors below are deliberately far under
        the real sizes: the point is to catch a stub, not to police an
        exact byte count.
        """
        return self.path.exists() and self.size_mb >= max(self.min_mb, 1e-6)


class KeyNeed(Need):
    """The API key, which is not always a file.

    Run from a checkout, the key lives in .env. Run from the container,
    it arrives as an environment variable and .env is deliberately absent
    - the image excludes it, because a credential baked into a layer is a
    credential that leaks with the image. Checking only for the file
    reported the key as MISSING on a machine that had it, which is a
    prerequisite check lying about the one thing it exists to confirm.
    """

    @property
    def present(self):
        return bool(os.environ.get("OPENAI_API_KEY")) or super().present


def requirements():
    """Everything a run touches, in the order one thing needs another."""
    return [
        KeyNeed(ENV_FILE, "OpenAI API key", ("agents",),
                hint="set OPENAI_API_KEY in the environment, or create .env "
                     "with OPENAI_API_KEY=sk-... (see .env.example)"),
        Need(RES_BULK, "RES bulk export, 517 MB", ("bootstrap",), min_mb=400, minutes=10,
             build=[sys.executable, "-m", "pipeline.sources.res_bulk", "--download"]),
        # Not a run input - card.py turns a NACE code into its name with
        # it, and build_ui_data.py raises FileNotFoundError without it.
        # Listed because the first clean rebuild proved the list was
        # wrong: --bootstrap finished, reported success, and left a
        # machine whose interface could not be built at all.
        Need(NACE_CODEBOOK, "CZ-NACE 2025 classification", ("bootstrap", "cards"), minutes=1,
             build=[sys.executable, "-m", "pipeline.sources.res_bulk", "--download-nace"]),
        # Everything below is derived, and each is derived from the one
        # above it - which is why the list is ordered rather than a dict.
        Need(CANDIDATES, "ICP candidates enriched through ARES", ("refresh", "gate", "select"),
             build=[sys.executable, "-m", "pipeline.sources.ares", "--all", "--archive"],
             minutes=50, min_mb=5),
        Need(MPSV_VACANCIES, "MPSV vacancies", ("refresh", "gate", "agents"),
             build=[sys.executable, "-m", "pipeline.sources.mpsv", "--refresh", "--archive"],
             minutes=5),
        Need(SUBSIDIES, "EU subsidies", ("refresh", "gate"),
             build=[sys.executable, "-m", "pipeline.sources.dotace_eu", "--refresh", "--archive"],
             minutes=3),
        Need(WEBSITES, "resolved company domains", ("enrich", "agents", "select"),
             build=[sys.executable, "-m", "pipeline.sources.website", "--all", "--archive"],
             minutes=90),
        Need(CONTACTS, "contacts matched to register names", ("cards", "select"),
             build=[sys.executable, "-m", "pipeline.sources.contacts", "--all", "--archive"],
             minutes=15),
        # The tenders. load_tenders() returns {} when the file is absent,
        # which is the worst possible failure for a prerequisite list: a
        # clean install ran, reported success, and simply never produced
        # a class C reason - "open procurement" was unreachable and
        # nothing said so. Silence is why it is listed.
        Need(TENDERS, "tenders published on NEN", ("select", "cards"),
             build=[sys.executable, "-m", "pipeline.sources.nen", "--all", "--archive"],
             minutes=30),
        # The interface's own data, and the last thing built: the screens
        # read companies.db and three codebooks out of data/ui, and none
        # of it is written by a run. companies.db is the sentinel because
        # build_ui_data.py writes it last - if it is there, the rest is.
        Need(UI_DB, "interface data: companies.db and the codebooks", ("bootstrap",),
             build=[sys.executable, "-m", "pipeline.build_ui_data"],
             minutes=15, min_mb=50),
    ]


def check(stages=STAGES, verbose=True):
    """Report every prerequisite. Returns (missing_but_buildable, missing_manual).

    MPSV history is deliberately not required. It accumulates from
    repeated --refresh calls and a first run simply has none - that
    weakens the vacancy signal for a week rather than breaking anything,
    and refusing to start over it would be wrong.
    """
    buildable, manual = [], []
    for need in requirements():
        # "bootstrap" is not a run stage - it is the flag for things only
        # needed to build other things, never read by a run directly.
        relevant = "bootstrap" in need.stages or set(need.stages) & set(stages)
        if not relevant:
            continue
        if need.present:
            if verbose:
                print(f"  ok       {str(need.path):40} {need.size_mb:8.1f} MB  {need.what}")
            continue
        (buildable if need.build else manual).append(need)
        if verbose:
            how = f"~{need.minutes} min, automatic" if need.build else "MANUAL"
            # A present-but-undersized file is a different problem from an
            # absent one, and saying so saves the reader from re-downloading
            # something they already have most of.
            state = "TRUNCATED" if need.path.exists() else "MISSING  "
            print(f"  {state} {str(need.path):40} {how:22}  {need.what}"
                  + (f"  (have {need.size_mb:.1f} MB, expect >{need.min_mb} MB)"
                     if need.path.exists() and need.min_mb else ""))

    if verbose and manual:
        print("\nThese cannot be produced here - do them first:")
        for need in manual:
            print(f"  {need.path}\n    {need.hint}")

    return buildable, manual


def bootstrap(stages=STAGES, dry_run=False):
    """Build every missing prerequisite this code can build, in order.

    Stops at the first manual requirement rather than pressing on: every
    later step derives from the registry export, so continuing without it
    would produce a chain of empty files that look like real ones.
    """
    print("preflight:", file=sys.stderr)
    buildable, manual = check(stages)

    if manual:
        print("\nbootstrap stopped - a manual step comes first (above).", file=sys.stderr)
        return False
    if not buildable:
        print("\nnothing to build, all prerequisites present.", file=sys.stderr)
        return True

    total = sum(n.minutes or 0 for n in buildable)
    print(f"\nbuilding {len(buildable)} missing prerequisite(s), roughly {total} min:",
          file=sys.stderr)
    for need in buildable:
        print(f"\n  -> {need.path}  ({need.what}, ~{need.minutes} min)", file=sys.stderr)
        print(f"     {' '.join(need.build)}", file=sys.stderr)
        if dry_run:
            continue
        started = time.time()
        result = subprocess.run(need.build)
        if result.returncode != 0:
            print(f"\n  FAILED after {(time.time()-started)/60:.0f} min: {need.path}\n"
                  f"  later stages depend on it, stopping here.", file=sys.stderr)
            return False
        print(f"     done in {(time.time()-started)/60:.0f} min", file=sys.stderr)
    return True


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def default_icp():
    """RTsoft's ICP as the pipeline ships it, before anyone edits it.

    Separate from load_icp() because the interface needs the same thing:
    an empty filter screen would ask the salesperson to retype a profile
    the prototype already knows. One list of NACE codes for both ends.

    `location` is the brief's own geography, ARCHITECTURE.md §2: "preferovaně
    Plzensky kraj -> Karlovarsky, Jihocesky, Stredocesky, Praha (~150
    km)" - RTsoft sits in Plzen and drives to the shop floor. It is a
    priority ("preferovaně") in the ICP document - but the salesperson
    using the tool overruled that: a radius on the screen is a promise
    about what comes back, so filters/brief.py enforces it whoever set
    it. `from_default` is kept as a record of who chose the number, not
    as a switch: nothing branches on it any more.
    """
    from pipeline.sources.res_bulk import ICP_FORMA, ICP_KATPO, ICP_NACE
    return {
        "nace": sorted(ICP_NACE),
        # The sized bands only. The unsized tier (ICP_KATPO_UNKNOWN) is
        # still a tier the interface can tick, and scoring/select.py's
        # ordering() knows what to do when it is - but it is no longer
        # ticked for the salesperson who never asked.
        #
        # Measured on the pool it actually produced, which is what
        # changed the answer: 18 companies, all of them without a proven
        # domain or a single channel, firing a registry event at 20 % a
        # week against 0.06 % for everyone else. RES leaves the headcount
        # empty for a company that files nothing, so the tier in practice
        # holds shells - and shells change directors constantly, which is
        # our cleanest signal. Admitting them by default put four of one
        # week's seven events on companies nobody can call.
        #
        # This is not hypothesis E reversed. Absence of a headcount is
        # still not a headcount of zero: the tier remains selectable, the
        # companies remain in the base, and a run that ticks it ranks
        # them below every company whose size is known instead of
        # dropping them.
        "katpo": sorted(ICP_KATPO),
        "forma": sorted(ICP_FORMA),
        "regions": None,
        "location": {
            "from": "Plzeň", "km": 150,
            "origin": {"name": "Plzeň", "lat": 49.7529, "lon": 13.3566},
            "from_default": True,
        },
    }


def with_defaults(icp, fallback=None):
    """Any field left empty always falls back to RTsoft's own ICP.

    One rule for every criterion - nace, katpo, forma, regions, the
    radius - not just geography. An empty field is never read as "the
    salesperson chose everything"; it means nobody has decided yet, and
    RTsoft's own profile answers until something is picked instead. Both
    ends read a saved brief through this - api/main.py for the screen,
    load_icp() below for the actual run - so what the interface shows
    "selected" and what the pipeline filters on never disagree about
    what an empty field means.
    """
    fallback = fallback or default_icp()
    merged = dict(icp)
    for key in ("nace", "katpo", "forma", "regions"):
        if not merged.get(key):
            merged[key] = fallback.get(key)
    if not (merged.get("location") or {}).get("km"):
        merged["location"] = fallback.get("location")
    return merged


def load_icp():
    """The salesperson's brief, or the built-in ICP when none was saved.

    Requirement 1 of the brief is to accept the ICP as input, and the
    point of recording it on the run is that a week later "why these
    companies" has an answer. A missing file is not an error - it means
    nobody has pressed save in the interface yet - but the fallback is
    recorded explicitly as the fallback, not passed off as a choice.
    """
    if ICP_FILE.exists():
        try:
            icp = json.loads(ICP_FILE.read_text(encoding="utf-8"))
            icp = with_defaults(icp)
            icp["_source"] = str(ICP_FILE)
            return icp
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            # A saved brief that will not parse must not take the run
            # down with it. Found live: a stray keystroke after the
            # closing brace, which crashed the whole pipeline at stage
            # one before a single company was looked at. The built-in
            # ICP is a worse answer than the saved one but an enormously
            # better answer than a traceback - and saying which was used
            # is what keeps it from passing unnoticed.
            print(f"run: {ICP_FILE} is not valid JSON ({error}); "
                  f"falling back to the built-in ICP", file=sys.stderr)

    icp = default_icp()
    icp["_source"] = "built-in default (web/icp.json not saved yet)"
    return icp


def save_candidates(updated, path=CANDIDATES):
    """Write refreshed companies back into the candidate file, atomically.

    THE REFRESH USED TO REFRESH NOTHING THE GATE COULD SEE. get_company()
    archives each raw ARES response - which is what a claim later cites -
    and returns the parsed company, and stage_refresh threw that return
    value away. The gate reads this file, so a board change picked up on
    Monday sat in the evidence store while the gate went on reading a
    snapshot from the week before. Measured on the live data: the file's
    newest registry date was 25.08 on a run made on 01.09, so a 7-day
    window covered one day of actual data.

    Rewritten whole rather than edited in place: JSONL rows are variable
    length, so changing one means rewriting the tail regardless, and
    3299 rows cost milliseconds. Lines nobody refreshed are copied
    across untouched rather than re-serialised - a rewrite should not
    quietly reformat, or drop, 3269 rows it was not asked about. Written
    to a temporary file and renamed, for the reason res_bulk.download()
    gives: a half-written candidate list looks exactly like a complete
    short one.
    """
    path = Path(path)
    if not updated or not path.exists():
        return 0

    lines, replaced = [], 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            try:
                ico = json.loads(line).get("ico")
            except json.JSONDecodeError:
                ico = None
            if ico in updated:
                lines.append(json.dumps(updated[ico], ensure_ascii=False))
                replaced += 1
            else:
                lines.append(line)

    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(path)
    return replaced


# How many companies the change stream may add to the base in one run.
#
# A ceiling, because the intake is unbounded by nature: 4 549 companies
# changed nationally in one week that are not ours, and the share of
# them the brief accepts is not something the run controls. Enriching
# every match is four ARES requests each - measured at 21 s per request
# on a slow evening, so a few hundred matches is a night rather than a
# stage. The ones left over are not lost work: they keep changing, and
# the register keeps saying so.
#
# Sized bands are taken first. A company the register places at 50-99
# employees is an ICP candidate on the register's word; one whose
# headcount was never recorded is a maybe, and a maybe waits behind a
# yes when there is a queue.
MAX_NEWCOMERS = 25


def admit_newcomers(icos, icp, archive, run_id):
    """Companies the register moved that the brief wants and we do not have.

    THE ONLY AFFORDABLE DOOR FOR THE UNSIZED TIER. 67 129 live companies
    in the ICP's own industries carry no headcount in the register at
    all (res_bulk.ICP_KATPO_UNKNOWN). Enriching them all to find out
    whether anything ever happens to them is about nineteen hours of
    ARES for a population that is mostly micro or dormant. Coming at it
    from the event end costs one pass over the bulk file: of the few
    thousand companies that changed nationally this week, ask which ones
    the brief would have wanted, and enrich only those.

    This is the mechanism ARCHITECTURE.md section 6 describes and the pipeline
    never had - "the event finds the company, not the other way round",
    and its stated advantage is exactly this one: it finds companies the
    base does not contain yet.
    """
    from pipeline.sources import ares
    from pipeline.sources.res_bulk import ICP_KATPO, lookup

    admitted = lookup(icos, nace=icp.get("nace"), katpo=icp.get("katpo"),
                      forma=icp.get("forma"))
    # Sized bands first, then the unsized tier; stable by ICO inside
    # each so a repeated run works through the same queue in the same
    # order rather than sampling it differently every week.
    queue = sorted(admitted.items(),
                   key=lambda item: (item[1]["KATPO"] not in ICP_KATPO, item[0]))
    taken = queue[:MAX_NEWCOMERS]

    rows = []
    for ico, _ in taken:
        try:
            rows.append(ares.get_company(ico, archive=archive, run_id=run_id))
        except Exception as error:
            print(f"  newcomer {ico}: {type(error).__name__}", file=sys.stderr)

    if rows:
        with open(CANDIDATES, "a", encoding="utf-8") as sink:
            for row in rows:
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(admitted), len(rows), len(queue) - len(taken)


def stage_refresh(archive, run_id, days, icp=None):
    """Bring the volatile sources up to date. Registry: only what moved."""
    from pipeline.sources import ares, ares_notifications, dotace_eu, mpsv

    icos, newcomers, meta = ares_notifications.to_consider(days=days)
    print(f"  registry: {meta['changes']} changed nationally, {len(icos)} of ours, "
          f"{len(newcomers)} not in the base", file=sys.stderr)
    if meta["history_too_short"]:
        print("  WARNING: window exceeds ARES's retained batches - "
              "registry events before "
              f"{meta['covered_from']} are not covered. Run ares.py --all.", file=sys.stderr)

    updated, failed = {}, 0
    for ico in icos:
        try:
            updated[ico] = ares.get_company(ico, archive=archive, run_id=run_id)
        except Exception as error:
            # One unreachable company must not cost the other 29 their
            # refresh - and its old row stays in the file untouched,
            # which is a stale answer rather than no answer.
            failed += 1
            print(f"  registry: {ico} not refreshed ({type(error).__name__})", file=sys.stderr)

    replaced = save_candidates(updated)
    print(f"  registry: {len(updated)} companies re-fetched, {replaced} rows updated"
          + (f", {failed} failed" if failed else ""), file=sys.stderr)

    matched = added = deferred = 0
    if icp and newcomers:
        matched, added, deferred = admit_newcomers(newcomers, icp, archive, run_id)
        print(f"  registry: {matched} of {len(newcomers)} newcomers match the brief, "
              f"{added} added to the base"
              + (f", {deferred} queued for a later run" if deferred else ""),
              file=sys.stderr)

    # One unreachable source must not end the run. Found the hard way:
    # the machine lost its network mid-run, mpsv.refresh() raised
    # getaddrinfo, and a run that had already re-fetched 27 companies
    # and matched 317 newcomers died on the stack trace instead of
    # gating anything. Every source here is a weekly snapshot the run
    # can survive without - it is a week staler, which is a worse run,
    # not a failed one - so the failure is named and the stage
    # continues.
    sources = {}
    for name, refresh_source in (("mpsv", mpsv.refresh), ("dotace_eu", dotace_eu.refresh)):
        try:
            refresh_source(archive=archive)
            sources[name] = "ok"
        except Exception as error:
            sources[name] = f"{type(error).__name__}"
            print(f"  {name}: refresh failed ({type(error).__name__}) - "
                  f"the run continues on the copy already on disk", file=sys.stderr)

    return {"sources": sources,
            "registry_refreshed": replaced, "registry_failed": failed,
            "newcomers_matched": matched, "newcomers_added": added,
            "newcomers_deferred": deferred, "registry_meta": meta}


def workplaces_of(ico, archive=None, run_id=None):
    """This company's establishments, with coordinates, or None if unknown.

    Only the RZP endpoint, not get_company's four: the other three say
    nothing about where the work happens, and this runs over the gated
    few on every run. None means the register could not be reached and
    the caller should keep whatever it already believed - an unreachable
    ARES must never look like "this company has no shop floor", which is
    the same rule the DNS resolver taught website.py.
    """
    from pipeline.sources import coords
    from pipeline.sources.ares import BASE_URL, fetch, parse_rzp

    try:
        payload = fetch("ekonomicke-subjekty-rzp", ico)
    except Exception as error:
        print(f"    {ico}: establishments not read ({type(error).__name__})",
              file=sys.stderr)
        return None
    if payload is None:
        # A 404 is an answer: this company is not in the trade register,
        # so it has no establishments to weigh against its seat.
        return []

    if archive is not None:
        archive.store(ico, "ares", json.dumps(payload, ensure_ascii=False),
                      url=f"{BASE_URL}/ekonomicke-subjekty-rzp/{ico}", run_id=run_id)

    sites = parse_rzp(payload).get("establishments") or []
    for site in sites:
        site["coordinates"] = (coords.get_coordinates(site["address_code"])
                               if site.get("address_code") else None)
    save_establishments(ico, sites)
    return sites


ESTABLISHMENTS_CACHE = Path("data/raw/establishments.jsonl")


def save_establishments(ico, sites, path=ESTABLISHMENTS_CACHE):
    """Keep the sites where a card can find them later.

    The run reads establishments for the gated few and hands them to
    select.py in memory, which is enough for the run itself - but a card
    rendered afterwards (python -m pipeline.scoring.card, or
    /api/card/{ico} from the week page) rebuilds the company from the
    candidate file, which has no establishments in it. The distance line
    then silently loses its "pozor, provozovna ... je 234 km", which is
    the one thing on it worth reading.

    Same shape as turnover.jsonl: append-only, last line wins, cheap to
    read. Not the archive, because this is a lookup rather than a claim
    about the company - nothing here is quoted to a salesperson.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as sink:
        sink.write(json.dumps({"ico": str(ico).zfill(8), "establishments": sites},
                              ensure_ascii=False) + "\n")


def stage_gate(window_days, icp, archive=None, run_id=None):
    """Three cuts, cheapest first: the brief, the negative filters, NOW.

    THE BRIEF COMES FIRST AND USED NOT TO COME AT ALL. The candidate file
    was built once with the built-in NACE and size sets, so a run gated
    every company in it regardless of what the salesperson had saved -
    the brief was archived on the run row and never applied to anything
    (filters/brief.py has the full account). Applying it here, over a
    file already on disk, costs one pass and no request.

    The negative filter stays where it was, before the gate rather than
    after it, for the reason the plan gives: throw work away while it is
    still cheap. A company in insolvency matches every line of the ICP
    and cannot buy anything, and letting it through would spend a site
    crawl and an LLM pass to produce a card nobody can act on.
    """
    from pipeline.filters import brief as brief_module
    from pipeline.filters.brief import describe
    from pipeline.scoring.select import eligible
    from pipeline.signals.now import (VACANCY_FRESH_DAYS, VACANCY_WINDOW, find,
                                      load_companies, load_history, load_tenders,
                                      publication_lag, record)
    from pipeline.sources.dotace_eu import load as load_subsidies

    companies = list(load_companies(CANDIDATES))
    pool, funnel = eligible(companies, icp)
    history, subsidies = load_history(), load_subsidies()
    tenders = load_tenders()

    print(f"  brief: {describe(icp)}", file=sys.stderr)

    # `window_days` is the registry window; every other source keeps its
    # own, sized to how far behind it publishes (signals/now.py). The
    # vacancy one is derived from a lag that moves, so it is re-measured
    # here on every run - a constant that has quietly stopped being true
    # is exactly how this signal produced zero for weeks without anybody
    # being able to see why.
    lag = publication_lag()
    if lag is not None:
        print(f"  windows: registry {window_days}d, vacancies {VACANCY_WINDOW}d "
              f"(MPSV lag measured at {lag}d)", file=sys.stderr)
        if lag + window_days > VACANCY_WINDOW:
            print(f"    WARNING: MPSV now lags {lag} days, so a {VACANCY_WINDOW}-day "
                  f"window no longer reaches back a full run cadence - postings can "
                  f"fall between two runs. Re-derive VACANCY_LAG "
                  f"(python -m pipeline.signals.now --lag).", file=sys.stderr)
    print(f"  {funnel['total']} candidates -> {funnel['pool']} after the brief "
          f"and the negative filters", file=sys.stderr)
    # Every reason printed, not just the total: a brief that drops 3200
    # of 3299 has to say whether that was the size band or a region
    # nobody meant to tick.
    for reason, count in sorted(funnel["brief_rejected"].items(), key=lambda i: -i[1]):
        print(f"    {count:5}  {reason}", file=sys.stderr)
    if funnel["excluded"]:
        print(f"    {funnel['excluded']:5}  negative (insolvency, liquidation)",
              file=sys.stderr)

    qualified = {}
    for company in pool:
        events = find(company, history, window_days,
                      subsidies=subsidies, tenders=tenders)
        if events:
            qualified[company["ico"]] = events

    print(f"  {len(qualified)} of {funnel['pool']} companies have a dated event",
          file=sys.stderr)

    # Where each gated company actually works, read from the register.
    #
    # Not to reject anybody: the radius admits on the nearest address a
    # company keeps, and the seat already passed above. This is for the
    # two things the seat alone gets wrong.
    #
    # It can bring a company CLOSER - SaM silnice a mosty is seated
    # 134 km out and has an establishment at 116 - and re-admitting on
    # the nearest point is why this runs before select rather than after.
    #
    # And it lets a card warn: ATOMO PROJEKT is seated at a Prague
    # office 87 km away while all three of its establishments are in
    # Moravia, the nearest 234 km. It stays in the week, because
    # something of it really is inside the radius, but the card no
    # longer implies a 90-minute drive to a shop floor that is four
    # hours away. scoring/select.py::geography() prints the far one.
    #
    # Over 9843 candidates this would cost hours; over the handful with
    # a dated reason it is seconds, which is the only reason it can be
    # asked from the register at all rather than guessed from a website.
    by_ico = {c["ico"]: c for c in pool}
    sites_by_ico = {}
    for ico in qualified:
        sites = workplaces_of(ico, archive, run_id)
        if sites is None:
            continue
        by_ico[ico]["establishments"] = sites
        sites_by_ico[ico] = sites
    print(f"  establishments read for {len(sites_by_ico)} companies, "
          f"{sum(1 for s in sites_by_ico.values() if s)} have at least one",
          file=sys.stderr)

    # Record what the gate found, for the companies it let through only.
    # Without this a run gates correctly and then renders a card with an
    # empty "why now": the events existed in memory and were never
    # written, so nothing downstream could cite them. Recording here
    # rather than in a stage of its own keeps the claim and the decision
    # it justified in the same place.
    if archive is not None:
        by_ico = {c["ico"]: c for c in pool}
        states = {"fact": 0, "inference": 0, "discard": 0}
        orphaned = 0
        for ico in qualified:
            _, missing, counts = record(archive, by_ico[ico], history, window_days,
                                        run_id, subsidies=subsidies, tenders=tenders)
            orphaned += missing
            for state, n in counts.items():
                states[state] += n
        print(f"  claims recorded: {states}"
              + (f", {orphaned} without a snapshot" if orphaned else ""),
              file=sys.stderr)

    # The sites travel with the gate result: select.py rebuilds its
    # company dicts from the candidate file, which has no
    # establishments in it, so handing them over is the only way the
    # ranking measures the same distance the gate did.
    return qualified, sites_by_ico


def stage_enrich(archive, run_id, icos):
    """Re-read the sites of the gated few - the pull-based expensive step.

    Over the whole base this is ~90 minutes. Over the ten companies the
    gate let through it is a couple of minutes, and it is the only way
    the card carries what the site says *today* rather than whenever the
    last full crawl happened.
    """
    from pipeline.scoring.select import load_jsonl
    from pipeline.sources import certificates
    from pipeline.sources.website import Fetcher, harvest, keep, resolve

    status = load_jsonl(WEBSITES)
    fetcher = Fetcher()
    refreshed = resolved = 0

    # A company the base gained after the last full domain sweep has no
    # row in websites.jsonl at all, and every later stage reads that file
    # to decide whether it may quote the site. Left alone, the newest
    # candidates - the ones the change stream just brought in - would be
    # exactly the ones arriving with the thinnest cards. Resolving here
    # costs a handful of DNS lookups per company and only for the few
    # the gate let through.
    from pipeline.signals.now import load_companies
    known = {c["ico"]: c for c in load_companies(CANDIDATES)}
    for ico in icos:
        if ico in status:
            continue
        company = known.get(ico) or {}
        # What the register says beyond the name, so the address and
        # person tiers can fire. Natural persons only: an owner that is
        # a company names the group, and the group's site is not this
        # company's - see website.grade().
        members = (company.get("directors") or []) + (company.get("owners") or [])
        facts = {
            "street": company.get("street"),
            "house_number": company.get("house_number"),
            "people": [p["name"] for p in members
                       if p.get("name") and not p.get("is_legal_entity")],
        }
        try:
            found = resolve(ico, company.get("name") or "", company.get("city"),
                            fetcher=fetcher, archive=archive, run_id=run_id,
                            facts=facts)
        except Exception as error:
            print(f"  {ico}: resolve failed, {type(error).__name__}", file=sys.stderr)
            continue
        status[ico] = found
        resolved += 1
        with open(WEBSITES, "a", encoding="utf-8") as sink:
            sink.write(json.dumps(found, ensure_ascii=False) + "\n")
    if resolved:
        print(f"  {resolved} domains resolved for companies new to the base",
              file=sys.stderr)

    for ico in icos:
        site = status.get(ico) or {}
        # Only proven domains. An unproven guess is, by website.py's own
        # measurement, someone else's company 46% of the time.
        if site.get("status") != "proven" or not site.get("domain"):
            continue
        try:
            for url, body, kind in harvest(fetcher, site["domain"]):
                keep(archive, ico, url, body, run_id, kind)
            certificates.record(
                archive, ico,
                certificates.from_archive(archive, ico, fetcher, run_id), run_id)
            refreshed += 1
        except Exception as error:               # one dead site is not a failed run
            print(f"  {ico}: {type(error).__name__} {error}", file=sys.stderr)

    print(f"  {refreshed} of {len(icos)} re-crawled (rest have no proven domain)",
          file=sys.stderr)

    # Contacts off the pages just harvested, for companies the contact
    # sweep has never seen. Without this a company that entered the base
    # this week reaches the card with its directors named from the
    # register and no channel to any of them - the register knows who may
    # sign, only the site knows how to reach them.
    from pipeline.sources.contacts import get_contacts
    from pipeline.scoring.select import CONTACTS as CONTACTS_FILE
    have = load_jsonl(CONTACTS_FILE)
    added = 0
    for ico in icos:
        site = status.get(ico) or {}
        # Proven domains only. A guessed one belongs to a different
        # company 46 % of the time, and a contact read off it would be a
        # stranger's - see select.undeliverable().
        if ico in have or site.get("status") != "proven":
            continue
        try:
            row = get_contacts(ico, site, known.get(ico) or {}, fetcher, archive)
        except Exception as error:
            print(f"  {ico}: contacts failed, {type(error).__name__}", file=sys.stderr)
            continue
        with open(CONTACTS_FILE, "a", encoding="utf-8") as sink:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")
        added += 1
    if added:
        print(f"  contacts read for {added} companies new to the base", file=sys.stderr)

    return {"recrawled": refreshed, "domains_resolved": resolved, "contacts_added": added}


def stage_agents(icos):
    """The LLM pass, only over the gated companies."""
    from pipeline.llm.prompts import pain, production_mode, relevance, turnover_web

    summary = {}
    for name, module in (("production_mode", production_mode), ("pain", pain)):
        try:
            summary[name] = len(module.run(icos=list(icos)) or [])
        except Exception as error:
            print(f"  {name}: {type(error).__name__} {error}", file=sys.stderr)
            summary[name] = None

    # Strictly after pain: the judge reads the claims that agent just
    # wrote. It can only mark a verified claim as beside the point, never
    # create one, so a failure here leaves the run with unjudged
    # evidence - which is the state every run before it was in, not a
    # broken one.
    try:
        summary["relevance"] = len(relevance.run(list(icos)) or [])
    except Exception as error:
        print(f"  relevance: {type(error).__name__} {error}", file=sys.stderr)
        summary["relevance"] = None

    try:
        summary["turnover_web"] = len(turnover_web.run(icos=set(icos)) or [])
    except Exception as error:
        print(f"  turnover_web: {type(error).__name__} {error}", file=sys.stderr)
        summary["turnover_web"] = None
    return summary


def stage_cards(archive, run_id, ranked, top, fetch_turnover=True, icp=None,
                qualified_count=None):
    """Render the week's dossiers and record what was handed over."""
    from pipeline.scoring.card import build, load_turnover_cache, render
    from pipeline.signals.now import load_tenders
    from pipeline.scoring.select import CONTACTS as C, WEBSITES as W, load_jsonl
    from pipeline.signals.now import load_companies

    companies = {c["ico"]: c for c in load_companies(CANDIDATES)}
    websites, contacts = load_jsonl(W), load_jsonl(C)
    turnover_cache = load_turnover_cache()
    # Loaded once for the whole batch rather than per card - build()
    # would otherwise re-read nen.jsonl five times over.
    tenders = load_tenders()

    cards = []
    for row in ranked[:top]:
        card = build(row["ico"], archive, companies, websites, contacts,
                     turnover_cache, fetch_turnover, tenders, icp)
        card["pain_score"] = row["pain_score"]
        # Why this company sits where it sits. Carried from the ranking
        # rather than recomputed, so the card cannot describe a different
        # ordering from the one that actually chose it.
        card["reason"] = row["reason"]
        # Other companies of the same group whose reason is this same
        # event - one call, not three (select.collapse_groups).
        card["group_siblings"] = row.get("group_siblings")
        # Whether this company holds the reserved slot for its class of
        # reason rather than a place it outranked somebody for. Carried,
        # not recomputed, for the reason `reason` above is carried.
        card["class_slot"] = bool(row.get("class_slot"))
        card["rank"] = ranked.index(row) + 1
        card["of_qualified"] = qualified_count if qualified_count is not None else len(ranked)
        cards.append(card)
        print(render(card))
        print()
        archive.mark_delivered(row["ico"], run_id)
    return cards


# ---------------------------------------------------------------------------
# The week, as the interface reads it
# ---------------------------------------------------------------------------


# What the industry tier is called on a card. Only the two that are worth
# saying: a company in the core of the ICP does not need a chip announcing
# that it is where it should be. Named, and no advice attached - the card
# states what the classification is and lets the salesperson decide.
TIER_CZ = {"other": "obor mimo jádro", "service": "výjezdní služba"}


def catalogue_fit(fit):
    """The FIT block with its labels already in Czech.

    The words travel with the data rather than being looked up again in
    the browser. web/week/ had its own copy of the production-mode names
    for one commit, guessed rather than taken from here, and every card
    showed an empty mode while the dossier - reading MODE_CZ - had it
    right. One vocabulary, written where the values are produced.
    """
    from pipeline.scoring.card import MODE_CZ

    fit = dict(fit or {})
    mode_label, _ = MODE_CZ.get(fit.get("mode"), ("", ""))
    fit["mode_label"] = mode_label or None
    fit["tier_label"] = TIER_CZ.get(fit.get("nace_tier"))
    return fit


def catalogue_contact(card):
    """The one person the catalogue names, or nothing.

    A channel is what makes a name worth printing, so a person with one
    wins over the first person in the register. GDPR (section 7): name,
    function and channel only - no score, no history, nothing joined
    across sources about the human being.
    """
    # Three sources, best first, and a channel is required at every step.
    # A person with no channel is not a fallback, it is a blank line: the
    # catalogue would print a name and a role and give the salesperson
    # nothing to do with them.
    #
    # But the contact does NOT have to be the owner. Requiring a jednatel
    # with their own e-mail cost three of one week's companies, and what
    # they were missing was a personal address, not a way to be reached -
    # TREJ - servis prints info@trej-servis.cz and four numbers. So a
    # named person from the contact page comes next, and the company's
    # own channel after that. Only a company nobody can reach at all
    # drops out (see catalogue()).
    people = card.get("contacts") or []
    person = next((p for p in people if p.get("email") or p.get("phone")), None)
    if person:
        return {
            "name": person.get("name"),
            "role": person.get("role_registered") or "",
            "email": person.get("email"),
            "phone": person.get("phone"),
        }

    channels = card.get("channels") or {}
    other = next(iter(channels.get("people") or []), None)
    if other:
        # The suffix is not decoration. This name and this job title came
        # off the company's own page, and the register may say something
        # else entirely - it does for INCO engineering, whose site calls
        # Pavel Špitálník a jednatel and whose register does not. A card
        # that prints "jednatel" for both kinds makes the two look
        # equally certain, which is the exact failure this project is
        # built against.
        role = other.get("role")
        return {
            "name": other.get("name"),
            "role": f"{role} — dle webu" if role else "kontakt z webu firmy",
            "email": other.get("email"),
            "phone": other.get("phone"),
        }

    email = next(iter(channels.get("emails") or channels.get("personal_emails") or []), None)
    phone = next(iter(channels.get("phones") or []), None)
    if email or phone:
        return {"name": None, "role": "obecný kontakt firmy",
                "email": email, "phone": phone}
    return None


def catalogue_row(row, card):
    """One company's line in the week's catalogue.

    Built from the ranking row and from the company's card - so the
    catalogue says the sentence the dossier says instead of writing a
    second, shorter description of the same event. Only companies that
    were handed over reach this, and every one of them has a card.
    """
    from pipeline.scoring.select import is_mode

    card = card or {}
    events = card.get("why_now") or []

    # Counted off the card when there is one, so the catalogue's tally and
    # the dossier's footer cannot disagree about the same company.
    #
    # signal_facts is the same tally with the production mode left out,
    # and it is what the week is ordered by for reading - see
    # select.is_mode() for why that kind does not count. Computed here
    # from the card's own facts rather than carried over from the row, so
    # it stays the number the card would show if somebody counted its
    # lines by hand.
    evidence = card.get("evidence")
    verified = ({"facts": len(evidence["facts"]),
                 "inferences": len(evidence["inferences"]),
                 "signal_facts": sum(1 for fact in evidence["facts"]
                                     if not is_mode(fact.get("kind")))}
                if evidence else (row.get("pain") or {}).get("verified") or {})

    turnover = card.get("turnover") or {}
    return {
        "ico": row["ico"],
        "name": card.get("name") or row.get("name"),
        "city": card.get("city"),
        "region": card.get("region"),
        # The band without the word: the catalogue prints it under a
        # column already headed VELIKOST, where "zaměstnanců" only costs
        # a second line. The dossier, which has no such column, keeps it.
        "size": (card.get("size") or "").replace("zaměstnanců", "").strip() or None,
        "turnover_czk": turnover.get("value_czk"),
        "site_domain": row.get("site_domain"),
        "site_status": row.get("site_status"),
        "fit": catalogue_fit(row.get("fit")),
        "geography": row.get("geography"),
        "reason": row.get("reason"),
        # Same reasoning as `demoted` and `size_unknown`: the interface
        # has to be able to say why a company sat where it sat, and
        # "it is the only one of its class" is one of those reasons.
        "class_slot": bool(row.get("class_slot") or card.get("class_slot")),
        "now_events": events,
        "negative": row.get("negative") or [],
        "demoted": bool(row.get("demoted")),
        # Carried for the same reason as `demoted`: the card has to be
        # able to say why a company sat where it sat. See ordering().
        "size_unknown": bool(row.get("size_unknown")),
        "group_siblings": row.get("group_siblings") or [],
        "certificates": [{"standard": c["standard"]} for c in card.get("certificates") or []
                         if c.get("standard")],
        "contact": catalogue_contact(card),
        "pain": {"verified": verified},
    }


def catalogue(run_id, window_days, ranked, cards):
    """The week as a document: the companies actually handed over, only.

    A withheld company does NOT get a line. It was held back because its
    domain is unproven or it has no channel - and in practice that means
    no site was ever read for it, so its card would carry a name, an IČO
    and one line from the register. That is the "seznam" the brief says
    the output must not be, and putting it on the same screen as a real
    dossier is worse than a short week.

    The count of what fell out stays in the header. A week of one has to
    explain itself; it just does not do so with cards.
    """
    by_ico = {row["ico"]: row for row in ranked}
    rows = [catalogue_row(by_ico.get(card["ico"], {"ico": card["ico"]}), card)
            for card in cards]
    # Last guard: no reachable person, no line. The gate above should have
    # stopped these already, so this normally removes nothing.
    rows = [row for row in rows if row["contact"]]
    withheld = sum(1 for row in ranked
                   if row.get("undeliverable") and not row.get("suppressed_by"))

    return {
        "run_id": run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": window_days,
        "qualified": len(ranked),
        "delivered": len(rows),
        "withheld": withheld,
        "top": rows,
    }


def write_results(document, path=RESULTS):
    """Written to a temporary file and renamed, for the reason every other
    write in this module gives: a half-written results file looks exactly
    like a complete short week."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
    temporary.replace(path)
    return path


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(stages=STAGES, window_days=DEFAULT_WINDOW, top=DEFAULT_TOP,
        refresh_days=None, fetch_turnover=True):
    """The weekly run. Returns the cards handed over."""
    buildable, manual = check(stages, verbose=False)
    if buildable or manual:
        print("preflight failed - missing prerequisites:\n", file=sys.stderr)
        check(stages)
        print("\nrun with --bootstrap to build what can be built.", file=sys.stderr)
        return None

    archive = Archive()
    icp = load_icp()
    run_id = archive.start_run(note=f"weekly run {date.today().isoformat()}", icp=icp)
    print(f"run {run_id} · ICP from {icp['_source']}", file=sys.stderr)

    report = {"run_id": run_id, "icp": icp["_source"], "stages": {}}

    if "refresh" in stages:
        print("\n[refresh]", file=sys.stderr)
        report["stages"]["refresh"] = stage_refresh(
            archive, run_id, refresh_days or window_days, icp)

    print("\n[gate]", file=sys.stderr)
    qualified, sites_by_ico = stage_gate(window_days, icp, archive, run_id)
    report["stages"]["gate"] = len(qualified)

    if not qualified:
        print("\nno company has a dated reason this week - nothing to hand over.",
              file=sys.stderr)
        archive.finish_run(run_id)
        archive.close()
        return []

    if "enrich" in stages:
        print("\n[enrich]", file=sys.stderr)
        report["stages"]["enrich"] = stage_enrich(archive, run_id, qualified)

    if "agents" in stages:
        print("\n[agents]", file=sys.stderr)
        report["stages"]["agents"] = stage_agents(qualified)

    print("\n[select]", file=sys.stderr)
    from pipeline.scoring.select import run as select_run
    # Two lists on purpose: everyone who qualified, and the ones that can
    # actually be handed over. A company with no way to reach anybody is
    # not a dossier, so it stays in the ranking and out of the week.
    deliverable, ranked = select_run(window_days, top, archive=archive,
                                     qualified=qualified, icp=icp,
                                     establishments=sites_by_ico)
    report["stages"]["select"] = {"qualified": len(ranked), "deliverable": len(deliverable)}

    print("\n[cards]", file=sys.stderr)
    cards = stage_cards(archive, run_id, deliverable, top, fetch_turnover, icp, len(ranked))

    # 22.3: fewer than five is an answer, not a shortfall to paper over.
    if len(cards) < top:
        print(f"only {len(cards)} companies had a real reason this week, not {top}. "
              f"Handing over {len(cards)} rather than padding the list.", file=sys.stderr)

    written = write_results(catalogue(run_id, window_days, ranked, cards))
    print(f"\nresults for the interface: {written}", file=sys.stderr)

    archive.finish_run(run_id)
    archive.close()
    report["delivered"] = [c["ico"] for c in cards]
    return cards


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="One weekly icp-scout run.")
    parser.add_argument("--check", action="store_true", help="report prerequisites and exit")
    parser.add_argument("--bootstrap", action="store_true",
                        help="build every missing prerequisite that can be built")
    parser.add_argument("--dry-run", action="store_true", help="with --bootstrap: print, do not run")
    parser.add_argument("--stage", action="append", choices=STAGES,
                        help="run only these stages (repeatable)")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help="NOW window in days (default 7, the run cadence)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    parser.add_argument("--no-turnover", action="store_true",
                        help="skip the Sbirka listin lookup (slow, cached otherwise)")
    args = parser.parse_args()

    chosen = tuple(args.stage) if args.stage else STAGES

    if args.check:
        print("prerequisites:")
        buildable, manual = check(chosen)
        raise SystemExit(1 if (buildable or manual) else 0)

    if args.bootstrap:
        raise SystemExit(0 if bootstrap(chosen, args.dry_run) else 1)

    result = run(chosen, args.window, args.top, fetch_turnover=not args.no_turnover)
    raise SystemExit(0 if result is not None else 1)
