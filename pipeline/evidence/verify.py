"""The verifier: code checks the model, never the other way round.

This is the architectural centrepiece the log calls out in section 1.
A model returns a statement plus, optionally, a quote. What happens next
is a decision tree with three branches, and every branch is decided by
plain string containment - not by asking the model to grade itself, not
by a second model as critic:

    quote given, found in the archived snapshot     -> fact
    quote given, NOT found in the archived snapshot  -> discard
    no quote given, statement is declared an inference -> inference

There is no fourth branch where a missing quote quietly becomes a fact,
and no branch where "the model sounded confident" counts for anything.
The only way a statement reaches the salesperson labelled a fact is a
successful substring search.

WHAT THIS CANNOT DO, AND WHAT WAS ADDED BESIDE IT. Containment proves
that a sentence is on the page; it says nothing about whether the
sentence answers the question that was asked, and a run put "Jednosměnný
provoz" on a card as proof of a pain sign for exactly that reason. Two
guards sit next to the three branches, further down this file, and
neither of them touches the fact/inference/discard decision above:
states_absence() refuses to count a quoteless "there is no mention of X"
as evidence of X, and unrelated() reads the verdict of the second-layer
judge in llm/prompts/relevance.py. The judge may only subtract - it can
mark a verified fact as beside the point, never turn anything into a
fact - so the rule that only string containment produces a fact still
holds.

Why exact containment and not fuzzy matching: a looser comparison would
have to be tuned, and there is no ground truth to tune it against yet.
Exact matching after normalisation is the only version of this that
needs no calibration and cannot be gamed by paraphrasing - the trade a
looser match makes is exactly the one this project cannot afford, per
its own stated position on hallucination.

Both sides of the comparison go through archive.normalize() - the same
function the text was stored with. Skipping this is the bug dotace_eu.py
and sbirka.py both hit already: a non-breaking space between two words
is invisible on screen and makes a plain equality check fail silently,
even though the quote is, to a human eye, right there.

ONE MORE THING NORMALISED, FOUND BUILDING pain.py: the model sometimes
wraps its own citation in literal quote-mark punctuation - '"text"' as
the field value, not just text - because it is typographically marking
"this is a quotation", the way a person writing prose would, without
being asked to. Measured live: 46 of 48 discards in pain.py's first
15-company sample carried this wrapping, and 36 of those 46 were,
underneath the added punctuation, verbatim in the source. That is not a
paraphrase and not a fabrication - it is the model's own citation
marks, which by definition cannot appear in a page that does not know
it is being quoted. find_quote() strips exactly one layer of that
wrapping as a fallback, after the unwrapped form already failed - still
an exact containment check on the actual content, just tolerant of
punctuation the model added around it. The stored claim always carries
the form confirmed present in the text, never the model's wrapped one.

Run:
    python -m pipeline.evidence.verify --demo
"""

import argparse
import re

from pipeline.evidence.archive import Archive, SUPPORTS, UNRELATED, normalize

# Characters the model uses to typographically mark its own citation -
# straight, Czech-style „low" / "high", and curly single/double. Checked
# independently on each end (not paired) since the model is not
# consistent about which style it reaches for.
_WRAPPING_QUOTE_CHARS = '"„“”‘’\''

# "…" and "..." at the very end versus anywhere else. The position is
# the whole distinction: at the end it means the sentence continues, in
# the middle it means two pieces of text were joined that are not
# adjacent in the source.
_TRAILING_ELLIPSIS = re.compile(r"(?:\.{2,}|…)\s*$")
_INTERNAL_ELLIPSIS = re.compile(r"(?:\.{2,}|…)")

# A single closing full stop, and nothing else. Same instinct as the
# wrapping quote marks above - the model finishes its citation the way a
# sentence is finished, whether or not the page does. Measured: discard
# 172 of run 150 was 265 of its 266 characters verbatim and failed on a
# final "." where the source has a comma.
#
# Only ONE character and only a full stop: a comma or a colon at the end
# of a quote can be real text the model copied correctly, and stripping
# those would start repairing quotes rather than tolerating punctuation.
# Two dots or more are an ellipsis and belong to the rule above, so the
# negative lookbehind keeps this from eating into one.
_TRAILING_STOP = re.compile(r"(?<!\.)\.\s*$")


