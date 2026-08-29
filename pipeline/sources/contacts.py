"""Contacts: who to call at a company, and the proof it is really them.

The ICP aims at one role - jednatel or majitel. Their names are already
free and complete: ARES -vr gives them for all 3294 candidates, with the
date each took office. What is missing is a channel, and that only ever
lives on the company's own contact page.

So this module does not look for people. It looks for a way to reach
people we already know about, plus the generic company channels as a
fallback.

Two rules from the decision log shape everything here.

Section 6 - never invent an address. Guessing that "Petr Vojta" is
petr.vojta@firma.cz produces rubbish half the time. Matching an address
that is printed on the page against a name from the register is a
different act entirely: it is an observation, and it comes with a quote.
This module only ever matches; it never constructs.

Section 7 - the profile is of the company, not of the human. Everything
factual is keyed by ICO. A person appears only inside `people`, carrying
name, role as printed, channel and the source - no scores, no history,
nothing joined across sources. Addresses of personal shape are flagged,
because a person emailed at their own address must be able to opt out.

Evidence, as everywhere in this project: each contact carries the quote
it was read from, so verify.py can find that string in the archived page
later. A contact with no quote is not a contact.

Run:
    python -m pipeline.sources.contacts 26516189
"""

import argparse
import html as html_module
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from pipeline.sources.website import (
    Fetcher, LINK, MARKUP, contact_links, fold, to_text,
)

SITES = "data/raw/websites.jsonl"
COMPANIES = "data/raw/ares_candidates_v2.jsonl"
OUTPUT = "data/raw/contacts.jsonl"

# How much text around an anchor counts as "the same block". Measured on
# real pages: a person's row is name, role, e-mail and phone within about
# 120 characters. Wider than this and the next person's phone bleeds in.
WINDOW = 130

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Czech numbers appear as +420 123 456 789, 00420 123 456 789, or bare
# 123 456 789. The +420 form is matched first so the prefix is not left
# dangling in front of a "bare" match.
PHONE = re.compile(
    r"(?:(?:\+|00)420[\s -]?)?(?<!\d)(\d{3}[\s -]?\d{3}[\s -]?\d{3})(?!\d)"
)

# Local parts that belong to a desk, not a person. Anything else that
# looks like a surname is treated as personal - see personal_address().
FUNCTIONAL = {
    "info", "obchod", "office", "sekretariat", "podatelna", "firma",
    "kancelar", "prodej", "servis", "mail", "post", "sales", "export",
    "import", "fakturace", "faktury", "ucetni", "uctarna", "expedice",
    "logistika", "doprava", "vyroba", "sklad", "reklamace", "poptavka",
    "objednavky", "nakup", "personalni", "kariera", "hr", "praca",
    "prace", "job", "jobs", "marketing", "it", "helpdesk", "support",
    "dispecink", "dispecer", "recepce", "spravce", "admin", "webmaster",
    "gdpr", "kontakt", "contact", "zakaznik", "eshop", "shop", "no-reply",
    "noreply", "newsletter", "studio", "atelier", "provoz", "technik",
}

# Roles the ICP cares about. The buyer is the owner or CEO; their right
# hands and the production side are users, worth recording but not the
# person to call.
ROLE = re.compile(
    r"(?i)jednatel(?:ka)?|majitel(?:ka)?|spolumajitel|prokurist|"
    r"[řr]editel(?:ka)?|CEO|CTO|CFO|vedouc[ií]|manaž?er(?:ka)?|"
    r"obchodn[ií]|v[ýy]robn[ií]|technick|mistr|dispe[čc]er|pl[áa]nova[čc]|"
    r"p[řr]edseda|[čc]len p[řr]edstavenstva"
)

DECIDES = re.compile(r"(?i)jednatel|majitel|prokurist|[řr]editel|CEO|p[řr]edseda")

