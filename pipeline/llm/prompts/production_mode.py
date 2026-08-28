"""Agent: production mode (zakázková / sériová / mixed), read by the model
instead of matched by regex - and measured against the regex baseline
that already exists in signals/mode.py.

That baseline (built in research session 18.2) settled three design
questions the hard way: a vacancy outranks a website page because it is
written to inform a welder, not to sell to a buyer; structural traces
("dle výkresové dokumentace") only reached 53 % accuracy against direct
statements because a shopping-cart link in a site's navigation menu is
not evidence about the company; and the verdict is not binary - 20 % of
companies with a direct statement claim both ends at once, honestly.

This agent keeps all three lessons rather than re-learning them:

* both vacancies and website pages are read, vacancies are not weighted
  above pages in this agent's prompt (the model is asked to read
  context, not to apply a source-rank rule a regex needed instead)
* the model is asked for evidence, not a verdict - it returns
  {"side": ..., "value": ..., "quote": ...} items, and the aggregate
  label (made_to_order / serial / mixed / unknown) is computed from
  whatever survives evidence/verify.py, the same way classify() derives
  it from scan() findings in the regex module
* "mixed" and "unknown" are first-class outcomes, never forced into one
  box

WHY VACANCY TEXT NEEDS ITS OWN PLAIN SNAPSHOT, NOT THE EXISTING mpsv ONE:
mpsv.py archives a company's vacancies as one JSON blob (source="mpsv").
A real newline inside a vacancy's free text becomes the two literal
characters backslash-n inside that JSON string. archive.normalize()
collapses whitespace via \\s+, which matches an actual newline but not
the two-character sequence \\n - so a quote spanning a paragraph break,
which the model sees with a real newline because Python decoded the
JSON before building the prompt, would silently fail verification
against the raw JSON snapshot. Same class of bug as the non-breaking
space in dotace_eu.py and sbirka.py, caught here before it shipped
rather than after. The fix is to archive vacancy text as its own plain
document (source="mpsv_text"), built the same way website pages are -
readable prose, not an escaped structure.

Run:
    python -m pipeline.llm.prompts.production_mode --sample 15
"""

import argparse
import json
import sys
from collections import Counter

from pipeline.evidence.archive import Archive, digest, normalize
from pipeline.evidence.verify import check_many_against_any
from pipeline.llm.client import LLM, usage_summary
from pipeline.scoring.select import WEBSITES, load_jsonl
from pipeline.sources.mpsv import load as load_vacancies, texts as vacancy_texts

# website.py's own status vocabulary: proven (domain ownership proved),
# probable (best guess, unproven), not_found / no_lead (no usable
# domain). Only "proven" may back a claim about THIS ico - anything
# weaker is a guess that could be, and per website.py's own measurement
# sometimes is (46% of resolved guesses), a different company's page.
# Found live: 09938249's only archived "website" snapshot turned out to
# be owp.cz - a publisher matched on initials to "Orsman. WB personal
# s.r.o.", harvested before its domain was rejected, but the harvested
# page itself was never deleted from the archive. gather_documents()
# used to read it anyway.
TRUSTED_SITE_STATUS = {"proven"}

PROMPT_NAME = "production_mode"
PROMPT_VERSION = 1

# Keeps cost predictable and low - gpt-4.1-mini is priced by the token.
# 2500 was a guess and it was measured wrong: the median harvested page
# is 3129 characters, so the median page was being cut, and across the
# archive the cap discarded 173 of 220 million characters - 79 % of text
# already paid for and stored. An A/B on five companies (2500 vs 8000)
# returned 16 vs 18 verified facts for less than a cent of extra spend,
# so the cap was buying nothing it cost. 8000 sits just under the 90th
# percentile of page length (9678), which keeps whole pages whole
# without letting one enormous page eat a prompt.
MAX_CHARS_PER_DOCUMENT = 8000
MAX_DOCUMENTS = 6

# Which pages are worth a slot when there are more than MAX_DOCUMENTS.
# Measured need: OK Záchlumí and TNS SERVIS both have 10 harvested URLs,
# so four of them are dropped on every call and WHICH four used to be
# decided by whatever order the archive returned - alphabetical by URL,
# which is not a statement about usefulness. Ordered here instead:
# career and production carry how the company works, contact carries
# only the proof that the domain is theirs, which this stage no longer
# needs because website.py already settled it.
KIND_PRIORITY = ("career", "production", "about", "certificates",
                 "home", "references", "contact")

