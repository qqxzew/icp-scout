"""One-off measurement: how many companies leave at each step, and where.

Throwaway - counts only, changes nothing.
"""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

from pipeline.sources.res_bulk import ICP_FORMA, ICP_KATPO, ICP_NACE

RES = Path("data/raw/res_data.csv")

# --- step 01: the bulk register ------------------------------------------
drops = Counter()
kept = 0
with open(RES, encoding="utf-8", newline="") as handle:
    reader = csv.reader(handle)
    header = next(reader)
    idx = {name: i for i, name in enumerate(header)}
    c_dead, c_forma = idx["DDATZAN"], idx["FORMA"]
    c_katpo, c_nace, c_nace25 = idx["KATPO"], idx["NACE"], idx["NACE2025"]
    total = 0
    for row in reader:
        total += 1
        if row[c_dead]:
            drops["01a zaniklý subjekt (DDATZAN)"] += 1
            continue
        if row[c_forma] not in ICP_FORMA:
            drops["01b právní forma není s.r.o./a.s."] += 1
            continue
        if row[c_katpo] not in ICP_KATPO:
            drops["01c velikost mimo 50-199 (KATPO)"] += 1
            continue
        code = row[c_nace25] or row[c_nace] or ""
        if not any(code.startswith(p) for p in ICP_NACE):
            drops["01d NACE mimo 19 divizí"] += 1
            continue
        kept += 1

print(f"res_data.csv rows: {total}")
for reason, count in sorted(drops.items()):
    print(f"  {reason:42} -{count}")
print(f"  -> candidates: {kept}\n")

# --- step 02: ARES enrichment --------------------------------------------
rows = [json.loads(line) for line in
        open("data/raw/ares_candidates_v2.jsonl", encoding="utf-8")]
errors = [r for r in rows if "error" in r]
ok = [r for r in rows if "error" not in r]
print(f"ares_candidates_v2.jsonl: {len(rows)} lines, {len(errors)} errors, {len(ok)} usable")
for field in ("region", "coordinates", "directors", "employee_range", "file_number"):
    have = sum(1 for r in ok if r.get(field))
    print(f"  {field:16} {have:5}  ({100*have/len(ok):.1f} %)")
print()

# --- lateral sources ------------------------------------------------------
def load_keys(path, key="ico"):
    if not Path(path).exists():
        return set()
    out = set()
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            out.add(json.loads(line).get(key))
    return out

base = {r["ico"] for r in ok}
sites = {}
for line in open("data/raw/websites.jsonl", encoding="utf-8"):
    row = json.loads(line)
    sites[row["ico"]] = row.get("status")
site_counts = Counter(sites.values())
print("websites.jsonl:", dict(site_counts), f"of {len(base)}")

contacts = 0
people = 0
for line in open("data/raw/contacts.jsonl", encoding="utf-8"):
    row = json.loads(line)
    named = [p for p in (row.get("people") or []) if p.get("email") or p.get("phone")]
    if named:
        contacts += 1
        people += len(named)
print(f"contacts.jsonl: {contacts} companies with a reachable person, {people} people")

for path, label in (("data/raw/mpsv_vacancies.jsonl", "MPSV vacancies"),
                    ("data/raw/mpsv_history.jsonl", "MPSV history"),
                    ("data/raw/dotace_eu.jsonl", "EU subsidies"),
                    ("data/raw/nen.jsonl", "NEN tenders"),
                    ("data/raw/turnover.jsonl", "turnover")):
    if not Path(path).exists():
        print(f"{label:16} (missing)")
        continue
    icos, lines = set(), 0
    for line in open(path, encoding="utf-8"):
        if line.strip():
            lines += 1
            icos.add(json.loads(line).get("ico"))
    print(f"{label:16} {lines:6} rows, {len(icos & base):5} of our companies")
print()

# --- step 03/04: brief, negative filters, NOW ------------------------------
from pipeline.filters import brief as brief_filter
from pipeline.filters.negative import EXCLUDE, check, verdict
from pipeline.run import load_icp
from pipeline.scoring.select import eligible
from pipeline.signals.now import find, load_history, load_tenders
from pipeline.sources.dotace_eu import load as load_subsidies

icp = load_icp()
pool, funnel = eligible(ok, icp)
print("brief:", brief_filter.describe(icp))
print(f"  brief drops: {funnel['brief_rejected']}")
print(f"  negative drops: {funnel['excluded']}  -> pool {funnel['pool']}")
print("  negative by reason:",
      dict(Counter(f["reason"][:40] for c in ok for f in check(c))))
print()

history, subsidies, tenders = load_history(), load_subsidies(), load_tenders()
for window in (7, 14, 30, 90):
    qualified = {}
    kinds = Counter()
    for company in pool:
        events = find(company, history, window, subsidies=subsidies, tenders=tenders)
        if events:
            qualified[company["ico"]] = events
            for event in events:
                kinds[event["kind"]] += 1
    print(f"NOW window {window:3}d -> {len(qualified):4} companies, events {dict(kinds)}")