# Pages that sometimes list management when the contact page does not.
# Tried only as a fallback, and the reason is measured: on 25 companies
# whose contact page gave nothing but a switchboard, this found a named
# person at exactly one of them. Cheap enough to keep - contacts are
# enrichment of the final five, not a step run over the whole base - but
# it is not the missing half. Companies that print no names print none.
TEAM_HINT = re.compile(
    r"(?i)veden[ií]|management|p[řr]edstavenstvo|struktura|"
    r"n[áa][šs].?t[ýy]m|team|lid[ée]|o-?n[áa]s|o-?spole[čc]nosti"
)


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------


# The whole element is matched, not just the attribute. Replacing only
# the attribute puts the decoded address *inside* a tag, and the tag
# stripper then removes it along with the tag - the decode ran, the
# result was thrown away, and the page still looked address-free.
CFEMAIL = re.compile(r'<[^>]*\bdata-cfemail="([0-9a-fA-F]{6,})"[^>]*>')


def decode_cfemail(payload):
    """Undo Cloudflare's e-mail obfuscation.

    Cloudflare replaces addresses with a hex blob whose first byte is a
    XOR key for the rest. Sites behind it look like they publish no
    addresses at all: cobap.cz renders a 5300-character contact page on
    which a plain reader finds zero e-mails and which actually carries
    twenty-two.
    """
    try:
        key = int(payload[:2], 16)
        return "".join(
            chr(int(payload[i:i + 2], 16) ^ key)
            for i in range(2, len(payload), 2)
        )
    except ValueError:
        return ""


def page_text(html):
    """Visible text, with hidden addresses restored first.

    Order matters and it is not cosmetic. Two ways of hiding an address
    from scrapers are common enough to be worth undoing:

    * HTML entities - buzuluk.cz prints `&#105;nfo&#64;buzuluk.cz`
    * Cloudflare's data-cfemail blobs - see above

    Strip the tags before undoing either and the address is simply not
    in the text. Measured on 60 contact pages: Cloudflare on 2 %, and a
    JavaScript decoder on a further 5 % which is *not* handled here -
    running the page's script is out of scope, so those stay invisible.
    """
    restored = CFEMAIL.sub(lambda m: " " + decode_cfemail(m.group(1)) + " ", html)
    return to_text(html_module.unescape(restored))


def split_name(full_name):
    """(given, surname) with titles and initials dropped.

    ARES writes people as "Ing. PETR JEŘÁBEK" or "Bc. MARTIN JANEČKA",
    so the academic prefix has to go before either part can be read.
    """
    parts = [
        part for part in fold(full_name or "").replace(",", " ").split()
        if len(part) > 2 and not part.endswith(".")
    ]
    if not parts:
        return None, None
    return (parts[0] if len(parts) > 1 else None), parts[-1]


# ---------------------------------------------------------------------------
# Classifying what was found
# ---------------------------------------------------------------------------


def personal_address(address):
    """Whether an address is addressed at a human rather than a desk.

    Section 7 asks for these to be marked: a person written to at their
    own address has to be able to say stop, and the salesperson has to
    know which addresses those are.
    """
    local = address.split("@")[0].lower()
    stem = re.split(r"[._-]", local)[0]
    return local not in FUNCTIONAL and stem not in FUNCTIONAL


def mail_domains(text, site_domain):
    """Which mail hosts on this page belong to the company.

    Matching the website domain alone is too strict: ZVU Servis a.s.
    lives at zvuservis.cz but writes from @zvu.cz, the group's mail
    domain, so every named address on its contact page was thrown away.

    So the host that most addresses on the page share counts as the
    company's too. An agency's or a customer's address appears once;
    the company's own appears on every row of the contact table.
    """
    hosts = [address.split("@")[-1].lower() for address in EMAIL.findall(text)]
    accepted = set()

    if site_domain:
        accepted.add(site_domain.replace("www.", "").lower())

    if hosts:
        counts = {}
        for host in hosts:
            counts[host] = counts.get(host, 0) + 1
        dominant, seen = max(counts.items(), key=lambda item: item[1])
        # One lone address proves nothing - it could be the web studio.
        # Freemail is never a company's mail domain, however often it
        # appears; it is admitted only by the rule below, per address.
        if seen >= 2 and dominant not in FREEMAIL:
            accepted.add(dominant)

    return accepted


