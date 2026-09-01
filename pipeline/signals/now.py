"""NOW: a dated reason to call this week, not just a good-fit company.

Hypothesis A of the brief says fit and timing are two different
questions. This module answers only the second one - it never scores
whether a company matches the ICP, only whether something dated and
verifiable happened to it recently.

Two groups produce events here, out of the three the log collapsed the
nine triggers into (reseni-log.md, section 5):

    A  registry     a director or owner arrived or departed - ARES -vr,
                     dated by the register itself. Almost impossible to
                     fabricate.
    C  growth       a management/planning vacancy was posted - MPSV,
                     dated by the posting.

Group B ("the system creaks") has no dated source at all - it is a
quality of a company, not an event with a timestamp - so it produces no
NOW claims here. It belongs to PAIN, once that scorer exists.

A departure is scored the same way as an arrival, deliberately. The
log's own recognition question - "kdo u vás ví, co se má dělat zítra, a
co se stane, když onemocní" - is a departure happening, not an arrival.
Treating "somebody just left" as weaker than "somebody just joined"
would miss the trigger the ICP calls out by name.

What this module does NOT do: decide who goes into the final five. It
produces claims, each with a snapshot to point at; scoring and selection
are later stages, per the pipeline order in section 5 of CLAUDE.md.

Run:
    python -m pipeline.signals.now 00543551
    python -m pipeline.signals.now --all --window 30
"""

import argparse
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ARES_CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")
MPSV_HISTORY = Path("data/raw/mpsv_history.jsonl")
ARES_BASE = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"

# The archive's earliest day (docs/reseni-log.md 16.2, 18.7) - a vacancy
# dated before this cannot be trusted, and a company silent since before
# this cannot be told "never posted" from "we never watched them post".
HISTORY_START = date(2024, 10, 31)

# ISCO groups whose FIRST appearance for a company is worth reading as
# an event at all - production/logistics planning and management roles.
# Picked in the 18.4/18.7 research: 4322/4323 are the "in-between
# person" clerks, the rest are the management layer the strongest
# trigger describes taking shape.
MANAGEMENT_ISCO = {"1219", "1321", "1322", "2141", "3122", "3123", "4322", "4323"}


def parse_date(value, earliest=None):
    """Parse an ISO date, rejecting the future and anything before `earliest`.

    Registry dates (ARES) go back decades - VAPE spol. s r.o. has board
    changes from 1993 - and are valid however old they are; only the
    MPSV archive has a real earliest day, because nothing before it was
    ever recorded (see docs/reseni-log.md 18.7). Applying that MPSV
    boundary to registry dates was a real bug caught while testing this
    module: it silently discarded a genuine departure dated 2024-08-12,
    twelve weeks before the archive's own start, as if it never
    happened, instead of just falling outside whatever window the
    caller asked for.
    """
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None
    if parsed > date.today():
        return None
    if earliest and parsed < earliest:
        return None
    return parsed


# ---------------------------------------------------------------------------
# Group A: registry events
# ---------------------------------------------------------------------------


def registry_events(company, window_days, today=None):
    """Director/owner arrivals and departures within the window.

    Two filters exist only to keep a young company's founding board from
    looking like a leadership change, and both are measured, not guessed
    (reseni-log.md 13.5):

    * a board dated the same day the company itself was founded is not a
      change - it is the company being new. Guarded by comparing against
      `established`.
    * an entire board replaced on one shared date is a mass event -
      re-registration, a template default, or similar - not a personal
      one; distinct people leaving or joining on distinct dates is what
      the trigger actually describes.
    """
    today = today or date.today()
    established = parse_date(company.get("established"))
    events = []

    groups = (
        ("directors", "director_joined", "since"),
        ("owners", "owner_joined", "since"),
        ("departed_directors", "director_departed", "until"),
        ("departed_owners", "owner_departed", "until"),
    )
    for field, kind, date_key in groups:
        people = company.get(field) or []
        dated = [(p, parse_date(p.get(date_key))) for p in people]
        dated = [(p, d) for p, d in dated if d]
        if not dated:
            continue

        distinct_dates = {d for _, d in dated}
        # A freshly founded company has an all-new board because it is
        # new, not because anyone was replaced.
        if established and len(distinct_dates) == 1:
            only = distinct_dates.pop()
            if (only - established).days < 400:
                continue
            distinct_dates = {only}
        # Every person moving on the exact same day, across two or more
        # people, is a mass event rather than an individual one - unless
        # there is only one person, which is the normal case.
        if len(dated) > 1 and len(distinct_dates) == 1:
            continue

        for person, event_date in dated:
            if (today - event_date).days > window_days:
                continue
            events.append({
                "kind": kind,
                "date": event_date.isoformat(),
                "name": person.get("name"),
                "role": person.get("role"),
                "age_days": (today - event_date).days,
            })
    return events


