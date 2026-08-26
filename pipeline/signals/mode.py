"""Production mode: does this company work to order, or to stock?

The ICP opens with it - "Zakázkový, ne sériový / opakovaný" - and no
register has the field. It is an inference by construction, and the
project's own note says so: a live salesperson also judges it by
reading the site.

Everything here is a pure function over text that somebody else fetched.
The module never decides whether a company is a good lead; it says what
the text claims about how they produce, and shows the sentence it read.

Three design decisions, each of which can be switched off and measured
rather than believed - see compare() and the CLI.

1. TWO SOURCES, NOT ONE, AND THEY ARE NOT EQUAL.
   The log records that website wording is marketing: "zakázková výroba
   / na míru / individuální přístup" is written by almost everyone,
   serial producers included, and three companies checked by hand gave
   0 clean answers. But the same words inside a *vacancy* were clean on
   inspection - because a job ad describes the work to a welder, while a
   homepage sells to a buyer. So a vacancy outranks a page.

2. STRUCTURAL TRACES, NOT ONLY THE WORDS.
   Direct statements cover 1.2 % of the base. But make-to-order leaves
   marks that are not claims about itself: work "dle výkresové
   dokumentace" is contract manufacturing by definition - you cannot
   machine to a customer's drawing and be a serial producer of your own
   catalogue. Serial leaves the opposite marks: stock, a price list, an
   e-shop.

3. THE VERDICT IS NOT BINARY.
   Real answers in the data include "pro standardní i zakázkovou
   výrobu" and "kusová i sériová" on one page. Forcing those into one
   box is a lie, and small-batch - which the ICP does want - is neither
   end. So `mixed`, `small_batch` and `unknown` are first-class answers.
"""

import re
import unicodedata

# Source tiers. Not numeric weights - a rank, so that a direct statement
# in a vacancy can never be outvoted by marketing on a front page.
VACANCY = "vacancy"
PRODUCTION_PAGE = "production_page"
HOMEPAGE = "homepage"
SOURCE_RANK = {VACANCY: 3, PRODUCTION_PAGE: 2, HOMEPAGE: 1}


