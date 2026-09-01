"""NEN: the tender a company publishes when it is actually buying.

The subsidy says money was granted; this says the money is being spent.
That difference is the whole reason this module exists, and it is a
difference in *freshness*, measured: a subsidy reaches us 21 to 51 days
after signing, because MMR publishes one file a month covering up to the
23rd of the previous month. A tender is on NEN the day it is published.

WHY A PRIVATE MANUFACTURER IS IN A PUBLIC PROCUREMENT SYSTEM AT ALL.
NEN is normally where ministries and towns buy things. But a company
that took an OP TAK subsidy is required to run its purchase through a
certified contracting-authority profile, because it is spending European
money - and NEN is one of those profiles. So ordinary private factories
turn up, publishing things like "dodávka, implementace, customizace ERP
systému" or "hardwarových terminálů". The legal basis was checked
against apiagentura.gov.cz; the source names no monetary threshold.

THE SIGN IS TWO-SIDED, AND THAT IS NOT THIS MODULE'S PROBLEM TO SOLVE.
A company running an ERP tender is either about to fix the exact gap
RTsoft sells into, or about to become a competitor's customer. RTsoft
were asked directly and answered: "někdy to bude hot lead, jindy
ztracený zákazník, ale na první pohled to vidět nebude - je potřeba ty
leady vidět a pak v nich hledat vodítka." So this module surfaces and
labels; it does not judge.

HOW IT IS QUERIED, AND WHY NOT THE OBVIOUS WAY:

* By our own ICO, one company at a time (`zadavatelICO`), not by
  crawling the catalogue. NEN holds ~278 000 procurements across 5563
  pages; we care about 3299 companies, almost none of which have ever
  published anything. Asking about ours is the same pull-based shape as
  ares_notifications - the event finds the company.
* NOT by the full-text box. Measured: `query=ERP` returns "Bezhotovostní
  odběr pohonných hmot formou karet" as its first hit, so it matches
  something other than the procurement's name and is useless as a
  keyword filter. Subject keywords are applied to what comes back
  instead.

A SESSION COOKIE IS REQUIRED AND THIS IS NOT OPTIONAL. Without one the
server answers 11 kB of "Nemáte povolený javascript" instead of the
listing; with one, the same URL returns the fully rendered table and the
filter is applied server-side. Verified both ways: ICO 27975924 gives 0
rows, ICO 00006947 (Ministerstvo financí) gives 50, all of them theirs.

Run:
    python -m pipeline.sources.nen --ico 00006947
    python -m pipeline.sources.nen --all --limit 200
"""

import argparse
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date
from http.cookiejar import CookieJar
from pathlib import Path

BASE = "https://nen.nipez.cz"
LISTING = f"{BASE}/verejne-zakazky"
OUTPUT = Path("data/raw/nen.jsonl")
CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")

USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"
TIMEOUT = 45
DELAY = 0.6          # seconds between requests; nothing here is urgent

# Same policy as ares.py, for the same reason: a 5xx during a long sweep
# means the server is busy, not that the answer is empty.
MAX_ATTEMPTS = 4
BACKOFF = 2          # seconds, doubled on every further attempt
RETRY_STATUSES = (429, 500, 502, 503, 504)

# The listing renders one <td> per field, each labelled by data-title.
# Parsing on that rather than on column order, because column order is a
# layout decision and NEN is free to change it.
CELL = re.compile(r'data-title="([^"]+)">([^<]*)')
ROW = re.compile(r'<tr class="gov-table__row">(.*?)</tr>', re.S)
DETAIL_LINK = re.compile(r'detail-zakazky/(N\d{3}-\d{2}-V\d+)')

FIELDS = {
    "Systémové číslo NEN": "number",
    "Název zadávacího postupu": "name",
    "Aktuální stav": "status",
    "Zadavatel": "authority",
    "Lhůta podání nabídek": "deadline",
}

# WHAT COUNTS AS AN INTERESTING SUBJECT - CODEBOOK FIRST, WORDS SECOND.
#
# Every tender is classified against CPV, the EU procurement codebook,
# by the buyer themselves. That is a structured fact where a keyword
# pattern is a guess, and the measurement was decisive: of 74 open
# tenders this regex rejected, 11 were IT by CPV - including
# "Rozšíření informačního systému BYZNYS" (a Czech ERP),
# "Digitální transformace ve společnosti PILA MARTINŮ" and
# "Pořízení a implementace CAD/PDM SW". The names say "digitalizace" and
# "digitální podnik", words no sensible pattern would have contained,
# and CPV files them correctly regardless. Open relevant tenders went
# from 4 to 15 on the same data.
#
#   48     software packages and information systems
#   72     IT services - programming, implementation, support
#   42961  control and command systems, which is where MES and shop
#          floor automation land rather than under software
RELEVANT_CPV = ("48", "72", "42961")