def _unwrap(quote):
    """Strip one layer of wrapping quote-mark punctuation, if present."""
    if quote and len(quote) >= 2 and quote[0] in _WRAPPING_QUOTE_CHARS \
            and quote[-1] in _WRAPPING_QUOTE_CHARS:
        return quote[1:-1]
    return None


def find_quote(quote, snapshot_text):
    """The form of `quote` actually present in `snapshot_text`, or None.

    Three attempts, each strictly a containment check, each justified by
    a false negative found by auditing every discard in the archive by
    hand (5 of them; 3 turned out to be true statements thrown away):

    1. the quote as given
    2. one layer of wrapping quote-mark punctuation stripped - the model
       marks its own citation typographically (see module docstring)
    2b. a single trailing full stop removed, for the same reason: the
       model ends its citation like a sentence even where the page runs
       on. Measured on run 150's discards, one of nine was this and
       nothing else - 265 of 266 characters verbatim, rejected on a "."
       the source spells as ",". Never more than one dot, so an ellipsis
       stays the splice it is.
    3. case-insensitively. TNS SERVIS's discard differed from the source
       by exactly one character: the model wrote "vyvíjíme" where the
       page began a sentence with "Vyvíjíme". 130 characters identical,
       rejected on a capital letter. Case-folding cannot turn an
       invention into a match - two texts that differ only in case say
       the same thing - so this costs nothing and recovers real
       evidence.

    What is deliberately NOT done: matching a quote that contains an
    internal ellipsis. That is the splice - REMET's discard joined one
    real address to two that appear nowhere, using "..." as the joint -
    and accepting it would let a model assemble a plausible sentence out
    of parts that never sat together. A trailing ellipsis is honest
    truncation; an internal one is construction. The distinction is the
    difference between quoting and writing.

    The stored claim always carries the form confirmed present in the
    text, never the model's decorated version.
    """
    if not quote or not snapshot_text:
        return None

    normalized_text = normalize(snapshot_text)
    candidates = [quote]
    unwrapped = _unwrap(quote)
    if unwrapped:
        candidates.append(unwrapped)

    # A trailing ellipsis is the model saying "and it continues" - the
    # text before it is still a real, contiguous span. An internal one
    # is the model joining passages that never sat together, so a quote
    # carrying one is never repaired, only rejected.
    for candidate in list(candidates):
        trimmed = _TRAILING_ELLIPSIS.sub("", candidate).rstrip()
        if trimmed != candidate and not _INTERNAL_ELLIPSIS.search(trimmed):
            candidates.append(trimmed)

    # After the ellipsis pass, so a quote ending in "..." is handled as a
    # truncation rather than losing one of its dots to this.
    for candidate in list(candidates):
        without_stop = _TRAILING_STOP.sub("", candidate).rstrip()
        if without_stop != candidate:
            candidates.append(without_stop)

    for candidate in candidates:
        if normalize(candidate) in normalized_text:
            return candidate

    lowered_text = normalized_text.lower()
    for candidate in candidates:
        normalized = normalize(candidate)
        position = lowered_text.find(normalized.lower())
        if position >= 0:
            # Return what the SOURCE says, not what the model wrote: the
            # quote on the card should read as the page reads.
            return normalized_text[position:position + len(normalized)]
    return None


def quote_found(quote, snapshot_text):
    """Whether `quote` appears verbatim in `snapshot_text` (see find_quote)."""
    return find_quote(quote, snapshot_text) is not None


# ---------------------------------------------------------------------------
# Statements that assert nothing
# ---------------------------------------------------------------------------
#
# A model asked to find pain signs answers, quite reasonably, when it
# finds none - "na webu není zmínka o ručním převodu dat". That sentence
# has no quote, so check() files it as an inference, the card prints it
# under the company's pain evidence, and select.py scores it. Measured on
# the last full run: four of the five inferences on the top card were
# statements that the pain is NOT there, and the company was ranked
# higher for each of them. Absence of a signal became evidence of one.
#
# The shape they share is that they are about the DOCUMENT, not about the
# company: a negation followed by a word for mentioning or stating. Real
# pain phrased negatively - "plánování není nikde zdokumentováno",
# "výroba není řízena systémem" - is about the company and does not
# match, which is the distinction this pattern is built on.
_ABSENCE_NEGATION = (r"(?:není|nejsou|nebyl\w*|chybí|nelze|nemá|nemáme|"
                     r"nenalez\w*|neuvád\w*|nezmiňuj\w*|neobsahuj\w*)")