def fold(text):
    """Lowercase, diacritics dropped - patterns are written folded."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(ch) != "Mn"
    ).lower()


def _p(*patterns):
    return [re.compile(p) for p in patterns]


# ---------------------------------------------------------------------------
# Direct statements: the company says what it does
# ---------------------------------------------------------------------------

# Deliberately demanding: the word must sit next to "výroba" in some
# form. Bare "zakázka" means an order and every firm has orders; bare
# "na míru" is the marketing phrase the log warns about, and it lives
# in STRUCTURAL below where it belongs.
DIRECT = {
    "made_to_order": _p(
        r"zakazkov\w*\s+vyrob\w*",
        r"vyrob\w*\s+na\s+zakazku",
        r"kusov\w*\s+vyrob\w*",
        r"jednokusov\w*",
        r"atypick\w*\s+vyrob\w*",
        r"kazd\w*\s+(projekt|zakazka)\s+je\s+jin\w*",
    ),
    "serial": _p(
        r"velkoseriov\w*",
        r"hromadn\w*\s+vyrob\w*",
        r"linkov\w*\s+vyrob\w*",
        r"pasov\w*\s+vyrob\w*",
        # plain "sériová výroba", but not the malo-/středně- compounds,
        # which are a different answer and are matched below
        r"(?<!malo)(?<!stredne)seriov\w*\s+vyrob\w*",
    ),
    "small_batch": _p(
        r"maloseriov\w*",
        r"stredneseriov\w*",
        r"mal\w*\s+seri\w*",
    ),
}

# One sentence claiming both ends at once. Stronger than finding the two
# words apart, because it is the company itself saying it is mixed.
BOTH_AT_ONCE = _p(
    r"kusov\w*\s+i\s+seriov\w*",
    r"seriov\w*\s+i\s+kusov\w*",
    r"standardn\w*\s+i\s+zakazkov\w*",
    r"zakazkov\w*\s+i\s+standardn\w*",
    r"zakazkov\w*\s+i\s+seriov\w*",
    r"seriov\w*\s+i\s+zakazkov\w*",
)

# ---------------------------------------------------------------------------
# Structural traces: consequences of the mode, not claims about it
# ---------------------------------------------------------------------------

STRUCTURAL = {
    "made_to_order": _p(
        # Machining to the customer's drawing is contract manufacturing
        # by definition - the design is not yours.
        r"dle\s+vykresov\w*\s+dokumentace",
        r"podle\s+vykres\w*",
        r"dle\s+dodan\w*\s+dokumentace",
        r"dle\s+prani\s+zakaznika",
        r"dle\s+pozadavk\w*\s+zakaznika",
        r"na\s+zaklade\s+pozadavk\w*",
        r"nezavazn\w*\s+poptavk\w*",
        r"poptavkov\w*\s+formular",
        r"jednoucelov\w*",
        r"individualn\w*\s+reseni",
        r"vyroba\s+na\s+miru",
    ),
    "serial": _p(
        r"\bskladem\b",
        r"na\s+sklade\b",
        r"\be-?shop\w*",
        r"nakupni\s+kosik",
        r"objednat\s+online",
        r"katalog\s+(vyrobku|produktu)",
        r"typov\w*\s+rada",
        r"standardni\s+sortiment",
        r"\bcenik\b",
    ),
}

SIDES = ("made_to_order", "serial", "small_batch")


# ---------------------------------------------------------------------------
# Reading one text
# ---------------------------------------------------------------------------


def scan(text, source, use_structural=True, width=90):
    """Every mode claim one text makes, with the sentence it came from.

    Returns a list of findings; a text can honestly produce several,
    including contradictory ones. Deciding what that means is classify's
    job, not this function's.
    """
    folded = fold(text)
    findings = []

    def add(side, kind, match):
        start = max(0, match.start() - width)
        end = min(len(text), match.end() + width)
        findings.append({
            "side": side,
            "kind": kind,
            "source": source,
            "matched": folded[match.start():match.end()],
            # The quote is taken from the ORIGINAL text, not the folded
            # copy: it has to be findable in the archived page later.
            "quote": " ".join(text[start:end].split()),
        })

    for match in (m for pattern in BOTH_AT_ONCE for m in pattern.finditer(folded)):
        add("mixed", "direct", match)

    for side, patterns in DIRECT.items():
        for pattern in patterns:
            for match in pattern.finditer(folded):
                add(side, "direct", match)

    if use_structural:
        for side, patterns in STRUCTURAL.items():
            for pattern in patterns:
                for match in pattern.finditer(folded):
                    add(side, "structural", match)

    return findings


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def classify(findings, use_structural=True, allow_mixed=True):
    """Turn findings into one verdict, keeping the evidence attached.

    Verdicts:

        made_to_order / serial / small_batch  a direct statement, uncontested
        mixed                                 both ends claimed directly
        leaning_made_to_order / leaning_serial   structural evidence only
        unknown                               nothing said either way

    `leaning_*` exists so that a consequence is never reported as a
    claim. A company whose site has a shopping basket probably sells
    from stock, but it never said so, and the card must not pretend it
    did.
    """
    if not use_structural:
        findings = [f for f in findings if f["kind"] == "direct"]

    verdict = {"mode": "unknown", "basis": None, "evidence": findings,
               "sources": sorted({f["source"] for f in findings})}
    if not findings:
        return verdict

    direct = [f for f in findings if f["kind"] == "direct"]

    if allow_mixed and any(f["side"] == "mixed" for f in direct):
        verdict.update(mode="mixed", basis="direct")
        return verdict

    # Among direct claims, the best-ranked source wins; a vacancy
    # outranks a page because a job ad is written to inform, not to sell.
    if direct:
        stated = {s for s in SIDES if any(f["side"] == s for f in direct)}
        if len(stated) > 1:
            if allow_mixed:
                verdict.update(mode="mixed", basis="direct")
                return verdict
            best = max(direct, key=lambda f: (SOURCE_RANK.get(f["source"], 0),
                                              f["side"] != "small_batch"))
            verdict.update(mode=best["side"], basis="direct")
            return verdict
        verdict.update(mode=stated.pop(), basis="direct")
        return verdict

    counts = {s: sum(1 for f in findings if f["side"] == s) for s in SIDES}
    if counts["made_to_order"] == counts["serial"]:
        verdict.update(mode="unknown", basis="structural_tie")
        return verdict
    side = "made_to_order" if counts["made_to_order"] > counts["serial"] else "serial"
    verdict.update(mode=f"leaning_{side}", basis="structural")
    return verdict


def read(documents, **options):
    """documents: iterable of (text, source). Returns one verdict."""
    findings = []
    for text, source in documents:
        findings += scan(text, source, options.get("use_structural", True))
    return classify(findings, **options)
