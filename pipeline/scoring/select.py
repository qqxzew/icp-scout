"""Weekly selection: from the candidate list to five companies with reasons.

The formula, settled in conversation rather than guessed, is deliberately
small - three stages, each doing one job and nothing else:

    FIT   two halves. The hard one is eligible(): the salesperson's
          saved brief (filters/brief.py) and the negative filters
          (filters/negative.py) decide who may be considered at all.
          The soft one is fit_assessment() plus distance: the industry
          tier and how far the company sits from the brief's origin
          never exclude anybody - they group the survivors, and the tier
          is the first thing the ordering looks at. The production mode
          is read and shown but never ranked on, because no register
          carries it and the site says what marketing wrote. Distance
          is a sort key rather than a filter because the ICP says
          "preferovaně", not "pouze"; a radius somebody typed by hand is
          the one exception and it is applied earlier, in brief.py.

    NOW   a gate, not a score. A company passes if it has at least one
          dated event inside ITS SOURCE'S window - 7 days for the
          register, 24 for vacancies, 120 for subsidies, none at all for
          an open tender. The 7 days is the run cadence and applies only
          to the register, because the register is the only source that
          publishes faster than the run repeats; signals/now.py has the
          measured lag behind each of the others. Nothing about NOW ranks
          companies against each other; it only decides who is worth
          the expensive PAIN pass at all. This is what keeps a weekly
          run cheap: only NOW-gated companies ever reach harvest().

    PAIN  no longer ranks anything, and that is a result rather than a
          simplification. It was a weighted sum, and the sum turned out
          to be one term wearing eight hats: `facts` carried 62.6 % of
          the points and correlates +0.81 with the number of pages
          harvested from the site, so the week's five were chosen
          largely by whose website was biggest - and only 35 % of random
          weight vectors reproduced the same five. Two experiments then
          showed no better numbers were available: against companies
          that demonstrably bought planning software, neither the
          hand-built components (p = 0.72-1.00) nor a 1536-dimension
          embedding of the same sites (AUC 0.39) separated buyers from
          anyone else. So PAIN became what it can honestly be - the
          evidence a salesperson reads on the card - and what orders the
          list is the class of the REASON, graded by the brief. See
          reason_class() and ordering().

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
from pipeline.evidence.verify import counts_as_evidence, usable
from pipeline.filters import brief as brief_filter
from pipeline.filters import negative
from pipeline.signals import mode as mode_signal
from pipeline.signals.now import (SUBSIDY_WINDOW, find as now_events, load_companies,
                                  load_history, load_tenders)
from pipeline.sources.coords import distance_km
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
# FIT, the hard half: who may be considered at all
# ---------------------------------------------------------------------------


def eligible(companies, icp):
    """The pool a run may draw from, and the funnel that produced it.

    Both entry points go through this - run.py's gate before the
    expensive stages, and this module's own CLI - because they used to
    disagree. run.py applied the negative filters and select.py did not,
    so `python -m pipeline.scoring.select` ranked companies in
    insolvency that a full run had already thrown out, and neither
    applied the saved brief at all. Two pools are two answers to "who
    was considered this week", and the card would then describe one
    while the ranking described the other.

    Returns (pool, funnel). The funnel is per-reason rather than a
    single number so a small pool can be explained instead of guessed
    at - see filters/brief.py.
    """
    kept, rejected = brief_filter.apply(companies, icp)
    pool = [c for c in kept if negative.verdict(c) != negative.EXCLUDE]
    return pool, {
        "total": len(companies),
        "brief_rejected": dict(rejected),
        "excluded": len(kept) - len(pool),
        "pool": len(pool),
    }


# ---------------------------------------------------------------------------
# NOW: the gate
# ---------------------------------------------------------------------------


def now_qualified(companies, history, window_days=DEFAULT_WINDOW, subsidies=None):
    """Companies with at least one dated NOW event inside its source's window.

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
    # Same lesson, same shape: an optional argument left at None silently
    # removes a whole signal group. Loaded here rather than defaulted
    # away, exactly as subsidies now are.
    tenders = load_tenders()
    qualified = {}
    for company in companies:
        events = now_events(company, history, window_days,
                            subsidies=subsidies, tenders=tenders)
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
    """Named people with a channel, plus whether there is any channel at all.

    `any_channel` is what decides whether the company may be handed over
    (see reachable() below), so it counts the switchboard and the info@
    address too: a card the salesperson cannot act on is not a lead, but
    a general number is something to act on and a name with no way to
    reach it is not.
    """
    if not contacts_row:
        return {"named_people": 0, "register_confirmed": 0, "any_channel": False}
    people = contacts_row.get("people") or []
    reachable = [p for p in people if p.get("email") or p.get("phone")]
    confirmed = [p for p in reachable if (p.get("source") or "register") == "register"]
    company = contacts_row.get("company") or {}
    shared = bool(company.get("emails") or company.get("phones")
                  or company.get("personal_emails"))
    return {"named_people": len(reachable), "register_confirmed": len(confirmed),
            "any_channel": bool(reachable) or shared}