_ABSENCE_OBJECT = (r"(?:uveden\w*|zmín\w*|zmiňuj\w*|explicit\w*|doložen\w*|doklad\w*|"
                   r"informac\w*|údaj\w*|patrn\w*|dostupn\w*|nalezen\w*|k dispozici)")
_ABSENCE = re.compile(rf"(?i)\b{_ABSENCE_NEGATION}\b(?:\W+\w+){{0,4}}?\W+{_ABSENCE_OBJECT}")
# Verbs that already carry the whole statement - "text neuvádí", "inzerát
# nezmiňuje" - and need no object to be an assertion about the source.
_ABSENCE_VERB = re.compile(r"(?i)\b(?:nezmiňuj\w*|neuvád\w*|neobsahuj\w*|nenalez\w*)\b")


def states_absence(value):
    """Whether a statement says the evidence is missing rather than what it is."""
    text = value or ""
    return bool(_ABSENCE.search(text) or _ABSENCE_VERB.search(text))


# ---------------------------------------------------------------------------
# Evidence that argues the other way
# ---------------------------------------------------------------------------
#
# A quote can be genuinely on the page, genuinely about the company, and
# still be evidence AGAINST the sign it was filed under. Single-shift
# working is the one case where that is decidable without reading the
# context: sign 1 is "a high number of units to schedule in time", and a
# company running one shift has fewer units to schedule, not more. It
# reached a card three times - "Jednosměnný provoz", "Práce na jednu
# směnu." - twice marked `supports` by the relevance judge, which is why
# this is code and not another line in a prompt.
#
# Deliberately the ONLY rule of its kind here, and deliberately scoped to
# pain:scale. Everything else that looked like boilerplate turned out to
# depend on context that a pattern cannot see: "Výkresy zasílejte na:
# vypocty@betonpres.cz" is mostly an e-mail address and is also a real
# trace of a manual process, and "PO ZAUČENÍ případný postup na pozici
# zástupce vedoucího" is a training platitude wrapped around a new
# deputy role, which is sign 4 itself. Those belong to the second-layer
# judge in llm/prompts/relevance.py, which reads meaning; this one line
# is here because no amount of surrounding text makes one shift into
# many units.
_SINGLE_SHIFT = re.compile(r"(?i)(jednosměnn\w*|jedn[uú]\s*směn\w*|jedna\s*směn\w*)")

# The second case, and the last one: a training platitude offered as
# proof that key know-how lives in one person's head. "Zaučení zkušeným
# kolegou", "Při nástupu vás důkladně zaškolíme" - every second Czech job
# ad says this, so it separates no company from any other, which is the
# §13 hole ("the sign fires at 45 % of companies") in one line.
#
# Both the agent's prompt and the judge's prompt name this case and ask
# for it to be left out. Both still let it through, which is precisely
# when a rule belongs in code: measured across the archive's 20 quoted
# tacit_knowledge claims, seven were this.
_TRAINING = re.compile(r"(?i)(zauč|zaškol|zaprac|zácvik|proškol)")
# What makes a mention of training a finding rather than a platitude: a
# stated duration, knowledge passed on by mouth, or a role defined
# against a specific person. Measured on the same 20: this keeps "PO
# ZAUČENÍ případný postup na pozici zástupce vedoucího" - a deputy role
# appearing, which is sign 4 itself - and "První týdny u nás...", while
# dropping the seven that say only that training exists.
_TRAINING_SPECIFIC = re.compile(
    r"(?i)(\d|měsíc\w*|rok\w*|týdn\w*|ústně|ústní|předáván\w*|"
    r"zástupc\w*|nástupc\w*|jedin\w*|odchod\w*)")


def argues_against(kind, quote):
    """Whether a quote fails the sign it was filed under, regardless of context.

    Two rules only, both scoped to one sign, both measured. Everything
    else that looked like boilerplate turned out to need the context a
    pattern cannot see, and belongs to the judge in
    llm/prompts/relevance.py instead.
    """
    if not quote:
        return False
    if kind == "pain:scale":
        # One shift is fewer units to schedule, not more. No surrounding
        # text changes that, which is why it is decidable here.
        return bool(_SINGLE_SHIFT.search(quote))
    if kind == "pain:tacit_knowledge":
        return bool(_TRAINING.search(quote) and not _TRAINING_SPECIFIC.search(quote))
    return False


