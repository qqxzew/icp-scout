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

EVERY SOURCE KEEPS ITS OWN WINDOW, and they are not close to each other:

    register     2 days behind   window = the run's own, 7 days
    vacancies    10 days behind  window 24 days   (VACANCY_WINDOW)
    subsidies    35 days behind  window 120 days  (SUBSIDY_WINDOW)
    tenders      published same day, no window at all - a bid deadline
                 decides, and an age never could

(The register's two days is the freshest event we hold, so it covers
ARES's own delay and our refresh cadence together; the other three are
the source's delay alone, measured inside the file it hands us.)

Measured, on 04.09.2026, and it is the whole reason a weekly run looked
for months as though only one signal worked. It did: the register is the
only source that publishes faster than the run repeats. Asking the other
three "what happened in the last seven days" is asking them for
something they physically do not contain yet, and getting a zero back
that says nothing about whether anything happened.

A departure is scored the same way as an arrival, deliberately. The
log's own recognition question - "kdo u vás ví, co se má dělat zítra, a
co se stane, když onemocní" - is a departure happening, not an arrival.
Treating "somebody just left" as weaker than "somebody just joined"
would miss the trigger the ICP calls out by name.

What this module does NOT do: decide who goes into the final five. It
produces claims, each with a snapshot to point at; scoring and selection
are later stages, per the pipeline order in section 4 of
ARCHITECTURE.md.

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
# The daily export as it stands today - see load_history() for why the
# archive of increments is not enough on its own.
MPSV_CURRENT = Path("data/raw/mpsv_vacancies.jsonl")
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
                # Whether the owner is a company rather than a person.
                # ARES states it and the event used to drop it, which is
                # what let one holding reorganisation look like three
                # unrelated leads - see scoring/select.py::group_key.
                "legal_entity": bool(person.get("is_legal_entity")),
                "age_days": (today - event_date).days,
            })
    return events


# ---------------------------------------------------------------------------
# Group C: growth via management vacancies
# ---------------------------------------------------------------------------


def isco4(value):
    """The four-digit ISCO group, whichever way MPSV wrote it.

    The daily export says "CzIsco/93291" and the increment archive says
    "93291". Same code, two spellings, and comparing the prefixed form
    against MANAGEMENT_ISCO silently matched nothing at all - which is
    not an error, just a signal that quietly produces zero.
    """
    return str(value or "").rsplit("/", 1)[-1][:4]


# THE WINDOW IS A PROPERTY OF THE SOURCE, NOT OF THE RUN, and this signal
# spent weeks producing zero because it was given the run's window.
#
# MPSV publishes its export ten days behind the postings in it. Measured
# on the file itself: postings aged 0-8 days number one in 38142, and
# then 9d has 316, 10d has 327, 11d has 591. So a 7-day window - the
# cadence of the weekly run - matches nothing on this source, ever, for
# exactly the reason dotace_eu.py records for the monthly subsidy file.
# Two separate measurements said "the growth signal does not fire" when
# what they had measured was that it could not.
#
# The width is derived, not chosen:
#
#   lag                     the export's own delay, measured below
#   + VACANCY_FRESH_DAYS    how long after we could first have learned of
#                           a posting it still counts as a reason to call
#
# The floor under VACANCY_FRESH_DAYS is the run cadence, and that is
# arithmetic rather than taste: a run on day X can only see postings
# dated X - lag or earlier, so the next run seven days later must reach
# back lag + 7 days or a posting falls between the two runs and neither
# ever sees it. It is set to twice the cadence so that one missed run - a
# holiday, a failed fetch - does not silently drop a week of postings.
#
# NOT WIDENED FURTHER, and the temptation was measured: 30 days yields 10
# companies against 6 at 24. run.py's own docstring settles it - "fewer
# than five is a result", no widening to fill the quota - and 26.7 in the
# log is the record of doing it anyway with the subsidy window and having
# to undo it. The number below has a derivation; 30 would only have had
# an outcome.
VACANCY_FRESH_DAYS = 14

# Measured 04.09.2026 by publication_lag() over the current export.
# Kept as a constant so find() stays cheap and pure - it is called once
# per company - while run.py re-measures on every run and says so when
# the file drifts away from this number.
VACANCY_LAG = 10
VACANCY_WINDOW = VACANCY_LAG + VACANCY_FRESH_DAYS

