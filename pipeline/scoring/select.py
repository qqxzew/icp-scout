"""Weekly selection: from the candidate list to five companies with reasons.

The formula, settled in conversation rather than guessed, is deliberately
small - three stages, each doing one job and nothing else:

    FIT   already done by the time this module runs. res_bulk.py's
          candidate list *is* the FIT-qualified set - size and NACE are
          hard filters applied there, not here. Distance is never a
          filter (the ICP says "preferovaně", not "pouze"); it is a sort
          key the UI applies for display, not a reason to drop anyone.

    NOW   a gate, not a score. A company passes if it has at least one
          dated event within the run window - and the window is 7 days
          because the pipeline runs weekly. Nothing about NOW ranks
          companies against each other; it only decides who is worth
          the expensive PAIN pass at all. This is what keeps a weekly
          run cheap: only NOW-gated companies ever reach harvest().

    PAIN  ranks what NOW let through. Not a verdict-scorer - a count of
          how much verifiable evidence exists for a company. More
          evidence means a stronger card, so "we can say the most about
          this company, with sources" is the tie-breaker when there are
          more NOW-qualified companies than five slots in a week.

Nothing here fetches anything. Every number is read from files and the
archive that earlier pipeline stages already wrote - running this costs
no network request and can be re-run freely while tuning the weights.

Run:
    python -m pipeline.scoring.select
    python -m pipeline.scoring.select --window 7 --top 5
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from pipeline.evidence.archive import Archive
from pipeline.signals import mode as mode_signal
from pipeline.signals.now import SUBSIDY_WINDOW, find as now_events, load_companies, load_history
from pipeline.sources.dotace_eu import load as load_subsidies

ARES_CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")
WEBSITES = Path("data/raw/websites.jsonl")
CONTACTS = Path("data/raw/contacts.jsonl")
MPSV_VACANCIES = Path("data/raw/mpsv_vacancies.jsonl")

DEFAULT_WINDOW = 7   # days - matches the weekly run cadence, not a guess
DEFAULT_TOP = 5


# ---------------------------------------------------------------------------
# Loading what earlier stages already produced
# ---------------------------------------------------------------------------


def load_jsonl(path):
    if not Path(path).exists():
        return {}
    out = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if "ico" in row:
                out[row["ico"]] = row
    return out


def load_mpsv_by_ico(path=MPSV_VACANCIES):
    """ICO -> list of current vacancy texts, for PAIN richness and mode.py."""
    out = {}
    if not Path(path).exists():
        return out
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            out.setdefault(row["ico"], []).append(row)
    return out


# ---------------------------------------------------------------------------
# NOW: the gate
# ---------------------------------------------------------------------------


def now_qualified(companies, history, window_days=DEFAULT_WINDOW, subsidies=None):
    """Companies with at least one dated NOW event inside the window.

    Returns {ico: events}. Everything not in this dict skipped the
    expensive PAIN pass entirely for this run - that is the point of
    putting the gate here rather than scoring everyone and filtering
    after the fact.

    `subsidies` defaults to loading dotace_eu.jsonl itself rather than
    silently running registry+vacancy only - found live while hand-
    tracing a run end to end: this call was passing nothing, so the
    entire EU-subsidy signal group (its own 120-day window, see
    signals/now.py) never reached the gate, and a real run undercounted
    every week by however many companies only had a subsidy event.
    """
    if subsidies is None:
        subsidies = load_subsidies()
    qualified = {}
    for company in companies:
        events = now_events(company, history, window_days, subsidies=subsidies)
        if events:
            qualified[company["ico"]] = events
    return qualified


# ---------------------------------------------------------------------------
# PAIN: how much can we actually say about this company
# ---------------------------------------------------------------------------


def website_richness(archive, ico, site_status=None):
    """Harvested pages by kind, and how many carry a production-mode claim.

    Reads what website.py's harvest() already archived - no fetch here.
    Pages are weighted by kind because a contact page proves the domain
    but a production/career/about page is what PAIN actually reasons
    over; an "about" page with three mode.py findings says more about
    the company than five bare contact pages would.

    Only pages from a PROVEN domain count. Measured: 300 of 300 sampled
    "probable" companies were contributing pages to their own PAIN score
    here, and by website.py's own measurement 46 % of guessed live
    domains belong to somebody else - so an unproven site could rank a
    company on evidence about a different business entirely. The LLM
    agents were fixed for this; the score was still counting them.
    """
    status = (site_status or {}).get(ico, {}).get("status")
    if site_status is not None and status not in ("proven",):
        return {"pages": 0, "mode_findings": 0}

    documents = archive.documents(ico, source="website")
    pages = len(documents)

    findings = 0
    for row, text in documents:
        if not text:
            continue
        findings += len(mode_signal.scan(text, row["kind"] or "unknown"))

    return {"pages": pages, "mode_findings": findings}


def contact_richness(contacts_row):
    """Named people with a channel, and whether any is register-confirmed."""
    if not contacts_row:
        return {"named_people": 0, "register_confirmed": 0}
    people = contacts_row.get("people") or []
    reachable = [p for p in people if p.get("email") or p.get("phone")]
    confirmed = [p for p in reachable if (p.get("source") or "register") == "register"]
    return {"named_people": len(reachable), "register_confirmed": len(confirmed)}


def vacancy_richness(vacancies):
    """How much the company has said about itself through hiring."""
    texts = sum(1 for v in vacancies if (v.get("text") or "").strip())
    return {"vacancy_count": len(vacancies), "vacancy_text_count": texts}


def verified_richness(archive, ico):
    """Claims that survived evidence/verify.py, split by how they are held.

    This is the number the ranking should actually turn on, and it was
    missing: the score below used to rank companies purely on regex
    findings and page counts, while the LLM agents' verified facts -
    the ones that carry a quote and a URL and are what the salesperson
    reads on the card - counted for nothing. A company was therefore
    ranked on how much text we had rather than on how much we could
    prove, which is the opposite of the brief's own position.

    Facts and inferences are counted apart because they are not the
    same currency: a fact carries a quote found in an archived page, an
    inference is the model reasoning without one.
    """
    facts = inferences = 0
    for row in archive.claims(ico):
        if row["kind"].startswith("now:"):     # NOW is a gate, counted separately
            continue
        if row["state"] == "fact":
            facts += 1
        elif row["state"] == "inference":
            inferences += 1
    return {"facts": facts, "inferences": inferences}


def pain_score(website, contact, vacancy, now_event_count, verified=None):
    """One sortable number from the richness counts above.

    Not a calibrated model - a priority order. A verified fact outranks
    everything else because it is the only item that reaches the
    salesperson with a quote and a source behind it; an inference counts,
    but at a third of a fact, because it is the model reasoning rather
    than the page speaking. Register-confirmed contacts stay high for the
    same reason - the person is named in a state register, not guessed.
    Page and vacancy counts fall to what they always should have been:
    a tie-breaker measuring how much material exists, not how much of it
    turned out to be true. Documented as a ranking rationale, not tuned
    against ground truth - there is none yet.
    """
    verified = verified or {"facts": 0, "inferences": 0}
    return round(
        verified["facts"] * 6
        + verified["inferences"] * 2
        + contact["register_confirmed"] * 5
        + website["mode_findings"] * 3
        + contact["named_people"]
        + website["pages"]
        + vacancy["vacancy_text_count"]
        + now_event_count
    )


def evaluate(ico, archive, websites, contacts, vacancies_by_ico, events):
    site = websites.get(ico, {})
    web_r = website_richness(archive, ico, websites)
    contact_r = contact_richness(contacts.get(ico))
    vac_r = vacancy_richness(vacancies_by_ico.get(ico, []))
    verified_r = verified_richness(archive, ico)
    score = pain_score(web_r, contact_r, vac_r, len(events), verified_r)
    return {
        "ico": ico,
        "site_status": site.get("status"),
        "site_domain": site.get("domain"),
        "now_events": events,
        "pain": {"website": web_r, "contact": contact_r, "vacancy": vac_r,
                 "verified": verified_r},
        "pain_score": score,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(window_days=DEFAULT_WINDOW, top=DEFAULT_TOP, archive=None, qualified=None):
    """Full weekly selection: FIT (already done) -> NOW gate -> PAIN rank.

    Returns the ranked list of NOW-qualified companies, longest first;
    the caller decides how many of them become the week's five - keeping
    that a caller decision, not baked in here, is what lets the CLI print
    "here are all 23 that qualified, and the top 5" in one pass.
    """
    archive = archive or Archive()
    companies = list(load_companies(ARES_CANDIDATES))
    history = load_history()
    websites = load_jsonl(WEBSITES)
    contacts = load_jsonl(CONTACTS)
    vacancies_by_ico = load_mpsv_by_ico()

    # run.py has already run the gate to decide who was worth enriching,
    # so it hands the result in rather than paying for a second pass over
    # 3299 companies. Recomputing was not just wasteful - two gates with
    # separately-passed arguments are two chances to disagree about who
    # qualified, and the card would then be built for one set while the
    # ranking described another.
    if qualified is None:
        qualified = now_qualified(companies, history, window_days)
    by_ico = {c["ico"]: c for c in companies}

    ranked = [
        evaluate(ico, archive, websites, contacts, vacancies_by_ico, events)
        for ico, events in qualified.items()
    ]
    ranked.sort(key=lambda r: r["pain_score"], reverse=True)

    for row in ranked:
        row["name"] = by_ico[row["ico"]].get("name")

    return ranked[:top], ranked


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly FIT->NOW->PAIN selection.")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help="NOW gate window in days (default: 7, matches weekly runs)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    args = parser.parse_args()

    top5, all_qualified = run(args.window, args.top)

    print(f"NOW-qualified this window: {len(all_qualified)}", file=sys.stderr)
    print(f"top {len(top5)}, ranked by PAIN richness:\n", file=sys.stderr)
    for rank, row in enumerate(top5, 1):
        print(f"{rank}. {row['name'][:42]:44} score={row['pain_score']:3}  "
              f"site={row['site_status']}  now={len(row['now_events'])} event(s)",
              file=sys.stderr)

    print(json.dumps({"top": top5, "qualified": all_qualified}, ensure_ascii=False, indent=2))