def is_absence_claim(value, quote):
    """A quoteless statement that the evidence is not there - never evidence.

    Both halves matter. Without a quote nothing was read, so the sentence
    can only be the model reporting what it failed to find. WITH a quote
    the same wording is a fact about a real sentence on a real page, and
    the pattern above is deliberately not trusted enough to overrule
    that: "není k dispozici žádný plánovací systém", printed on a
    company's own site, is a pain sign of the first order.
    """
    return not quote and states_absence(value)


# ---------------------------------------------------------------------------
# Relevance: verified, and still beside the point
# ---------------------------------------------------------------------------
#
# find_quote() proves that a sentence is on the page. It cannot prove
# that the sentence has anything to do with what was asked - "Jednosměnný
# provoz" and "výrobní haly o ploše 8.906 m2" both reached a card as
# proof of the ICP's first pain sign, both genuinely quoted, neither
# about a number of units to schedule. llm/prompts/relevance.py is the
# second layer that judges that; archive.py owns the two words it may
# write onto a claim. Only UNRELATED changes anything - a claim nobody
# judged keeps counting, because an unasked question is not a no.


def unrelated(claim_row):
    """Whether the relevance judge rejected this claim as beside the point."""
    keys = claim_row.keys() if hasattr(claim_row, "keys") else ()
    return "relevance" in keys and claim_row["relevance"] == UNRELATED


def counts_as_evidence(claim_row):
    """Whether a stored claim may be shown on a card and scored.

    One predicate for both readers - scoring/select.py and
    scoring/card.py - so a claim can never be scored in one place and
    hidden in the other.
    """
    return (not unrelated(claim_row)
            and not is_absence_claim(claim_row["value"], claim_row["quote"])
            and not argues_against(claim_row["kind"], claim_row["quote"]))


def newest_pass(rows):
    """One agent's latest reading of one company replaces its earlier ones.

    Deduplicating on the quote is not enough, because the model does not
    quote identically twice: re-running pain.py over CENTES produced
    "Zaučení zkušeným kolegou" one week and "důkladné zaškolení
    zkušeným kolegou" the next - one platitude, two rows, no shared key.
    Left alone, a card grows every time an agent is re-run, and the
    growth is the same finding reworded.

    So an agent's newest pass over a company wins outright: for each
    kind family (`pain`, `production_mode`, ...), only the claims from
    the highest run_id in which that family appears for this company are
    kept. Re-running an agent then corrects a card instead of adding to
    it, which is what "re-run" is supposed to mean - and it is what
    makes the prompt fixes above actually reach the salesperson, rather
    than sitting underneath the output of the prompt they replaced.

    Per family, not globally: agents run at different times, and
    production_mode's latest pass must not delete pain's findings just
    because it happened later.

    Per company, not per run: a company the newest pass never looked at
    keeps everything it had. That preserves the case archive.claims() is
    unfiltered for in the first place - a vacancy that has since left
    MPSV's export still has its verified claim, because no later pass
    reconsidered that company at all.

    Rows with no run_id (hand-built in a test) are kept as they are.
    """
    newest = {}
    for row in rows:
        keys = row.keys() if hasattr(row, "keys") else ()
        if "run_id" not in keys or row["run_id"] is None:
            continue
        family = row["kind"].split(":", 1)[0]
        newest[family] = max(newest.get(family, 0), row["run_id"])

    kept = []
    for row in rows:
        keys = row.keys() if hasattr(row, "keys") else ()
        if "run_id" not in keys or row["run_id"] is None:
            kept.append(row)
            continue
        if row["run_id"] == newest[row["kind"].split(":", 1)[0]]:
            kept.append(row)
    return kept


def _dedup_key(quote):
    """The form two quotes have to share to count as the same sentence.

    Normalised and case-folded like everything else here, plus the final
    full stop removed - for exactly the reason find_quote() tolerates
    one. Two runs of the same agent quote the same line, one of them
    ending the sentence and one not, and both verify successfully
    because both really are substrings of the page. Without this they
    are two keys and the card prints the line twice, which is what it
    did with CENTES's "Při nástupu vás důkladně zaškolíme".
    """
    return _TRAILING_STOP.sub("", normalize(quote)).rstrip().lower()