# Small firms really do run their business off a freemail box. The
# address is then only theirs if the local part is the company itself -
# alfafacility@seznam.cz belongs to Alfa Facility s.r.o.; a bare
# novak@seznam.cz on the same page does not.
FREEMAIL = {
    "gmail.com", "seznam.cz", "email.cz", "centrum.cz", "volny.cz",
    "post.cz", "atlas.cz", "tiscali.cz", "quick.cz", "outlook.com",
    "hotmail.com", "yahoo.com", "icloud.com", "protonmail.com", "iol.cz",
}


def company_freemail(address, company_name):
    """Whether a freemail address is the company's own business box."""
    host = address.split("@")[-1].lower()
    if host not in FREEMAIL:
        return False
    local = re.sub(r"[^a-z0-9]", "", fold(address.split("@")[0]))
    stem = re.sub(r"[^a-z0-9]", "", fold(SUFFIX_FREE.sub("", company_name or "")))
    return bool(local) and bool(stem) and (local in stem or stem in local)


SUFFIX_FREE = re.compile(r"(?i)[\s,]*(a\.?\s?s\.?|s\.?\s?r\.?\s?o\.?|spol\.?)[\s.,]*$")


def owns_domain(address, accepted):
    """Whether an address is on one of the company's own mail hosts."""
    if not accepted:
        return True
    host = address.split("@")[-1].lower()
    return any(host == good or host.endswith("." + good) or
               good.split(".")[0] == host.split(".")[0] for good in accepted)


def normalise_phone(raw):
    """One shape for a Czech number, so duplicates collapse."""
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00420"):
        digits = digits[5:]
    elif digits.startswith("420") and len(digits) > 9:
        digits = digits[3:]
    return "+420 " + " ".join([digits[:3], digits[3:6], digits[6:9]]) if len(digits) == 9 else None


def matches_name(address, surname, given=None):
    """Whether a printed address belongs to this particular person.

    Deliberately narrow: the address must already exist on the page.
    janecka@globalfol.cz matches Janečka, petr.jerabek@zvu.cz matches
    Jeřábek. Nothing is built from a name.

    The given name is checked because a surname alone is not a person.
    ZEPOS RS has Roman Ševců as jednatel and David Ševců as sales
    director; matching on "sevcu" handed Roman his colleague's address
    - a plausible, quotable and completely wrong contact.
    """
    if not surname:
        return False

    parts = [p for p in re.split(r"[._\-0-9]+", fold(address.split("@")[0])) if p]
    if surname not in parts:
        return False

    # Nothing but the surname: nobody else to confuse it with.
    others = [p for p in parts if p != surname and len(p) > 1]
    if not others:
        return True

    # first.last shape - the other part has to be this person's own
    # first name, or an initial of it.
    if not given:
        return False
    return any(part == given or (len(part) <= 2 and given.startswith(part))
               for part in others)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def window_at(text, position, width=WINDOW):
    start = max(0, position - width)
    end = min(len(text), position + width)
    return text[start:end].strip()


