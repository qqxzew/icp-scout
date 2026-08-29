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

from pipeline.evidence.archive import Archive, normalize

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
