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

from pipeline.evidence.archive import Archive
from pipeline.scoring.select import CONTACTS, WEBSITES, fit_assessment, load_jsonl
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
          turnover_cache=None, fetch_turnover=True, tenders=None):
    """Everything known about one company, grouped the way it is read."""
    ico = str(ico).zfill(8)
    websites = websites if websites is not None else load_jsonl(WEBSITES)
    contacts = contacts if contacts is not None else load_jsonl(CONTACTS)
    # Loaded rather than left to default to None - the fourth time an
    # optional argument would have quietly switched a source off.
    tenders = tenders if tenders is not None else load_tenders()
    company = (companies or {}).get(ico, {})

    claims = archive.claims(ico)
    evidence = {"facts": [], "inferences": []}
    for row in claims:
        if row["kind"].startswith("now:"):
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
    by_name = {p.get("name"): p for p in ((contacts.get(ico) or {}).get("people") or [])}
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
        })

    tender_people = tender_contacts(ico, tenders)

    return {
        "ico": ico,
        "name": company.get("name") or site.get("name"),
        "region": company.get("region"),
        "district": company.get("district"),
        "city": company.get("city"),
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
        "why_now": now_events,
        "evidence": evidence,
        "discarded": len(archive.discards(ico=ico)),
    }


ARES_REST = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"

LABEL = 22          # width of the label column
VALUE = 66          # width of the value column before the source


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
    """
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
        row("Velikost", card.get("size"), f"{ARES_REST}/ekonomicke-subjekty-res/{ico}"),
        row("NACE", card.get("nace"), f"{ARES_REST}/ekonomicke-subjekty-res/{ico}"),
    ]

    # The ICP's first criterion, answered from evidence rather than
    # assumed - and the row that was missing when a construction firm
    # reached a card with nothing saying so.
    fit = card.get("fit") or {}
    MODE_CZ = {"made_to_order": "zakázková (přímé tvrzení)",
               "mixed": "zakázková i sériová (přímá tvrzení)",
               "small_batch": "malosériová (přímé tvrzení)",
               "leaning_made_to_order": "spíše zakázková (nepřímé stopy)",
               "leaning_serial": "spíše sériová (nepřímé stopy)",
               "serial": "sériová (přímé tvrzení)",
               "unknown": ""}
    mode_note = MODE_CZ.get(fit.get("mode"), "")
    tier_note = ("obor mimo jádro ICP — prověřit, zda plánuje vlastní kapacity"
                 if fit.get("nace_tier") == "service" else "")
    joined = " · ".join(x for x in (mode_note, tier_note) if x)
    out.append(row("Režim výroby", joined,
                   f"https://{card['website']['domain']}" if card["website"].get("domain") else ""))

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
    TIER_NOTE = {
        "register": "jednatel z rejstříku",
        "company":  "zaměstnanec firmy",
        "external": "administrátor zakázky, ne firma",
    }
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

    if card["why_now"]:
        for event in card["why_now"]:
            out.append(row("Proč teď", f"{event['kind']}: {event['value']}",
                           event.get("url") or "registr"))
    else:
        out.append(row("Proč teď", ""))

    # Pain evidence last, one row per finding, quote underneath. This is
    # the part that justifies the call, so it is the part where every
    # line has to be checkable on its own.
    for fact in card["evidence"]["facts"]:
        if fact["kind"].startswith(("certificate:", "turnover_web")):
            continue                      # already printed in their own rows
        out.append(row(fact["kind"], fact["value"], fact["source"]))
        if fact["quote"]:
            out.append(f"  {'':<{LABEL}}„{fact['quote'][:VALUE - 2]}\"")
    for guess in card["evidence"]["inferences"]:
        out.append(row(guess["kind"], guess["value"], guess["source"], state="inference"))

    out.append("")
    out.append(f"  {len(card['evidence']['facts'])} ověřených faktů · "
               f"{len(card['evidence']['inferences'])} úsudků · "
               f"{card['discarded']} zahozeno při ověření")
    return "\n".join(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the salesperson's dossier.")
    parser.add_argument("ico", nargs="*")
    parser.add_argument("--top", type=int, help="build for the week's top N from select.py")
    parser.add_argument("--no-fetch", action="store_true",
                        help="use cached turnover only, never call justice.cz")
    parser.add_argument("--json", action="store_true")
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
        cards.append(card)
        if not args.json:
            print(render(card))
            print()

    if args.json:
        print(json.dumps(cards, ensure_ascii=False, indent=2))

    archive.close()