def find_people(text, directors, company_name, domain):
    """Locate the register's people on the page and read their channel.

    Anchored on names we already hold from ARES rather than on anything
    that looks like a name: the register is the authority on who runs
    the company, and inventing a person-detector would only add a way to
    be wrong.
    """
    folded = fold(text)
    company_key = fold(company_name or "")
    accepted = mail_domains(text, domain)
    found = []

    for person in directors or []:
        name = person.get("name")
        given, surname = split_name(name)
        if not surname:
            continue

        # A surname that is part of the company's own name matches on
        # every page and proves nothing. HrubyMOVING TRANSPORT a.s. has
        # a director named Hrubý, and the word is in the logo, the title
        # and every menu item.
        if surname in re.sub(r"[^a-z]", "", company_key):
            found.append({
                "name": name, "role_registered": person.get("role"),
                "since": person.get("since"), "email": None, "phone": None,
                "role_on_page": None, "quote": None,
                "note": "surname is part of the company name - page mentions are not evidence",
            })
            continue

        entry = {
            "name": name, "role_registered": person.get("role"),
            "since": person.get("since"), "email": None, "phone": None,
            "role_on_page": None, "quote": None,
        }

        # How many different people on this page share the surname. If
        # more than one, a bare surname@ address does not identify
        # anybody: ZEPOS RS prints sevcu@, david.sevcu@, hana.sevcu@ and
        # vojtech.sevcu@ on one page.
        namesakes = {
            fold(a.split("@")[0]) for a in EMAIL.findall(text)
            if owns_domain(a, accepted) and surname in re.split(r"[._\-0-9]+", fold(a.split("@")[0]))
        }
        shared = len(namesakes) > 1

        best_score = -1
        for match in re.finditer(re.escape(surname), folded):
            # Details are read forward from the name, not from a window
            # centred on it. A contact table runs "name role e-mail
            # phone, name role e-mail phone" with no separator the text
            # layer preserves, so a centred window reaches back into the
            # previous person's row - on globalfol.cz that gave three
            # different jednatelé the same telephone number.
            segment = text[match.start():match.start() + WINDOW]

            emails = [a for a in EMAIL.findall(segment)
                      if owns_domain(a, accepted) or company_freemail(a, company_name)]
            mine = [a for a in emails if matches_name(a, surname, given)]
            phones = [p for p in (normalise_phone(x) for x in PHONE.findall(segment)) if p]
            role = ROLE.search(segment)

            if not (mine or phones or role):
                continue

            # A bare surname@ address when several colleagues share the
            # surname names a family, not a person - keep it, but say so.
            bare = mine and "." not in mine[0].split("@")[0]
            ambiguous = bool(bare and shared)

            # Every occurrence of the name is scored and the fullest one
            # wins. Breaking on the first address found lost the row that
            # also carried "majitel a jednatel" and the direct line.
            score = (3 if mine and not ambiguous else 0) + (2 if role else 0) + (1 if phones else 0)
            if score <= best_score:
                continue
            best_score = score

            entry["quote"] = text[max(0, match.start() - 40):match.start() + WINDOW].strip()
            entry["email"] = mine[0] if mine else None
            entry["phone"] = phones[0] if phones else None
            entry["role_on_page"] = role.group(0) if role else None
            entry["ambiguous_email"] = ambiguous or None

        found.append(entry)

    return found


# A Czech personal name as printed on a contact page: given name in
# title case, surname in either case, optionally behind academic titles.
PAGE_NAME = re.compile(
    r"(?:(?:Ing|Bc|Mgr|MUDr|JUDr|Dr|PhDr|Ph\.D|MBA|DiS)\.\s*){0,2}"
    r"([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]{2,})\s+"
    r"([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽa-záčďéěíňóřšťúůýž]{2,})"
)

# Words that look like a name to the expression above but are not one.
# Every entry here came out of a live false positive: "Velká Bystřice"
# and "Stará Voda" are towns, "Výroba Alois" and "Slovensko Robert" are
# a heading glued to the next word, "Tábor IČ" is a fragment of an
# address block.
NOT_A_NAME = {
    "velka", "stara", "nova", "nove", "novy", "mesto", "mesta", "obec",
    "praha", "brno", "ostrava", "plzen", "olomouc", "liberec", "tabor",
    "slovensko", "cesko", "ceska", "ceske", "republika", "vyroba",
    "vyrobni", "obchod", "obchodni", "servis", "sklad", "provoz",
    "kancelar", "sidlo", "adresa", "ulice", "namesti", "divize",
    "pobocka", "centrala", "zavod", "stredisko", "oddeleni", "kontakt",
    "telefon", "mobil", "email", "fakturace", "ico", "dic", "spolecnost",
}


