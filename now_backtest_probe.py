"""Probe 2: is one working signal enough to fill five cards a week?

Probe 1 measured this week. This one measures every week of the last two
years, because a single week is one draw from a distribution and the
question "do we get five" is a question about the distribution.

Also measures the two things probe 1 could only show as zeroes: how far
behind MPSV actually publishes, and what the vacancy source would yield
if the signal were not narrowed to management ISCO codes.

Read-only, no network.
"""

import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

from pipeline.scoring import select
from pipeline.signals import now as now_signal

TODAY = date.today()


def parse(value):
    return now_signal.parse_date(value)


# ---------------------------------------------------------------------------
# The backtest
# ---------------------------------------------------------------------------


def all_registry_dates(company):
    """Every dated board/ownership movement this company's record holds.

    Deliberately NOT registry_events(): that reads arrivals only from the
    people currently listed, so a director who joined in March and left in
    June is invisible as an arrival today. For "what would week N have
    looked like" the arrival has to be counted in March, and the departed
    lists carry both of its dates.
    """
    out = []
    for field, key, kind in (
        ("directors", "since", "director_joined"),
        ("owners", "since", "owner_joined"),
        ("departed_directors", "since", "director_joined"),
        ("departed_owners", "since", "owner_joined"),
        ("departed_directors", "until", "director_departed"),
        ("departed_owners", "until", "owner_departed"),
    ):
        for person in company.get(field) or []:
            parsed = parse(person.get(key))
            if parsed:
                out.append((parsed, kind, (person.get("name") or "").casefold()))
    return out


def backtest(pool, websites, contacts, weeks=104):
    """Registry events per week, as a weekly run would have seen them.

    Two filters from the live code are applied in a simplified form:
    same-day arrival/departure pairs (drop_reentries) and a whole board
    dated on the company's founding day (registry_events' guard). Both are
    cheap to reproduce and leaving them out would inflate every week.
    """
    print("=" * 78)
    print(f"1. BACKTEST: registry events per week, last {weeks} weeks")
    print("=" * 78)

    deliverable_ico = set()
    for company in pool:
        ico = company["ico"]
        if (websites.get(ico, {}).get("status") == "proven"
                and select.contact_richness(contacts.get(ico))["any_channel"]):
            deliverable_ico.add(ico)
    print(f"  {len(deliverable_ico)} of {len(pool)} pool companies could be handed "
          f"over at all (proven site + a channel)\n")

    per_week = defaultdict(set)
    per_week_deliverable = defaultdict(set)
    for company in pool:
        ico = company["ico"]
        established = parse(company.get("established"))
        dates = all_registry_dates(company)

        # A whole board dated the company's own founding day is the
        # company being new, not a change of leadership.
        distinct = {d for d, _, _ in dates}
        if established and len(distinct) == 1 and abs((distinct.pop() - established).days) < 400:
            continue

        # Same person in and out within three days is one amendment.
        joined = {(name, d) for d, kind, name in dates if kind.endswith("_joined")}
        dropped = set()
        for d, kind, name in dates:
            if not kind.endswith("_departed"):
                continue
            for jname, jd in joined:
                if jname == name and abs((jd - d).days) <= now_signal.REENTRY_DAYS:
                    dropped.add((name, d))
                    dropped.add((name, jd))
        for event_date, kind, name in dates:
            if (name, event_date) in dropped:
                continue
            age = (TODAY - event_date).days
            if age < 0 or age >= weeks * 7:
                continue
            week = age // 7
            per_week[week].add(ico)
            if ico in deliverable_ico:
                per_week_deliverable[week].add(ico)

    counts = [len(per_week.get(w, ())) for w in range(weeks)]
    deliverable = [len(per_week_deliverable.get(w, ())) for w in range(weeks)]

    def describe(label, values):
        values = sorted(values)
        print(f"  {label:32} median {statistics.median(values):5.1f}   "
              f"mean {statistics.mean(values):5.1f}   "
              f"min {values[0]:>3}   max {values[-1]:>3}   "
              f"weeks with >= 5: {sum(1 for v in values if v >= 5)}/{len(values)}")

    describe("companies with an event", counts)
    describe("...of them deliverable", deliverable)

    print("\n  Most recent 12 weeks (week 0 = the last 7 days):")
    for week in range(12):
        bar = "#" * len(per_week.get(week, ()))
        print(f"    week -{week:<2} {len(per_week.get(week, ())):>3} events "
              f"({len(per_week_deliverable.get(week, ())):>2} deliverable)  {bar}")

    # Seasonality matters for a defence: a run demoed in August draws from
    # the thinnest month of the year.
    by_month = Counter()
    weeks_per_month = Counter()
    for week in range(weeks):
        day = TODAY - timedelta(days=week * 7)
        by_month[day.month] += len(per_week.get(week, ()))
        weeks_per_month[day.month] += 1
    print("\n  Average events per week by calendar month:")
    for month in range(1, 13):
        if weeks_per_month[month]:
            print(f"    {month:>2}: {by_month[month] / weeks_per_month[month]:5.1f}")


