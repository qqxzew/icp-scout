"""Agent: is a vendor already named in an "already_buying" subsidy project?

Born from a single observation while reading real data by hand
(reseni-log.md 19.x): dotace_eu.py's regex classifier buckets a project
as `already_buying` whenever its TITLE contains a word like "ERP" -
but BUSE s.r.o.'s title is "Komplexní podnikový ERP systém" while its
150-word description reads "Předmětem projektu je pořízení..." (future
tense, "acquisition of") and "...by měl nahradit stávající nevyhovující
uspořádání IS...tvořen dílčími aplikacemi nad MS Access...suplována v
MS Excel" - a company that has NOT yet chosen a system, describing
exactly ICP trigger 2 in its own words, with confirmed budget.

The title alone cannot tell "already signed with a competitor" apart
from "budget approved, vendor still open" - both produce a title full
of the same words. The description can, because it says what stage the
project is at. That distinction is exactly the kind of thing a regex
cannot do and reading comprehension can - so this is deliberately the
first LLM agent built, both because it is cheap (13 short texts) and
because getting it wrong has an unusually sharp cost: a false
"already_buying" throws away what may be the single strongest NOW
signal available (confirmed budget + self-described pain, in the
company's own words), and a false negative hands the salesperson a
call to a competitor's customer.

Output shape, enforced by the schema (not requested in prose):

    vendor_named    bool
    vendor          string or null - the product/company name if found
    evidence        [{"value": ..., "quote": ...}]  - see verify.py

Every item in `evidence` goes through evidence/verify.py exactly like
any other agent's output: the quote must appear verbatim in the
archived snapshot (the project description) or the statement is
discarded, never presented as fact. `vendor_named` and `vendor` are
the routing decision; `evidence` is what makes that decision auditable.

Run:
    python -m pipeline.llm.prompts.subsidy_vendor --dry-run
    python -m pipeline.llm.prompts.subsidy_vendor
"""

import argparse
import json
import sys

from pipeline.evidence.archive import Archive
from pipeline.evidence.verify import check_many
from pipeline.llm.client import LLM
from pipeline.sources.dotace_eu import classify_project, load as load_subsidies

PROMPT_NAME = "subsidy_vendor"
PROMPT_VERSION = 1

SYSTEM = """Jsi analytik čtoucí popisy dotačních projektů z EU fondů. \
Tvým jediným úkolem je zjistit, zda popis JIŽ JMENUJE konkrétního \
dodavatele nebo konkrétní produkt (např. Helios, SAP, K2, ABRA, \
konkrétní jméno firmy dodávající systém).

Pravidla:
- "vendor_named": true POUZE pokud je v textu doslova napsáno jméno \
  produktu nebo dodavatelské firmy.
- Formulace jako "vybraný dodavatel", "dodavatel systému", "nový ERP \
  systém", "pořízení systému" BEZ konkrétního jména NEZNAMENÁ, že je \
  dodavatel jmenován - "vendor_named" musí být false.
- Budoucí čas ("bude pořízen", "má nahradit", "je předmětem projektu") \
  je náznak, že výběr ještě neproběhl, ale sám o sobě nerozhoduje - \
  rozhoduje pouze přítomnost konkrétního jména.
- Každé tvrzení v "evidence" musí nést "quote" - doslovnou citaci z \
  textu, kterou lze najít vyhledáním v původním textu. Pokud tvrzení \
  nemá přímou citaci, nastav "quote" na null a označ ho tak jako úsudek, \
  nikdy si citaci nevymýšlej."""

SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_named": {"type": "boolean"},
        "vendor": {"type": ["string", "null"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "quote": {"type": ["string", "null"]},
                },
                "required": ["value", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["vendor_named", "vendor", "evidence"],
    "additionalProperties": False,
}


def user_prompt(project_name, description):
    return (
        f"Název projektu: {project_name}\n\n"
        f"Popis projektu:\n{description}\n\n"
        "Je v popisu jmenován konkrétní dodavatel nebo produkt?"
    )


def already_buying_projects():
    """(ico, project_row) pairs for every already_buying subsidy project.

    Reads dotace_eu.jsonl - already on disk, no network. Two rows can
    share an ico if a company has more than one such project; each is
    checked independently, since the vendor decision is per-project.
    """
    out = []
    for ico, projects in load_subsidies().items():
        for project in projects:
            if classify_project(project["project"]) == "already_buying":
                out.append((ico, project))
    return out


def run(dry_run=False):
    """Check every already_buying project, verify the model's citations,
    and report the routing decision plus the real cost of getting it.
    """
    targets = already_buying_projects()
    print(f"already_buying projects to check: {len(targets)}", file=sys.stderr)

    if dry_run:
        for ico, project in targets:
            print(f"  {ico}  {project['project'][:70]}")
        return

    archive = Archive()
    llm = LLM()
    run_id = archive.start_run(note="subsidy_vendor agent")

    results = []
    for ico, project in targets:
        # The snapshot this claim is checked against is the company's
        # subsidy set already archived by dotace_eu.py --archive - the
        # same document the model is being asked to read, so the quote
        # it returns has something real to be found in.
        snapshot = archive.latest(ico, source="dotace_eu")
        if snapshot is None:
            print(f"  {ico}  SKIPPED - not archived, run dotace_eu.py --archive first",
                  file=sys.stderr)
            continue

        answer = llm.complete(
            PROMPT_NAME, PROMPT_VERSION, SYSTEM,
            user_prompt(project["project"], project["description"]),
            SCHEMA,
        )

        verified, summary = check_many(
            archive, ico, "subsidy_vendor_evidence", answer["evidence"],
            snapshot["id"], run_id,
        )

        routing = "already_buying (confirmed)" if answer["vendor_named"] else "OPEN - no vendor yet"
        results.append({
            "ico": ico, "project": project["project"][:70],
            "vendor_named": answer["vendor_named"], "vendor": answer["vendor"],
            "routing": routing, "evidence_summary": summary,
        })
        print(f"  {ico}  [{routing:24}] {project['project'][:60]}", file=sys.stderr)
        if answer["vendor_named"]:
            print(f"           vendor: {answer['vendor']}", file=sys.stderr)
        print(f"           evidence: {summary}", file=sys.stderr)

    archive.finish_run(run_id)

    from pipeline.llm.client import usage_summary
    print(f"\ntotal: {usage_summary()}", file=sys.stderr)

    open_leads = [r for r in results if not r["vendor_named"]]
    print(f"\n{len(open_leads)} of {len(results)} projects: budget confirmed, "
          f"vendor NOT named - these are the reclassified NOW signal", file=sys.stderr)

    archive.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Vendor-named check on already_buying subsidies.")
    parser.add_argument("--dry-run", action="store_true", help="list targets, no API calls")
    args = parser.parse_args()

    output = run(dry_run=args.dry_run)
    if output:
        print(json.dumps(output, ensure_ascii=False, indent=2))