def looks_like_person(given, surname, city=None):
    """Reject the things the name expression matches but should not."""
    g, s = fold(given), fold(surname)
    # Compared by stem, so one entry covers kontakt/kontakty,
    # mesto/mesta, obchod/obchodni.
    if any(g.startswith(w) or s.startswith(w) for w in NOT_A_NAME):
        return False
    # The company's own town, written as "Velká Bystřice", reads as a
    # first name plus a surname to any pattern loose enough to catch
    # "Zdeněk MÁDR".
    if city and fold(city) in f"{g} {s}":
        return False
    return True


def find_page_people(text, domain, company_name, city, known):
    """Named people printed on the page who are not in the register.

    Why this exists: on 27 % of the companies where no register name
    could be found, the contact page does list people - ENETEX
    TECHNOLOGY prints six of them with roles and direct lines, and not
    one is its jednatel. Refusing to read them was throwing away the
    users the ICP names: vedoucí výroby, mistr, dispečer.

    These are weaker than a register match by construction - the
    register never confirmed this person works here - so they are
    reported at their own tier and are never presented as the decision
    maker. Two tiers again:

        page_named       an address whose local part carries the name
        page_named_phone a name and a number, nothing tying them
                         together beyond sitting side by side

    The second one is where the web studio hides: "Pavel Procházka
    +420 595 223 218" turned up on the contact pages of two unrelated
    companies, because it is the agency that built both sites.
    """
    accepted = mail_domains(text, domain)
    people, seen = [], set(known)

    for match in ROLE.finditer(text):
        lead = text[max(0, match.start() - 70):match.start()]
        names = PAGE_NAME.findall(lead)
        if not names:
            continue

        given, surname = names[-1]
        if not looks_like_person(given, surname, city):
            continue
        key = (fold(given), fold(surname))
        if key in seen:
            continue

        segment = text[match.start():match.start() + WINDOW]
        emails = [
            a for a in EMAIL.findall(segment)
            if owns_domain(a, accepted) or company_freemail(a, company_name)
        ]
        mine = [a for a in emails if matches_name(a, fold(surname), fold(given))]
        phones = [p for p in (normalise_phone(x) for x in PHONE.findall(segment)) if p]
        if not (mine or phones):
            continue

        seen.add(key)
        people.append({
            "name": f"{given} {surname}",
            "role_registered": None,
            "since": None,
            "role_on_page": match.group(0),
            "email": mine[0] if mine else None,
            "phone": phones[0] if phones else None,
            "source": "page" if mine else "page_weak",
            "quote": text[max(0, match.start() - 70):match.start() + WINDOW].strip(),
        })

    return people


def company_channels(text, domain, company_name=None):
    """The generic channels, and every personal address on the page.

    The fallback from section 6: when no address can be tied to a named
    person, the salesperson still gets the company line plus the name
    from the register, and asks for them by name.
    """
    accepted = mail_domains(text, domain)
    addresses = sorted({
        address.lower() for address in EMAIL.findall(text)
        if owns_domain(address.lower(), accepted)
        or company_freemail(address.lower(), company_name)
    })
    phones = sorted({p for p in (normalise_phone(x) for x in PHONE.findall(text)) if p})

    generic = [a for a in addresses if not personal_address(a)]
    personal = [a for a in addresses if personal_address(a)]

    return {"emails": generic, "personal_emails": personal, "phones": phones}


def team_links(html, base_url, limit=3):
    """Same-site links that might list management, best effort."""
    from urllib.parse import urljoin, urlparse

    host = urlparse(base_url).netloc.lower().replace("www.", "")
    found, seen = [], set()

    for href, anchor in LINK.findall(html):
        label = MARKUP.sub(" ", anchor)
        if not (TEAM_HINT.search(href) or TEAM_HINT.search(label)):
            continue
        url = urljoin(base_url, href.strip())
        parts = urlparse(url)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().replace("www.", "") != host or url in seen:
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= limit:
            break
    return found