def vacancy_richness(vacancies):
    """How much the company has said about itself through hiring."""
    texts = sum(1 for v in vacancies if (v.get("text") or "").strip())
    return {"vacancy_count": len(vacancies), "vacancy_text_count": texts}


# The ICP's own production divisions against the service ones that were
# added to widen the field (ARCHITECTURE.md 2: the five ICP examples span
# three NACE sections, so "section C only" contradicted the brief). The
# widening was right for candidate selection and wrong to forget at
# selection time: NACE 41 construction qualified through the gate on a
# genuine board change and reached the salesperson with nothing anywhere
# asking "is this a manufacturer at all".
NACE_CORE = {"16", "18", "22", "23", "25", "26", "27", "28", "31", "32", "33"}
NACE_SERVICE = {"38", "41", "42", "43", "49", "77", "81", "95"}

# THE PRODUCTION MODE DOES NOT ORDER ANYTHING, AND THAT TOOK TWO
# CORRECTIONS TO GET RIGHT.
#
# It began as a rank: made-to-order best, unknown in the middle, serial
# last, on the reasoning that the ICP's first criterion is "zakazkovy,
# ne seriovy". The middle position went first. Measured on a real gate,
# 14 of 31 companies had not a single harvested page and 12 of those
# were `unknown` for that reason alone; the mode rank correlated -0.53
# with the page count, so it was ranking whether the crawler had managed
# to open the site, not how the company produces.
#
# The rest of it went for a better reason: the verdict is not solid
# enough to move anybody. It is read out of website prose and job ads,
# and the project's own record of checking it by hand is 0 clean
# answers out of 3 companies (ARCHITECTURE.md 10) - Robex says "na
# zakazku" and keeps a catalogue,
# Laub says "kusova i seriova" on one page, Jaro says nothing. The ICP
# itself marks the criterion "vyvod, ne fakt": no register carries it.
# Ordering on a marketing sentence is exactly the kind of confident
# wrongness this pipeline is supposed to refuse.
#
# Measured before removing it: of 24 qualified companies the mode
# demoted zero - 17 unknown, 6 made-to-order, 1 mixed. It was already
# doing nothing; what it kept was the ability to sink a company one day
# on a sentence written by a marketing agency.
#
# So the mode stays on the card, where a salesperson reads it as
# context with its basis stated, and stays out of the sort key. What
# remains in FIT is the NACE tier, which comes from the register rather
# than from scraped prose and is what caught a construction firm with a
# rich website (KVAZAR).
TIER_RANK = {"core": 0, "other": 1, "service": 2}


