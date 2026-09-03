"""Stage 06: the dossier a salesperson actually reads.

The brief's own words are "připravené podklady, ne seznam" - so this
module's job is not to rank anything (select.py did that) but to put one
company's evidence in front of a person in a form where every line can
be challenged. Three rules, all of them consequences of the evidence
layer rather than presentation choices:

* every claim is shown with its state - fact, inference - and a fact
  carries the quote and the URL it was found in. Nothing arrives
  unlabelled.
* a missing field is printed as missing. Turnover is the clearest case:
  it exists for a minority of companies (16.5 % measured), because a
  small company is not legally required to file a P&L at all. An empty
  turnover line is the honest answer and is not filled with an estimate.
* discards are counted and shown. "The model claimed three things we
  could not verify and they were dropped" is information about how much
  to trust the rest of the card.
* what was verified and still does not count is counted too, on the
  same line. A quote the relevance judge found beside the point, and a
  quoteless "there is no mention of X", are both real records in the
  archive that must not be read as evidence - and a card that shows two
  facts because five statements were discounted is a different card
  from one where there were only two to begin with.
* every label is in Czech, including the ones that come from a claim
  kind. `pain`, printed six times in a row, told the salesperson
  nothing about which of the ICP's signs had fired; and
  `production_mode:made_to_order` is 29 characters in a 22-character
  column, so it ran into its own value.

TURNOVER IS FETCHED HERE AND NOWHERE EARLIER. sbirka.py is a slow,
multi-request source (subject id -> document list -> detail page ->
file), and its coverage is too thin to filter on: the log's §15.5
settled that turnover may enrich a card but must never remove a
candidate, because the headcount filter already ran first and turnover
could only subtract. Running it at stage 06, over five companies rather
than 3299, is what makes that affordable.

Run:
    python -m pipeline.scoring.card 27975924
    python -m pipeline.scoring.card --top 5
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import quote as urlquote

from pipeline.evidence.archive import Archive
from pipeline.evidence.verify import counts_as_evidence, usable
from pipeline.filters import negative
from pipeline.scoring.select import (CONTACTS, WEBSITES, fit_assessment, geography,
                                     load_jsonl)
from pipeline.signals.now import load_tenders, parse_deadline as parse_tender_deadline

TURNOVER_CACHE = Path("data/raw/turnover.jsonl")


def load_turnover_cache(path=TURNOVER_CACHE):
    """Previously fetched turnover, keyed by ICO.

    Cached on disk because a filing does not change between weekly runs -
    and because every entry cost four sequential requests to justice.cz,
    which is the one source here rude enough to be worth not repeating.
    """
    if not Path(path).exists():
        return {}
    out = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            out[row["ico"]] = row
    return out


def save_turnover(row, path=TURNOVER_CACHE):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as sink:
        sink.write(json.dumps(row, ensure_ascii=False) + "\n")


def turnover_for(ico, cache=None, fetch=True):
    """Turnover for one company as a card-ready dict - possibly empty.

    The status is kept alongside the value on purpose. "No statement
    filed" and "filed but scanned without a text layer" are different
    facts about a company, and collapsing both into a blank cell throws
    away the difference (hypothesis E: absence of data is not binary).
    """
    cache = cache if cache is not None else load_turnover_cache()
    ico = str(ico).zfill(8)

    row = cache.get(ico)
    if row is None and fetch:
        from pipeline.sources.sbirka import get_turnover
        row = get_turnover(ico)
        save_turnover(row)
        cache[ico] = row
    if row is None:
        return {"value_czk": None, "status": "not_checked", "note": None}

    status = row.get("status")
    note = {
        "no_subject":     "není ve Sbírce listin",
        "no_statement":   "žádná účetní závěrka za poslední roky",
        "no_pdf":         "podání neobsahuje čitelný soubor",
        "scanned":        "závěrka je sken bez textové vrstvy",
        "no_revenue_row": "podána jen rozvaha, výkaz zisku a ztráty chybí",
        "found_rows":     "nalezeno v PDF, výběr sloupce nepotvrzen",
    }.get(status)

    return {
        "value_czk": row.get("value_czk") if status == "found" else None,
        "year": row.get("year"),
        "status": status,
        "note": note,
        "source_url": row.get("source_url"),
        "document_ref": row.get("document_ref"),
        # Only the XML path names the period per figure, so only it
        # produces a number this project is willing to print as fact.
        "state": "fact" if status == "found" else "unknown",
    }


# What to print in the source column when a snapshot carries no URL.
# Every claim has a snapshot by construction, but not every snapshot came
# off a web page - vacancy text is assembled from MPSV's daily JSON, and
# a registry claim points at an API response.
SOURCE_NAMES = {
    "mpsv_text": "inzerát MPSV",
    "mpsv": "inzerát MPSV",
    "ares": "ARES",
    "dotace_eu": "dotaceEU",
    "certificate": "certifikát",
    "website": "web firmy",
}


def source_label(row):
    """Something citable for the source column - a URL, or a source name."""
    if row["url"]:
        return row["url"]
    return SOURCE_NAMES.get(row["source"], row["source"] or "—")


# Chrome/Edge/Opera implement the Text Fragments spec (#:~:text=...): a
# link that both scrolls to and highlights the cited passage, on any page,
# with no cooperation needed from the site. Safari and Firefox ignore the
# suffix and just open the page - safe degradation, not a broken link.
#
# Measured live on maskop99.cz while wiring this up: a short, single-node
# anchor (one e-mail address) matched reliably; a longer synthetic span
# built to cross several block-level elements (a contact card's name,
# role, e-mail and phone, each its own line) did not, because the
# fragment matcher only collapses whitespace confidently within one
# block, not across several with unpredictable markup between them. A
# verified pain-evidence quote is normally lifted from one paragraph of
# prose, so it does not hit this - but it can still be long, and a very
# long text= value both risks a similar cross-node failure and makes an
# unwieldy URL. So: short quotes are passed whole; long ones are trimmed
# to their first and last few words, which the spec's start,end form
# matches as "the passage beginning here and ending there" - long enough
# to be unambiguous, short enough to usually stay inside one block.
WORD_THRESHOLD = 12
EDGE_WORDS = 6


def fragment_url(url, quote):
    """A link that jumps straight to the cited passage, where a browser supports it.

    Falls back to the bare url when there is nothing to anchor on - no
    url, or no quote (an inference has none by construction; see
    evidence/verify.py). Never guesses a passage that was not actually
    verified: the quote this takes is always one that already survived
    the archive check, so the link can only point at real, checked text.
    """
    if not url or not quote:
        return url
    words = quote.split()
    if len(words) <= WORD_THRESHOLD:
        return f"{url}#:~:text={urlquote(quote, safe='')}"
    start = " ".join(words[:EDGE_WORDS])
    end = " ".join(words[-EDGE_WORDS:])
    return f"{url}#:~:text={urlquote(start, safe='')},{urlquote(end, safe='')}"


def turnover_from_site(archive, ico):
    """Turnover the company states on its own website, if any.

    A second, independent source for the field the register covers worst.
    Measured: the filed accounts yield a figure for 16.5 % of companies,
    while 4.5 % state one in prose on their own site - and the two sets
    only partly overlap, because a company that is not required to file a
    P&L will still brag about its revenue on an about page.

    Never merged into the register figure. The site number is what the
    company says about itself and comes with the qualifications the agent
    extracted (whose turnover, which year, annual or cumulative), so it
    is carried separately and labelled - a group's 10 bn and this
    company's 160 m must not end up in the same cell.
    """
    out = []
    for row in archive.claims(ico, kind="turnover_web"):
        out.append({
            "value": row["value"], "quote": row["quote"],
            "url": row["url"], "state": row["state"],
        })
    return out


def relevant_tenders(ico, tenders):
    """This company's procurements whose subject we could supply.

    Open ones are the reason to call; awarded ones are the opposite, and
    both belong on the card. "They bought an ERP in March" is not a lead
    but it is exactly the context a salesperson needs before dialling -
    hiding it would leave them to discover it mid-call.
    """
    rows = (tenders or {}).get(str(ico).zfill(8), [])
    return [r for r in rows if r.get("relevant")]


def tender_contacts(ico, tenders):
    """Contact people named on this company's relevant tenders.

    Each carries the tier nen.py assigned it - register, company or
    external - because that is the difference between the owner's own
    address and a grant consultancy administering the paperwork, and on
    the measured sample two of every three were the consultancy.
    """
    out, seen = [], set()
    for row in relevant_tenders(ico, tenders):
        contact = row.get("contact") or {}
        key = (contact.get("name"), contact.get("email"))
        if not contact.get("name") or key in seen:
            continue
        seen.add(key)
        out.append({
            "name": contact.get("name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "tier": contact.get("tier"),
            "about": row.get("name"),
            "url": row.get("url"),
        })
    # Register-matched people first: a name confirmed in a state register
    # outranks any amount of matching on an email domain.
    order = {"register": 0, "company": 1, "external": 2}
    out.sort(key=lambda c: order.get(c["tier"], 3))
    return out


def certificates_for(ico, cache_path=Path("data/raw/certificates.jsonl")):
    """Certificates already collected by sources/certificates.py, if any."""
    if not Path(cache_path).exists():
        return []
    ico = str(ico).zfill(8)
    with open(cache_path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("ico") == ico:
                return row.get("certificates", [])
    return []


def build(ico, archive, companies=None, websites=None, contacts=None,
          turnover_cache=None, fetch_turnover=True, tenders=None, icp=None):
    """Everything known about one company, grouped the way it is read."""
    ico = str(ico).zfill(8)
    websites = websites if websites is not None else load_jsonl(WEBSITES)
    contacts = contacts if contacts is not None else load_jsonl(CONTACTS)
    # Loaded rather than left to default to None - the fourth time an
    # optional argument would have quietly switched a source off.
    tenders = tenders if tenders is not None else load_tenders()
    if icp is None:
        # The brief carries the origin distance is measured from. A run
        # passes its own; building one card by hand falls back to the
        # saved brief, the same one the pipeline would run with.
        from pipeline.run import load_icp
        icp = load_icp()
    company = (companies or {}).get(ico, {})

    claims = archive.claims(ico)
    evidence = {"facts": [], "inferences": []}
    # The claims that survive both guards and the duplicate left behind
    # by pain.py's kind split; everything else is counted, not printed.
    kept_ids = {row["id"] for row in usable([c for c in claims
                                             if not c["kind"].startswith("now:")])}
    # Claims that were really produced and really verified, and still
    # must not be read as evidence: a quoteless "there is no mention of
    # X", and a quote the relevance judge found beside the point. They
    # stay in the archive - the record is the record - and are counted
    # here so a thin card says why it is thin instead of looking like a
    # company nobody could learn anything about. See evidence/verify.py.
    discounted = 0
    for row in claims:
        if row["kind"].startswith("now:"):
            continue
        if not counts_as_evidence(row):
            discounted += 1
            continue
        # A duplicate is not a loss of information - the same statement
        # is printed once, under the sign it belongs to - so it is
        # skipped without being counted as anything.
        if row["id"] not in kept_ids:
            continue
        item = {
            "kind": row["kind"], "value": row["value"],
            "quote": row["quote"], "url": row["url"], "seen_at": row["fetched_at"],
            "source": source_label(row),
        }
        (evidence["facts"] if row["state"] == "fact" else evidence["inferences"]).append(item)

    now_events = [
        {"kind": row["kind"].removeprefix("now:"), "value": row["value"],
         "url": row["url"], "seen_at": row["fetched_at"]}
        for row in claims if row["kind"].startswith("now:")
    ]

    # Tenders arrive through the claim table like every other NOW event -
    # nen.py archives the tender page, so the claim points at a snapshot
    # that cannot change, and the quote in it was checked against that
    # snapshot. Read directly from nen.jsonl for one commit while the
    # archiving was missing; that shortcut is gone, and with it the one
    # line on the card that cited a live page instead of a stored copy.
    if not any(e["kind"] == "tender_open" for e in now_events):
        from pipeline.signals.now import tender_events
        # Same rule as the gate: still accepting bids, or it is context
        # rather than a reason. Reached only when card.py runs on its own
        # outside a full run, where nothing has recorded the claims yet.
        for event in tender_events(ico, tenders):
            now_events.append({
                "kind": "tender_open",
                "value": f"otevřená zakázka, do uzávěrky {event['days_left']} dní: "
                         f"{(event.get('subject') or '')[:60]}",
                "url": event.get("url"),
                "seen_at": event.get("date"),
            })

    site = websites.get(ico, {})

    # People come from the REGISTER, and a channel is attached when one
    # was found - not the other way round. Filtering on "has an email or
    # a phone" hid the person entirely whenever no channel turned up,
    # which threw away the one thing this pipeline knows about almost
    # every company: who is legally allowed to sign. Names are present
    # for 99.9 % of the base, a channel for far fewer, so the empty cell
    # belongs in the channel column, not in place of the row.
    contacts_row = contacts.get(ico) or {}
    by_name = {p.get("name"): p for p in (contacts_row.get("people") or [])}
    people = []
    for director in company.get("directors") or []:
        found = by_name.get(director.get("name")) or {}
        people.append({
            "name": director.get("name"),
            "role_registered": director.get("role"),
            "since": director.get("since"),
            "email": found.get("email"),
            "phone": found.get("phone"),
            "quote": found.get("quote"),
            "source": "web" if (found.get("email") or found.get("phone")) else None,
            # The page this channel was read from - contacts.py harvests
            # one team/contact page per company, so every person on it
            # shares the same source_url. Carried per-person rather than
            # read off the card's top level because a card with no
            # directors should not force a caller to fall back to
            # somewhere else for it.
            "page_url": contacts_row.get("source_url"),
        })

    tender_people = tender_contacts(ico, tenders)

    return {
        "ico": ico,
        "name": company.get("name") or site.get("name"),
        "region": company.get("region"),
        "district": company.get("district"),
        "city": company.get("city"),
        # How far this is from where the brief measures from. The ICP
        # calls geography a preference, so it never removed anybody -
        # but it was not on the card either, and a run handed over a
        # company in Ostrava, 380 km from the Plzeň the salesperson
        # drives out of, with nothing anywhere saying so.
        "geography": geography(company, icp),
        # ARES phrases the size band in English ("100-199 employees").
        # The card is Czech and is read by a Czech salesperson, so the
        # band is said in Czech here rather than passed through.
        "size": (company.get("employee_range") or "").replace("employees", "zaměstnanců").strip(),
        "nace": company.get("nace"),
        # Empty for most companies, and that is the honest state - see
        # the module docstring.
        "turnover": turnover_for(ico, turnover_cache, fetch_turnover),
        "turnover_site": turnover_from_site(archive, ico),
        "certificates": certificates_for(ico),
        "website": {"domain": site.get("domain"), "status": site.get("status")},
        # Fit is said on the card, not only used in the ordering: the
        # salesperson seeing "stavební firma, mimo jádro ICP" before
        # dialling is the whole point of having assessed it.
        "fit": fit_assessment(archive, company or {"ico": ico}, websites),
        "contacts": people,
        # Kept apart from `contacts` rather than merged into it. A tender
        # contact is the person handling THAT purchase, which is both
        # more useful for this conversation and less certain as a
        # company contact - measured, 20 of 29 were grant consultancies.
        # Merging would erase which is which; the card shows both and
        # says where each came from.
        "tender_contacts": tender_people,
        "tenders": relevant_tenders(ico, tenders),
        # What the negative filters found and did not exclude on. A
        # company whose every establishment is closed still gets a card -
        # nothing in a register proves it will not buy - but the
        # salesperson has to see the finding, with the field it came
        # from, before spending a call on it.
        "negative": negative.check(company),
        "why_now": now_events,
        "evidence": evidence,
        "discarded": len(archive.discards(ico=ico)),
        "discounted": discounted,
    }


ARES_REST = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"
# The same endpoint sources/coords.py queries for RUIAN coordinates -
# named here only for the card's own "where did this come from" link, so
# a distance line points at something rather than a bare label.
RUIAN_URL = "https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer/1/query"

LABEL = 22          # width of the label column
VALUE = 66          # width of the value column before the source


# The card is read by a Czech salesperson, so the label column is
# Czech. Claim kinds are English identifiers by the project's own rule,
# and printing them raw did two things wrong at once: the reader could
# not tell which of the ICP's pain signs had fired - every one of them
# said `pain` - and `production_mode:made_to_order` is 29 characters in
# a 22-character column, so the value ran straight into the label with
# no space between them.
CLAIM_LABELS = {
    "pain:scale": "Rozsah k rozvržení",
    "pain:manual_data": "Ruční přenos dat",
    "pain:tacit_knowledge": "Know-how v hlavě",
    # Claims written before pain.py split the kind by sign. Kept so old
    # evidence still reads as something, rather than disappearing from
    # cards built on an archive that predates the change.
    "pain": "Signál bolesti",
    "production_mode": "Doklad režimu",
}


def plural(count, one, few, many):
    """Czech counts: 1 fakt, 2 fakty, 5 faktů.

    The summary line at the foot of every card said "1 úsudků", which is
    the kind of small wrongness that makes a reader trust the rest of
    the page less - and this card is asking to be trusted about a
    company's turnover.
    """
    if count == 1:
        return one
    return few if 2 <= count <= 4 else many


def label_for(kind):
    """The Czech label for a claim kind, falling back to the kind itself.

    An unknown kind is printed as it is rather than hidden or renamed to
    something generic: a label nobody wrote is a gap in this dictionary,
    and it should be visible as one.
    """
    if kind in CLAIM_LABELS:
        return CLAIM_LABELS[kind]
    return CLAIM_LABELS.get(kind.split(":", 1)[0], kind)


def row(label, value, source="", state="fact"):
    """One line of the card: label, value, where it came from.

    `state` is what the reader is looking at, not how sure anyone feels:

        fact       plain text, a quote was found in the archived source
        inference  marked, because a model derived it rather than read it
        empty      value is blank and stays blank

    An empty value prints an empty line rather than a dash-and-excuse.
    The one exception is turnover, where the caller passes a reason,
    because "not required to file" and "filed as a scan" are different
    facts about a company and collapsing them loses information
    (hypothesis E).

    The label is clamped one character short of its column so there is
    always a gap before the value. Padding alone does not do this: a
    label longer than the column is printed in full by f-string padding
    and the value simply follows it.
    """
    if len(label) > LABEL - 1:
        label = label[:LABEL - 2] + "…"
    if not value:
        return f"  {label:<{LABEL}}"
    mark = "~ " if state == "inference" else ""
    text = f"{mark}{value}"
    # Truncate two short of the column so the ellipsis never touches the
    # source that follows - "…registr" reads as one word and hides where
    # the claim came from, which is the one thing this column is for.
    if len(text) > VALUE - 2:
        text = text[:VALUE - 3] + "…"
    return f"  {label:<{LABEL}}{text:<{VALUE}}{source}"


# Mode label and its basis, kept apart rather than pre-joined: render()
# wants them as one string ("zakázková (přímé tvrzení)"), for_web() wants
# them as separate fields (a value and a note), and a dict of ready-made
# sentences cannot serve the second shape without being re-split.
MODE_CZ = {
    "made_to_order": ("zakázková", "přímé tvrzení"),
    "mixed": ("zakázková i sériová", "přímá tvrzení"),
    "small_batch": ("malosériová", "přímé tvrzení"),
    "leaning_made_to_order": ("spíše zakázková", "nepřímé stopy"),
    "leaning_serial": ("spíše sériová", "nepřímé stopy"),
    "serial": ("sériová", "přímé tvrzení"),
    "unknown": ("", ""),
}

# What each tier of tender contact means, in words a salesperson reads
# where the name is - not in a margin they might miss. Two of every three
# measured were the consultancy administering the paperwork rather than
# the company, so this is the warning that matters most on the card.
TIER_NOTE = {
    "register": "jednatel z rejstříku",
    "company":  "zaměstnanec firmy",
    "external": "administrátor zakázky, ne firma",
}


def distance_note(geo):
    """"185 km (výchozí bod Plzeň)" - and, when it is too far, that too.

    Empty when the company has no coordinates, which is the card's rule
    for everything: an unplaced company is not a distant one, and
    printing "? km" would invite reading it as a large number.

    Phrased around the place name rather than "od Plzně" on purpose: the
    origin is whatever town the salesperson typed, and Czech would
    decline it. An apposition ("výchozí bod Plzeň") is grammatical for
    every name without the code having to know how to inflect it.
    """
    geo = geo or {}
    if geo.get("distance_km") is None:
        return ""
    origin = geo.get("from") or "Plzeň"
    note = f"{geo['distance_km']:.0f} km (výchozí bod {origin})"
    if geo.get("limit_km") and geo.get("preferred") is False:
        # Said, not enforced: the ICP's word is "preferovaně". The
        # ordering already put this company behind the nearer ones; the
        # card only has to make sure nobody dials it by surprise.
        note += f" — mimo preferovaný okruh {geo['limit_km']} km"
    return note


def render(card):
    """The card as a flat list - same rows, same order, for every company.

    Deliberately not grouped into sections. A salesperson reading the
    tenth card of the week should find turnover exactly where it was on
    the first one, so the layout is a fixed sequence of rows: registry
    facts first (cheap, structural, certain), then what had to be found
    (site, turnover, certificates), then people, then the reason to call,
    then the pain evidence. Every row carries where it came from, so any
    line can be challenged on its own without reading the rest.
    """
    ico = card["ico"]
    ares = f"{ARES_REST}/ekonomicke-subjekty/{ico}"
    out = [
        f"{card['name']}   (IČO {ico})",
        f"  {'ověřeno: běžný text':<{LABEL}}{'~ úsudek modelu':<{VALUE}}prázdné = nezjištěno",
        "",
        row("IČO", ico, ares),
        row("Sídlo", " · ".join(x for x in (card.get("city"), card.get("district"),
                                            card.get("region")) if x), ares),
        row("Vzdálenost", distance_note(card.get("geography")), "RÚIAN"),
        row("Velikost", card.get("size"), f"{ARES_REST}/ekonomicke-subjekty-res/{ico}"),
        row("NACE", card.get("nace"), f"{ARES_REST}/ekonomicke-subjekty-res/{ico}"),
    ]

    # The ICP's first criterion, answered from evidence rather than
    # assumed - and the row that was missing when a construction firm
    # reached a card with nothing saying so.
    fit = card.get("fit") or {}
    mode_label, mode_basis = MODE_CZ.get(fit.get("mode"), ("", ""))
    mode_note = f"{mode_label} ({mode_basis})" if mode_label else ""
    tier_note = ("obor mimo jádro ICP — prověřit, zda plánuje vlastní kapacity"
                 if fit.get("nace_tier") == "service" else "")
    joined = " · ".join(x for x in (mode_note, tier_note) if x)
    out.append(row("Režim výroby", joined,
                   f"https://{card['website']['domain']}" if card["website"].get("domain") else ""))

    # Register findings that lower a company without excluding it. High
    # up on purpose - "all establishments are closed" changes whether
    # the rest of the card is worth reading - and each one names the
    # field it came from, so the salesperson can look at the same field
    # in ARES and disagree.
    for finding in card.get("negative") or []:
        out.append(row("Riziko",
                       f"{finding['reason']} ({finding['field']}: {finding['value']})",
                       ares))

    # Two independent sources, two rows - never merged. The register's
    # figure is audited; the website's is the company talking about
    # itself, and it carries qualifiers (whose turnover, which year,
    # annual or cumulative) that the register's does not need.
    t = card["turnover"]
    if t["value_czk"]:
        out.append(row("Obrat (závěrka)", f"{t['value_czk']:,}".replace(",", " ") + f" Kč ({t['year']})",
                       t.get("source_url") or "justice.cz"))
    else:
        out.append(row("Obrat (závěrka)", t["note"] or "", t.get("source_url") or ""))

    for site_turnover in card.get("turnover_site") or []:
        out.append(row("Obrat (web)", site_turnover["value"], site_turnover["url"] or "",
                       state=site_turnover.get("state", "fact")))
        if site_turnover.get("quote"):
            out.append(f"  {'':<{LABEL}}„{site_turnover['quote'][:VALUE - 2]}\"")

    site = card["website"]
    out.append(row("Web", f"{site['domain']} [{site['status']}]" if site["domain"] else "",
                   site.get("url") or (f"https://{site['domain']}" if site["domain"] else "")))

    certs = [c for c in card["certificates"] if c.get("standard")]
    if certs:
        for c in certs:
            bits = [c["standard"]]
            if c.get("number"):
                bits.append(f"č. {c['number']}")
            if c.get("issuer"):
                bits.append(f"vydal {c['issuer']}")
            out.append(row("Certifikát", " · ".join(bits), c.get("source_url") or ""))
    else:
        out.append(row("Certifikát", ""))

    vr = f"{ARES_REST}/ekonomicke-subjekty-vr/{ico}"
    people = card["contacts"]
    if people:
        for person in people[:4]:
            role = person.get("role_registered") or ""
            since = f", od {person['since']}" if person.get("since") else ""
            out.append(row("Jednatel", f"{person.get('name')} · {role}{since}".strip(" ·"), vr))
            # Separate row on purpose: the name is a register fact, the
            # channel is something we had to find on a page and often
            # did not. Printing them on one line would let a missing
            # channel look like a missing person.
            out.append(row("  ↳ kanál z webu",
                           person.get("email") or person.get("phone") or "",
                           card["website"].get("domain") or ""))
    else:
        out.append(row("Jednatel", "", vr))

    # The tender's own contact, kept separate from the website one. It is
    # the person running that purchase - better for this conversation,
    # and worse as a company contact, because two of every three measured
    # were a grant consultancy rather than the company. The tier says
    # which, so the salesperson chooses instead of being told.
    # Labels stay short enough for the column, and the tier is said in
    # the value instead - "administrátor zakázky" is the warning that
    # matters and it belongs where the name is, not in the margin.
    for contact in card.get("tender_contacts") or []:
        channel = " · ".join(x for x in (contact.get("email"), contact.get("phone")) if x)
        note = TIER_NOTE.get(contact["tier"], contact["tier"])
        out.append(row("Kontakt ze zakázky",
                       f"{contact['name']} ({note}) — {channel}".strip(" —"),
                       contact.get("url") or ""))

    for tender in card.get("tenders") or []:
        published = tender.get("published") or ""
        # The state a salesperson needs is "can I still bid", which is
        # the deadline - not NEN's record status, which stays
        # "Neukončen" for years after bidding closed and made four of
        # five companies in one run look like live opportunities.
        deadline = parse_tender_deadline(tender.get("deadline"))
        if tender.get("status") in ("Neukončen", "Plánován"):
            state = ("otevřená" if deadline and deadline >= date.today()
                     else "po uzávěrce")
        else:
            state = tender.get("status")
        out.append(row("Zakázka",
                       f"[{state}] {tender.get('name', '')[:52]}"
                       + (f" · {published[:10]}" if published else ""),
                       tender.get("url") or ""))
        if tender.get("cpv"):
            out.append(row("  ↳ CPV",
                           f"{tender['cpv']} {tender.get('cpv_name', '')[:44]}",
                           tender.get("url") or ""))

    # Why this company and not another - the question the brief asks in
    # requirement 7 and the card never answered. It explains the ORDER,
    # so it sits directly above the reason itself.
    reason = card.get("reason") or {}
    if reason:
        bits = [f"třída {reason['class']} — {reason['label']}"]
        if reason.get("corroborated"):
            bits.append(f"{len(reason.get('kinds') or [])} typy událostí najednou")
        if card.get("rank"):
            bits.append(f"{card['rank']}. z {card.get('of_qualified', '?')} kvalifikovaných")
        out.append(row("Pořadí", " · ".join(bits)))

    # The same event at other companies of the same group. Printed
    # because it changes the call rather than decorating it: one owner
    # took over three subsidiaries on one day, and the conversation is
    # with the owner, not three times with the plants.
    siblings = card.get("group_siblings") or []
    if siblings:
        names = " · ".join((s.get("name") or s["ico"])[:28] for s in siblings)
        out.append(row("Táž událost ve skupině", f"{len(siblings)} další firmy: {names}"))

    if card["why_now"]:
        for event in card["why_now"]:
            # The value is already a Czech sentence - signals/now.py's
            # describe() writes it for exactly this line. Prefixing it
            # with the event kind put `director_departed:` in front of
            # "Ing. Čeněk Fajkus opustil statutární orgán", which is an
            # English identifier explaining a Czech sentence that
            # explains itself.
            out.append(row("Proč teď", event["value"],
                           event.get("url") or "registr"))
    else:
        out.append(row("Proč teď", ""))

    # Pain evidence last, one row per finding, quote underneath. This is
    # the part that justifies the call, so it is the part where every
    # line has to be checkable on its own.
    for fact in card["evidence"]["facts"]:
        if fact["kind"].startswith(("certificate:", "turnover_web")):
            continue                      # already printed in their own rows
        out.append(row(label_for(fact["kind"]), fact["value"], fact["source"]))
        if fact["quote"]:
            out.append(f"  {'':<{LABEL}}„{fact['quote'][:VALUE - 2]}\"")
    for guess in card["evidence"]["inferences"]:
        out.append(row(label_for(guess["kind"]), guess["value"], guess["source"],
                       state="inference"))

    out.append("")
    # Both ways a statement can fail are counted, because they mean
    # different things about the card above. "Zahozeno" is the model
    # quoting something the page does not contain; "nezapočteno" is a
    # real quote that turned out not to prove the sign it was filed
    # under, or a statement that the evidence is missing at all.
    facts, guesses = len(card["evidence"]["facts"]), len(card["evidence"]["inferences"])
    tail = (f"  {facts} {plural(facts, 'ověřený fakt', 'ověřené fakty', 'ověřených faktů')} · "
            f"{guesses} {plural(guesses, 'úsudek', 'úsudky', 'úsudků')} · "
            f"{card['discarded']} zahozeno při ověření")
    if card.get("discounted"):
        tail += f" · {card['discounted']} nezapočteno (mimo signál)"
    out.append(tail)
    return "\n".join(out)


def for_web(card):
    """Card data as rows a browser can walk, instead of an ASCII table.

    Same content, same order, same decisions as render() - the Czech
    labels (label_for), the mode phrasing (MODE_CZ), which quotes are
    worth showing at all - so a web view and the terminal one can never
    quietly drift apart on what a company's card actually says. The one
    thing this adds that render() has no use for is fragment_url(): every
    row that carries a verified quote gets a link built to jump straight
    to it, computed once here rather than reinvented per company by
    whoever writes the page that renders this.

    Every row is a dict with at least `label`, `value`, `source`. A row
    with nothing to link to carries `source: None` rather than being
    left out - a missing source is itself something a reader should see,
    not silently lose.
    """
    ico = card["ico"]
    ares = f"{ARES_REST}/ekonomicke-subjekty/{ico}"
    ares_res = f"{ARES_REST}/ekonomicke-subjekty-res/{ico}"
    ares_vr = f"{ARES_REST}/ekonomicke-subjekty-vr/{ico}"
    site_domain = card["website"].get("domain")
    site_url = f"https://{site_domain}" if site_domain else None

    rows = [
        {"label": "Sídlo",
         "value": " · ".join(x for x in (card.get("city"), card.get("district"),
                                          card.get("region")) if x),
         "source": ares},
        {"label": "Vzdálenost", "value": distance_note(card.get("geography")),
         "source": RUIAN_URL},
        {"label": "Velikost", "value": card.get("size"), "source": ares_res},
        {"label": "NACE", "value": card.get("nace"), "source": ares_res},
    ]

    fit = card.get("fit") or {}
    mode_label, mode_basis = MODE_CZ.get(fit.get("mode"), ("", ""))
    tier_note = ("obor mimo jádro ICP — prověřit, zda plánuje vlastní kapacity"
                 if fit.get("nace_tier") == "service" else "")
    rows.append({
        "label": "Režim výroby", "value": mode_label,
        "note": " · ".join(x for x in (mode_basis, tier_note) if x) or None,
        "source": site_url,
    })

    for finding in card.get("negative") or []:
        rows.append({
            "label": "Riziko",
            "value": f"{finding['reason']} ({finding['field']}: {finding['value']})",
            "source": ares, "risk": True,
        })

    t = card["turnover"]
    if t["value_czk"]:
        rows.append({
            "label": "Obrat (závěrka)",
            "value": f"{t['value_czk']:,}".replace(",", " ") + " Kč",
            "note": f"{t['year']}, z účetní závěrky" if t.get("year") else None,
            "source": t.get("source_url"),
        })
    else:
        rows.append({"label": "Obrat (závěrka)", "value": None,
                      "empty_note": t.get("note"), "source": t.get("source_url")})

    for site_turnover in card.get("turnover_site") or []:
        rows.append({
            "label": "Obrat (web)", "value": site_turnover["value"],
            "source": fragment_url(site_turnover["url"], site_turnover.get("quote")),
            "state": site_turnover.get("state", "fact"),
            "quote": site_turnover.get("quote"),
        })

    rows.append({"label": "Web", "value": site_domain,
                 "pill": card["website"].get("status"), "source": site_url})

    certs = [c for c in card["certificates"] if c.get("standard")]
    if certs:
        for c in certs:
            bits = [c["standard"]]
            if c.get("number"):
                bits.append(f"č. {c['number']}")
            if c.get("issuer"):
                bits.append(f"vydal {c['issuer']}")
            rows.append({"label": "Certifikát", "value": " · ".join(bits),
                         "source": c.get("source_url")})
    else:
        rows.append({"label": "Certifikát", "value": None, "source": None})

    contact_rows = []
    for person in card["contacts"]:
        role = person.get("role_registered") or ""
        since = f", od {person['since']}" if person.get("since") else ""
        contact_rows.append({
            "label": "Jednatel",
            "value": f"{person.get('name')} · {role}{since}".strip(" ·"),
            "source": ares_vr,
        })
        # Anchored on the channel value itself (an e-mail or a phone
        # number), not on the surrounding prose quote: measured live, a
        # contact card's name/role/e-mail/phone sit in separate
        # block-level elements, and a fragment spanning several of them
        # did not match, while the short single-token value did. That
        # holds for any company's team page, not just this one.
        channel = person.get("email") or person.get("phone")
        contact_rows.append({
            "label": "kanál z webu", "sub": True, "value": channel,
            "source": fragment_url(person.get("page_url"), channel) if channel else None,
        })
    if not contact_rows:
        contact_rows.append({"label": "Jednatel", "value": None, "source": ares_vr})

    for contact in card.get("tender_contacts") or []:
        channel = " · ".join(x for x in (contact.get("email"), contact.get("phone")) if x)
        note = TIER_NOTE.get(contact["tier"], contact["tier"])
        contact_rows.append({
            "label": "Kontakt ze zakázky",
            "value": f"{contact['name']} ({note}) — {channel}".strip(" —"),
            "source": contact.get("url"),
        })

    # No note field here on purpose, matching render(): `seen_at` is when
    # the claim was archived, not the event's own date, and the two can
    # differ - showing it as if it answered "when" would be exactly the
    # kind of confident-looking wrong thing this project's evidence layer
    # exists to prevent. The event's own date, when signals/now.py has
    # one, is already worded into `value`.
    why_now_rows = [
        {"label": "Proč teď", "value": event["value"], "source": event.get("url")}
        for event in card["why_now"]
    ] or [{"label": "Proč teď", "value": None, "source": None}]

    tender_rows = []
    for tender in card.get("tenders") or []:
        published = tender.get("published") or ""
        deadline = parse_tender_deadline(tender.get("deadline"))
        if tender.get("status") in ("Neukončen", "Plánován"):
            state = "otevřená" if deadline and deadline >= date.today() else "po uzávěrce"
        else:
            state = tender.get("status")
        tender_rows.append({
            "label": "Zakázka",
            "value": f"[{state}] {tender.get('name', '')}"
                     + (f" · {published[:10]}" if published else ""),
            "source": tender.get("url"),
        })
        if tender.get("cpv"):
            tender_rows.append({
                "label": "CPV", "sub": True,
                "value": f"{tender['cpv']} {tender.get('cpv_name', '')}",
                "source": tender.get("url"),
            })

    claims = []
    for fact in card["evidence"]["facts"]:
        if fact["kind"].startswith(("certificate:", "turnover_web")):
            continue                      # already carried in their own rows above
        claims.append({
            "badge": "fakt", "label": label_for(fact["kind"]), "value": fact["value"],
            "quote": fact["quote"], "source": fragment_url(fact["url"], fact["quote"]),
        })
    for guess in card["evidence"]["inferences"]:
        claims.append({
            "badge": "~ úsudek", "label": label_for(guess["kind"]), "value": guess["value"],
            "quote": guess["quote"], "source": fragment_url(guess["url"], guess["quote"]),
        })

    facts_n, guesses_n = len(card["evidence"]["facts"]), len(card["evidence"]["inferences"])
    footer = {
        "facts": facts_n,
        "facts_label": plural(facts_n, "ověřený fakt", "ověřené fakty", "ověřených faktů"),
        "inferences": guesses_n,
        "inferences_label": plural(guesses_n, "úsudek", "úsudky", "úsudků"),
        "discarded": card["discarded"], "discounted": card.get("discounted", 0),
    }

    sections = [{"title": "Kontakty", "rows": contact_rows},
                {"title": "Proč teď", "rows": why_now_rows}]
    if tender_rows:
        sections.append({"title": "Zakázky", "rows": tender_rows})
    sections.append({"title": "Doklady bolesti", "claims": claims})

    return {"ico": ico, "name": card.get("name"), "rows": rows,
            "sections": sections, "footer": footer}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the salesperson's dossier.")
    parser.add_argument("ico", nargs="*")
    parser.add_argument("--top", type=int, help="build for the week's top N from select.py")
    parser.add_argument("--no-fetch", action="store_true",
                        help="use cached turnover only, never call justice.cz")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--web", action="store_true",
                        help="print for_web() shape instead of build()'s raw dict")
    args = parser.parse_args()

    archive = Archive()
    from pipeline.signals.now import ARES_CANDIDATES, load_companies
    companies = {c["ico"]: c for c in load_companies(ARES_CANDIDATES)}

    if args.top:
        from pipeline.scoring.select import run as select_run
        top, _ = select_run(top=args.top, archive=archive)
        icos = [row["ico"] for row in top]
    else:
        icos = args.ico

    turnover_cache = load_turnover_cache()
    websites, contacts = load_jsonl(WEBSITES), load_jsonl(CONTACTS)

    cards = []
    for ico in icos:
        card = build(ico, archive, companies, websites, contacts,
                     turnover_cache, fetch_turnover=not args.no_fetch)
        cards.append(for_web(card) if args.web else card)
        if not args.json and not args.web:
            print(render(card))
            print()

    if args.json or args.web:
        print(json.dumps(cards, ensure_ascii=False, indent=2))

    archive.close()

