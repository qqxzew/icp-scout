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
from pathlib import Path

from pipeline.evidence.archive import Archive
from pipeline.scoring.select import CONTACTS, WEBSITES, load_jsonl

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
          turnover_cache=None, fetch_turnover=True):
    """Everything known about one company, grouped the way it is read."""
    ico = str(ico).zfill(8)
    websites = websites if websites is not None else load_jsonl(WEBSITES)
    contacts = contacts if contacts is not None else load_jsonl(CONTACTS)
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

    site = websites.get(ico, {})
    people = [p for p in ((contacts.get(ico) or {}).get("people") or [])
              if p.get("email") or p.get("phone")]

    return {
        "ico": ico,
        "name": company.get("name") or site.get("name"),
        "region": company.get("region"),
        "size": company.get("employee_range"),
        "nace": company.get("nace"),
        # Empty for most companies, and that is the honest state - see
        # the module docstring.
        "turnover": turnover_for(ico, turnover_cache, fetch_turnover),
        "turnover_site": turnover_from_site(archive, ico),
        "certificates": certificates_for(ico),
        "website": {"domain": site.get("domain"), "status": site.get("status")},
        "contacts": people,
        "why_now": now_events,
        "evidence": evidence,
        "discarded": len(archive.discards(ico=ico)),
    }


def render(card):
    """The card as plain text - what a salesperson would actually skim."""
    out = [f"{card['name']}   (IČO {card['ico']})",
           f"  {card['region']} · {card['size']} · NACE {card['nace']}"]

    # Two independent sources, printed as two lines rather than one
    # merged figure - the register's number is audited, the site's is the
    # company's own claim, and a salesperson quoting either should know
    # which one they are holding.
    t = card["turnover"]
    if t["value_czk"]:
        out.append(f"  Obrat (závěrka): {t['value_czk']:,} Kč ({t['year']})".replace(",", " "))
    else:
        out.append(f"  Obrat (závěrka): — ({t['note'] or t['status']})")

    for site_turnover in card.get("turnover_site") or []:
        out.append(f"  Obrat (web): {site_turnover['value']}")
        if site_turnover["quote"]:
            out.append(f"        „{site_turnover['quote'][:96]}\"  {site_turnover['url'] or ''}")

    certs = card["certificates"]
    if certs:
        for c in certs:
            if not c.get("standard"):
                continue
            bits = [c["standard"]]
            if c.get("number"):
                bits.append(f"č. {c['number']}")
            if c.get("issuer"):
                bits.append(f"vydal {c['issuer']}")
            out.append(f"  Certifikát: {' · '.join(bits)}  [{c['tier']}]")
    else:
        out.append("  Certifikát: —")

    site = card["website"]
    out.append(f"  Web: {site['domain'] or '—'} [{site['status']}]")

    out.append("  PROČ TEĎ:")
    for e in card["why_now"] or []:
        out.append(f"    · {e['kind']}: {e['value']}")
    if not card["why_now"]:
        out.append("    · —")

    out.append("  KOMU VOLAT:")
    for p in card["contacts"][:4]:
        channel = p.get("email") or p.get("phone")
        out.append(f"    · {p.get('name')} ({p.get('role_registered')}) — {channel}")
    if not card["contacts"]:
        out.append("    · — (jen obecný kanál)")

    out.append(f"  DŮKAZY: {len(card['evidence']['facts'])} faktů, "
               f"{len(card['evidence']['inferences'])} úsudků, "
               f"{card['discarded']} zahozeno")
    for f in card["evidence"]["facts"][:6]:
        out.append(f"    ✓ [{f['kind']}] {f['value'][:88]}")
        if f["quote"]:
            out.append(f"        „{f['quote'][:96]}\"  {f['source']}")
    for i in card["evidence"]["inferences"][:3]:
        out.append(f"    ~ [{i['kind']}] {i['value'][:88]}")

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