# ---------------------------------------------------------------------------
# Group C: growth via management vacancies
# ---------------------------------------------------------------------------


def load_history(path=MPSV_HISTORY):
    """ICO -> [(date, isco4, title)], the dated vacancy record.

    Loaded whole rather than filtered on read: it is 25 740 rows, small
    enough that re-reading it per company would be the slower design for
    no benefit.
    """
    index = {}
    if not Path(path).exists():
        return index
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            event_date = parse_date(row.get("date"), earliest=HISTORY_START)
            isco = (row.get("isco") or "")[:4]
            if not event_date or not isco:
                continue
            index.setdefault(row["ico"], []).append((event_date, isco, row.get("title")))
    return index


def vacancy_events(ico, history, window_days, today=None):
    """Management-role postings within the window, from the local index."""
    today = today or date.today()
    events = []
    for event_date, isco, title in history.get(ico, []):
        if isco not in MANAGEMENT_ISCO:
            continue
        age = (today - event_date).days
        if age > window_days:
            continue
        events.append({
            "kind": "management_vacancy",
            "date": event_date.isoformat(),
            "isco": isco,
            "title": title,
            "age_days": age,
        })
    return events


def role_history_depth(ico, history, today=None):
    """Months the company has actually been observable on MPSV.

    Exists because "no management vacancy in two years" is not the same
    claim for a company posted every month and one posted twice ever.
    Measured in 18.7: 85 % of companies are visible under six months, and
    for those, silence proves nothing - there was no real chance to see
    the event even if it happened. Absence is only evidence when there
    was an opportunity for presence.
    """
    today = today or date.today()
    months = {(d.year, d.month) for d, _, _ in history.get(ico, [])}
    return len(months)


# ---------------------------------------------------------------------------
# Combining
# ---------------------------------------------------------------------------


# A record superseded and immediately re-entered is a bookkeeping
# amendment, not a change of person. The register does this whenever a
# detail is corrected - an address, a spelling, a share - by closing the
# old row with datumVymazu and opening a new one the same day.
REENTRY_DAYS = 3


def drop_reentries(events):
    """Remove arrival/departure pairs that are one person re-registered.

    Found on live data: FORCE TRADE s.r.o. showed Petra Doležalová both
    joining and leaving the board on 2026-07-27, and again as owner on
    the same day. Read literally that is two leadership events in one
    day; read correctly it is one amendment to an existing record.

    Measured across the base: 41 of 152 events in a 30-day window - 27 %
    - were these pairs. Left in, better than a quarter of every "why
    now" reason handed to a salesperson would be a clerical correction.
    """
    departures = {}
    for event in events:
        if event["kind"].endswith("_departed") and event.get("name"):
            departures.setdefault(event["name"], []).append(date.fromisoformat(event["date"]))

    paired = set()
    for event in events:
        if not event["kind"].endswith("_joined") or not event.get("name"):
            continue
        joined = date.fromisoformat(event["date"])
        for left in departures.get(event["name"], []):
            if abs((joined - left).days) <= REENTRY_DAYS:
                paired.add((event["name"], event["date"]))
                paired.add((event["name"], left.isoformat()))

    return [e for e in events if (e.get("name"), e["date"]) not in paired]


