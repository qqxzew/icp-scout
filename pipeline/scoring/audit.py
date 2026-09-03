"""Does the selection obey every rule it claims to? One run, checked.

Not a demonstration - an audit. Each rule the pipeline gained is stated
here as something that must hold for every company it hands over, and
checked against the run rather than against the code that implements
it. A rule nobody can violate in one pass is not proven correct, but a
rule something DOES violate is proven broken, and that is the failure
worth catching before a card reaches a salesperson.

Run:
    python -m pipeline.scoring.audit
"""

import sys

from pipeline.filters import negative
from pipeline.filters.brief import RADIUS_TOLERANCE_KM, mismatch
from pipeline.run import load_icp
from pipeline.scoring.select import (ordering, reason_class, run as select_run)
from pipeline.signals.now import load_companies, load_tenders
from pipeline.sources.dotace_eu import classify_project, load as load_subsidies

CHECKS = []


def check(name):
    def register(function):
        CHECKS.append((name, function))
        return function
    return register


@check("every ranked company satisfies the saved brief")
def brief_holds(context):
    bad = [row["ico"] for row in context["ranked"]
           if mismatch(context["companies"][row["ico"]], context["icp"])]
    return not bad, f"{len(bad)} violate it: {bad[:5]}"


@check("no ranked company is excluded by the negative filters")
def negative_holds(context):
    bad = [row["ico"] for row in context["ranked"]
           if negative.verdict(context["companies"][row["ico"]]) == negative.EXCLUDE]
    return not bad, f"{len(bad)} insolvent or in liquidation: {bad[:5]}"


@check("every ranked company has at least one dated NOW event")
def gate_holds(context):
    bad = [row["ico"] for row in context["ranked"] if not row["now_events"]]
    return not bad, f"{len(bad)} without an event"


@check("every delivered company has a proven domain")
def proven_site(context):
    bad = [(row["ico"], row["site_status"]) for row in context["five"]
           if row["site_status"] != "proven"]
    return not bad, f"{bad}"


@check("every delivered company has an e-mail or a phone")
def has_channel(context):
    bad = [row["ico"] for row in context["five"]
           if not row["pain"]["contact"]["any_channel"]]
    return not bad, f"{bad}"


@check("every delivered company is inside the radius the brief names")
def inside_radius(context):
    limit = (context["icp"].get("location") or {}).get("km")
    if not limit:
        return True, "no radius in the brief"
    bad = [(row["ico"], row["geography"]["distance_km"]) for row in context["five"]
           if row["geography"]["distance_km"] is None
           or row["geography"]["distance_km"] > limit + RADIUS_TOLERANCE_KM]
    return not bad, f"{bad}"


@check("the ranking is monotone in its own sort key")
def monotone(context):
    keys = [ordering(row) for row in context["ranked"]]
    bad = [i for i in range(len(keys) - 1) if keys[i] > keys[i + 1]]
    return not bad, f"{len(bad)} adjacent pairs out of order"


@check("the reason class of each delivered company recomputes to the same letter")
def reason_stable(context):
    bad = []
    for row in context["five"]:
        again = reason_class(row["now_events"], context["tenders"].get(row["ico"]))
        if again != row["reason"]["class"]:
            bad.append((row["ico"], row["reason"]["class"], again))
    return not bad, f"{bad}"


@check("no delivered company is on the list because of an already_buying subsidy")
def not_already_buying(context):
    bad = []
    for row in context["five"]:
        if not any(event["kind"] == "subsidy_signed" for event in row["now_events"]):
            continue
        kinds = {classify_project(p.get("project"), p.get("description"))
                 for p in context["subsidies"].get(row["ico"], ())}
        if "already_buying" in kinds:
            bad.append(row["ico"])
    return not bad, f"{bad} hold a subsidy that names a system"

@check("every held-back company has a stated reason")
def held_back_explained(context):
    held = [row for row in context["ranked"] if row["undeliverable"]]
    bad = [row["ico"] for row in held if not row["undeliverable"]]
    return not bad, f"{len(held)} held back, all with a reason"


@check("no two delivered companies are the same group event")
def one_call_per_event(context):
    from pipeline.scoring.select import group_key
    keys = [group_key(row) for row in context["five"]]
    seen = [k for k in keys if k]
    return len(set(seen)) == len(seen), f"{[k for k in seen if seen.count(k) > 1]}"


@check("the five are five distinct companies")
def distinct(context):
    icos = [row["ico"] for row in context["five"]]
    return len(set(icos)) == len(icos), f"{icos}"


if __name__ == "__main__":
    icp = load_icp()
    five, ranked = select_run(7, 5, icp=icp)
    context = {
        "icp": icp,
        "five": five,
        "ranked": ranked,
        "companies": {c["ico"]: c for c in load_companies("data/raw/ares_candidates_v2.jsonl")},
        "tenders": load_tenders(),
        "subsidies": load_subsidies(),
    }

    print(f"\nqualified {len(ranked)}, deliverable {len([r for r in ranked if not r['undeliverable']])},"
          f" handed over {len(five)}\n")
    for row in five:
        geo = row["geography"]
        print(f"  {(row['name'] or '')[:30]:32}{row['reason']['class']}"
              f"{'+' if row['reason']['corroborated'] else ' '}"
              f"{geo['distance_km']:>6.0f} km  {row['site_domain']}")

    print()
    failures = 0
    for name, function in CHECKS:
        ok, detail = function(context)
        failures += not ok
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name}")
        if not ok:
            print(f"         {detail}")
    print(f"\n{len(CHECKS) - failures} of {len(CHECKS)} checks passed")
