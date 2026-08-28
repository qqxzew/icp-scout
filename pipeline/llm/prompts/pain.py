"""Agent: pain signs from ICP section 2 ("Podle čeho ho poznám").

Research session 18 found the five signs split into two classes that
explain every earlier measurement (18.1): ABILITY (scale, certificates)
is a company's own showcase and it publishes that itself; DYSFUNCTION
(manual data entry, tacit know-how) is never published on purpose - a
showcase does not advertise its own breakage. That asymmetry is why
`Excel` in vacancy text scored near-zero precision (1.84 %, §4.3) and
why a bare word search cannot do this job: dysfunction only shows up as
an indirect trace, and reading comprehension is what tells a trace from
noise.

Three signs, not five, reach this agent:

    scale            sign 1 - vysoký počet jednotek k rozvržení. The one
                     quantitative sign; headcount already comes free
                     from ARES, so this agent only adds what a register
                     cannot see - a named machine park, shift count,
                     number of positions/crews mentioned in the text.

    manual_data      signs 2+3 collapsed into one, per §4 of the log -
                     "krabicové řešení nepokrývá proces" and "data
                     vznikají u lidí v provozu" are the same phenomenon
                     seen from two sides. The chain the ICP describes is
                     cesty -> paper/Excel/foreman -> a person -> ERP;
                     the person in the middle is the signal, not the
                     tool. ISCO 4322 already catches the structural
                     version of this (signals/now.py); this agent
                     catches the textual one - a job ad or a site page
                     describing that chain in its own words.

    tacit_knowledge  sign 4 - klíčové know-how drží jeden člověk v
                     hlavě. RTsoft's own strongest trigger and the
                     worst-evidenced by design (18.1): nobody writes
                     "we would fall apart if one person got sick." What
                     can appear: a long, informal handover process, a
                     role explicitly created because something used to
                     live in one head, a vacancy admitting the company
                     has no documented planning process yet.

Sign 5 (ISO) is left out on purpose - the log already found it does not
discriminate between companies at 50-200 employees (§4) and is picked
up for free from certificate PDFs already on the harvested pages,
without needing a model to read for it.

Same corpus, same verification, same cost discipline as
production_mode.py: website pages plus vacancy text, gpt-4.1-mini,
every quote checked against the actual archived documents before it is
allowed to become a fact.

Run:
    python -m pipeline.llm.prompts.pain --sample 15
"""

import argparse
import json
import sys
from collections import Counter

from pipeline.evidence.archive import Archive
from pipeline.evidence.verify import check_many_against_any
from pipeline.llm.client import LLM, usage_summary
from pipeline.llm.prompts.production_mode import TRUSTED_SITE_STATUS, gather_documents, user_prompt
from pipeline.scoring.select import WEBSITES, load_jsonl
from pipeline.sources.mpsv import load as load_vacancies

PROMPT_NAME = "pain"
PROMPT_VERSION = 1