def quote_for(text, needle):
    """The neighbourhood a value was read from, for the evidence layer."""
    position = text.find(needle)
    return window_at(text, position) if position >= 0 else None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def from_archive(archive, ico):
    """Pages already harvested for this company, or [] if none.

    Reading here instead of fetching again is the point of the single
    harvest in website.py: the same sites used to be visited three times
    - once to resolve the domain, once for contacts, once for the
    production-mode signal. The archived text is what the resolver read,
    with entities and Cloudflare blobs already decoded, so nothing is
    lost by not having the markup.
    """
    if archive is None:
        return []
    pages = []
    for row, text in archive.documents(ico, source="website"):
        if text:
            pages.append((row["url"], text, row["kind"]))
    # Contact pages first: they carry the named people, and read() stops
    # improving once it has found them.
    order = {"contact": 0, "about": 1, "home": 2, "career": 3}
    pages.sort(key=lambda p: order.get(p[2], 9))
    return pages


def get_contacts(ico, site, company, fetcher=None, archive=None):
    """Collect contacts for one company from its own website.

    `site` is a row of websites.jsonl, `company` a row of
    ares_candidates.jsonl. Returns a dict that always carries `status`:

        ok            something was read off the site
        no_site       no website was ever resolved
        unreachable   the site would not answer this time

    The site's own proof status is carried through untouched. A contact
    read from a `probable` site is a contact of a site we could not tie
    to this ICO, and the card must say so rather than quietly presenting
    it as this company's phone number.
    """
    fetcher = fetcher or Fetcher()
    result = {
        "ico": ico,
        "name": company.get("name"),
        "status": None,
        "site_status": site.get("status") if site else None,
        "site_evidence": site.get("evidence") if site else None,
        "domain": site.get("domain") if site else None,
        "source_url": None,
        "company": {"emails": [], "personal_emails": [], "phones": []},
        "people": [],
        "retrieved_at": date.today().isoformat(),
    }

    if not site or site.get("status") not in ("proven", "probable") or not site.get("domain"):
        result["status"] = "no_site"
        return result

    # Prefer what the harvest already stored. Falling back to the
    # network keeps this module usable on a company that was resolved
    # before the archive existed, or when it is run on its own.
    archived = from_archive(archive, ico)
    pages = [(url, text, kind) for url, text, kind in archived]
    result["read_from"] = "archive" if pages else "network"

    if not pages:
        first = site.get("url") or f"https://{site['domain']}"
        html, url = fetcher.get(first)
        if html:
            pages.append((url, page_text(html), None))
            if "kontakt" not in url.lower():
                for link in contact_links(html, url, limit=2):
                    body, resolved = fetcher.get(link)
                    if body:
                        pages.append((resolved, page_text(body), None))

    if not pages:
        result["status"] = "unreachable"
        return result

    def read(collected):
        best = None
        for url, text, _kind in collected:
            channels = company_channels(text, site["domain"], company.get("name"))
            people = find_people(text, company.get("directors"), company.get("name"), site["domain"])
            # People the register knows come first and are never
            # overwritten; the page can only add to them.
            # Keyed on given+surname, not on the printed string: the
            # register writes "Bc. MARTIN JANEČKA" and the page writes
            # "Martin Janečka", and comparing those as text listed the
            # same man twice.
            known = {split_name(p["name"]) for p in people if p.get("name")}
            people = people + find_page_people(
                text, site["domain"], company.get("name"), company.get("city"), known
            )
            reachable = sum(1 for person in people if person.get("email") or person.get("phone"))
            score = reachable * 10 + len(channels["emails"]) + len(channels["phones"])
            if best is None or score > best[0]:
                best = (score, url, channels, people)
        return best

    best = read(pages)

    # Nobody from the register was reachable. The management-page
    # fallback only applies when reading from the network - the harvest
    # already collected `about` pages, which is where those names live,
    # so from the archive there is nothing further to fetch.
    if (result["read_from"] == "network" and best
            and not any(p.get("email") or p.get("phone") for p in best[3])):
        html, url = fetcher.get(pages[0][0])
        if html:
            for link in team_links(html, url):
                body, resolved = fetcher.get(link)
                if body:
                    pages.append((resolved, page_text(body), None))
            best = read(pages)

    _, url, channels, people = best
    result.update({"status": "ok", "source_url": url, "company": channels, "people": people})

    # Where the generic channels were read from, so the card can show it.
    text = next(t for u, t, _ in pages if u == url)
    result["company_quote"] = quote_for(text, (channels["emails"] or channels["phones"] or [""])[0])
    return result