def _preference(row):
    """Sort key deciding which of several claims on one quote is kept.

    Lower is better, because the winner is taken with min().

    1. a claim the relevance judge confirmed beats one nobody judged -
       it is the only signal here that anything looked at whether the
       sentence proves what it was filed under.
    2. a specific kind (`pain:scale`) beats a flat one (`pain`), which
       is the old kind-split case this function already handled.
    3. the newest claim wins, because it came from the newest version of
       the agent - the one whose prompt was last corrected.
    """
    keys = row.keys() if hasattr(row, "keys") else ()
    judged_first = 0 if ("relevance" in keys and row["relevance"] == SUPPORTS) else 1
    specific_first = 0 if ":" in row["kind"] else 1
    # A row built by hand in a test carries no id; ordering by insertion
    # is then as good an answer as any, and it must not raise.
    newest_first = -row["id"] if "id" in keys else 0
    return (judged_first, specific_first, newest_first)


def usable(rows):
    """Out of one company's claims, the ones that may be shown and scored.

    Everything counts_as_evidence() rejects, plus the duplicates - which
    are only visible with the whole list in hand, and which arrive by
    three different routes:

    ONE QUOTE, RE-RUN. archive.claims() is not filtered by run, on
    purpose: a card shows what is known about a company, not what one
    run happened to look at, and a vacancy that has since left MPSV's
    export still has its verified claim in the archive. The cost is that
    running an agent twice files the same sentence twice, worded
    differently each time - "firma vyrábí desítky druhů dřevěných obalů"
    and "výroba desítek druhů dřevěných obalů, dodávky po kamionech",
    both quoting the same line. Measured on the five companies of run
    147 after a single re-run: 16-33 % of their evidence was this, and
    it compounds with every further run.

    ONE QUOTE, TWO SIGNS. The agent may file the same sentence under two
    signs - CENTES's "Zaučení zkušeným kolegou" arrived as both
    tacit_knowledge and manual_data. At most one of those is right, and
    which one cannot be decided here, so the claim is kept once under
    the sign _preference() picks and the other is dropped rather than
    printed as a second finding.

    ONE QUOTE, TWO KIND FORMATS. The original case: pain.py used to file
    findings under the flat kind `pain` and now uses `pain:<sign>`, so an
    archive written across that change holds both.

    All three are the same defect from the card's point of view - the
    salesperson reads one sentence twice - so all three are one rule:
    a quote may support one printed finding. Nothing is deleted from the
    archive; the record of what this pipeline did stays intact, the same
    sentence just stops being counted and printed more than once.

    Matched on the QUOTE, never on the wording of the statement. Two
    runs summarise one sentence differently, so comparing statements
    finds no duplicate at all; the quote is the evidence and is
    identical by construction, both having been verified against the
    same archived text. Normalised the same way the archive stores text,
    and case-folded, because "Zaučení zkušeným kolegou" and "zaučení
    zkušeným kolegou" are one sentence quoted twice.

    Inferences carry no quote, so they are grouped by their statement
    instead - the only thing they have. Two inferences with the same
    wording are the same inference; two with different wording are kept
    apart, which is the conservative direction for a claim that was
    never checked against anything.
    """
    rows = newest_pass(rows)

    groups = {}
    for row in rows:
        if not counts_as_evidence(row):
            continue
        if row["quote"]:
            key = ("quote", _dedup_key(row["quote"]))
        else:
            # Family, not full kind: an inference filed under `pain` and
            # the same sentence under `pain:scale` are one inference.
            key = ("value", row["kind"].split(":", 1)[0],
                   normalize(row["value"] or "").lower())
        groups.setdefault(key, []).append(row)

    kept = [min(group, key=_preference) for group in groups.values()]
    # Back into the order the caller handed them in, so a card keeps the
    # archive's own newest-first ordering rather than dict insertion.
    order = {id(row): index for index, row in enumerate(rows)}
    kept.sort(key=lambda row: order[id(row)])
    return kept