SYSTEM = """Jsi analytik hledající v textu webu firmy a jejích \
pracovních inzerátů stopy tří konkrétních věcí. Firma sama tyto věci \
nikdy přímo neinzeruje - hledáš nepřímé stopy, ne prohlášení.

1) SCALE (vysoký počet jednotek k rozvržení)
   Hledej konkrétní čísla: počet strojů, směn, pracovišť, vozidel, \
   poboček, pozic v inzerátech. Čím víc jednotek firma zmiňuje, tím \
   složitější má plánovací úlohu. NEHODNOŤ celkový počet zaměstnanců - \
   ten už máme z registru; zajímá nás cokoli JINÉHO, co se musí \
   rozvrhovat.

2) MANUAL_DATA (ruční přenos dat mezi provozem a systémem)
   Hledej stopy řetězce provoz -> papír/Excel/mistr -> člověk -> \
   systém. Typické formulace: "vedení výrobní dokumentace", "ruční \
   zadávání dat", "papírové výkazy", "excelové tabulky pro evidenci \
   výroby", pozice typu "přípravář výroby", "koordinátor výroby", \
   "administrativní pracovník pro plánování". NEHODNOŤ zmínky o \
   tabletu, čtečce nebo GPS bez ručního zadávání - to dokazuje opak.

3) TACIT_KNOWLEDGE (klíčové know-how v jedné hlavě)
   Toto je nejslabší a nejvzácnější stopa - většinou nenajdeš nic, a to \
   je v pořádku. Hledej: dlouhé zaškolení popsané jako nutnost \
   ("zaškolení trvá až rok", "znalosti předávané ústně"), nově vzniklou \
   roli explicitně nahrazující jednoho člověka, nebo přímé přiznání \
   závislosti na jedné osobě.

Pravidla pro všechny tři kategorie:
- Vrať POUZE nálezy, pro které máš oporu v textu - u firmy, kde nic \
  není, vrať prázdné pole findings. Prázdný výsledek je platná a časná \
  odpověď, ne selhání.
- Každý nález nese DOSLOVNOU citaci v "quote". Citaci nikdy nezkracuj \
  třemi tečkami a nikdy nespojuj dvě nesousedící věty do jedné citace - \
  pokud chceš citovat dvě různé věty, vrať dva samostatné nálezy.
- Pole "quote" obsahuje POUZE samotný text ze zdroje, BEZ uvozovek na \
  začátku a na konci - pole samo o sobě už je citace, uvozovky nepřidávej.
- Pokud tvrzení je tvůj úsudek z kontextu (např. "firma pravděpodobně \
  spoléhá na jednoho vedoucího"), nastav "quote" na null."""

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sign": {"type": "string",
                             "enum": ["scale", "manual_data", "tacit_knowledge"]},
                    "value": {"type": "string"},
                    "quote": {"type": ["string", "null"]},
                },
                "required": ["sign", "value", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def run(sample_size=None, icos=None):
    archive = Archive()
    llm = LLM()
    vacancies_by_ico = load_vacancies()
    site_status = load_jsonl(WEBSITES)

    if icos is None:
        proven = {ico for ico, row in site_status.items() if row.get("status") in TRUSTED_SITE_STATUS}
        icos = [i for i in proven if i in vacancies_by_ico][:sample_size]

    print(f"companies in sample: {len(icos)}", file=sys.stderr)

    run_id = archive.start_run(note="pain agent")
    report = []

    for ico in icos:
        documents = gather_documents(archive, ico, vacancies_by_ico, run_id, site_status)
        if not documents:
            continue
        snapshot_ids = [snap_id for snap_id, _, _ in documents]

        answer = llm.complete(PROMPT_NAME, PROMPT_VERSION, SYSTEM,
                              user_prompt(documents), SCHEMA)

        verified, summary = check_many_against_any(
            archive, ico, "pain", answer["findings"], snapshot_ids, run_id,
        )

        signs_confirmed = sorted({
            item["sign"] for item, r in zip(answer["findings"], verified)
            if r["state"] == "fact"
        })
        report.append({"ico": ico, "documents": len(documents),
                       "signs": signs_confirmed, "evidence": summary})
        print(f"  {ico}  docs={len(documents)}  signs={signs_confirmed}  {summary}",
              file=sys.stderr)

    archive.finish_run(run_id)

    by_sign = Counter(s for row in report for s in row["signs"])
    print(f"\nsigns confirmed across {len(report)} companies: {dict(by_sign)}", file=sys.stderr)

    ev_totals = Counter()
    for row in report:
        for state, count in row["evidence"].items():
            ev_totals[state] += count
    print(f"evidence verification totals: {dict(ev_totals)}", file=sys.stderr)
    print(f"cost: {usage_summary()}", file=sys.stderr)

    archive.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LLM pain-signs agent.")
    parser.add_argument("--sample", type=int, default=15)
    parser.add_argument("--ico", nargs="*")
    args = parser.parse_args()

    output = run(sample_size=args.sample, icos=args.ico)
    print(json.dumps(output, ensure_ascii=False, indent=2))
