"""Agent: turnover as the company states it on its own website.

sbirka.py reads the filed accounts and reaches 16.5 % of companies -
the rest either file no P&L (small companies are not required to) or
file a scan with no text layer. But a company that keeps its turnover
out of the register will often put it on its own about page, because
there it is a selling point rather than an obligation. Measured on the
archive: 142 of 3166 companies with a harvested site (4.5 %) state a
turnover figure in prose.

WHY THIS IS NOT A REGEX. The same measurement surfaced three ways the
number on a page is not this company's turnover, and none of them are
distinguishable by pattern:

    "Chart tržby přesahují 1 miliardu USD"        the parent's figure
    "Holding enteria spojuje ... s ročním         the group's figure
     obratem téměř 10 mld. Kč"
    "v roce 2019 ... obrat činil přes 172 mil."   a figure from 2019

A regex reading any of those would put a confident, wrong number on a
card - and turnover is exactly the field a salesperson repeats out loud
first. So the regex only nominates candidates (cheap, over archived
text, no network), and the model decides whose figure it is and for
what year. Every answer still carries a quote that has to be found in
the archived page, so a fabricated figure cannot survive.

The scope field is the point of the whole agent: `group` is not a
failure, it is a different true fact, and the card can say "group
turnover 10 bn" rather than silently attributing it to this ICO. Same
distinction the log already had to make for domains owned by a parent
(whois_org_group).

Run:
    python -m pipeline.llm.prompts.turnover_web --scan
    python -m pipeline.llm.prompts.turnover_web --all
"""

import argparse
import json
import re
import sys
from collections import Counter

from pipeline.evidence.archive import Archive
from pipeline.evidence.verify import check_many_against_any
from pipeline.llm.client import LLM, usage_summary

PROMPT_NAME = "turnover_web"
PROMPT_VERSION = 1

# Deliberately generous - this only nominates a page for reading, and a
# missed candidate is a company we never look at. Precision is the
# model's job, not this pattern's.
CANDIDATE = re.compile(
    r"(?i)(obrat|tr[žz]by|v[ýy]nosy|turnover|revenue)[^.]{0,60}?"
    r"(\d[\d\s.,]{0,12})\s*(mil|mld|tis|milion|miliard|m\.?\s*K[čc]|K[čc]|EUR|USD)"
)

# How much text around the match the model gets to read. Wide enough to
# carry the subject of the sentence - which is the whole question when
# deciding whether "obrat 10 mld." belongs to this company or its group.
CONTEXT = 400

SYSTEM = """Jsi analytik, který v textu z webu firmy hledá údaj o \
obratu (tržbách) a určuje, KOMU ten údaj patří a za JAKÝ ROK platí.

V zadání vždy dostaneš na prvním řádku NÁZEV ANALYZOVANÉ FIRMY. Ten je \
jediné vodítko, podle kterého poznáš, zda je nalezený obrat její.

Pravidla:
- "scope" nastav na "company" pouze tehdy, když údaj patří firmě \
  uvedené v zadání. Pokud věta mluví o holdingu, skupině, mateřské či \
  sesterské společnosti, nastav "group". Pokud mluví o ZCELA JINÉ \
  jmenované firmě - typicky zákazníkovi, dodavateli nebo partnerovi \
  zmíněnému v referencích - nastav "unknown" a rozhodně ne "company". \
  Web firmy běžně jmenuje cizí firmy a uvádí u nich jejich čísla.
- "period" rozlišuje ROČNÍ obrat od SOUHRNNÉHO za více let. Věta \
  "od roku 1990 jsme dosáhli celkového obratu 3 miliardy" NENÍ roční \
  obrat - nastav "cumulative". Roční obrat je "annual". Když to z textu \
  nelze určit, nastav "unknown".
- "year" vyplň jen tehdy, je-li rok v textu uveden. Nikdy rok nehádej \
  a nedopočítávej.
- "value" přepiš přesně tak, jak je číslo v textu (např. "1,4 miliardy \
  korun", "160 až 180 milionů Kč"). Nepřevádět, nezaokrouhlovat.
- "currency" podle textu: CZK, EUR, USD.
- Pokud text žádný obrat neuvádí, vrať prázdné pole findings.
- Každý nález nese DOSLOVNOU citaci v "quote", zkopírovanou znak po \
  znaku z předloženého textu. Citaci nikdy nezkracuj třemi tečkami, \
  nespojuj nesousedící věty a nepřidávej uvozovky navíc."""

SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "value": {"type": "string"},
                    "currency": {"type": "string", "enum": ["CZK", "EUR", "USD"]},
                    "year": {"type": ["string", "null"]},
                    "scope": {"type": "string", "enum": ["company", "group", "unknown"]},
                    # A lifetime total reads exactly like an annual figure on a
                    # card and is ~20x larger. Measured live: one company's
                    # "od roku 1990 ... celkového obratu 3 miliardy" came back
                    # beside its real annual "160 až 180 milionů".
                    "period": {"type": "string", "enum": ["annual", "cumulative", "unknown"]},
                    "quote": {"type": "string"},
                },
                "required": ["value", "currency", "year", "scope", "period", "quote"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def candidates(archive, ico):
    """(snapshot_id, url, excerpt) for every archived page naming a turnover.

    Reads only what website.py already stored - no network, so scanning
    the whole base costs nothing but disk.
    """
    out = []
    for row, text in archive.documents(ico, source="website"):
        if not text:
            continue
        match = CANDIDATE.search(text)
        if not match:
            continue
        start = max(0, match.start() - CONTEXT // 2)
        end = min(len(text), match.end() + CONTEXT // 2)
        out.append((row["id"], row["url"], text[start:end]))
    return out


def scan(archive):
    """Every company whose site states a turnover figure. No API calls."""
    icos = [r["ico"] for r in archive.db.execute(
        "SELECT DISTINCT ico FROM snapshot WHERE source='website'"
    ).fetchall()]
    return [(ico, found) for ico in icos if (found := candidates(archive, ico))]


def user_prompt(excerpts, name=None):
    """The excerpts, headed by whose website this is.

    The company name is not decoration. Without it the first version
    read "Chart tržby přesahují 1 miliardu USD" off Howden ČKD
    Compressors' own reference page and labelled it that company's
    turnover - the model had no way to know whose site it was reading,
    so "is this figure theirs?" was a question it could not answer.
    """
    header = f"ANALYZOVANÁ FIRMA: {name}\n\n" if name else ""
    parts = [f"=== ZDROJ: {url} ===\n{text}" for _, url, text in excerpts]
    return header + "\n\n".join(parts)


def run(limit=None, icos=None):
    archive = Archive()
    llm = LLM()

    from pipeline.signals.now import ARES_CANDIDATES, load_companies
    names = {c["ico"]: c.get("name") for c in load_companies(ARES_CANDIDATES)}

    targets = scan(archive)
    if icos:
        targets = [(i, c) for i, c in targets if i in icos]
    if limit:
        targets = targets[:limit]
    print(f"companies whose site names a turnover: {len(targets)}", file=sys.stderr)

    run_id = archive.start_run(note="turnover_web agent")
    report, scopes, states = [], Counter(), Counter()

    for ico, excerpts in targets:
        answer = llm.complete(PROMPT_NAME, PROMPT_VERSION, SYSTEM,
                              user_prompt(excerpts[:3], names.get(ico)), SCHEMA)
        snapshot_ids = [snap for snap, _, _ in excerpts]

        items = [{"value": f"{f['value']} ({f['currency']}"
                           + (f", {f['year']}" if f.get('year') else "")
                           + f", {f['scope']}, {f['period']})",
                  "quote": f["quote"]} for f in answer["findings"]]
        verified, summary = check_many_against_any(
            archive, ico, "turnover_web", items, snapshot_ids, run_id)

        for finding, result in zip(answer["findings"], verified):
            scopes[finding["scope"]] += 1
            states[result["state"]] += 1
            if result["state"] == "fact":
                print(f"  {ico}  [{finding['scope']:7}|{finding['period']:10}] "
                      f"{finding['value']} {finding['currency']} "
                      f"{finding.get('year') or ''}", file=sys.stderr)

        report.append({"ico": ico, "findings": answer["findings"], "evidence": summary})

    archive.finish_run(run_id)
    print(f"\nscope: {dict(scopes)}", file=sys.stderr)
    print(f"verification: {dict(states)}", file=sys.stderr)
    print(f"cost: {usage_summary()}", file=sys.stderr)
    archive.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Turnover stated on a company's own site.")
    parser.add_argument("--scan", action="store_true", help="count candidates, no API calls")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ico", nargs="*")
    args = parser.parse_args()

    if args.scan:
        archive = Archive()
        found = scan(archive)
        print(f"{len(found)} companies name a turnover on their site")
        for ico, excerpts in found[:20]:
            print(f"  {ico}  {len(excerpts)} page(s)")
        archive.close()
    else:
        output = run(limit=args.limit, icos=args.ico)
        print(json.dumps(output, ensure_ascii=False, indent=2))
