"""The salesperson's brief, applied to a candidate company.

THIS MODULE EXISTS BECAUSE THE BRIEF WAS BEING RECORDED AND NEVER READ.
run.py loaded web/icp.json, wrote it into the run row - requirement 1 of
the brief, "accept the ICP as the task" - and then gated a candidate
list that had been frozen months earlier with the built-in NACE and size
sets baked into it. Changing every filter in the interface and pressing
save produced exactly the same five companies. The brief was accepted,
saved, archived and ignored, and only the archiving made it look done.

EVERY CRITERION ON THE SCREEN IS A FILTER, GEOGRAPHY INCLUDED. Industry,
size and legal form are sets, and a set has always been a filter here.
Geography was not: the ICP says "celá ČR; preferovaně Plzeňský ->
Karlovarský, Jihočeský, Středočeský, Praha, ~150 km", and "preferovaně"
was read as "order them, do not exclude them".

That reading is overruled by the person the tool is for. A radius shown
in the interface is a promise about what comes back, and a run that
answers with a company 352 km away has broken it - which is exactly
what happened: two of one week's five sat at 285 and 352 km because the
radius only sorted. So the number on the screen is now enforced, whether
the salesperson typed it or it came with the built-in profile, and the
same goes for a ticked region. `from_default` survives as a record of
who chose the radius; nothing branches on it any more.

WHY A REASON AND NOT A BOOLEAN. Every rejection returns which criterion
did it. A run that drops 3200 of 3299 candidates has to be able to say
whether that was the size band or a region nobody meant to tick - a bare
count of survivors is exactly the kind of silent wrongness this project
keeps digging out afterwards.

Run:
    python -m pipeline.filters.brief
    python -m pipeline.filters.brief --list nace
"""

import argparse
from collections import Counter
from pathlib import Path

from pipeline.sources.codebooks import region_names
from pipeline.sources.coords import distance_km

CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")

# How far past the radius a company may still sit. A great-circle
# distance is not a drive, and the interface offers round numbers, so the
# cut is 150 km meaning "about 150", not "150.0".
RADIUS_TOLERANCE_KM = 5


def _one_of(value, allowed, name):
    """Set membership with the unknown case kept separate.

    A missing field is reported as `<name>_unknown` rather than as a
    plain mismatch: 12 of the 3299 candidates have no region in ARES at
    all, and "we cannot tell which kraj this is" is a different fact
    from "this is the wrong kraj". Both drop the company - a filter is a
    filter - but the run log says which, so an unexpectedly small
    candidate list can be traced to missing data instead of to the
    criterion.
    """
    if not allowed:
        return None
    if not value:
        return f"{name}_unknown"
    return None if value in allowed else name


def mismatch(company, icp):
    """Which brief criterion rejects this company, or None if it passes.

    Checked cheapest first, and the order is not cosmetic: NACE and size
    are single string comparisons, the radius needs a coordinate pair and
    a trigonometric call, so the expensive one only runs for companies
    everything else already accepted.
    """
    nace = icp.get("nace")
    if nace:
        code = company.get("nace") or ""
        if not code:
            return "nace_unknown"
        if not any(code.startswith(prefix) for prefix in nace):
            return "nace"

    for value, allowed, name in (
        (company.get("employee_code"), icp.get("katpo"), "size"),
        (company.get("legal_form"), icp.get("forma"), "legal_form"),
        (company.get("region"), region_names(icp.get("regions")), "region"),
    ):
        reason = _one_of(value, allowed, name)
        if reason:
            return reason

    location = icp.get("location") or {}
    # A radius excludes. It used to only order, on the reading that the
    # ICP says "preferovaně" - but the salesperson's instruction is that
    # geography is a hard criterion: 150 km means at most 150 km, and a
    # region ticked in the interface means that region only. So the
    # number on the screen is now the number the pipeline enforces,
    # whether it was typed or came with the built-in profile.
    #
    # The tolerance exists because the distance is a straight line
    # between two RUIAN points while the salesperson drives a road: a
    # company at 152 km as the crow flies is not meaningfully farther
    # than one at 149, and cutting between them would be false
    # precision, not strictness.
    if location.get("km"):
        distance = distance_km(location.get("origin"), company.get("coordinates"))
        if distance is None:
            return "location_unknown"
        if distance > location["km"] + RADIUS_TOLERANCE_KM:
            return "outside_radius"

    return None


def apply(companies, icp):
    """(companies the brief admits, Counter of why the rest were dropped)."""
    kept, rejected = [], Counter()
    for company in companies:
        reason = mismatch(company, icp)
        if reason:
            rejected[reason] += 1
        else:
            kept.append(company)
    return kept, rejected


def describe(icp):
    """The brief in one line, for the run log.

    Printed next to the funnel numbers on purpose: "3299 -> 214" means
    nothing without the criteria that did it, and a salesperson watching
    a run should be able to recognise their own filters in the output.
    """
    location = icp.get("location") or {}
    bits = [
        f"nace={len(icp.get('nace') or [])}",
        f"katpo={','.join(icp.get('katpo') or []) or 'vše'}",
        f"kraje={len(icp.get('regions') or []) or 'vše'}",
    ]
    if location.get("km"):
        chosen = "výchozí" if location.get("from_default") else "zadaný"
        bits.append(f"okruh={location['km']}±{RADIUS_TOLERANCE_KM} km "
                    f"od {location.get('from') or '?'} ({chosen})")
    return " · ".join(bits)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply the saved brief to the candidate list.")
    parser.add_argument("--list", metavar="REASON",
                        help="print the companies dropped for one reason")
    args = parser.parse_args()

    from pipeline.run import load_icp
    from pipeline.signals.now import load_companies

    icp = load_icp()
    companies = list(load_companies(CANDIDATES))
    kept, rejected = apply(companies, icp)

    print(f"brief: {describe(icp)}  (from {icp['_source']})")
    print(f"candidates: {len(companies)} -> {len(kept)}")
    for reason, count in rejected.most_common():
        print(f"  dropped {count:5}  {reason}")

    if args.list:
        for company in companies:
            if mismatch(company, icp) == args.list:
                print(f"{company['ico']}  {(company.get('name') or '')[:42]:44} "
                      f"{company.get('nace')}  {company.get('region')}")