def load(path, key="ico"):
    with open(path, encoding="utf-8") as handle:
        return {json.loads(line)[key]: json.loads(line) for line in handle}


def run_all(limit=None, workers=8, archive=None):
    """Read contacts for every company whose website is known.

    Appends one line per company as it finishes, and skips ICOs already
    in the file, so an interrupted run resumes instead of restarting.
    That is not a general principle, it is a lesson: the WHOIS pass in
    website.py collects in memory and rewrites at the end, and losing it
    to one stray click cost forty minutes.

    Only companies with a resolved domain are worth asking about - the
    rest have nowhere to read from.
    """
    sites, companies = load(SITES), load(COMPANIES)

    done = set()
    if Path(OUTPUT).exists():
        with open(OUTPUT, encoding="utf-8") as handle:
            for line in handle:
                try:
                    done.add(json.loads(line)["ico"])
                except (json.JSONDecodeError, KeyError):
                    continue
        print(f"resuming: {len(done)} already done", file=sys.stderr)

    todo = [
        ico for ico, site in sites.items()
        if ico not in done
        and site.get("status") in ("proven", "probable")
        and site.get("domain")
        and ico in companies
    ]
    if limit:
        todo = todo[:limit]
    print(f"reading contacts for {len(todo)} companies with {workers} workers",
          file=sys.stderr)

    fetcher = Fetcher()

    def work(ico):
        try:
            return get_contacts(ico, sites[ico], companies[ico], fetcher, archive)
        except Exception as error:  # one broken site must not end the run
            return {
                "ico": ico, "name": companies[ico].get("name"), "status": "error",
                "reason": f"{type(error).__name__}: {error}",
                "people": [], "company": {"emails": [], "personal_emails": [], "phones": []},
                "retrieved_at": date.today().isoformat(),
            }

    tally = {}
    Path(OUTPUT).parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "a", encoding="utf-8") as sink:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, row in enumerate(pool.map(work, todo), 1):
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()

                reachable = [p for p in row["people"] if p.get("email") or p.get("phone")]
                if row["status"] != "ok":
                    key = row["status"]
                elif any((p.get("source") or "register") == "register" for p in reachable):
                    key = "register"
                elif any(p.get("source") == "page" for p in reachable):
                    key = "page"
                elif reachable:
                    key = "page_weak"
                elif row["company"]["emails"] or row["company"]["phones"]:
                    key = "generic_only"
                else:
                    key = "nothing"
                tally[key] = tally.get(key, 0) + 1

                if index % 50 == 0:
                    print(f"  {index}/{len(todo)}  {tally}", file=sys.stderr)

    total = sum(tally.values()) or 1
    print("\nfinished:", file=sys.stderr)
    for key in ("register", "page", "page_weak", "generic_only", "nothing",
                "unreachable", "no_site", "error"):
        count = tally.get(key, 0)
        if count:
            print(f"  {key:13} {count:5}  {count / total * 100:5.1f} %", file=sys.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read contacts off company websites.")
    parser.add_argument("ico", nargs="?", help="single ICO")
    parser.add_argument("--all", action="store_true", help="every company with a known site")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--archive", action="store_true",
                        help="read pages from the evidence store instead of refetching")
    args = parser.parse_args()

    store = None
    if args.archive:
        from pipeline.evidence.archive import Archive
        store = Archive()

    if args.all:
        run_all(args.limit, args.workers, store)
    elif args.ico:
        target = str(args.ico).zfill(8)
        print(json.dumps(
            get_contacts(target, load(SITES).get(target), load(COMPANIES).get(target, {}),
                         archive=store),
            ensure_ascii=False, indent=2,
        ))
    else:
        parser.error("give an ICO, or --all")
