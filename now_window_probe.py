"""One-off probe: what actually fires inside the weekly NOW window, and why.

Answers three questions with numbers instead of impressions:

  1. how stale each source is when it reaches us (publication lag),
  2. how many companies each signal family gates in, per window width,
  3. how strong the registry signal is on its own - base rate, what the
     events actually are, and what survives the delivery gate.

Read-only. Touches no network and no archive.
"""

import sys
from collections import Counter, defaultdict
from datetime import date

from pipeline.scoring import select
from pipeline.signals import now as now_signal
from pipeline.sources.dotace_eu import load as load_subsidies

TODAY = date.today()
WINDOWS = (7, 14, 30, 60, 90, 120, 180, 365, 730)


def parse(value):
    return now_signal.parse_date(value)


def source_lag(companies, history, subsidies, tenders):
    """Newest record each source carries, against today."""
    print("=" * 78)
    print(f"1. PUBLICATION LAG  (today = {TODAY})")
    print("=" * 78)

    registry = []
    for company in companies:
        for field, key in (("directors", "since"), ("owners", "since"),
                           ("departed_directors", "until"), ("departed_owners", "until")):
            for person in company.get(field) or []:
                parsed = parse(person.get(key))
                if parsed:
                    registry.append(parsed)
    vacancies = [d for rows in history.values() for d, _, _ in rows]
    grants = [parse(row.get("signed")) for rows in subsidies.values() for row in rows]
    grants = [d for d in grants if d]
    published = []
    for rows in tenders.values():
        for row in rows:
            parsed = now_signal.parse_deadline(row.get("published"))
            if parsed:
                published.append(parsed)

    for label, dates in (("ARES registry (events)", registry),
                         ("MPSV vacancies (posted)", vacancies),
                         ("EU subsidies (signed)", grants),
                         ("NEN tenders (published)", published)):
        if not dates:
            print(f"  {label:26} -- nothing")
            continue
        newest = max(dates)
        # How many days back you have to look before the source has
        # anything at all to say.
        print(f"  {label:26} newest {newest}  = {(TODAY - newest).days:>3} days old"
              f"   ({len(dates)} dated records)")


def window_sweep(pool, history, subsidies, tenders):
    """Companies gated in per family, per window width."""
    print()
    print("=" * 78)
    print(f"2. YIELD BY WINDOW  (pool = {len(pool)} companies after brief + negatives)")
    print("=" * 78)
    print(f"  {'window':>7} | {'registry':>8} {'growth':>7} {'subsidy':>8} {'tender':>7}"
          f" | {'any':>5}")

    for window in WINDOWS:
        per_family = Counter()
        gated = 0
        for company in pool:
            events = now_signal.find(company, history, window, TODAY,
                                     subsidies=subsidies, tenders=tenders)
            if not events:
                continue
            gated += 1
            for family in select.families(events):
                per_family[family] += 1
        print(f"  {window:>5}d  | {per_family['registry']:>8} {per_family['growth']:>7}"
              f" {per_family['subsidy']:>8} {per_family['tender']:>7} | {gated:>5}")

    # The subsidy window is fixed inside find(); measure it apart so the
    # table above is not read as "subsidies respond to the window".
    print(f"\n  note: subsidies use their own fixed window "
          f"(SUBSIDY_WINDOW = {now_signal.SUBSIDY_WINDOW}d), tenders none at all "
          f"(bid deadline decides)")


def growth_without_depth(pool, history):
    """What the vacancy signal would give without the confidence filter."""
    print()
    print("-" * 78)
    print("2b. GROWTH: what the depth filter costs")
    print("-" * 78)
    for window in (7, 14, 30, 60, 90, 180):
        raw = kept = 0
        for company in pool:
            events = now_signal.vacancy_events(company["ico"], history, window, TODAY)
            if not events:
                continue
            raw += 1
            depth = now_signal.role_history_depth(company["ico"], history, TODAY)
            if depth >= 6:
                kept += 1
        print(f"  {window:>4}d  management vacancy: {raw:>4} companies, "
              f"{kept:>4} survive role_history_depth >= 6")


