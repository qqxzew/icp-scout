"""The second layer: verified, and still beside the point.

evidence/verify.py proves that a sentence is on the page. That is the
one thing this project refuses to delegate to a model, and it stays that
way. But containment cannot answer the next question, and a finished run
showed exactly what that costs:

    pain  firma má výrobní haly o celkové kryté ploše 8.906 m2
    pain  Jednosměnný provoz
    pain  vedení zácviku nových pracovníků

All three quotes are real, found verbatim in an archived page, and none
of them is a pain sign. The first is floor area, not units to schedule;
the second is one shift, which is the opposite of a scheduling problem;
the third is onboarding. Filed as facts, printed on the card as the
reason to call, counted at six points each in the ranking.

So the judge asks one question the verifier cannot: does this quote
actually demonstrate the sign it was filed under. Three rules keep it
from becoming a second source of invention:

* IT MAY ONLY SUBTRACT. The verdict is written into claim.relevance and
  nothing else. A claim it rejects stays in the archive with its quote
  and its snapshot; it stops being counted and stops being printed as
  evidence. No verdict of this model can turn anything INTO a fact -
  only string containment does that.
* IT NEVER SEES THE PAGE, ONLY THE QUOTE. It is judging the statement
  that was already proven, not looking for new ones.
* IT LEANS TOWARDS KEEPING. Told to answer supports=true when unsure,
  because the failure it prevents (an irrelevant line on a card) is
  smaller than the one it could cause (hiding real evidence), and the
  salesperson can dismiss a weak line in a second.

Scope is deliberately narrow: pain claims only. production_mode carries
its side in the kind and is checked by fit_assessment against the ICP
already; turnover is a number. Ten companies a week, one call each.

Run:
    python -m pipeline.llm.prompts.relevance --ico 27975924
"""

import argparse
import json
import sys

from pipeline.evidence.archive import Archive, SUPPORTS, UNRELATED
from pipeline.llm.client import LLM, usage_summary
from pipeline.llm.prompts.pain import SIGNS

PROMPT_NAME = "relevance"
PROMPT_VERSION = 1

SYSTEM = """Rozhoduješ, zda ověřená citace ze zdroje skutečně dokládá \
konkrétní signál, pod kterým byla uložena.

Co NEPOSUZUJEŠ: zda je citace pravdivá nebo zda na stránce opravdu je - \
to už bylo ověřeno porovnáním s archivovanou stránkou.

Co posuzuješ: zda ta věta dokazuje daný signál u té firmy. Například \
u signálu "vysoký počet jednotek k rozvržení" je "24 CNC strojů ve \
dvousměnném provozu" doklad, kdežto "výrobní hala o ploše 8.906 m2" je \
údaj o ploše, ne o počtu jednotek, a "jednosměnný provoz" dokonce \
svědčí proti.

Pravidla:
- Odpověz supports=false jen tehdy, když je citace zjevně mimo signál. \
  Pokud váháš, odpověz supports=true - slabý doklad si obchodník \
  přebere sám, chybějící doklad už neuvidí.
- Do "why" napiš jednu krátkou větu česky, proč ano nebo ne.
- Vrať právě jeden verdikt ke každému číslu, které dostaneš."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "supports": {"type": "boolean"},
                    "why": {"type": "string"},
                },
                "required": ["id", "supports", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def to_judge(archive, ico):
    """This company's proven pain claims that nobody has judged yet.

    Already-judged claims are skipped rather than re-asked: the verdict
    is about a fixed pair of (statement, quote), which add_claim() treats
    as one claim across runs, so re-judging would pay for the same answer
    every week.

    Claims stored under the flat kind `pain`, from before the agent
    split its kinds by sign, are judged too. Their sign is lost, so the
    prompt asks whether the quote proves ANY of the three - which is
    exactly the question worth asking about them, and they are the ones
    most in need of it: "Jednosměnný provoz", filed as a pain sign, is
    one of those rows.
    """
    return [row for row in archive.claims(ico)
            if row["kind"].split(":", 1)[0] == "pain"
            and row["state"] == "fact"
            and row["quote"]
            and row["relevance"] is None]


def user_prompt(rows):
    """One numbered block per claim: the sign, the statement, the quote.

    Numbered from 1 rather than by claim id - the model has no business
    seeing database keys, and a hallucinated id would silently write a
    verdict onto the wrong claim. The numbers are mapped back here.
    """
    blocks = []
    for number, row in enumerate(rows, 1):
        _, _, sign = row["kind"].partition(":")
        if sign:
            asked = SIGNS.get(sign, sign)
        else:
            # A claim from before the kinds carried the sign. The
            # question becomes "does it prove any of them", which is
            # weaker but still the question that matters.
            asked = "kterýkoli z těchto signálů: " + "; ".join(SIGNS.values())
        blocks.append(
            f"[{number}] signál: {asked}\n"
            f"     tvrzení: {row['value']}\n"
            f"     citace ze zdroje: „{row['quote']}\""
        )
    return "\n\n".join(blocks)


def run(icos):
    archive = Archive()
    llm = LLM()
    run_id = archive.start_run(note="relevance judge")
    report = []

    for ico in icos:
        rows = to_judge(archive, ico)
        if not rows:
            continue

        answer = llm.complete(PROMPT_NAME, PROMPT_VERSION, SYSTEM,
                              user_prompt(rows), SCHEMA)
        verdicts = {item["id"]: item for item in answer["verdicts"]}

        supported = rejected = unjudged = 0
        for number, row in enumerate(rows, 1):
            verdict = verdicts.get(number)
            # A claim the model skipped keeps relevance NULL, and NULL
            # means "still counts" everywhere it is read. That is the
            # right way round: a question that was never answered must
            # not remove evidence, or a truncated response would quietly
            # empty a card.
            if verdict is None:
                unjudged += 1
                continue
            archive.set_relevance(
                row["id"], SUPPORTS if verdict["supports"] else UNRELATED,
                verdict.get("why"))
            if verdict["supports"]:
                supported += 1
            else:
                rejected += 1
                print(f"    unrelated: {(row['value'] or '')[:60]} — "
                      f"{verdict.get('why', '')[:60]}", file=sys.stderr)

        report.append({"ico": ico, "judged": len(rows), "supports": supported,
                       "unrelated": rejected, "unjudged": unjudged})
        print(f"  {ico}  judged={len(rows)}  supports={supported}  "
              f"unrelated={rejected}"
              + (f"  unjudged={unjudged}" if unjudged else ""), file=sys.stderr)

    archive.finish_run(run_id)
    total = sum(row["unrelated"] for row in report)
    print(f"\n{total} verified claims marked as beside the point "
          f"across {len(report)} companies", file=sys.stderr)
    print(f"cost: {usage_summary()}", file=sys.stderr)
    archive.close()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Second layer: is the proof on the point?")
    parser.add_argument("--ico", nargs="+", required=True)
    args = parser.parse_args()

    print(json.dumps(run([str(i).zfill(8) for i in args.ico]),
                     ensure_ascii=False, indent=2))
