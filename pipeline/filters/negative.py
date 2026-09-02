"""Who to throw out: the class that fits the ICP and still will not buy.

This class appears nowhere in the brief. It is a finding, and it should
be presented as one - the ICP describes who to look for and says nothing
about who is a waste of a phone call despite matching every line of it.

Measured on all 3299 candidates before any of this was written, because
two of the three filters the plan assumed would matter turned out not to:

    provozovny: some, 0 active   50    1.5 %
    insolvency active (ISIR)     11    0.3 %
    "v likvidaci" in the name      1
    not s.r.o. / a.s.              0   already filtered in res_bulk.py

So insolvency and legal form - the two the plan named first - together
reach 12 companies, and one of them was already handled upstream.

OWNERSHIP IS NOT HERE, AND THAT IS THE RESULT OF THE WORK RATHER THAN
AN OMISSION. A first version flagged the 597 companies whose owner is a
company registered abroad, reasoning that a group picks its systems
centrally. Three measurements took it apart:

  * a third of those companies (199) are run entirely by directors
    resident in Czechia - foreign ownership there is a holding
    structure, not foreign management;
  * a Czech plant on a group's SAP is a textbook instance of the ICP's
    own triggers 5 and 8 ("the standard solution hit a specific
    process", "the ERP is too big for them"), so the same fact argues
    for calling as loudly as against;
  * the obvious way out - detect the group ERP in the company's own
    text instead of guessing from ownership - was measured and does not
    work: 4.5 % of foreign-owned against 3.0 % of Czech-owned mention a
    large ERP, and the matches are a product called "SMARTCAB SAP 4",
    Oracle as a database, a news article about Epicor, and twice a
    mis-decoded byte sequence. Same near-zero precision as the `Excel`
    search in vacancies.

So the sign is unknown, no available data settles it, and the honest
move was to carry nothing rather than to score a guess or to hand the
salesperson a switch they have no basis to flip. What settles it is one
question to RTsoft: have they ever sold to a Czech subsidiary of a
foreign group over the group's IT? Until that is answered, the module
stays quiet about ownership.

THREE VERDICTS, NOT A BOOLEAN. The confidence behind these findings is
not remotely the same, and collapsing them would be the mistake the log
keeps recording (hypothesis E: absence and doubt are different states):

    exclude        provable and final. A company in insolvency has no
                   budget; a company in liquidation is closing. 12 of
                   3299 - small, certain, free.
    deprioritise   probably not worth a call, but nobody can prove it
                   from a register - every establishment closed while
                   the company itself stays active. Ranked lower, never
                   silently dropped.
    ok             nothing found.

Every verdict carries the field and value it was derived from, the same
way a claim carries its quote. Registry data is structured, so there is
no page to quote - but "which field said so" has to survive to the card,
or the salesperson cannot argue with the machine.

WHAT IS DELIBERATELY NOT HERE: vendor reference lists. The plan called
for scraping Helios, ABRA, K2 and the rest to find companies that have
already bought a competitor's system. Checked before building it:
Helios publishes about 15 references, several anonymised ("an
engineering company"), with no ICO and no year; ABRA keeps no
consolidated list at all, only scattered blog case studies. Matching
those to 3299 candidates would be name matching, and website.py already
measured what name matching costs - 46 % of domains guessed from a name
belonged to somebody else. Meanwhile the same signal already exists in a
better form: dotace_eu.py's `already_buying` class is keyed by ICO,
carries a signing date, and says in the project description what is
being bought. A weaker source for a signal already held is not worth the
scraper.

Run:
    python -m pipeline.filters.negative --all
    python -m pipeline.filters.negative 02112507
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")

# A company in liquidation must carry the suffix by law, which is why
# this is read off the name and not searched for in the register text.
# Searching the text was tried and is wrong: "likvidace lomů" is a
# mining activity, and one company's 1990 record says a state enterprise
# was dissolved *bez likvidace* - without liquidation. Both would have
# been flagged by a substring search for "likvidac".
IN_LIQUIDATION = re.compile(r"(?i)\bv\s+likvidaci\b")

EXCLUDE = "exclude"
DEPRIORITISE = "deprioritise"

# A deprioritising finding costs one step in the ordering, not a number
# of points. It was points - PENALTY = {DEPRIORITISE: 8} - and two
# things were wrong with that: nothing anywhere ever subtracted it, so
# "ranked lower, never silently dropped" was a promise the code did not
# keep; and 8 was uncalibrated against a score with no ceiling, so it
# would have either swamped a thin card or vanished under a rich one.
# An ordering step says precisely what was meant - below any otherwise
# equal candidate - without inventing a scale. See scoring/select.py's
# ordering().


def check(company):
    """Every negative finding about one company, worst first.

    Returns a list of {level, reason, field, value} - empty when nothing
    was found. `field` and `value` are what makes a verdict arguable: a
    salesperson who disagrees can look at the same field in ARES and see
    what this decided on.
    """
    findings = []

    if company.get("insolvent") or company.get("insolvency_state") == "AKTIVNI":
        findings.append({
            "level": EXCLUDE,
            "reason": "insolvenční řízení je aktivní",
            "field": "seznamRegistraci.stavZdrojeIr",
            "value": company.get("insolvency_state"),
        })

    name = company.get("name") or ""
    if IN_LIQUIDATION.search(name):
        findings.append({
            "level": EXCLUDE,
            "reason": "firma je v likvidaci",
            "field": "obchodniJmeno",
            "value": name,
        })

    # Every establishment closed while the company itself stays active is
    # not proof of anything on its own - but it is the shape of a company
    # winding down its operations, and the log already flagged the
    # counter as worth reading ("0 active out of 2 is itself a signal").
    total = company.get("establishments_total")
    active = company.get("establishments_active")
    if total and active == 0:
        findings.append({
            "level": DEPRIORITISE,
            "reason": f"všech {total} provozoven je neaktivních",
            "field": "provozovnyStav",
            "value": f"{active}/{total}",
        })

    return findings


def verdict(company):
    """One word for the whole company: exclude, deprioritise, or ok."""
    levels = {f["level"] for f in check(company)}
    if EXCLUDE in levels:
        return EXCLUDE
    if DEPRIORITISE in levels:
        return DEPRIORITISE
    return "ok"


def demotes(findings):
    """Whether these findings push a company down the ordering.

    Takes findings rather than a company so the caller that already
    holds them - it needs them for the card anyway - does not run every
    check twice. One definition of what "deprioritise" does, in the
    module that defines what it means.
    """
    return any(finding["level"] == DEPRIORITISE for finding in findings)


def load_companies(path=CANDIDATES):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if "error" not in row:
                yield row


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Negative filters - who not to call.")
    parser.add_argument("ico", nargs="?")
    parser.add_argument("--all", action="store_true", help="distribution over the whole base")
    parser.add_argument("--list", choices=[EXCLUDE, DEPRIORITISE],
                        help="print the companies at one level")
    args = parser.parse_args()

    companies = list(load_companies())

    if args.ico:
        target = next((c for c in companies if c["ico"] == args.ico.zfill(8)), None)
        if not target:
            sys.exit(f"{args.ico} is not in the candidate list")
        print(json.dumps({"ico": target["ico"], "name": target.get("name"),
                          "verdict": verdict(target), "findings": check(target)},
                         ensure_ascii=False, indent=2))
    elif args.list:
        shown = 0
        for company in companies:
            if verdict(company) != args.list:
                continue
            reasons = "; ".join(f["reason"] for f in check(company))
            print(f"{company['ico']}  {(company.get('name') or '')[:42]:44} {reasons}")
            shown += 1
        print(f"\n{shown} companies at level {args.list}", file=sys.stderr)
    elif args.all:
        verdicts, reasons = Counter(), Counter()
        for company in companies:
            verdicts[verdict(company)] += 1
            for finding in check(company):
                reasons[f"{finding['level']}: {finding['reason'][:48]}"] += 1
        total = len(companies)
        print(f"candidates: {total}\n")
        for level, count in verdicts.most_common():
            print(f"  {level:14} {count:5}  ({100 * count / total:.1f} %)")
        print("\nby reason:")
        for reason, count in reasons.most_common():
            print(f"  {count:5}  {reason}")
    else:
        parser.error("give an ICO, or --all, or --list")
