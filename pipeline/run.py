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
    3 gate      NOW - a dated event or the company does not proceed
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
    """
    from pipeline.sources.res_bulk import ICP_FORMA, ICP_KATPO, ICP_NACE
    return {
        "nace": sorted(ICP_NACE), "katpo": sorted(ICP_KATPO), "forma": sorted(ICP_FORMA),
    }


def load_icp():
    """The salesperson's brief, or the built-in ICP when none was saved.

    Requirement 1 of the brief is to accept the ICP as input, and the
    point of recording it on the run is that a week later "why these
    companies" has an answer. A missing file is not an error - it means
    nobody has pressed save in the interface yet - but the fallback is
    recorded explicitly as the fallback, not passed off as a choice.
    """
    if ICP_FILE.exists():
        icp = json.loads(ICP_FILE.read_text(encoding="utf-8"))
        icp["_source"] = str(ICP_FILE)
        return icp

    icp = default_icp()
    icp["_source"] = "built-in default (web/icp.json not saved yet)"
    return icp


def stage_refresh(archive, run_id, days):
    """Bring the volatile sources up to date. Registry: only what moved."""
    from pipeline.sources import ares, ares_notifications, dotace_eu, mpsv

    icos, meta = ares_notifications.to_refresh(days=days)
    print(f"  registry: {meta['changes']} changed nationally, {len(icos)} of ours",
          file=sys.stderr)
    if meta["history_too_short"]:
        print("  WARNING: window exceeds ARES's retained batches - "
              "registry events before "
              f"{meta['covered_from']} are not covered. Run ares.py --all.", file=sys.stderr)

    for ico in icos:
        ares.get_company(ico, archive=archive)
    print(f"  registry: {len(icos)} companies re-fetched", file=sys.stderr)

    mpsv.refresh(archive=archive)
    dotace_eu.refresh(archive=archive)
    return {"registry_refreshed": len(icos), "registry_meta": meta}


def stage_gate(window_days):
    """NOW: who has a dated reason this week. Everything else stops here.

    The negative filter runs here, before the gate rather than after it,
    for the reason the plan gives: throw work away while it is still
    cheap. A company in insolvency matches every line of the ICP and
    cannot buy anything, and letting it through would spend a site crawl
    and an LLM pass to produce a card nobody can act on.
    """
    from pipeline.filters.negative import EXCLUDE, verdict
    from pipeline.signals.now import find, load_companies, load_history, load_tenders
    from pipeline.sources.dotace_eu import load as load_subsidies

    companies = list(load_companies(CANDIDATES))
    history, subsidies = load_history(), load_subsidies()
    tenders = load_tenders()

    qualified, excluded = {}, 0
    for company in companies:
        if verdict(company) == EXCLUDE:
            excluded += 1
            continue
        events = find(company, history, window_days,
                      subsidies=subsidies, tenders=tenders)
        if events:
            qualified[company["ico"]] = events

    print(f"  {excluded} excluded by negative filters "
          f"(insolvency, liquidation)", file=sys.stderr)
    print(f"  {len(qualified)} of {len(companies)} companies have a dated event",
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
    from pipeline.sources.website import Fetcher, harvest, keep

    status = load_jsonl(WEBSITES)
    fetcher = Fetcher()
    refreshed = 0

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
    return {"recrawled": refreshed}


def stage_agents(icos):
    """The LLM pass, only over the gated companies."""
    from pipeline.llm.prompts import pain, production_mode, turnover_web

    summary = {}
    for name, module in (("production_mode", production_mode), ("pain", pain)):
        try:
            summary[name] = len(module.run(icos=list(icos)) or [])
        except Exception as error:
            print(f"  {name}: {type(error).__name__} {error}", file=sys.stderr)
            summary[name] = None

    try:
        summary["turnover_web"] = len(turnover_web.run(icos=set(icos)) or [])
    except Exception as error:
        print(f"  turnover_web: {type(error).__name__} {error}", file=sys.stderr)
        summary["turnover_web"] = None
    return summary


def stage_cards(archive, run_id, ranked, top, fetch_turnover=True):
    """Render the week's dossiers and record what was handed over."""
    from pipeline.scoring.card import build, load_turnover_cache, render
    from pipeline.scoring.select import CONTACTS as C, WEBSITES as W, load_jsonl
    from pipeline.signals.now import load_companies

    companies = {c["ico"]: c for c in load_companies(CANDIDATES)}
    websites, contacts = load_jsonl(W), load_jsonl(C)
    turnover_cache = load_turnover_cache()

    cards = []
    for row in ranked[:top]:
        card = build(row["ico"], archive, companies, websites, contacts,
                     turnover_cache, fetch_turnover)
        card["pain_score"] = row["pain_score"]
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
            archive, run_id, refresh_days or window_days)

    print("\n[gate]", file=sys.stderr)
    qualified = stage_gate(window_days)
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
    _, ranked = select_run(window_days, top, archive=archive, qualified=qualified)
    report["stages"]["select"] = len(ranked)

    print("\n[cards]", file=sys.stderr)
    cards = stage_cards(archive, run_id, ranked, top, fetch_turnover)

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