# Kept as a second chance, not as the rule. CPV is assigned by the buyer
# and is sometimes generic ("44" for a machine that happens to include
# planning software), so a name that says ERP outright still counts even
# when the code does not.
SUBJECT = re.compile(
    r"(?i)\bERP\b|\bMES\b|\bWMS\b|\bAPS\b|informa[cč]n[ií]\s*syst[ée]m|"
    r"[rř][ií]zen[ií]\s*v[ýy]roby|pl[aá]nov[aá]n[ií]\s*v[ýy]roby|"
    r"v[ýy]robn[ií]\s*syst[ée]m|termin[aá]l|[cč]&#x00E1;rov|čárov[ée]\s*k[oó]dy|"
    r"sb[eě]r\s*dat|dispe[cč]|sklado[vw]"
)


def relevant_cpv(code):
    """Whether a CPV code names something RTsoft could supply."""
    return bool(code) and any(code.startswith(p) for p in RELEVANT_CPV)


class Session:
    """One cookie-bearing connection to NEN, reused across queries.

    The cookie is not a nicety. A request without one gets an 11 kB page
    saying JavaScript is required; the same request with one gets the
    rendered table. One visit to the root sets it, and every later query
    rides along.
    """

    def __init__(self):
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [("User-Agent", USER_AGENT)]
        self._last = 0.0
        self.get(BASE + "/")          # sets the session cookie

    def get(self, url):
        """One page, retrying the failures that mean "not now" not "no".

        NEN answers 503 under a sustained sweep - seen live, one company
        lost mid-run before this existed. Swallowing it would record
        "this company has no tenders", which is the same mistake the DNS
        resolver made in website.py: a temporary refusal read as a final
        answer. The retry is what tells the two apart.
        """
        for attempt in range(MAX_ATTEMPTS):
            wait = DELAY - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                with self.opener.open(url, timeout=TIMEOUT) as response:
                    body = response.read()
                self._last = time.monotonic()
                return body.decode("utf-8", "replace")
            except urllib.error.HTTPError as error:
                self._last = time.monotonic()
                if error.code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS - 1:
                    raise
                delay = BACKOFF * (2 ** attempt)
                print(f"nen: HTTP {error.code}, retry {attempt + 1}/"
                      f"{MAX_ATTEMPTS - 1} in {delay}s", file=sys.stderr)
                time.sleep(delay)
        raise RuntimeError(f"nen: {url} still failing after {MAX_ATTEMPTS} attempts")


def parse_rows(html):
    """Every procurement on one listing page."""
    out = []
    for match in ROW.finditer(html):
        chunk = match.group(1)
        row = {FIELDS[label]: value.strip()
               for label, value in CELL.findall(chunk) if label in FIELDS}
        if not row.get("number"):
            continue
        link = DETAIL_LINK.search(chunk)
        if link:
            row["url"] = f"{BASE}/verejne-zakazky/detail-zakazky/{link.group(1)}"
        out.append(row)
    return out


def tenders_for(ico, session=None):
    """Every procurement this company has published, newest page first.

    An empty list is the normal answer: almost no company in the base has
    ever run a public tender, and that absence is itself half of the
    strongest signal - a subsidy granted with no tender yet means the
    money is there and the procurement has not started.
    """
    session = session or Session()
    ico = str(ico).zfill(8)
    page = session.get(f"{LISTING}/p:vz:zadavatelICO={ico}")
    rows = parse_rows(page)
    for row in rows:
        row["ico"] = ico
        # Provisional: the listing carries no CPV, so this is the weaker
        # of the two tests. with_detail() replaces it once the code is
        # known.
        row["relevant"] = bool(SUBJECT.search(row.get("name") or ""))
        row["retrieved_at"] = date.today().isoformat()
    return rows