# EU subsidies are published monthly and lag by weeks, so a 7-day
# window - the cadence of the weekly run - matches nothing from this
# source, ever. Measured on the 2026-08 file: newest signing date was
# 2026-07-23, a month old on arrival, and the yield only becomes
# non-zero at ~60 days. The subsidy window is therefore deliberately
# decoupled from the registry/vacancy one rather than sharing it.
SUBSIDY_WINDOW = 120

TENDERS = Path("data/raw/nen.jsonl")

# NEN's status vocabulary, split by what it means for a salesperson.
# This is the distinction the subsidy file could never make: a grant
# tells you money exists, a tender's status tells you whether the money
# has already been spent on somebody else.
TENDER_OPEN = {"Neukončen", "Plánován"}
TENDER_LOST = {"Zadán", "Ukončení plnění"}


def load_tenders(path=TENDERS):
    """ICO -> published procurements, from sources/nen.py's output."""
    out = {}
    if not Path(path).exists():
        return out
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                row = json.loads(line)
                out.setdefault(row["ico"], []).append(row)
    return out


def tender_events(ico, tenders, today=None):
    """Open procurements whose subject is something RTsoft sells.

    NO WINDOW, AND THAT IS DELIBERATE. Every other signal here answers
    "did this happen recently"; an open tender answers "is this
    happening now", which is a stronger question and needs no cutoff -
    a procurement accepting bids is current by definition, however long
    ago it was posted. So age_days is 0: it is not a recent event, it is
    a present state.

    The listing carries no publication date, only the deadline for
    bids - which turns out to be the more useful of the two anyway,
    because it says how long there is left to act rather than how long
    ago something was announced.

    Awarded tenders are not returned. They are the opposite of a reason
    to call, and scoring/card.py surfaces them separately so the
    salesperson sees "they already bought" rather than nothing at all.
    """
    today = today or date.today()
    events = []
    for row in tenders.get(str(ico).zfill(8), []):
        if not row.get("relevant") or row.get("status") not in TENDER_OPEN:
            continue
        deadline = parse_deadline(row.get("deadline"))
        events.append({
            "kind": "tender_open",
            "date": deadline.isoformat() if deadline else None,
            "age_days": 0,
            "days_left": (deadline - today).days if deadline else None,
            "subject": row.get("name"),
            "status": row.get("status"),
            "number": row.get("number"),
            "url": row.get("url"),
        })
    return events


def parse_deadline(value):
    """NEN prints '20. 08. 2025 10:00'. Returns a date, or None."""
    match = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", value or "")
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def find(company, history, window_days=30, today=None, subsidies=None,
         subsidy_window=SUBSIDY_WINDOW, tenders=None):
    """Every dated NOW event for one company, tagged with confidence.

    `confidence` is not a score - it is a visible flag for the one
    failure mode that matters here: a vacancy event on a company barely
    seen on MPSV is not stronger evidence than one on a company posted
    for a year, and collapsing that distinction would let a thin sample
    look as certain as a thick one.
    """
    events = drop_reentries(registry_events(company, window_days, today))

    depth = role_history_depth(company["ico"], history, today)
    for event in vacancy_events(company["ico"], history, window_days, today):
        event["confidence"] = "high" if depth >= 12 else "low" if depth >= 6 else None
        if event["confidence"]:
            events.append(event)

    # Subsidies use their own, much wider window - see SUBSIDY_WINDOW.
    # Passing `subsidies` is optional so that callers which have not
    # loaded the file still get registry and vacancy events rather than
    # an import error.
    if subsidies:
        from pipeline.sources.dotace_eu import funding_events
        # Only subsidies aimed at how the company produces count as a
        # reason to call. A grant for solar panels or a trade fair is
        # true, dated and irrelevant; and a grant to implement an ERP
        # means the opposite of a lead. Both are dropped here rather
        # than left for scoring to misread.
        events += [e for e in funding_events(company["ico"], subsidies, subsidy_window, today)
                   if e["is_signal"]]

    # Tenders answer the same question as subsidies from the other end,
    # and much sooner: a grant reaches us 21-51 days after signing
    # because MMR publishes monthly, while a procurement is on NEN the
    # day it opens. Measured on the 60 companies matching RTsoft's own
    # subsidy parameters: 8 had published a tender, 4 with a relevant
    # subject, and the status told a live purchase apart from a lost one
    # in every case.
    if tenders:
        events += tender_events(company["ico"], tenders, today)

    events.sort(key=lambda e: e["age_days"])
    return events