# At most this many pages of any one kind. Three /sluzby/* pages say
# roughly the same thing three times, and on TNS SERVIS they crowded out
# /o-nas/o-spolecnosti entirely - the page carrying "Roční produkce
# 15 mil. ks", which is the single strongest scale fact that company
# publishes. Breadth beats depth when the budget is six documents.
MAX_PER_KIND = 2

SYSTEM = """Jsi analytik, který z textu webu firmy a jejích pracovních \
inzerátů zjišťuje, jakým způsobem firma vyrábí: na zakázku \
(zakázková/kusová výroba), sériově, nebo malosériově - případně obojí \
najednou, což je zcela běžné a NENÍ třeba to násilně zjednodušovat na \
jednu odpověď.

Pravidla:
- Hledej jak přímá tvrzení ("zakázková výroba", "vyrábíme na zakázku", \
  "sériová výroba"), tak nepřímé stopy (práce "dle výkresové \
  dokumentace zákazníka" = zakázková výroba ze své podstaty; sklad, \
  ceník, e-shop, "skladem" = sériová výroba).
- Formulace jako "individuální přístup" nebo "na míru" na hlavní \
  stránce webu jsou často marketing, ne popis výroby - takové tvrzení \
  uveď, ale označ jako slabší (side zůstává stejné, ale bez quote, \
  pokud nejde o jednoznačnou citaci).
- Text z pracovního inzerátu je psán pro uchazeče o práci, ne pro \
  zákazníka - proto bývá spolehlivější než stránka webu, ale NENÍ \
  automaticky pravdivější, posuzuj obsah, ne zdroj.
- Pokud firma na různých místech tvrdí obojí (zakázková i sériová), \
  vrať OBA nálezy - to je platná a častá odpověď, ne rozpor k vyřešení.
- Každý nález musí nést DOSLOVNOU citaci z textu ve "quote". Pokud \
  citaci nemáš (jde o tvůj úsudek z kontextu, ne z konkrétní věty), \
  nastav "quote" na null - nikdy si citaci nevymýšlej ani ji nezkracuj \
  třemi tečkami."""

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "side": {"type": "string",
                             "enum": ["made_to_order", "serial", "small_batch"]},
                    "value": {"type": "string"},
                    "quote": {"type": ["string", "null"]},
                },
                "required": ["side", "value", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def vacancy_document(archive, ico, vacancies_by_ico, run_id=None):
    """Archive this company's vacancy text as plain prose, return its snapshot.

    archive.store() is written for real re-fetches from the network and
    always inserts a new row, on purpose - that is how a weekly run's
    "did this page change" signal works. This call is not a re-fetch: it
    re-derives the same plain text from already-downloaded vacancy JSON,
    so calling it again within the same or a later pipeline run must not
    look like a new visit. If the latest "mpsv_text" snapshot for this
    company already has this exact content, its id is reused instead of
    minting a new row - otherwise every repeated run would hand a claim
    a different snapshot_id for identical text, defeating add_claim()'s
    content-based idempotency in archive.py.
    """
    vacancies = vacancies_by_ico.get(ico, [])
    if not vacancies:
        return None
    blob = "\n\n---\n\n".join(f"{title}\n{text}" for text, title in vacancy_texts(vacancies))
    if not blob.strip():
        return None

    existing = archive.latest(ico, source="mpsv_text")
    if existing is not None and existing["sha256"] == digest(normalize(blob)):
        return existing["id"], blob

    snapshot_id, _ = archive.store(ico, "mpsv_text", blob, run_id=run_id)
    return snapshot_id, blob


def gather_documents(archive, ico, vacancies_by_ico, run_id=None, site_status=None):
    """Every readable document for one company, capped for cost.

    Returns [(snapshot_id, label, text)]. Website pages come from the
    single harvest() pass in website.py - already plain text, already
    archived, no network call here - but only when website.py's FINAL
    verdict for this ico is "proven" (see TRUSTED_SITE_STATUS above); a
    stale snapshot from a domain guess that was later rejected must not
    silently keep contributing "facts" about the wrong company. Callers
    that skip `site_status` get every archived website page, unfiltered
    - useful for ad-hoc debugging, never for a real run.
    Vacancy text is built and archived on demand by vacancy_document().
    """
    pages = []
    status = (site_status or {}).get(ico, {}).get("status")
    if site_status is None or status in TRUSTED_SITE_STATUS:
        for row, text in archive.documents(ico, source="website"):
            if text:
                pages.append((row["id"], row["kind"] or "website", text[:MAX_CHARS_PER_DOCUMENT]))

    def rank(page):
        kind = page[1]
        return KIND_PRIORITY.index(kind) if kind in KIND_PRIORITY else len(KIND_PRIORITY)

    pages.sort(key=rank)

    per_kind, spread = {}, []
    for page in pages:
        kind = page[1]
        if per_kind.get(kind, 0) >= MAX_PER_KIND:
            continue
        per_kind[kind] = per_kind.get(kind, 0) + 1
        spread.append(page)
    pages = spread

    # The vacancy slot is reserved, not appended. Appending it last and
    # then truncating the list is what the first version did, and on a
    # company with six or more harvested pages that silently threw the
    # vacancy away - measured at 220 of 400 companies. It is the source
    # the log rates highest (18.2: written to inform an applicant, not
    # to sell to a buyer), so losing it to a page-count accident is the
    # opposite of the intended trade.
    vacancy = vacancy_document(archive, ico, vacancies_by_ico, run_id)
    if not vacancy:
        return pages[:MAX_DOCUMENTS]

    snapshot_id, blob = vacancy
    return ([(snapshot_id, "vacancy", blob[:MAX_CHARS_PER_DOCUMENT])]
            + pages[:MAX_DOCUMENTS - 1])


def user_prompt(documents):
    parts = [f"=== ZDROJ: {label} ===\n{text}" for _, label, text in documents]
    return "\n\n".join(parts)


def run(sample_size=None, icos=None):
    """Run the agent over a sample of companies, verify, and report.

    Companies are chosen from those that already have both a harvested
    website and vacancy data - anything else has nothing for this agent
    to read, and would only measure "no documents" rather than the
    agent itself.
    """
    archive = Archive()
    llm = LLM()
    vacancies_by_ico = load_vacancies()
    site_status = load_jsonl(WEBSITES)

    if icos is None:
        proven = {ico for ico, row in site_status.items() if row.get("status") in TRUSTED_SITE_STATUS}
        icos = [i for i in proven if i in vacancies_by_ico][:sample_size]

    print(f"companies in sample: {len(icos)}", file=sys.stderr)

    run_id = archive.start_run(note="production_mode agent")
    report = []

    for ico in icos:
        documents = gather_documents(archive, ico, vacancies_by_ico, run_id, site_status)
        if not documents:
            continue
        snapshot_ids = [snap_id for snap_id, _, _ in documents]

        answer = llm.complete(PROMPT_NAME, PROMPT_VERSION, SYSTEM,
                              user_prompt(documents), SCHEMA)

        verified, summary = check_many_against_any(
            archive, ico, "production_mode", answer["findings"], snapshot_ids, run_id,
        )

        sides_confirmed = {r["value"] for r in verified if r["state"] == "fact"}
        sides = {item["side"] for item, r in zip(answer["findings"], verified)
                 if r["state"] in ("fact", "inference")}

        report.append({
            "ico": ico, "documents": len(documents),
            "sides": sorted(sides), "evidence": summary,
        })
        print(f"  {ico}  docs={len(documents)}  sides={sorted(sides)}  {summary}",
              file=sys.stderr)

    archive.finish_run(run_id)

    totals = Counter()
    for row in report:
        totals[tuple(row["sides"]) or ("unknown",)] += 1
    print(f"\nlabel distribution across {len(report)} companies:", file=sys.stderr)
    for label, count in totals.most_common():
        print(f"  {label}: {count}", file=sys.stderr)

    ev_totals = Counter()
    for row in report:
        for state, count in row["evidence"].items():
            ev_totals[state] += count
    print(f"\nevidence verification totals: {dict(ev_totals)}", file=sys.stderr)
    print(f"\ncost: {usage_summary()}", file=sys.stderr)

    archive.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM production-mode agent.")
    parser.add_argument("--sample", type=int, default=15)
    parser.add_argument("--ico", nargs="*", help="specific ICOs instead of a sample")
    args = parser.parse_args()

    output = run(sample_size=args.sample, icos=args.ico)
    print(json.dumps(output, ensure_ascii=False, indent=2))