def with_detail(rows, session=None, register_people=(), domain=None):
    """Fetch each tender's own page and re-decide relevance on its CPV.

    One request per tender. Worth it: the codebook found nearly four
    times as many relevant open tenders as the name pattern did, and it
    also brings the publication date and the contact, neither of which
    the listing carries.
    """
    session = session or Session()
    for row in rows:
        try:
            found = detail(row["url"], session, register_people, domain)
        except Exception as error:
            row["detail_error"] = f"{type(error).__name__}: {error}"
            continue
        row.update(found)
        row["relevant"] = relevant_cpv(row.get("cpv")) or row["relevant"]
    return rows


# The detail page lays every field out as one tile: a label in <h3> and
# its value in the <p> that follows. Parsing on that shape rather than on
# a list of expected labels means a field NEN adds later shows up on its
# own instead of being silently dropped.
TILE = re.compile(
    r'<div[^>]*class="gov-grid-tile"[^>]*>\s*<h3[^>]*>(.*?)</h3>\s*<p[^>]*>(.*?)</p>',
    re.S,
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+420[\d\s]{9,}")
TAGS = re.compile(r"<[^>]+>")

DETAIL_FIELDS = {
    "Aktuální stav ZP": "status",
    "Datum uveřejnění ZP na profil": "published",
    "Lhůta pro podání nabídek": "deadline",
    "Režim VZ dle volby zadavatele": "regime",
    "Druh zadávacího postupu": "procedure",
    "Druh": "contract_type",
    "Kód z číselníku CPV": "cpv",
    "Název z číselníku CPV": "cpv_name",
    "Hlavní místo plnění": "place",
    "Jméno": "contact_first_name",
    "Příjmení": "contact_last_name",
}

# Obvious filler. A procurement notice is a legal document and the phone
# field still gets typed as 111111111 - seen live on one of the first
# three companies checked, so it is worth refusing rather than printing
# on a card as if someone could ring it.
FAKE_PHONE = re.compile(r"^\+420\s*(\d)\1{8}$")


def strip_tags(markup):
    return html.unescape(TAGS.sub(" ", markup or "")).strip()


def fold(text):
    """Lowercase, diacritics removed - for comparing names across sources."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


def classify_contact(name, email, register_people, domain):
    """How much a tender's contact person is worth, and why.

    Three tiers, and the order is the point - a name checked against the
    state register beats any amount of matching on an email domain:

        register  the name is a director or owner in ARES. This is the
                  person the ICP actually wants, reached on a channel
                  they published themselves.
        company   not in the register, but the address is on the
                  company's own proven domain - an employee, so a real
                  way in, just not the decision-maker.
        external  a different domain entirely. Measured on the first
                  three companies checked: two of three were grant
                  consultancies (eufc.cz, grantex.cz) administering the
                  procurement on the company's behalf. Useful to know,
                  wrong to present as the company's own contact.

    This is contacts.py's rule applied to a new source: do not look for
    people, look for a channel to the people the register already names.
    """
    surnames = {fold(p).split()[-1] for p in register_people if fold(p).split()}
    folded = fold(name)
    if folded and folded.split()[-1] in surnames:
        return "register"
    if email and domain and email.split("@")[-1].lower().endswith(domain.lower()):
        return "company"
    return "external"


def detail(url, session=None, register_people=(), domain=None):
    """Everything the tender's own page states, with the contact graded.

    One extra request per tender, which is why it is not done during the
    listing sweep: the listing answers "is this company buying at all",
    and only the few that are get read in full.
    """
    session = session or Session()
    page = session.get(url)

    fields = {}
    for label, value in TILE.findall(page):
        key = DETAIL_FIELDS.get(strip_tags(label))
        if key and key not in fields:
            fields[key] = strip_tags(value)

    # Email, phone and the subject description sit in a tile whose inner
    # markup merges them, so they are pulled from the page directly
    # rather than from the tile map - but from the text, not the markup.
    # Searching the raw HTML returned "%22jonas.runa@eufc.cz": the
    # address also appears inside a percent-encoded mailto attribute, and
    # the encoded quote in front of it looks like part of a local-part to
    # a regex.
    text = strip_tags(page)
    email = EMAIL.search(text)
    phone = PHONE.search(text)
    phone_value = " ".join(phone.group(0).split()) if phone else None
    if phone_value and FAKE_PHONE.match(phone_value):
        phone_value = None

    name = " ".join(part for part in (fields.pop("contact_first_name", None),
                                      fields.pop("contact_last_name", None)) if part)
    fields["url"] = url
    fields["contact"] = {
        "name": name or None,
        "email": email.group(0) if email else None,
        "phone": phone_value,
        "tier": classify_contact(name, email.group(0) if email else None,
                                 register_people, domain),
    }
    return fields


def parse_deadline(value):
    """NEN prints '20. 08. 2025 10:00'. Returns a date, or None."""
    match = re.search(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", value or "")
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def load_candidates(path=CANDIDATES):
    if not Path(path).exists():
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line)["ico"] for line in handle if line.strip()]


def register_people(path=CANDIDATES):
    """ICO -> names of directors and owners, for grading tender contacts."""
    out = {}
    if not Path(path).exists():
        return out
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            names = [p.get("name") for p in
                     (row.get("directors") or []) + (row.get("owners") or [])
                     if p.get("name")]
            out[row["ico"]] = names
    return out


def proven_domains(path=Path("data/raw/websites.jsonl")):
    """ICO -> domain, but only where website.py actually proved ownership.

    A guessed domain must not grade a contact: by website.py's own
    measurement 46 % of resolved guesses belong to someone else, so
    matching an email against one would promote a stranger's address to
    "the company's own".
    """
    out = {}
    if not Path(path).exists():
        return out
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row.get("status") == "proven" and row.get("domain"):
                out[row["ico"]] = row["domain"]
    return out


def subsidised_candidates():
    """Only the companies that have any EU subsidy at all.

    Asking all 3299 was the first plan and the measurement killed it: a
    query takes ~2.6 s (the listing is 288 kB), so the full base is 2.4
    hours, and twelve randomly picked candidates returned zero tenders
    between them. That is not bad luck - a private manufacturer has no
    reason to run a public procurement unless something obliges it, and
    what obliges it is EU money. Every hit in the first sample came from
    the subsidy set.

    692 companies instead of 3299 turns a 2.4-hour sweep into half an
    hour, aimed at where the answers actually are.
    """
    from pipeline.sources.dotace_eu import load as load_subsidies
    return sorted(load_subsidies())


def run_all(limit=None, output=OUTPUT, icos=None):
    """Ask NEN about a set of companies - one request each, deliberately.

    Defaults to the subsidised set rather than the whole base; pass
    `icos` to override. Slow either way, and that is the price of not
    crawling 278 000 procurements to find the handful that are ours.
    """
    session = Session()
    icos = (icos if icos is not None else subsidised_candidates())[:limit]
    output.parent.mkdir(parents=True, exist_ok=True)
    people, domains = register_people(), proven_domains()

    found = with_tender = 0
    with open(output, "w", encoding="utf-8") as sink:
        for index, ico in enumerate(icos, 1):
            try:
                rows = tenders_for(ico, session)
                if rows:
                    rows = with_detail(rows, session,
                                       people.get(ico, ()), domains.get(ico))
            except Exception as error:
                print(f"  {ico}: {type(error).__name__} {error}", file=sys.stderr)
                continue
            if rows:
                with_tender += 1
                found += len(rows)
                for row in rows:
                    sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()   # so a long sweep can be watched, not guessed at
                relevant = [r for r in rows if r["relevant"]]
                if relevant:
                    print(f"  {ico}  {len(rows)} tender(s), "
                          f"{len(relevant)} relevant: {relevant[0]['name'][:60]}",
                          file=sys.stderr)
            if index % 200 == 0:
                print(f"  {index}/{len(icos)}  companies with a tender: {with_tender}",
                      file=sys.stderr)

    print(f"\n{with_tender} of {len(icos)} companies have published a tender; "
          f"{found} procurements total", file=sys.stderr)
    return with_tender


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tenders published on NEN.")
    parser.add_argument("--ico", help="one company")
    parser.add_argument("--all", action="store_true",
                        help="every company holding an EU subsidy (~692, ~30 min)")
    parser.add_argument("--every-candidate", action="store_true",
                        help="all 3299 instead - ~2.4 h, and measured to add nothing")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if args.all or args.every_candidate:
        icos = load_candidates() if args.every_candidate else None
        raise SystemExit(0 if run_all(args.limit, icos=icos) is not None else 1)
    if not args.ico:
        parser.error("give --ico or --all")

    rows = tenders_for(args.ico)
    print(f"{len(rows)} procurement(s)", file=sys.stderr)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