# Where each kind of event was read from, so a claim can point at the
# snapshot that backs it. Registry events come from the commercial
# register endpoint; vacancy events from that company's MPSV set.
EVENT_SOURCE = {
    "director_joined":    ("ares", "ekonomicke-subjekty-vr"),
    "director_departed":  ("ares", "ekonomicke-subjekty-vr"),
    "owner_joined":       ("ares", "ekonomicke-subjekty-vr"),
    "owner_departed":     ("ares", "ekonomicke-subjekty-vr"),
    "management_vacancy": ("mpsv", None),
    "subsidy_signed":     ("dotace_eu", None),
}


def describe(event):
    """One human sentence per event - what the salesperson actually reads.

    Czech, unlike the rest of this codebase, because this string is not
    a label for a developer: it is stored verbatim as claim.value and
    printed straight onto the card a Czech salesperson reads. The
    project's rule is English identifiers and comments; user-facing text
    follows the user. Found when the finished card rendered "signed an
    EU subsidy" in the middle of an otherwise Czech dossier.
    """
    if event["kind"] == "management_vacancy":
        return f"inzerát na řídící/plánovací roli: {event.get('title') or event['isco']}"
    if event["kind"] == "subsidy_signed":
        try:
            millions = f"{float(event.get('total_czk') or 0) / 1e6:.1f} mil. Kč"
        except (TypeError, ValueError):
            millions = "částka neuvedena"
        return f"podepsaná dotace EU ({millions}): {event.get('project', '')[:90]}"
    who = event.get("name") or "neuvedeno"
    role = f" ({event['role']})" if event.get("role") else ""
    verb = {
        "director_joined": "nastoupil do statutárního orgánu",
        "director_departed": "opustil statutární orgán",
        "owner_joined": "stal se vlastníkem",
        "owner_departed": "přestal být vlastníkem",
    }[event["kind"]]
    return f"{who}{role} — {verb}"


def snapshot_for(archive, ico, kind):
    """The archived document a given event was read from, or None."""
    source, endpoint = EVENT_SOURCE.get(kind, (None, None))
    if not source:
        return None
    if endpoint:
        row = archive.latest(ico, source, url=f"{ARES_BASE}/{endpoint}/{ico}")
    else:
        row = archive.latest(ico, source)
    return row["id"] if row else None


# How much of the archived document to quote around the anchor. The
# registry stores a person as {"datumZapisu": ..., "datumVymazu": ...,
# "typAngazma": ..., "clenstvi": {... "jmeno": X, "prijmeni": Y}}, so
# the dates sit BEFORE the name - the window has to reach back far
# enough to carry them, or the quote proves the person exists without
# proving when anything happened to them.
QUOTE_BACK = 320
QUOTE_FORWARD = 90


def evidence_quote(event, text):
    """A verbatim substring of `text` that backs `event`, or None.

    The first version of record() put json.dumps(event) in the quote
    column and marked the claim a fact. That string is this module's own
    construction and appears nowhere in the ARES response, whose shape is
    completely different - so re-verifying the archive found 100 of 172
    stored "facts" unprovable, every one of them a NOW event. The
    verifier had not failed; these claims had never been through it.

    The irony is that the registry is the *best*-evidenced source in the
    project - the log calls it the one signal that cannot be
    hallucinated - and it was the only one asserting rather than
    proving. What is quoted now is the raw fragment of the archived
    document itself: ugly to read, but it is what the source actually
    says, and a card can render `value` while the quote stays checkable.
    """
    if not text:
        return None

    anchors = []
    if event["kind"] == "subsidy_signed":
        anchors.append((event.get("project") or "")[:80])
    elif event["kind"] == "management_vacancy":
        anchors += [event.get("title") or "", str(event.get("isco") or "")]
    else:
        # Surname alone is the reliable anchor: ARES splits a person into
        # separate "jmeno"/"prijmeni" fields, so the full name as the
        # event carries it ("JAROSLAV JEDINÁK") is never one substring.
        parts = (event.get("name") or "").split()
        if parts:
            anchors.append(f'"prijmeni": "{parts[-1]}"')
            anchors.append(parts[-1])

    for anchor in anchors:
        if not anchor:
            continue
        position = text.find(anchor)
        if position < 0:
            continue
        start = max(0, position - QUOTE_BACK)
        end = min(len(text), position + len(anchor) + QUOTE_FORWARD)
        return text[start:end]
    return None