def registry_strength(pool, history, subsidies, tenders):
    """What registry events are, how often they happen, and to whom."""
    print()
    print("=" * 78)
    print("3. THE REGISTRY SIGNAL ON ITS OWN")
    print("=" * 78)

    by_window = {}
    for window in (7, 30, 90, 365):
        hits = []
        for company in pool:
            events = [e for e in now_signal.drop_reentries(
                now_signal.registry_events(company, window, TODAY), company)]
            if events:
                hits.append((company, events))
        by_window[window] = hits
        share = 100 * len(hits) / len(pool)
        print(f"  {window:>4}d: {len(hits):>4} of {len(pool)} companies "
              f"({share:5.2f} %)")

    year = by_window[365]
    print(f"\n  Ceiling: {len(year)} companies fire a registry event in a year "
          f"= {len(year) / 52:.1f} per week on average.")

    print("\n  What the events are (365d window):")
    kinds = Counter(e["kind"] for _, events in year for e in events)
    total = sum(kinds.values())
    for kind, count in kinds.most_common():
        print(f"    {kind:20} {count:>5}  ({100 * count / total:4.1f} %)")

    legal = sum(1 for _, events in year for e in events
                if e["kind"].startswith("owner_") and e.get("legal_entity"))
    owner_events = sum(1 for _, events in year for e in events
                       if e["kind"].startswith("owner_"))
    if owner_events:
        print(f"\n    of {owner_events} ownership events, {legal} "
              f"({100 * legal / owner_events:.0f} %) have a COMPANY as the owner "
              f"- a holding move, not a new person")

    # Is the arriving director actually new to the company, or was he
    # already an owner / a director who left and came back? The ICP's
    # trigger 7 is "novy manazer ve funkci" - a familiar face moving one
    # chair over is not that.
    fresh = familiar = 0
    for company, events in year:
        known = {(p.get("name") or "").casefold()
                 for field in ("owners", "departed_directors", "departed_owners")
                 for p in (company.get(field) or [])}
        for event in events:
            if event["kind"] != "director_joined":
                continue
            name = (event.get("name") or "").casefold()
            if name in known:
                familiar += 1
            else:
                fresh += 1
    if fresh + familiar:
        print(f"\n    of {fresh + familiar} directors ARRIVING, {familiar} "
              f"({100 * familiar / (fresh + familiar):.0f} %) already appear elsewhere "
              f"in the company's own register record")

    # Do registry events land on companies we can actually hand over?
    print("\n  Does the week's registry hit survive the delivery gate?")
    websites = select.load_jsonl(select.WEBSITES)
    contacts = select.load_jsonl(select.CONTACTS)
    for window in (7, 30, 90):
        hits = [c for c, _ in by_window.get(window, ())] if window in by_window else [
            c for c in pool
            if now_signal.drop_reentries(
                now_signal.registry_events(c, window, TODAY), c)]
        proven = sum(1 for c in hits
                     if websites.get(c["ico"], {}).get("status") == "proven")
        channel = sum(1 for c in hits
                      if select.contact_richness(contacts.get(c["ico"]))["any_channel"])
        both = sum(1 for c in hits
                   if websites.get(c["ico"], {}).get("status") == "proven"
                   and select.contact_richness(contacts.get(c["ico"]))["any_channel"])
        unknown_size = sum(1 for c in hits if str(c.get("employee_code") or "000") == "000")
        print(f"    {window:>4}d: {len(hits):>4} hits -> proven site {proven:>4}, "
              f"channel {channel:>4}, both {both:>4}   "
              f"(headcount unrecorded: {unknown_size})")

    # Base-rate comparison the ordering() docstring already claims: shells
    # change directors far more often than real companies.
    print("\n  Registry rate by whether the register knows the headcount (30d):")
    for label, predicate in (("headcount recorded",
                              lambda c: str(c.get("employee_code") or "000") != "000"),
                             ("headcount unrecorded",
                              lambda c: str(c.get("employee_code") or "000") == "000")):
        group = [c for c in pool if predicate(c)]
        hits = sum(1 for c in group
                   if now_signal.drop_reentries(
                       now_signal.registry_events(c, 30, TODAY), c))
        if group:
            print(f"    {label:22} {hits:>4} of {len(group):>5} "
                  f"({100 * hits / len(group):5.2f} %)")


def age_profile(pool, history, subsidies, tenders):
    """For companies gated in at 30 days, how old is each family's event."""
    print()
    print("=" * 78)
    print("4. AGE OF WHAT WE CATCH (30-day window, per family)")
    print("=" * 78)
    ages = defaultdict(list)
    for company in pool:
        for event in now_signal.find(company, history, 30, TODAY,
                                     subsidies=subsidies, tenders=tenders):
            ages[select.FAMILY.get(event["kind"], "other")].append(event.get("age_days", 0))
    for family, values in sorted(ages.items()):
        values.sort()
        median = values[len(values) // 2]
        print(f"  {family:10} {len(values):>4} events, "
              f"youngest {values[0]}d, median {median}d, oldest {values[-1]}d")


def main():
    companies = list(now_signal.load_companies(select.ARES_CANDIDATES))
    history = now_signal.load_history()
    subsidies = load_subsidies()
    tenders = now_signal.load_tenders()

    from pipeline.run import load_icp
    icp = load_icp()
    pool, funnel = select.eligible(companies, icp)
    print(f"loaded {len(companies)} candidates, {len(pool)} pass the saved brief "
          f"and the negative filters\n", file=sys.stderr)

    source_lag(companies, history, subsidies, tenders)
    window_sweep(pool, history, subsidies, tenders)
    growth_without_depth(pool, history)
    registry_strength(pool, history, subsidies, tenders)
    age_profile(pool, history, subsidies, tenders)


if __name__ == "__main__":
    main()