# The lag is read off a low percentile rather than off the newest row.
# The newest row in the file is dated three days back and is the only one
# in eight days - one advert somebody backdated, or a correction - and
# taking it literally says the source is three days fresh when the next
# 38141 rows say it is ten. A percentile cannot be fooled by one row, and
# 1 % sits on the cliff itself: 0.1 % gives 9 days, 1 % gives 10, 2 %
# gives 11, which is the same answer three times.
LAG_PERCENTILE = 0.01


def publication_lag(path=MPSV_CURRENT, today=None):
    """How many days behind the postings in it the MPSV export runs.

    Measured rather than assumed, for the reason dotace_eu.py gives about
    its own monthly file: the export is replaced daily, so how stale it
    is changes under us, and a hard-coded lag is a fact that silently
    stops being true. Returns None when there is no file to measure.
    """
    today = today or date.today()
    ages = []
    if not Path(path).exists():
        return None
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            posted = parse_date(row.get("posted"))
            if posted:
                ages.append((today - posted).days)
    if not ages:
        return None
    ages.sort()
    return ages[int(len(ages) * LAG_PERCENTILE)]


def load_history(path=MPSV_HISTORY, current=MPSV_CURRENT):
    """ICO -> [(date, isco4, title)], the dated vacancy record. Two sources.

    THE ARCHIVE OF INCREMENTS IS NOT BEING WRITTEN. mpsv_history.jsonl
    was meant to accumulate as vacancies appear and disappear, and
    run.py's preflight still says it "accumulates from repeated
    --refresh calls" - but mpsv.refresh() writes only the current
    snapshot, and nothing in the pipeline appends to the history. It has
    therefore been frozen since the day it was last built by hand. On a
    run made 01.09 its newest row was 21.08, i.e. eleven days old
    against a seven-day window: the growth signal could not fire, and
    the reason had nothing to do with whether anybody was hiring.

    So the current export is read too. Every open vacancy carries
    MPSV's own `datumVlozeni`, which is exactly the dated event this
    module is looking for, and it needs no archive to be trustworthy -
    the register states when the advert was posted.

    What the archive still covers, and why it is not simply dropped: a
    vacancy posted AND withdrawn inside the window is gone from the
    export and only the increment record would hold it. Both are read
    and merged; a posting present in both is one event, not two.

    No `earliest` bound on the export's dates, unlike the archive's. The
    HISTORY_START guard exists because an increment row is dated by when
    WE saw it, so nothing before we started watching means anything. A
    posting date from MPSV is the register's own fact about the advert,
    true whenever it was made - and role_history_depth() gets a more
    honest measure of how long a company has been visible because of it.
    """
    index, seen = {}, set()

    def remember(ico, event_date, isco, title):
        if not ico or not event_date or not isco:
            return
        # Same company, same day, same role group is one posting however
        # many sources mention it.
        key = (ico, event_date, isco)
        if key in seen:
            return
        seen.add(key)
        index.setdefault(ico, []).append((event_date, isco, title))

    if Path(path).exists():
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                remember(row.get("ico"),
                         parse_date(row.get("date"), earliest=HISTORY_START),
                         isco4(row.get("isco")), row.get("title"))

    if Path(current).exists():
        with open(current, encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                remember(row.get("ico"), parse_date(row.get("posted")),
                         isco4(row.get("isco")), row.get("title"))
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


def drop_reentries(events, company=None):
    """Remove arrival/departure pairs that are one person re-registered.

    Found on live data: FORCE TRADE s.r.o. showed Petra Doležalová both
    joining and leaving the board on 2026-07-27, and again as owner on
    the same day. Read literally that is two leadership events in one
    day; read correctly it is one amendment to an existing record.

    Measured across the base: 41 of 152 events in a 30-day window - 27 %
    - were these pairs. Left in, better than a quarter of every "why
    now" reason handed to a salesperson would be a clerical correction.

    PAIRING ALONE IS NOT ENOUGH, because the arrival it needs is not
    always there to pair with. MASKOP 99 re-entered both jednatelé on
    2026-08-29, so registry_events()'s mass-event guard removed both
    arrivals, and TOMÁŠ JUPA's departure of that same day survived on its
    own - telling a salesperson that the man to call had left a board he
    currently sits on. So `company` is consulted as a second pass: a
    departure the present register contradicts is not a departure.

    The two passes run in this order for a reason. Filtering on the
    register first would delete the departure half of a genuine pair
    before this function could see it, and the arrival half would then
    survive alone - a false "new jednatel" in place of a false departure,
    which is no better. Measured after the fix: 13 false departures at 11
    companies removed in a 7-day window, against 22 real ones kept.
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

    kept = [e for e in events if (e.get("name"), e["date"]) not in paired]
    if not company:
        return kept

    # ARES spells the same person differently across records ("TOMÁŠ
    # JUPA" in the current entry, "Tomáš Jupa" in the 2016 one), so the
    # comparison folds case rather than trusting the spelling. Compared
    # per role, not across both: somebody can genuinely stop being a
    # jednatel while staying an owner, and WMW - Production is exactly
    # that case - Michael Grauel left the board on 2026-08-27 and still
    # holds his share, which is a real change and stays a reason to call.
    still_listed = {
        "director_departed": {(p.get("name") or "").casefold()
                              for p in (company.get("directors") or [])},
        "owner_departed": {(p.get("name") or "").casefold()
                           for p in (company.get("owners") or [])},
    }
    return [e for e in kept
            if (e.get("name") or "").casefold() not in still_listed.get(e["kind"], ())]


# EU subsidies are published monthly and lag by weeks, so a 7-day
# window - the cadence of the weekly run - matches nothing from this
# source, ever. Measured on the 2026-08 file: newest signing date was
# 2026-07-23, a month old on arrival, and the yield only becomes
# non-zero at ~60 days. The subsidy window is therefore deliberately
# decoupled from the registry/vacancy one rather than sharing it.
# WIDENED TO 365 AND PUT BACK, AND THE ATTEMPT IS THE POINT. Measured
# across the whole pool on 04.09.2026, the yield rises smoothly with the
# window - 120d 0 companies, 180d 4, 240d 6, 300d 9, 365d 15, 730d 32 -
# so widening looks like a free way to fill a week that the registry
# alone leaves at one company.
#
# It is not free, and the run's own docstring already said so: fewer
# than five is a result, and the window is not to be widened to fill the
# quota. At 365 a whole week's five came out as grants signed 179, 262,
# 294, 316 and 345 days ago, and the one fresh registry event in the
# same week - a change of owner four days old - was pushed off the list
# by them. Class A outranks class B by the document, so a stale grant
# beats a current change of management every time; the number that
# suffers is not the count, it is what "proč právě teď" means.
#
# And the zero at 120 days was never a window problem. No signal-class
# grant was signed for ANY of our 4781 companies in May, June or July -
# a three-month gap against a base rate of one to four a month. The
# honest response to a source with nothing in it is that it contributes
# nothing this week, not that we reach back a year for something older.
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
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # The sweep that writes this file takes hours and a run
                # may read it while it is still going - see
                # load_companies() for the same reasoning.
                continue
            out.setdefault(row["ico"], []).append(row)
    return out


def tender_events(ico, tenders, today=None):
    """Procurements still accepting bids, whose subject RTsoft could supply.

    THE DEADLINE DECIDES, NOT THE STATUS. The first version of this took
    NEN's `Neukončen` to mean "open" and it was wrong in every single
    case: judging the five companies a full run delivered, all six of
    their "open" tenders had closed for bids 34, 127, 183, 312, 461 and
    734 days earlier. `Neukončen` is an administrative state of the
    record - a procurement sits there for years after bidding ends -
    and reading it as "buying now" produced four false leads out of
    five. The one real lead in that run had no tender at all: a subsidy
    granted 113 days ago with no procurement started, which is the
    strongest case in the whole matrix.

    So an event is produced only while bids can still be submitted. A
    tender whose deadline has passed is a fact about the company's past
    and belongs on the card as context - "they were buying an MES last
    autumn" is worth knowing before dialling - but it is not a reason to
    call this week, and scoring/card.py shows it separately.

    Still no window on top of that, and that part was right: a tender
    open for bids is current whenever it was posted, so age_days stays 0
    and `days_left` carries the number that matters.
    """
    today = today or date.today()
    events = []
    for row in tenders.get(str(ico).zfill(8), []):
        if not row.get("relevant") or row.get("status") not in TENDER_OPEN:
            continue
        deadline = parse_deadline(row.get("deadline"))
        # No deadline at all is not treated as open. It is far more often
        # a record NEN never filled in than a procurement with no closing
        # date, and guessing in favour of a lead is how four of the five
        # companies in the last run got a reason to call that was over a
        # year stale.
        if deadline is None or deadline < today:
            continue
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
         subsidy_window=SUBSIDY_WINDOW, tenders=None,
         vacancy_window=VACANCY_WINDOW):
    """Every dated NOW event for one company, tagged with confidence.

    `window_days` is the REGISTRY window and nothing else. Each source
    keeps its own, sized to how far behind that source publishes -
    subsidies 120 days, vacancies 24, tenders none at all because a bid
    deadline is not an age. Only the register publishes faster than the
    run repeats, which is why only the register can be asked "what
    happened in the last seven days".

    `confidence` is not a score - it is a visible flag for the one
    failure mode that matters here: a vacancy event on a company barely
    seen on MPSV is not stronger evidence than one on a company posted
    for a year, and collapsing that distinction would let a thin sample
    look as certain as a thick one.
    """
    events = drop_reentries(registry_events(company, window_days, today), company)

    depth = role_history_depth(company["ico"], history, today)
    for event in vacancy_events(company["ico"], history, vacancy_window, today):
        event["confidence"] = "high" if depth >= 12 else "low" if depth >= 6 else None
        # Carried onto the event, not just used to filter it: 18.7 says
        # the card must read "in N months of watching, never this role"
        # rather than "for the first time", and it cannot say that unless
        # N travels with the event to describe().
        event["depth_months"] = depth
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
    "tender_open":        ("nen", None),
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
        # The date is in the sentence for the same reason it is in the
        # subsidy line below: this window is 24 days wide because MPSV
        # publishes ten days late, so an "inzerát" with no date reads as
        # "posted this week", which it can never be - the source has
        # nothing that new in it.
        when = f", zveřejněno {event['date']}" if event.get("date") else ""
        # And the observation depth, because 18.7 asked for exactly this
        # and the card never carried it: the depth is what licenses any
        # reading of the advert as a NEW role, and without it printed the
        # salesperson cannot tell "they have never needed a planner
        # before" from "we have only been watching them since June".
        # It counts months in which this company posted anything at all,
        # so it is worded as observation, not as the role's own history.
        depth = event.get("depth_months")
        seen = f", firma inzeruje v {depth} sledovaných měsících" if depth else ""
        return (f"inzerát na řídící/plánovací roli{when}{seen}: "
                f"{event.get('title') or event['isco']}")
    if event["kind"] == "tender_open":
        left = event.get("days_left")
        # The deadline is the actionable number here, not the age: it
        # says how long there is left to bid.
        when = (f", do uzávěrky {left} dní" if isinstance(left, int) and left >= 0
                else ", po uzávěrce" if isinstance(left, int) else "")
        return f"otevřená zakázka{when}: {(event.get('subject') or '')[:90]}"
    if event["kind"] == "subsidy_signed":
        try:
            millions = f"{float(event.get('total_czk') or 0) / 1e6:.1f} mil. Kč"
        except (TypeError, ValueError):
            millions = "částka neuvedena"
        # The signing date belongs in the sentence, not only in the
        # event. Since SUBSIDY_WINDOW went to a year this line can carry
        # a grant signed ten months ago, and "podepsaná dotace" with no
        # date reads as "last week" - which is the one thing it is not.
        signed = event.get("date")
        when = f", podepsáno {signed}" if signed else ""
        stale = "" if event.get("fresh") else " (starší, ověřit stav)"
        return (f"podepsaná dotace EU ({millions}){when}{stale}: "
                f"{event.get('project', '')[:90]}")
    who = event.get("name") or "neuvedeno"
    role = f" ({event['role']})" if event.get("role") else ""
    verb = {
        "director_joined": "nastoupil do statutárního orgánu",
        "director_departed": "opustil statutární orgán",
        "owner_joined": "stal se vlastníkem",
        "owner_departed": "přestal být vlastníkem",
    }[event["kind"]]
    return f"{who}{role} — {verb}"


def snapshot_for(archive, ico, kind, url=None):
    """The archived document a given event was read from, or None.

    `url` matters when a company has several documents from one source.
    A company with two open tenders has two `nen` snapshots, and taking
    the latest would attach both claims to whichever was fetched last -
    so the event's own URL picks its own page. Registry and subsidy
    events have one document per company and do not need it.
    """
    source, endpoint = EVENT_SOURCE.get(kind, (None, None))
    if not source:
        return None
    if url:
        row = archive.latest(ico, source, url=url)
    elif endpoint:
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

# How far back the search for the event's own date may reach. One
# person's record - dates, address, name - runs to roughly a thousand
# characters, so this covers it without wandering into the record
# before, whose dates belong to somebody else.
MAX_QUOTE_BACK = 1200


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

    if event["kind"].startswith(("director", "owner")):
        quote = registry_quote(event, text)
        if quote:
            return quote
        # Fall through to the name anchor below when the date is not in
        # the document - better a quote proving the person than none.

    anchors = []
    if event["kind"] == "tender_open":
        # The subject is printed on the tender page verbatim, so it is
        # the anchor - and it is also what the card shows, which means
        # the quote proves the very line the salesperson reads.
        anchors.append((event.get("subject") or "")[:80])
    elif event["kind"] == "subsidy_signed":
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


def registry_quote(event, text):
    """A quote spanning the event's own date through to the person's name.

    Two bugs made this its own function rather than a wider window.

    First, the quote has to carry the event's OWN date or it proves only
    that the person exists in the register. HAVRÁNEK's "datumVymazu":
    "2026-08-25" sits 793 characters before his surname - the address
    block in between is long - so a 320-character window stopped short
    of it and instead caught the datumZapisu of the NEXT record, a date
    belonging to somebody else entirely.

    Second, anchoring on the name finds the WRONG occurrence when a
    person appears more than once. ČENĚK FAJKUS left KVAZAR's board
    twice, in 2025 and 2026; a search for his surname lands on the 2025
    record, and the quote then dates a 2026 departure to the year
    before.

    So the search runs date-first: find the event's date, then the name
    after it. That pairs the two the way the record itself does, and the
    quote reads as the register reads - date, role, person.
    """
    event_date = event.get("date")
    parts = (event.get("name") or "").split()
    if not event_date or not parts:
        return None

    surname = parts[-1]
    start = 0
    while True:
        found = text.find(f'"{event_date}"', start)
        if found < 0:
            return None
        # The name has to sit inside this record, not the next one.
        name_at = text.find(surname, found, found + MAX_QUOTE_BACK)
        if name_at >= 0:
            field = text.rfind('"datum', max(0, found - 40), found)
            begin = field if field >= 0 else found
            return text[begin:min(len(text), name_at + len(surname) + QUOTE_FORWARD)]
        start = found + 1


def record(archive, company, history, window_days=30, run_id=None, today=None,
           subsidies=None, tenders=None):
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
    for event in find(company, history, window_days, today,
                      subsidies=subsidies, tenders=tenders):
        snapshot_id = snapshot_for(archive, company["ico"], event["kind"],
                                   url=event.get("url"))
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
    """Stream the candidate file, tolerating a line still being written.

    The file is appended to while a run is in progress - the change
    stream adds companies the register just moved (run.admit_newcomers)
    and a long enrichment writes as it goes. A reader that opens the
    file at that moment can see a half-written final line, and a bare
    json.loads would take the whole stage down over one truncated row
    that will be complete a second later.
    """
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "error" not in row:
                yield row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dated NOW events per company.")
    parser.add_argument("ico", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--window", type=int, default=30,
                        help="REGISTRY window in days; the other sources keep "
                             "their own (see the module docstring)")
    parser.add_argument("--lag", action="store_true",
                        help="measure how far behind the MPSV export runs and exit")
    parser.add_argument("--record", action="store_true",
                        help="write events into the evidence store as claims")
    args = parser.parse_args()

    if args.lag:
        # The one number the whole vacancy window rests on, on demand:
        # the constant above was measured this way and this is how to
        # find out that it has stopped being true.
        measured = publication_lag()
        print(f"MPSV export lag: {measured} days (constant says {VACANCY_LAG}); "
              f"window would be {(measured or VACANCY_LAG) + VACANCY_FRESH_DAYS} days, "
              f"in use {VACANCY_WINDOW}")
        sys.exit(0)

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
    tenders = load_tenders()

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
                                                run_id, subsidies=subsidies,
                                                tenders=tenders)
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