def fit_assessment(archive, company, site_status=None):
    """How well this company matches the ICP - from evidence already held.

    This existed as data and not as a decision: the KVAZAR miss showed
    the disqualifying sentence ("realizuje výstavbu a rekonstrukce
    staveb") sitting verified in the archive while selection counted it
    as +1 richness. The scorer asked "how much can we prove about this
    company" and never "is this the ICP's company" - so a construction
    firm with a rich site outranked its own evidence.

    Nothing here excludes. Per RTsoft's answer ("je potřeba ty leady
    vidět a pak v nich hledat vodítka") and the log's 22.3, a poor fit
    is said on the card and sinks in the ordering; the human decides.
    """
    ico = company.get("ico")
    division = (company.get("nace") or "")[:2]
    tier = ("core" if division in NACE_CORE
            else "service" if division in NACE_SERVICE else "other")

    # Verified agent claims first: they carry the side in their kind
    # (production_mode:made_to_order) and survive vacancy rotation -
    # ŠROUBY Krupka's "výroba dle výkresové dokumentace" lives in a job
    # ad that has since left MPSV's current export, so the archive's
    # verified claim is the only place the mode still exists. The regex
    # scan over current documents is the fallback, not the authority.
    sides = set()
    for claim in archive.claims(ico):
        if (claim["kind"].startswith("production_mode:") and claim["state"] == "fact"
                and counts_as_evidence(claim)):
            sides.add(claim["kind"].split(":", 1)[1])
    if sides:
        if "made_to_order" in sides and "serial" in sides:
            agent_mode = "mixed"
        elif "made_to_order" in sides and "small_batch" in sides:
            # Both, and neither swallows the other. signals/mode.py made
            # the same point about its regex verdict: small_batch is a
            # full answer, not a weaker made_to_order. peform Chomutov
            # says "ZAKÁZKOVÉ ZPRACOVÁNÍ PLECHŮ" on one page and
            # "Realizujeme menší až střední série desítek až tisíců
            # kusů" on another, and a card that prints only "zakázková"
            # has dropped the half that says how big the runs are - the
            # half a scheduling system is actually sold against.
            agent_mode = "made_to_order_small_batch"
        elif "made_to_order" in sides:
            agent_mode = "made_to_order"
        elif "small_batch" in sides:
            agent_mode = "small_batch"
        else:
            agent_mode = "serial"
        return {
            "nace_tier": tier, "mode": agent_mode, "mode_basis": "agent_fact",
            "rank": TIER_RANK[tier],
        }

    findings = []
    status = (site_status or {}).get(ico, {}).get("status")
    if site_status is None or status == "proven":
        for row, text in archive.documents(ico, source="website"):
            if text:
                findings += mode_signal.scan(text, row["kind"] or "website")
    # Vacancy text is scanned regardless of the site status - it comes
    # from MPSV keyed by ICO, so it cannot belong to the wrong company.
    # And it matters: ŠROUBY Krupka's "výroba dle výkresové dokumentace
    # zákazníka" lives in a job ad, not on the site, and scanning the
    # site alone left its mode unknown while the proof sat in the
    # archive.
    for row, text in archive.documents(ico, source="mpsv_text"):
        if text:
            findings += mode_signal.scan(text, "vacancy")
    verdict = mode_signal.classify(findings)

    return {
        "nace_tier": tier,
        "mode": verdict["mode"],
        "mode_basis": verdict.get("basis"),
        # The NACE tier alone - see the note above MODE_RANK's remains
        # for why the production mode is carried and not ranked on.
        "rank": TIER_RANK[tier],
    }


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

    Two kinds of claim are counted and then not scored, and both were
    raising scores before this line existed (evidence/verify.py has the
    reasoning for each):

      * a quoteless statement that the evidence is NOT there. Four of
        the five inferences on the last run's top card were of this
        shape, so the company was ranked higher for every pain sign it
        turned out not to have.
      * a fact the relevance judge marked as beside the point.

    They stay in the archive - they were really produced and the record
    is the record - and `discounted` carries how many there were, so
    "this card is thin because half its evidence was discounted" stays
    visible instead of looking like a company nobody could say anything
    about.
    """
    # NOW is a gate and is counted separately, so it never reaches the
    # PAIN score.
    rows = [row for row in archive.claims(ico) if not row["kind"].startswith("now:")]
    kept = usable(rows)
    facts = sum(1 for row in kept if row["state"] == "fact")
    inferences = sum(1 for row in kept if row["state"] == "inference")
    return {"facts": facts, "inferences": inferences,
            "signal_facts": sum(1 for row in kept
                                if row["state"] == "fact" and not is_mode(row["kind"])),
            "discounted": len(rows) - len(kept)}


def is_mode(kind):
    """Is this claim only the production mode?

    Counted everywhere else, and deliberately not counted where the week
    is ordered for reading. Two reasons, both already written down:

    It is the largest kind by a distance - 57 of the 123 claims in the
    archive at the time of writing, against 24 for all three pain signs
    together - because every page that mentions making things to order
    files one, so it measures how many pages a company has rather than
    how much is known about it.

    And it is the least trustworthy thing on the card. Checked by hand on
    three companies, none gave a clean answer: one says "na zakázku" and
    keeps a catalogue, one says "kusová i sériová" on a single page, the
    third says nothing at all. A count led by that is a count led by
    marketing copy.

    A company whose only facts are mode facts therefore sorts as zero and
    keeps the ranking's own order, which is the honest outcome: nothing
    was learned about it that the ranking did not already weigh.
    """
    return str(kind).startswith("production_mode")


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


# ---------------------------------------------------------------------------
# The reason to call, graded by the brief rather than by a weight
# ---------------------------------------------------------------------------
#
# WHY THIS REPLACED A SCORE. pain_score used to decide the week's five,
# and three measurements took it apart: `facts` carried 62.6 % of every
# point, `facts` correlates +0.81 with the raw number of pages harvested
# from a site, and only 35 % of random weight vectors reproduced the same
# top five. So the five companies handed over were chosen mostly by whose
# website was biggest, and the numbers deciding it were invented.
#
# Two experiments then asked whether better numbers were even possible.
# Against companies that demonstrably bought planning software (an EU
# subsidy naming a vendor, or an awarded procurement), the hand-built
# components separated nothing: a dysfunction sign was found for 9/19
# buyers, 10/22 equally-subsidised non-buyers, 5/13 of a random draw,
# p = 0.72-1.00. Embedding the same sites in 1536 dimensions did no
# better - buyers against non-buyers gave a cross-validated AUC of 0.39,
# below chance. The ceiling is the source: a company's public website
# does not say whether it is about to buy.
#
# So PAIN stops deciding and becomes what it can honestly be - the
# evidence printed on the card - and the order comes from the brief.
# Every line below can be pointed at in the ICP document:
#
#   A  subsidy signed, no procurement started. Section 6's matrix calls
#      this "lepsi pripad, volat ted" - money already allocated to our
#      category, purchase not yet begun. The strongest wording in the
#      whole document.
#   B  someone arrived in or left the leadership. Triggers 6 and 7, and
#      the cleanest signal there is - a dated register entry.
#   C  an open procurement. They are buying now, the specification is
#      already written, so we are catching up rather than leading.
#   D  growth: a management or planning vacancy.
#   E  everything else, including the case the matrix calls lost - a
#      subsidy with a procurement already awarded. That falls here by
#      construction rather than by a rule of its own: the company has a
#      relevant tender, so it cannot be A, and nothing else claims it.
#
# A AND B WERE THE OTHER WAY ROUND UNTIL THE READING WAS CHECKED. "The
# cleanest signal" in section 2 is a statement about VERIFIABILITY - a
# structured register entry with a date, impossible to hallucinate - not
# about the likelihood of a purchase. Section 6 makes the likelihood
# claim, and it makes it about the subsidy. Measurements on the buyer
# label agree, but they cannot be cited as proof: that label is defined
# partly by holding a subsidy, so of course subsidies predict it. The
# order rests on the document and on the mechanism - allocated money,
# procurement not started - and the measurement is only consistent
# with it.
FAMILY = {
    "subsidy_signed": "subsidy",
    "director_joined": "registry",
    "director_departed": "registry",
    "owner_joined": "registry",
    "owner_departed": "registry",
    "tender_open": "tender",
    "management_vacancy": "growth",
}

REASON_ORDER = ("A", "B", "C", "D", "E")

REASON_LABEL = {
    "A": "dotace bez zahájené zakázky",
    "B": "změna ve vedení nebo vlastnictví",
    "C": "otevřená zakázka",
    "D": "inzerát na řídící/plánovací roli",
    "E": "jiná událost",
}

# Which family defines each class, for reading the age of the event that
# actually put the company where it is.
CLASS_FAMILY = {"A": "subsidy", "B": "registry", "C": "tender", "D": "growth"}


def families(events):
    """The kinds of thing that happened, collapsed to their signal group."""
    return {FAMILY.get(event["kind"], "other") for event in events}


def reason_class(events, tenders_for_company=None):
    """Which class of reason this company has, A being the strongest.

    `tenders_for_company` decides one thing only: whether a subsidy is
    still unspent. The matrix in section 6 turns on exactly that - money
    granted and no procurement is the case to call about, money granted
    with the procurement already awarded is the case that is gone.
    """
    present = families(events)
    has_tender = any(row.get("relevant") for row in tenders_for_company or ())

    if "subsidy" in present and not has_tender:
        return "A"
    if "registry" in present:
        return "B"
    if "tender" in present:
        return "C"
    if "growth" in present:
        return "D"
    return "E"


def class_age_days(events, reason):
    """Age of the event that put this company in its class.

    Kept as an ordering step, but NOT as a claim about probability. The
    gap between a registry event and a purchase has a median of 293 days,
    so an event three days old and one twenty-five days old are equally
    far from a signature and sorting them by likelihood would be reading
    noise. What freshness is actually good for is the phone call: "vsiml
    jsem si, ze jste v pondeli jmenovali noveho jednatele" is an opening,
    and last Monday opens better than five weeks ago.
    """
    family = CLASS_FAMILY.get(reason)
    ages = [event.get("age_days", 0) for event in events
            if FAMILY.get(event["kind"], "other") == family]
    if not ages:
        ages = [event.get("age_days", 0) for event in events]
    return min(ages) if ages else 10 ** 6


def reason_of(events, tenders_for_company=None):
    """The whole ordering-relevant view of why this company is on the list.

    `corroborated` is the one thing measurement supported outright: two
    or more different kinds of event at once was the only feature with a
    real lift on the buyer label (1.85, 24 % against 13 %).

    Counted by KIND rather than by signal family, which is how the lift
    was measured, and looking at what actually fires makes the reason
    clear: 11 of 31 gated companies qualify, and most of them because a
    departure is paired with an arrival. That is one succession rather
    than two independent signs - but a seat vacated AND refilled is a
    new person in the chair, which is the ICP's trigger 7 on top of its
    trigger 6, and it is a stronger fact than a bare departure. Counting
    by family instead gives 0 of 31, i.e. a level that never fires and
    a feature whose measured lift is thrown away.
    """
    present = families(events)
    reason = reason_class(events, tenders_for_company)
    kinds = {event["kind"] for event in events}
    return {
        "class": reason,
        "label": REASON_LABEL[reason],
        "families": sorted(present),
        "kinds": sorted(kinds),
        "corroborated": len(kinds) >= 2,
        "age_days": class_age_days(events, reason),
    }


def geography(company, icp):
    """How far the company is from the brief's origin, and whether that is near.

    Three states, not two, for the reason the whole project keeps
    repeating: `preferred` is None when the brief names no radius or
    when RUIAN never placed the company's address. An unplaced company
    is not a distant one. The ordering below does treat both as "not
    near" - we cannot promise a shop-floor visit to a company we cannot
    locate - but the card says which of the two it is rather than
    printing a distance nobody measured.
    """
    location = (icp or {}).get("location") or {}
    limit = location.get("km")
    # The same measurement filters/brief.py admitted the company on:
    # distance to where it works, which is its registered establishments
    # when it has any and its seat when it has none. Computing it a
    # second way here is how a card came to promise 87 km to a company
    # the radius should never have admitted at 234.
    distance, where = brief_filter.nearest_workplace(location.get("origin"), company)
    far, far_where = brief_filter.nearest_site(location.get("origin"), company)

    # The radius admits on the nearest address (filters/brief.py explains
    # why generously), so the card has to carry the other end of the
    # range or it promises a short drive the company cannot honour.
    # Reported only when that far site is meaningfully further AND
    # outside the radius - a second plant 20 km past the first is not
    # news, one 147 km past it is.
    tell_far = (far is not None and distance is not None and limit
                and far > limit and far - distance > 20)
    return {
        "distance_km": distance,
        "measured_to": where,
        "far_site_km": far if tell_far else None,
        "far_site": far_where if tell_far else None,
        "from": location.get("from") or (location.get("origin") or {}).get("name"),
        "limit_km": limit,
        "preferred": None if (distance is None or not limit) else distance <= limit,
    }


def evaluate(ico, archive, websites, contacts, vacancies_by_ico, events,
             company=None, icp=None, tenders=None):
    company = company or {"ico": ico}
    site = websites.get(ico, {})
    web_r = website_richness(archive, ico, websites)
    contact_r = contact_richness(contacts.get(ico))
    vac_r = vacancy_richness(vacancies_by_ico.get(ico, []))
    verified_r = verified_richness(archive, ico)
    fit = fit_assessment(archive, company, websites)
    score = pain_score(web_r, contact_r, vac_r, len(events), verified_r)
    # The negative filters' second verdict. `exclude` never reaches here
    # - eligible() dropped it - so everything found at this point is the
    # kind that lowers a company without erasing it, and it is carried
    # whole (field and value included) because the card has to be able
    # to say what it was, not just that there was one.
    findings = negative.check(company)
    return {
        "ico": ico,
        "site_status": site.get("status"),
        "site_domain": site.get("domain"),
        "now_events": events,
        "fit": fit,
        "reason": reason_of(events, (tenders or {}).get(ico)),
        "geography": geography(company, icp),
        "negative": findings,
        "demoted": negative.demotes(findings),
        # The register never recorded this company's headcount. Kept as
        # its own flag rather than folded into `demoted`, because it is
        # not a finding about the company - it is the absence of one,
        # and the card has to be able to say which of the two it is.
        "size_unknown": str(company.get("employee_code") or "000") == "000",
        # Not part of the ordering - a gate on being handed over at all.
        # See undeliverable().
        "undeliverable": undeliverable(site, contact_r),
        "pain": {"website": web_r, "contact": contact_r, "vacancy": vac_r,
                 "verified": verified_r},
        "pain_score": score,
    }


def group_key(row):
    """(owner, date) when this company's reason is a group-level event.

    Only an ownership change where the owner is a COMPANY counts. ARES
    states that in `is_legal_entity` and signals/now.py carries it onto
    the event, so this is a register fact rather than a guess at whether
    a name looks like a firm.
    """
    for event in row["now_events"]:
        if event["kind"].startswith("owner_") and event.get("legal_entity"):
            return ((event.get("name") or "").strip().lower(), event["date"])
    return None


def collapse_groups(rows):
    """One holding reorganisation is one phone call, not three cards.

    Found in a live ranking: RUML Industry, RUML Service and RUML
    Těsnění all had "RUML Holding s.r.o. became the owner" dated
    2026-08-25, and three of the week's ten were the same event at three
    subsidiaries. A salesperson makes one call there, and probably to
    the holding rather than to each plant.

    The best-ranked member is kept and carries the others on its card;
    the rest are marked and stay in the ranking, so nothing disappears
    silently and a human can still see the whole group. Rows are assumed
    to arrive in order, so "best" is simply the first one seen.

    Deliberately narrow: only companies whose REASON is the same group
    event are collapsed. Two subsidiaries that each hired a planner in
    the same week are two facts about two companies, and merging them on
    a shared owner would be inventing a connection the events do not
    have.
    """
    leaders, kept = {}, []
    for row in rows:
        key = group_key(row)
        if key and key in leaders:
            leader = leaders[key]
            leader.setdefault("group_siblings", []).append(
                {"ico": row["ico"], "name": row.get("name")})
            row["suppressed_by"] = leader["ico"]
            continue
        if key:
            leaders[key] = row
        kept.append(row)
    return kept


def undeliverable(site, contact):
    """Why this company must not be handed over, if it must not. Not a score.

    Two conditions, both about whether a dossier can exist at all rather
    than about how good the company is:

    AN UNPROVEN DOMAIN IS SOMEBODY ELSE'S COMPANY 46 % OF THE TIME -
    website.py measured that on its own guesses, and a `probable` status
    means exactly that: the domain matched the name and nothing else.
    ROMKA s.r.o. reached a week's top five on romka.eu, a site belonging
    to an unrelated person, and it brought a contact with it. Reading
    such a site for research is one thing; putting a stranger's phone
    number on a card as this company's is not, and the fastest way to
    make that impossible is to refuse to deliver the company at all.

    NO CHANNEL, NO DOSSIER. A card with a name from the register and no
    e-mail or phone anywhere is a list entry, which is the one thing the
    brief says the output must not be.

    Neither is a permanent verdict. The company stays in the base and in
    the ranking; next week its domain may be proven or the contact sweep
    may reach it.
    """
    problems = []
    if (site or {}).get("status") != "proven":
        problems.append("neprokázaný web")
    if not contact.get("any_channel"):
        problems.append("žádný kontakt")
    # `any_channel` counts the switchboard and info@ on purpose, and it is
    # the right level. It was briefly tightened to "a register-confirmed
    # person with a channel of their own" after TREJ - servis and INCO
    # engineering reached cards whose contact block read "PAVEL TREJBAL,
    # jednatel" and then nothing. But the fault was in the card, not in
    # the gate: both companies publish an info@ address and four phone
    # numbers, and nothing was printing them. card.py now carries those
    # channels and run.py's catalogue_contact() falls back to them, so
    # the condition is once again "can this company be reached", which is
    # what the brief asks - not "does its owner publish a private
    # address", which three of one week's five failed for no good reason.
    return problems


def spread_classes(rows, top):
    """One company per class of reason before a second of any class.

    THE ORDERING IS STRICT BY CLASS, AND THAT MADE FOUR OF THE FIVE
    CLASSES UNREACHABLE. reason_class() grades A to E straight out of the
    ICP, and ordering() sorts on that grade, so a week with six
    leadership changes hands over five leadership changes - a company
    whose reason is a planner vacancy sits behind every one of them and
    never arrives, however well it fits otherwise. Measured on the run
    that prompted this: every deliverable company in the ranking was
    class B until the vacancy window was widened, and the two class D
    companies that then appeared would have been pushed out again the
    first week the register produced five.

    Two reasons to reserve a slot rather than let the grade decide alone,
    and the second is the real one:

    * RTsoft asked for exactly this. "Je potřeba ty leady vidět a pak v
      nich hledat vodítka" (24.1) - you cannot look for clues in a class
      of lead that never leaves the building.

    * IT IS THE ONLY WAY THIS PROJECT WILL EVER GET GROUND TRUTH. 25.5
      established that no weighting can be calibrated because there are
      no labels, and a week of five identical reasons is one experiment
      run five times. Five different classes is five experiments, and
      after a few weeks the salesperson knows which class answers the
      phone. Nothing else in the pipeline can produce that information.

    The cost is stated rather than hidden: a strong class A company can
    lose its place to a weaker class E one. It is capped at one slot per
    class - the reserved pass takes the BEST row of each class by the
    same ordering(), and every remaining slot is filled in plain
    ordering() sequence, so with two classes present the other three
    slots still go to whoever earned them.

    Rows arrive sorted; the five come back sorted the same way, because
    the quota decides who is in the week and not who is first in it.
    """
    if len(rows) <= top:
        return rows

    # Who would have made it on the grade alone - the difference is what
    # the card has to be able to declare.
    baseline = {row["ico"] for row in rows[:top]}

    picked, seen = [], set()
    for row in rows:
        if len(picked) >= top:
            break
        if row["reason"]["class"] in seen:
            continue
        seen.add(row["reason"]["class"])
        picked.append(row)

    chosen = {row["ico"] for row in picked}
    for row in rows:
        if len(picked) >= top:
            break
        if row["ico"] not in chosen:
            picked.append(row)
            chosen.add(row["ico"])

    for row in picked:
        # Not a ranking field - a statement the card makes out loud. A
        # company that is in the week only because its class would
        # otherwise be missing has to say so, or the five look like five
        # verdicts of equal strength.
        row["class_slot"] = row["ico"] not in baseline

    picked.sort(key=ordering)
    return picked


def already_shown(archive, ico, events):
    """Was this company handed over already, with nothing new since?

    Requirement 8 of the brief: a repeat run has to know what it gave
    out last time. The table has been filled since 29.08 and never read,
    and it shows: 20 companies account for 100 deliveries, an average of
    five appearances each. Nobody chose that - the NOW window simply
    keeps matching the same event week after week, and the subsidy
    window is 120 days wide, so a company with one grant can hold a
    place for four months on a reason nobody has acted on.

    Not a permanent ban, which would be the wrong reading of the log
    (22.1): a company leaves the pool when the salesperson writes to it,
    not when it appears on a card. The condition is narrower - it is the
    REASON that goes stale, not the company. A new event dated after the
    last delivery is a new reason to call, and the company comes back
    with it.
    """
    rows = archive.delivered(ico=ico)
    if not rows:
        return False
    last = max(row["delivered_at"] for row in rows)[:10]
    for event in events:
        # Tenders carry the bid deadline as their date, which is in the
        # future; they are current for as long as bids are open, so they
        # never go stale this way.
        if event.get("kind") == "tender_open":
            return False
        if (event.get("date") or "") > last:
            return False
    return True


def ordering(row):
    """The sort key. Seven steps, each traceable to the brief or to a measurement.

    1. FIT group - the NACE tier, and nothing else. Put anything else
       first and a construction firm with a subsidy outranks a
       manufacturer from the core of the ICP; that exact case reached a
       card once already (KVAZAR). The production mode is deliberately
       NOT here - see TIER_RANK's note.
    2. A deprioritising negative finding. "Every establishment closed"
       is about whether the company can buy at all, so it outweighs
       every reason below it. This is where filters/negative.py's third
       verdict finally does something: it defined a PENALTY in points
       that nothing ever subtracted, and points were the wrong shape
       anyway - an uncalibrated 8 either swamps the score or drowns in
       it. One step down the order says exactly what was meant.
    3. No recorded headcount. Measured, and the measurement is the whole
       argument: the 18 such companies in the pool fire a registry event
       at 20 % a week against 0.06 % for the rest - 330 times the rate -
       and not one of them has a proven domain or a single channel. RES
       leaves KATPO empty for a company that files nothing, which a real
       50-200 person manufacturer cannot be; what is left are shells,
       and shells change directors constantly, so they land in the
       cleanest signal we have. They are NOT dropped: absence of a
       headcount is not a headcount of zero, and hypothesis E says so.
       They simply never take a place in the five from a company whose
       size is known.
    4. Class of the reason, A to E - see reason_class() for where each
       line comes from in the document.
    5. Corroboration: two or more different kinds of event at once. The
       only feature that showed a real lift against the buyer label
       (1.85, 24 % against 13 %).
    6. Inside the preferred radius. Geography is a row in the ICP's own
       "kdo to je" table and RTsoft drives to the shop floor.
    7. Freshness within the class - as an opening line for the call, not
       as a probability. See class_age_days().

    Then, and only then, the count of verified facts, purely to break a
    tie between two companies that are equal on all seven. PAIN no longer
    chooses anybody; it fills the card.
    """
    return (
        row["fit"]["rank"],
        1 if row["demoted"] else 0,
        1 if row["size_unknown"] else 0,
        REASON_ORDER.index(row["reason"]["class"]),
        0 if row["reason"]["corroborated"] else 1,
        0 if row["geography"]["preferred"] else 1,
        row["reason"]["age_days"],
        -row["pain"]["verified"]["facts"],
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(window_days=DEFAULT_WINDOW, top=DEFAULT_TOP, archive=None, qualified=None,
        icp=None, establishments=None):
    """Full weekly selection: FIT -> NOW gate -> PAIN rank.

    Returns the ranked list of NOW-qualified companies, longest first;
    the caller decides how many of them become the week's five - keeping
    that a caller decision, not baked in here, is what lets the CLI print
    "here are all 23 that qualified, and the top 5" in one pass.

    `icp` is needed even when the gate result is handed in: the brief
    carries the origin every distance is measured from, and the ordering
    below reads it.
    """
    archive = archive or Archive()
    if icp is None:
        # Lazy, and only on the standalone path: run.py always passes the
        # brief it started with, and importing it at module level would
        # tie this module to the runner it is called from.
        from pipeline.run import load_icp
        icp = load_icp()

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
        pool, funnel = eligible(companies, icp)
        print(f"brief and negative filters: {funnel['total']} -> {funnel['pool']}",
              file=sys.stderr)
        qualified = now_qualified(pool, history, window_days)
    by_ico = {c["ico"]: c for c in companies}

    # The establishments run.py read for the gated few. The candidate
    # file predates them and is rebuilt only on a full ARES pass, so
    # without this the distances here would be measured to the seat
    # while the gate measured to the nearest workplace - the two stages
    # disagreeing about geography is precisely the bug this fixes.
    for ico, sites in (establishments or {}).items():
        if ico in by_ico:
            by_ico[ico]["establishments"] = sites

    # Loaded once for the whole ranking: reason_class() needs to know
    # whether a company's subsidy already has a procurement against it,
    # and that answer lives in the tender file rather than in the events.
    tenders = load_tenders()
    ranked = [
        evaluate(ico, archive, websites, contacts, vacancies_by_ico, events,
                 company=by_ico.get(ico), icp=icp, tenders=tenders)
        for ico, events in qualified.items()
    ]
    # FIT orders the groups, PAIN orders inside them. Sorting purely on
    # pain_score is what let a construction firm with a rich site sit
    # level with manufacturers: the score measures how much we can
    # prove, and proof of the wrong trade counted the same as proof of
    # the right one. See ordering() for the full sequence.
    ranked.sort(key=ordering)

    for row in ranked:
        row["name"] = by_ico[row["ico"]].get("name")

    # Ranked keeps everyone, so a run can still be inspected; only the
    # deliverable slice is filtered. See undeliverable() - neither
    # condition removes a company from the base.
    deliverable = [row for row in ranked if not row["undeliverable"]]
    held = Counter(problem for row in ranked for problem in row["undeliverable"])
    if held:
        print(f"{len(ranked) - len(deliverable)} of {len(ranked)} qualified companies "
              f"held back: " + ", ".join(f"{count}× {reason}"
                                         for reason, count in held.most_common()),
              file=sys.stderr)

    # After the delivery gate and before folding groups: a company held
    # back for having no channel was never shown, so asking whether its
    # reason is stale would answer a question nobody asked.
    before = len(deliverable)
    deliverable = [row for row in deliverable
                   if not already_shown(archive, row["ico"], row["now_events"])]
    if len(deliverable) < before:
        print(f"{before - len(deliverable)} already delivered on the same reason",
              file=sys.stderr)

    before = len(deliverable)
    deliverable = collapse_groups(deliverable)
    if len(deliverable) < before:
        print(f"{before - len(deliverable)} folded into a company of the same group "
              f"with the same event", file=sys.stderr)

    # Last, and only here: every filter above decides who MAY be handed
    # over, this decides which of them the week is spent on. Running it
    # earlier would let a class quota rescue a company that the delivery
    # gate was about to refuse.
    week = spread_classes(deliverable, top)
    reserved = [row for row in week if row.get("class_slot")]
    if reserved:
        print(f"{len(reserved)} in the five on a reserved class slot: "
              + ", ".join(f"{row['reason']['class']} {(row.get('name') or row['ico'])[:24]}"
                          for row in reserved), file=sys.stderr)

    return week, ranked


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Weekly FIT->NOW->PAIN selection.")
    parser.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                        help="NOW gate window in days (default: 7, matches weekly runs)")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP)
    args = parser.parse_args()

    top5, all_qualified = run(args.window, args.top)

    print(f"NOW-qualified this window: {len(all_qualified)}", file=sys.stderr)
    print(f"top {len(top5)}, ordered by reason class (A best), then corroboration,"
          f" radius, freshness:\n", file=sys.stderr)
    for rank, row in enumerate(top5, 1):
        geo = row["geography"]
        # Everything the ordering actually looked at, in the order it
        # looked at it - a ranking nobody can read is a ranking nobody
        # can argue with.
        where = "?" if geo["distance_km"] is None else f"{geo['distance_km']:.0f} km"
        reason = row["reason"]
        print(f"{rank}. {(row['name'] or '')[:34]:36} "
              f"{row['fit']['nace_tier']:8}{row['fit']['mode']:20}"
              f"{reason['class']}{'+' if reason['corroborated'] else ' '} "
              f"{reason['age_days']:>4}d {where:>7}"
              f"{'  ↓' if row['demoted'] else '  ?' if row['size_unknown'] else '   '}  "
              f"{row['pain']['verified']['facts']:2} fact(s)"
              # Marked, not silent: this row is in the week because its
              # class would otherwise be missing, not because it outranked
              # the company it displaced. See spread_classes().
              f"{'  [class slot]' if row.get('class_slot') else ''}",
              file=sys.stderr)

    print(json.dumps({"top": top5, "qualified": all_qualified}, ensure_ascii=False, indent=2))
