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
import subprocess
import sys
import time
from datetime import date
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
ENV_FILE = Path(".env")

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


def requirements():
    """Everything a run touches, in the order one thing needs another."""
    return [
        Need(ENV_FILE, "OpenAI API key", ("agents",),
             hint="create .env with OPENAI_API_KEY=sk-... (see .env.example)"),
        Need(RES_BULK, "RES bulk export, 517 MB", ("bootstrap",), min_mb=400, minutes=10,
             build=[sys.executable, "-m", "pipeline.sources.res_bulk", "--download"]),
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

    `location` is the brief's own geography, CLAUDE.md §2: "preferovaně
    Plzensky kraj -> Karlovarsky, Jihocesky, Stredocesky, Praha (~150
    km)" - RTsoft sits in Plzen and drives to the shop floor. It is a
    priority ("preferovaně") in the ICP document - but the salesperson
    using the tool overruled that: a radius on the screen is a promise
    about what comes back, so filters/brief.py enforces it whoever set
    it. `from_default` is kept as a record of who chose the number, not
    as a switch: nothing branches on it any more.
    """
    from pipeline.sources.res_bulk import (ICP_FORMA, ICP_KATPO, ICP_KATPO_UNKNOWN,
                                           ICP_NACE)
    return {
        "nace": sorted(ICP_NACE),
        # Both the sized bands and the unsized tier. The brief admits a
        # company whose headcount the register never recorded - refusing
        # it would be reading absence as a negative answer - while
        # res_bulk decides separately how such a company is ever
        # enriched, which is not the same question.
        "katpo": sorted(ICP_KATPO + ICP_KATPO_UNKNOWN),
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

    This is the mechanism CLAUDE.md section 5 describes and the pipeline
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
    from pipeline.filters.brief import describe
    from pipeline.scoring.select import eligible
    from pipeline.signals.now import (find, load_companies, load_history,
                                      load_tenders, record)
    from pipeline.sources.dotace_eu import load as load_subsidies

    companies = list(load_companies(CANDIDATES))
    pool, funnel = eligible(companies, icp)
    history, subsidies = load_history(), load_subsidies()
    tenders = load_tenders()

    print(f"  brief: {describe(icp)}", file=sys.stderr)
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

    return qualified


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
        try:
            found = resolve(ico, company.get("name") or "", company.get("city"),
                            fetcher=fetcher, archive=archive, run_id=run_id)
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
        card["rank"] = ranked.index(row) + 1
        card["of_qualified"] = qualified_count if qualified_count is not None else len(ranked)
        cards.append(card)
        print(render(card))
        print()
        archive.mark_delivered(row["ico"], run_id)
    return cards


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
    qualified = stage_gate(window_days, icp, archive, run_id)
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
                                     qualified=qualified, icp=icp)
    report["stages"]["select"] = {"qualified": len(ranked), "deliverable": len(deliverable)}

    print("\n[cards]", file=sys.stderr)
    cards = stage_cards(archive, run_id, deliverable, top, fetch_turnover, icp, len(ranked))

    # 22.3: fewer than five is an answer, not a shortfall to paper over.
    if len(cards) < top:
        print(f"only {len(cards)} companies had a real reason this week, not {top}. "
              f"Handing over {len(cards)} rather than padding the list.", file=sys.stderr)

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