def check(archive, ico, kind, value, quote, snapshot_id, run_id=None):
    """Verify one model statement and record the outcome.

    `value` is the statement itself (what goes on the card); `quote` is
    what the model offered as proof, or None/"" when it is explicitly
    presenting the statement as its own inference rather than a quote.

    Returns a dict describing what happened - callers that process many
    statements from one model response use this to total up the
    hallucination rate for that call, which is the number worth reporting
    alongside cost.
    """
    if not quote:
        claim_id = archive.add_claim(ico, kind, value, "inference", snapshot_id, run_id=run_id)
        return {"state": "inference", "claim_id": claim_id, "value": value}

    text = archive.text_of(snapshot_id)
    matched = find_quote(quote, text)
    if matched is not None:
        claim_id = archive.add_claim(ico, kind, value, "fact", snapshot_id, quote=matched, run_id=run_id)
        return {"state": "fact", "claim_id": claim_id, "value": value, "quote": matched}

    discard_id = archive.add_discard(ico, kind, value, quote, snapshot_id, run_id=run_id)
    return {"state": "discard", "discard_id": discard_id, "value": value, "quote": quote}


def check_against_any(archive, ico, kind, value, quote, snapshot_ids, run_id=None):
    """Verify one statement against several candidate documents at once.

    subsidy_vendor has exactly one document per company, so check() is
    enough. An agent reading a company's whole harvested site - home,
    contact, career, production, about pages, plus vacancy text - has
    several, and the model is never asked which page a quote came from;
    it just quotes. So every candidate is tried and the first one that
    contains the quote wins. When none do, the discard is attributed to
    the first candidate - a discard still has to reference *some*
    snapshot to satisfy the same NOT NULL constraint every claim does,
    and which of several equally-wrong candidates it points at does not
    change what the record means: this quote was not found anywhere it
    was supposed to be.
    """
    if not snapshot_ids:
        raise ValueError("check_against_any needs at least one candidate snapshot")

    if not quote:
        claim_id = archive.add_claim(ico, kind, value, "inference", snapshot_ids[0], run_id=run_id)
        return {"state": "inference", "claim_id": claim_id, "value": value}

    for snapshot_id in snapshot_ids:
        text = archive.text_of(snapshot_id)
        matched = find_quote(quote, text)
        if matched is not None:
            claim_id = archive.add_claim(ico, kind, value, "fact", snapshot_id,
                                         quote=matched, run_id=run_id)
            return {"state": "fact", "claim_id": claim_id, "value": value,
                    "quote": matched, "snapshot_id": snapshot_id}

    discard_id = archive.add_discard(ico, kind, value, quote, snapshot_ids[0], run_id=run_id)
    return {"state": "discard", "discard_id": discard_id, "value": value, "quote": quote}


def check_many_against_any(archive, ico, kind, items, snapshot_ids, run_id=None):
    results = [
        check_against_any(archive, ico, kind, item.get("value"), item.get("quote"),
                          snapshot_ids, run_id)
        for item in items
    ]
    summary = {"fact": 0, "inference": 0, "discard": 0}
    for result in results:
        summary[result["state"]] += 1
    return results, summary


def check_many(archive, ico, kind, items, snapshot_id, run_id=None):
    """Verify a batch of (value, quote) pairs from one model response.

    Each item is a dict with "value" and "quote" keys - the shape every
    prompt in llm/prompts/ is required to return. Returns the same list
    of results as repeated check() calls, plus a summary so a caller can
    print "3 facts, 1 inference, 1 discard" without recounting.
    """
    results = [
        check(archive, ico, kind, item.get("value"), item.get("quote"), snapshot_id, run_id)
        for item in items
    ]
    summary = {"fact": 0, "inference": 0, "discard": 0}
    for result in results:
        summary[result["state"]] += 1
    return results, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Quote verifier - code checks the model.")
    parser.add_argument("--demo", action="store_true",
                        help="run the three branches against a scratch snapshot")
    args = parser.parse_args()

    if args.demo:
        archive = Archive()
        run_id = archive.start_run(note="verify.py demo")
        snap_id, _ = archive.store(
            "00000000", "demo",
            "Firma vyrábí na zakázku dle výkresové dokumentace zákazníka.",
            url="https://example.cz/o-nas", run_id=run_id,
        )

        print("fact:     ", check(archive, "00000000", "demo_kind",
              "vyrábí na zakázku", "vyrábí na zakázku", snap_id, run_id))
        print("discard:  ", check(archive, "00000000", "demo_kind",
              "má certifikaci ISO 9001", "certifikaci ISO 9001", snap_id, run_id))
        print("inference:", check(archive, "00000000", "demo_kind",
              "pravděpodobně zakázková výroba", None, snap_id, run_id))

        archive.finish_run(run_id)
        print("\nstats:", archive.stats())
        archive.close()