def record(archive, company, history, window_days=30, run_id=None, today=None,
           subsidies=None):
    """Turn one company's events into claims in the evidence store.

    Each event goes through evidence/verify.py like everything else. A
    registry date is not an interpretation - but "not an interpretation"
    is a reason to expect the quote to be found, not a reason to skip
    looking for it. When the anchor cannot be located in the archived
    document the event is still recorded, as an inference rather than a
    fact, so the signal is not lost and is not overstated either.

    An event whose snapshot is missing is skipped rather than recorded
    unsourced - the schema would reject it anyway, and silently dropping
    the foreign key would defeat the point of having one.
    """
    from pipeline.evidence.verify import check

    written, orphaned = 0, 0
    states = {"fact": 0, "inference": 0, "discard": 0}
    for event in find(company, history, window_days, today, subsidies=subsidies):
        snapshot_id = snapshot_for(archive, company["ico"], event["kind"])
        if snapshot_id is None:
            orphaned += 1
            continue
        quote = evidence_quote(event, archive.text_of(snapshot_id))
        result = check(archive, company["ico"], f"now:{event['kind']}",
                       describe(event), quote, snapshot_id, run_id=run_id)
        states[result["state"]] += 1
        written += 1
    return written, orphaned, states


def load_companies(path=ARES_CANDIDATES):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if "error" not in row:
                yield row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dated NOW events per company.")
    parser.add_argument("ico", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--window", type=int, default=30, help="days back to look")
    parser.add_argument("--record", action="store_true",
                        help="write events into the evidence store as claims")
    args = parser.parse_args()

    history = load_history()

    # Subsidies are loaded here rather than left to default to None.
    # find() takes them as an optional argument so that a caller without
    # the file still gets registry and vacancy events - convenient, and
    # exactly how the whole EU-subsidy group went missing twice: once
    # from scoring/select.py's gate, then again from --record, where the
    # events simply were never written and the card's "why now" section
    # came up empty for companies that had a perfectly good reason.
    from pipeline.sources.dotace_eu import load as load_subsidies
    subsidies = load_subsidies()

    if args.all:
        companies = {c["ico"]: c for c in load_companies()}

        if args.record:
            from pipeline.evidence.archive import Archive

            store = Archive()
            run_id = store.start_run(note=f"now events, window {args.window}d")
            written = orphaned = firms = 0
            totals = {"fact": 0, "inference": 0, "discard": 0}
            for company in companies.values():
                count, missing, states = record(store, company, history, args.window,
                                                run_id, subsidies=subsidies)
                written += count
                orphaned += missing
                firms += 1 if count else 0
                for state, n in states.items():
                    totals[state] += n
            store.finish_run(run_id)
            print(f"{written} claims for {firms} companies; "
                  f"{orphaned} events skipped for want of a snapshot", file=sys.stderr)
            print(f"verification: {totals}", file=sys.stderr)
        else:
            total = 0
            for ico, company in companies.items():
                events = find(company, history, args.window)
                if events:
                    total += 1
                    print(f"{ico}  {company.get('name','')[:40]:42} {len(events)} event(s)")
            print(f"\n{total} of {len(companies)} companies have a NOW event "
                  f"within {args.window} days", file=sys.stderr)
    elif args.ico:
        target = str(args.ico).zfill(8)
        company = next((c for c in load_companies() if c["ico"] == target), None)
        if not company:
            print(f"{target} not found in {ARES_CANDIDATES}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(find(company, history, args.window), ensure_ascii=False, indent=2))
    else:
        parser.error("give an ICO, or --all")