# ---------------------------------------------------------------------------
# Does MPSV actually lag?
# ---------------------------------------------------------------------------


def mpsv_lag(path=now_signal.MPSV_CURRENT):
    print()
    print("=" * 78)
    print("2. MPSV: how fresh is the current export, really")
    print("=" * 78)
    ages = Counter()
    total = 0
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            posted = parse(row.get("posted"))
            if not posted:
                continue
            total += 1
            age = (TODAY - posted).days
            if age <= 30:
                ages[age] += 1
    print(f"  {total} vacancies in the file (all companies, not just ours)")
    print("  postings by age in days:")
    for age in range(0, 31):
        bar = "#" * min(60, ages[age] // 20)
        print(f"    {age:>2}d {ages[age]:>5}  {bar}")


# ---------------------------------------------------------------------------
# What the vacancy source could give if the signal were widened
# ---------------------------------------------------------------------------


SIZE_MIDPOINT = {"220": 22, "230": 37, "240": 75, "310": 150, "330": 375,
                 "340": 750, "410": 1250, "210": 15, "130": 8, "120": 3}


def vacancy_ceiling(pool, history):
    print()
    print("=" * 78)
    print("3. VACANCY SIGNAL: what is there, against what we take")
    print("=" * 78)
    for window in (7, 14, 30, 60, 90):
        any_vacancy = manage = three_plus = share10 = 0
        for company in pool:
            rows = history.get(company["ico"], [])
            recent = [(d, isco, title) for d, isco, title in rows
                      if 0 <= (TODAY - d).days <= window]
            if not recent:
                continue
            any_vacancy += 1
            if len(recent) >= 3:
                three_plus += 1
            if any(isco in now_signal.MANAGEMENT_ISCO for _, isco, _ in recent):
                manage += 1
            headcount = SIZE_MIDPOINT.get(str(company.get("employee_code") or ""), None)
            if headcount and len(recent) / headcount >= 0.10:
                share10 += 1
        print(f"  {window:>3}d: any vacancy {any_vacancy:>4} | >=3 vacancies "
              f"{three_plus:>4} | >=10 % of headcount {share10:>4} | "
              f"management ISCO {manage:>4}  <- what we use")


def main():
    companies = list(now_signal.load_companies(select.ARES_CANDIDATES))
    history = now_signal.load_history()
    websites = select.load_jsonl(select.WEBSITES)
    contacts = select.load_jsonl(select.CONTACTS)

    from pipeline.run import load_icp
    pool, _ = select.eligible(companies, load_icp())
    print(f"pool: {len(pool)} companies\n", file=sys.stderr)

    backtest(pool, websites, contacts)
    mpsv_lag()
    vacancy_ceiling(pool, history)


if __name__ == "__main__":
    main()
