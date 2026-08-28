"""Website resolver: from an ICO to the company's own site, proven.

No register publishes a company's website. RES has 25 columns and none
of them is a URL; ARES returns no contact data at all (checked live).
So the address has to be found and then *proved to belong to that
company* - and the second half is the whole difficulty.

Measured on 200 ICP-matching companies before this module was written:
of the domains guessed from a business name that turn out to be live
websites, 46 % belong to somebody else. A resolver that guesses and
scrapes would therefore feed another company's text into the card
almost half the time, with nothing to notice it by - the wrong site is
live, Czech, and about manufacturing too.

Hence the design: candidates are cheap and generated generously, and
every one of them must earn its place by evidence found in the page.

    ico           the company's ICO is printed on the site       -> proof
    dic           its VAT number (CZ + ICO) is printed           -> proof
    whois_org     CZ.NIC says the company owns the domain        -> proof
    whois_person  the registrant sits on this company's board    -> proof
    name_city     business name AND registered town on the page  -> strong
    name          business name only                             -> weak
    whois_postcode  right postcode, wrong owner name             -> weak
    none          nothing tied the site to this company          -> rejected

The proof tiers are facts, and each is a statement held in a registry:
the ICO on a page, or the owner recorded at CZ.NIC. The rest are
inferences and are reported as such, never merged into the proven
bucket - that would be the exact failure this project is built against.

There are two passes because the two sources behave differently:

    --all     HTTP, ten workers, reads pages       ~54 companies/min
    --whois   port 43, strictly serial             ~47 domains/min

Cost control, in the order it matters:

* DNS before HTTP. 60 % of generated candidates do not resolve at all.
  A getaddrinfo costs ~10 ms, a dead TCP connect costs the full timeout.
  Gating on DNS is what makes it affordable to try twenty spellings of
  a name instead of three.
* Contact links are read off the homepage, never guessed as paths.
  Guessing /kontakt found 39.5 % of sites; following the actual link
  found 44.5 % on the same sample. Czech sites do not agree on a path.
* The registry pass runs only on what HTTP failed to prove, which is
  about half - and it is the slow one, so that ordering matters.

Run:
    python -m pipeline.sources.website 29092540 "RTsoft s.r.o."
    python -m pipeline.sources.website --all      # every candidate on file
    python -m pipeline.sources.website --whois    # then prove the rest
"""

import argparse
import html as html_module
import json
import re
import socket
import sqlite3
import sys
import threading
import time
import unicodedata
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

CANDIDATES = Path("data/raw/ares_candidates.jsonl")
OUTPUT = Path("data/raw/websites.jsonl")

TIMEOUT = 10
USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"

# How far to go per company. Generation is nearly free, DNS is cheap,
# HTTP is not - so the funnel narrows hard at the last step.
#
# Measured on the full base of 3294 companies before trusting these:
#   MAX_CANDIDATES  no company reached 24 - the most any name produced
#                   was 15, so this ceiling costs nothing and is left
#                   where it is.
#   MAX_LIVE        428 companies (13 %) DID hit the old cap of 6, and
#                   143 of those never got a proof. Raised to 12,
#                   because the cap was in the wrong place: resolves()
#                   already runs over every candidate before the slice,
#                   so the DNS work was paid for in full either way and
#                   the cap only withheld the cheap part - the HTTP
#                   probe. Bounded cost: at most six extra probes for
#                   the 13 % of companies that get that far.
MAX_CANDIDATES = 24      # spellings generated
MAX_LIVE = 12            # of those, how many that resolve get probed

# Contact links followed from a homepage - NOT the total pages read per
# company. Named MAX_PAGES until it was checked against the archive and
# companies turned out to hold up to 33 harvested pages: harvest() has
# its own per-kind budgets (contact 5, production 3, ...) that sum well
# past this number, and this constant never governed them.
MAX_CONTACT_LINKS = 5

# Hard ceilings on one response. `timeout` above only limits the gap
# between two packets, so a server that trickles bytes indefinitely
# never trips it - that is what stopped the first full run dead at 365
# of 3294 with every worker still alive. These two cut it off.
MAX_BYTES = 1_500_000   # a company homepage is nowhere near this
MAX_TRANSFER = 20       # seconds for the whole body

# Legal-form suffixes. Stripped before a name becomes a domain, and the
# expression is anchored so it only bites at the end of the name.
SUFFIX = re.compile(
    r"[\s,]*(akciov[aá]\s+spole[cč]nost|spole[cč]nost\s+s\s+ru[cč]en[ií]m\s+omezen[yý]m|"
    r"a\.?\s?s\.?|s\.?\s?r\.?\s?o\.?|spol\.?|v\.?\s?o\.?\s?s\.?|k\.?\s?s\.?|"
    r"z\.?\s?[su]\.?|o\.?\s?p\.?\s?s\.?|s\.?\s?e\.?|gmbh|ltd\.?|s\.?a\.?)"
    r"[\s.,]*$",
    re.IGNORECASE,
)

# Legal-form debris once the name has been split into words. The suffix
# expression above only bites at the end, but Czech names carry the form
# in the middle too - "ČSAD, s.r.o. Rychnov n. Kn." - and leaving it in
# produces csadsrorychnovnkn.cz, which exists nowhere.
LEGAL_TOKENS = {
    "s", "r", "o", "sro", "spol", "as", "a.s", "vos", "ks", "zs", "ops",
    "se", "spolecnost", "spolecnosti", "akciova", "gmbh", "ltd", "sa",
    "kg", "ag", "bv", "nv", "plc", "inc", "llc",
}

# Tokens that carry no identity on their own. Dropping them produces a
# second, shorter stem - "VINAMET CZ" also lives at vinamet.cz. Both
# spellings are generated; this list only decides what else to try, so a
# wrong entry here costs one DNS lookup, never a wrong answer.
FILLER = {
    "cz", "czech", "czechia", "bohemia", "moravia", "morava", "group",
    "holding", "company", "int", "international", "trade", "trading",
    "industry", "industries", "invest", "praha", "brno", "prague",
    "and", "the", "republic",
}

# Parking and placeholder pages: a domain that answers but is not a site.
# Treated as dead rather than as a mismatch - there is nothing there to
# have been wrong about.
PARKED = re.compile(
    r"doména\s+je\s+na\s+prodej|tato\s+doména|domain\s+(is\s+)?for\s+sale|"
    r"připravujeme|stránky\s+se\s+připravují|under\s+construction|"
    r"default\s+web\s+page|it\s+works!|welcome\s+to\s+nginx|apache2?\s+default",
    re.IGNORECASE,
)

# Anchor text and hrefs that lead to the page carrying the ICO.
CONTACT_HINT = re.compile(
    r"kontakt|contact|o-?n[aá]s|o[-_]?firme|o[-_]?spole[cč]nosti|about|"
    r"impressum|imprint|firemn[ií]|[uú]daje|z[aá]pat[ií]|footer",
    re.IGNORECASE,
)

LINK = re.compile(r'(?is)<a\b[^>]*href="([^"#]*)"[^>]*>(.*?)</a>')
SCRIPTS = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
MARKUP = re.compile(r"(?s)<[^>]+>")
META_CHARSET = re.compile(
    rb'(?is)<meta[^>]+charset=["\']?\s*([\w-]+)|<meta[^>]+content=["\'][^"\']*charset=([\w-]+)'
)


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------


def decode(body):
    """Decode a page body, honouring the charset it declares.

    Czech SME sites are old enough that windows-1250 and iso-8859-2 are
    still common, and requests' own fallback for text/* is latin-1,
    which turns every diacritic into a different character. That breaks
    name matching silently - the page looks fine, the comparison fails.
    """
    match = META_CHARSET.search(body[:4096])
    if match:
        declared = (match.group(1) or match.group(2) or b"").decode("ascii", "ignore")
        if declared:
            try:
                return body.decode(declared, "replace")
            except LookupError:
                pass  # a charset nobody has heard of; fall through

    for encoding in ("utf-8", "windows-1250", "iso-8859-2"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "replace")


# The whole element is matched, not just the attribute. Replacing only
# the attribute puts the decoded address *inside* a tag, and the tag
# stripper then removes it along with the tag - the decode runs, the
# result is thrown away, and the page still looks address-free.
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


def readable(html):
    """Visible text with hidden addresses restored - what gets archived.

    This has to happen before the markup is stripped, and before the
    text reaches the archive, because both ways of hiding an address
    live *in* the markup:

    * HTML entities - buzuluk.cz prints `&#105;nfo&#64;buzuluk.cz`
    * Cloudflare data-cfemail blobs, which sit inside an anchor tag

    Archive the output of to_text() alone and those addresses are gone
    for good - the tag that carried them is already deleted, so no later
    stage can recover them however clever it is. Measured on 60 contact
    pages: Cloudflare on 2 %, plus a JavaScript decoder on a further
    5 % which is *not* handled here, since running page scripts is out
    of scope.
    """
    restored = CFEMAIL.sub(lambda m: " " + decode_cfemail(m.group(1)) + " ", html)
    return to_text(html_module.unescape(restored))


def to_text(html):
    """Visible text of a page, whitespace collapsed.

    The same normalisation sbirka.py applies, and for the same reason:
    every later step - including quote verification - has to see one
    shape of text, or a quote taken here will not be found there.
    """
    return re.sub(r"\s+", " ", MARKUP.sub(" ", SCRIPTS.sub(" ", html))).strip()


def fold(text):
    """Lowercase, diacritics removed - for comparing names."""
    stripped = "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )
    return stripped.lower()


# ---------------------------------------------------------------------------
# Candidate generation - pure, no network
# ---------------------------------------------------------------------------


def stems(name):
    """Domain stems worth trying for a business name, best first.

    A stem is the part before the dot. Generating several is the point:
    "CHARVÁT AXL, a.s." lives at charvat-axl.cz, and the shortest guess
    (axl.cz) is somebody else's affiliate programme - so both have to be
    tried and the evidence check has to settle it.
    """
    base = SUFFIX.sub("", name).strip()
    base = fold(base)
    # Keep digits: "3 P" is a real company and 3p.cz is its site.
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()

    words = [w for w in base.split() if w and w not in LEGAL_TOKENS]
    if not words:
        # A name that is nothing but legal form - fall back to the raw
        # split rather than returning nothing at all.
        words = [w for w in base.split() if w]
    if not words:
        return []

    meaningful = [w for w in words if w not in FILLER] or words

    ordered = []

    def add(*parts):
        for value in parts:
            if value and value not in ordered and 2 <= len(value) <= 63:
                ordered.append(value)

    add("".join(words), "-".join(words))
    if meaningful != words:
        add("".join(meaningful), "-".join(meaningful))
    if len(meaningful) > 2:
        add("".join(meaningful[:2]), "-".join(meaningful[:2]))
    if len(meaningful) > 1:
        add(meaningful[0])
        # An acronym is how long descriptive names usually shorten:
        # "AKORD - stavební a obchodní společnost" is not akordstavebni.
        initials = "".join(w[0] for w in meaningful if w)
        if len(initials) >= 3:
            add(initials)
    return ordered


def candidates(name, seed_domains=()):
    """Full ranked candidate list: seeds first, then generated spellings.

    Seeds are domains lifted from a document the company itself filed -
    the contact e-mail on its MPSV vacancy. They rank above anything
    invented here, but they are not trusted either: 22 % of them turned
    out to be an agency's or a parent group's domain, so they go through
    the same evidence check as a guess.
    """
    out = []

    def add(domain):
        domain = domain.strip().lower().lstrip(".")
        domain = re.sub(r"^www\.", "", domain)
        if domain and domain not in out and "." in domain:
            out.append(domain)

    for domain in seed_domains:
        add(domain)

    parts = stems(name)
    for stem in parts:
        add(f"{stem}.cz")
    # .com and .eu only for the strongest stems: foreign-owned Czech
    # subsidiaries ("CIKAUTXO CZ", "Hengst Air Filtration Czech
    # Republic") sit on the group domain, and there is no .cz at all.
    for stem in parts[:3]:
        add(f"{stem}.com")
        add(f"{stem}.eu")

    return out[:MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Cheap gate: does the name resolve at all
# ---------------------------------------------------------------------------


# A DNS retry is not optional the way it looked when this was written.
# Measured on the first full harvest run (workers=10, ~90 minutes):
# 1899 of 2384 previously-proven companies came back no_lead - every one
# of their candidate domains failed getaddrinfo. Re-tested by hand right
# after the run: every single domain resolved instantly. The resolver
# itself was the thing failing under sustained concurrent load, and
# socket.gaierror is exactly what a Windows/glibc resolver raises for a
# timeout or a refused query, not only for a name that truly does not
# exist - so treating gaierror as final evidence of absence was wrong in
# precisely the case that matters. Two retries with a short backoff cost
# nothing on the 60 % that are genuinely dead (they still fail fast on
# the first try) and recover the rest.
DNS_ATTEMPTS = 3
DNS_BACKOFF = 0.3  # seconds, doubled on each retry


def resolves(domain):
    """True when the domain has an A/AAAA record, under either form.

    This is the single biggest cost lever in the module. Six in ten
    generated candidates do not exist; asking DNS costs milliseconds,
    while letting a dead host reach a TCP connect costs the full
    timeout. Everything downstream only sees names that exist.
    """
    for host in (domain, f"www.{domain}"):
        delay = DNS_BACKOFF
        for attempt in range(DNS_ATTEMPTS):
            try:
                socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
                return True
            except socket.gaierror:
                if attempt < DNS_ATTEMPTS - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                break  # exhausted retries for this host, try the next
            except Exception:
                return True  # resolver trouble is not evidence of absence
    return False


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class Fetcher:
    """HTTP access with a per-host robots.txt cache, safe to share.

    robots is honoured because the prototype has to be defensible, not
    because the sites would notice: the whole run reads at most six
    pages per company. Fetching it costs one request per live host,
    which the DNS gate has already made rare.

    One instance is shared by every worker so that the robots cache is
    shared too, but a requests.Session is not thread-safe - so the
    session itself is thread-local and the cache is a plain dict, whose
    get and set are atomic. Worst case two threads fetch the same
    robots.txt once each; there is nothing to corrupt.
    """

    def __init__(self, respect_robots=True):
        self.respect_robots = respect_robots
        self._robots = {}
        self._local = threading.local()

    @property
    def session(self):
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            self._local.session = session
        return self._local.session

    def allowed(self, url):
        if not self.respect_robots:
            return True

        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        if origin not in self._robots:
            parser = urllib.robotparser.RobotFileParser()
            try:
                response = self.session.get(
                    f"{origin}/robots.txt", timeout=TIMEOUT, allow_redirects=True
                )
                if response.status_code == 200:
                    parser.parse(decode(response.content).splitlines())
                else:
                    parser = None  # no robots.txt means no restriction
            except requests.RequestException:
                parser = None
            self._robots[origin] = parser

        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def get(self, url):
        """Return (html, final_url), or (None, None) when unreachable.

        The body is read in bounded chunks rather than through
        response.content, and this is not an optimisation. The `timeout`
        argument of requests caps the wait *between* bytes, not the whole
        transfer: a server that dribbles one byte per second keeps the
        worker forever and never raises. Seen on the full run - the
        counter stopped at 365 of 3294 with every worker still alive.
        """
        if not self.allowed(url):
            return None, None
        try:
            response = self.session.get(
                url, timeout=TIMEOUT, allow_redirects=True, stream=True
            )
            if response.status_code >= 400:
                return None, None

            deadline = time.monotonic() + MAX_TRANSFER
            body = bytearray()
            for chunk in response.iter_content(65536):
                body += chunk
                if len(body) >= MAX_BYTES or time.monotonic() > deadline:
                    break
        except requests.RequestException:
            return None, None
        finally:
            try:
                response.close()
            except (NameError, UnboundLocalError):
                pass

        if not body:
            return None, None
        return decode(bytes(body)), response.url


# The page that actually carries the ICO is the contact page, not any
# page that happens to sit under /o-nas/. Scored rather than taken in
# document order: on promareha.cz the /o-nas/ prefix matched four links
# in a row - kariera, ke-stazeni and two duplicates of o-spolecnosti -
# and pushed the real /kontakty out of the budget, turning a proof into
# a guess.
STRONG_SEGMENT = re.compile(r"^(kontakt|kontakty|contact|contacts|impressum|imprint)$", re.I)
CONTACT_WORD = re.compile(r"kontakt|contact|impressum|imprint", re.I)
LANGUAGE_PREFIX = re.compile(r"^(cs|cz|en|de|sk|pl|ru|fr|it|es)$", re.I)


def rank_link(url, label):
    """How likely this link leads to the page carrying the ICO."""
    segments = [s for s in urlparse(url).path.split("/") if s]
    last = segments[-1].rsplit(".", 1)[0] if segments else ""

    if STRONG_SEGMENT.match(last):
        return 3
    if CONTACT_WORD.search(last) or CONTACT_WORD.search(label):
        return 2
    return 1


def contact_links(html, base_url, limit=MAX_CONTACT_LINKS):
    """Contact-ish links from a page, best first, as same-site URLs.

    Same-site only, and deliberately so: an "Impressum" on a Czech
    subsidiary's page often points at the German parent, whose ICO is a
    different company's - following it would prove the wrong thing.
    """
    host = urlparse(base_url).netloc.lower().replace("www.", "")
    scored, seen = [], set()

    for href, anchor in LINK.findall(html):
        label = MARKUP.sub(" ", anchor).strip()
        if not (CONTACT_HINT.search(href) or CONTACT_HINT.search(label)):
            continue

        url = urljoin(base_url, href.strip())
        parts = urlparse(url)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().replace("www.", "") != host:
            continue

        # Multilingual sites repeat the same page under /cs/, /en/ and
        # bare, so the language prefix is dropped before comparing. The
        # rest of the path has to stay: pmb-zos.cz has both
        # /strojirenska-vyroba/kontakty/ and /home/kontakty/, and only
        # the second carries the ICO - keying on the last segment alone
        # threw the proof away.
        segments = [s for s in parts.path.split("/") if s]
        if segments and LANGUAGE_PREFIX.match(segments[0]):
            segments = segments[1:]
        key = fold("/".join(segments))
        if url in seen or key in seen:
            continue
        seen.add(url)
        seen.add(key)

        scored.append((rank_link(url, label), -len(parts.path), url))

    scored.sort(reverse=True)
    return [url for _, _, url in scored[:limit]]


# ---------------------------------------------------------------------------
# Evidence - pure functions over already-fetched text
# ---------------------------------------------------------------------------


NUMBER_RUN = re.compile(r"\d[\d\s .]{6,12}\d")


def find_ico(text, ico):
    """Locate the ICO on the page and return the surrounding quote.

    The register form is zero-padded to eight digits, printed forms drop
    the leading zero and often group the digits ("255 09 900"), so the
    comparison is made on digits only. A quote is returned rather than a
    boolean: it is the evidence, and the same string has to survive into
    the archive for verify.py to find later.
    """
    bare = ico.lstrip("0")
    for match in NUMBER_RUN.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if digits in (ico, bare):
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 60)
            return text[start:end].strip()
    return None


def find_dic(text, ico):
    """Locate the VAT number, which for a Czech company is CZ + ICO.

    Some sites print only the DIC. It is the same registration behind a
    prefix, so it proves the same thing.
    """
    bare = ico.lstrip("0")
    for match in re.finditer(r"(?i)CZ\s?(\d[\d\s ]{6,11})", text):
        digits = re.sub(r"\D", "", match.group(1))
        if digits in (ico, bare):
            start = max(0, match.start() - 60)
            end = min(len(text), match.end() + 60)
            return text[start:end].strip()
    return None


def find_name(text, name):
    """The longest form of the business name present on the page, or None.

    Compared folded and with separators removed, so "PMB-ZOS s.r.o."
    matches "PMB ZOS", "pmb-zos" and "PMBZOS" alike.

    Shortened forms have to be accepted too, and this is not laziness:
    "CIKAUTXO CZ s.r.o." is a Czech subsidiary whose site says CIKAUTXO
    GROUP and nothing else, so demanding the registered name in full
    rejects the company's actual website. Shorter stems are only tried
    once the full one fails, longest first, and never below four
    characters - "3p" would match half the internet.
    """
    squeezed = re.sub(r"[^a-z0-9]+", "", fold(text))

    forms = [re.sub(r"[^a-z0-9]+", "", fold(SUFFIX.sub("", name)))]
    forms += [stem.replace("-", "") for stem in stems(name)]

    for form in sorted(set(f for f in forms if f), key=len, reverse=True):
        if len(form) < 4:
            continue
        if form in squeezed:
            return form
    return None


def find_city(text, city):
    """Whether the registered town appears on the page.

    On its own this is worth nothing - half of Czech firms mention
    Praha. It is only ever used to strengthen a name match, never alone.
    """
    if not city or len(city) < 3:
        return False
    return fold(city) in fold(text)


def looks_parked(text):
    return bool(PARKED.search(text[:2000])) or len(text) < 200


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


# One harvest, not three passes. Resolution, contacts and the
# production-mode signal all used to fetch the same sites separately -
# ~12k, ~8k and ~1k requests over the same hosts. Classifying links once
# and taking every useful page in a single visit replaces all of it, and
# has a second effect worth more than the saving: the ICO is looked for
# across the whole harvest rather than only contact pages, so a company
# that prints its number on /o-nas now resolves instead of failing.
PAGE_KINDS = (
    # Contact gets the largest budget because that is where the ICO
    # lives, and a proof is worth more than any other page here. Cut to
    # three and REMAK a.s. stopped resolving: its number is on
    # /cs/kontakt/sidlo-spolecnosti/4482, the fourth contact link on the
    # page. Five matches what the previous single-purpose pass allowed.
    ("contact", re.compile(
        r"(?i)kontakt|contact|impressum|imprint|[uú]daje", ), 5),
    ("career", re.compile(
        r"(?i)karier|kari[eé]r|volna-?mist|voln[aá].?m[ií]st|nabidka-?prace|"
        r"prace-?u-?nas|\bjobs?\b|career|zamestnani|nabor", ), 2),
    # Certificates must be tested BEFORE "about" and "production":
    # classify_link returns the first pattern that matches, and Czech
    # sites file the certificate page under the company profile -
    # /cs/firemni-profil/certifikace/ matched "profil" in the about
    # pattern and was swallowed by a category whose budget was already
    # spent, so the page was never fetched at all. The more specific
    # category has to be asked first.
    ("certificates", re.compile(
        r"(?i)certifik|certificate|osvedcen|osv[eě]d[cč]en|jakost|kvalit|quality|"
        r"ke-?stazen[ií]|ke-?sta[zž]en", ), 2),
    ("production", re.compile(
        r"(?i)vyrob|v[yý]rob|sluzb|slu[zž]b|produkt|technolog|strojni-?park|"
        r"strojov|co-?delame|zamereni|sortiment", ), 3),
    ("about", re.compile(
        r"(?i)o-?n[aá]s|o-?firme|o-?spole[cč]nosti|about|profil|historie|"
        r"veden[ií]|management|struktura", ), 2),
    ("references", re.compile(
        r"(?i)referenc|realizac|projekty|nase-?prace|portfolio", ), 1),
)


def classify_link(href, label):
    """Which part of a site a link leads to, or None if it leads nowhere useful.

    Both the href and the anchor text are tested: Czech sites label the
    same page "Kontakty" in the menu and /kontaktni-udaje in the path,
    and either one alone misses a fair share.
    """
    for kind, pattern, _ in PAGE_KINDS:
        if pattern.search(href) or pattern.search(label):
            return kind
    return None


def harvest(fetcher, domain, limit_per_kind=None):
    """Read one site once: homepage plus the useful pages behind it.

    Returns [(url, html, kind)], homepage first. Budgets are per kind so
    that a site with forty product pages cannot crowd out its single
    contact page - which is what a flat "first N links" rule does, and
    is how the earlier version lost proofs (see the dedup note below).
    """
    home = None
    for scheme in ("https://", "http://"):
        html, final = fetcher.get(f"{scheme}{domain}")
        if html:
            home = (final, html, "home")
            break
    if home is None:
        return []

    pages = [home]
    final, html, _ = home
    host = urlparse(final).netloc.lower().replace("www.", "")
    budgets = {kind: (limit_per_kind or cap) for kind, _, cap in PAGE_KINDS}
    picked, seen = {}, set()

    for href, anchor in LINK.findall(html):
        label = MARKUP.sub(" ", anchor).strip()
        kind = classify_link(href, label)
        if not kind or budgets[kind] <= 0:
            continue

        url = urljoin(final, href.strip())
        parts = urlparse(url)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().replace("www.", "") != host:
            continue

        # Multilingual sites repeat a page under /cs/ and /en/; the path
        # without its language prefix identifies it. Keying on the last
        # segment alone would merge /home/kontakty with
        # /strojirenska-vyroba/kontakty, which cost a real proof once.
        segments = [s for s in parts.path.split("/") if s]
        if segments and LANGUAGE_PREFIX.match(segments[0]):
            segments = segments[1:]
        key = fold("/".join(segments))
        if url in seen or key in seen:
            continue
        seen.add(url)
        seen.add(key)

        picked.setdefault(kind, []).append((rank_link(url, label), url))
        budgets[kind] -= 1

    for kind, candidates in picked.items():
        for _, url in sorted(candidates, reverse=True):
            body, resolved = fetcher.get(url)
            if body:
                pages.append((resolved, body, kind))

    return pages


def keep(archive, ico, url, html, run_id=None, kind=None):
    """Put one fetched page into the evidence store.

    Called from inspect() rather than from Fetcher.get() on purpose: the
    fetcher does not know which company it is working for, and archiving
    robots.txt and dead candidate hosts would fill the store with pages
    nobody will ever quote.
    """
    if archive is None or not html:
        return None
    try:
        snapshot_id, _ = archive.store(
            ico, "website", readable(html), url=url, run_id=run_id, kind=kind
        )
        return snapshot_id
    except Exception as error:  # the archive must never break a run
        print(f"archive: {type(error).__name__} on {url}", file=sys.stderr)
        return None


def inspect(fetcher, domain, ico, name, city, archive=None, run_id=None):
    """Read one candidate site and grade the evidence it carries.

    Returns None when the domain is not usable at all, otherwise a dict
    with the strongest evidence found.
    """
    pages = harvest(fetcher, domain)
    if not pages:
        return None

    # A site whose navigation is built by JavaScript hands us a homepage
    # with no usable links at all. Two guessed paths are a cheap last
    # resort - as a fallback only, never as the primary strategy.
    if len(pages) == 1:
        root_url = pages[0][0]
        root = f"{urlparse(root_url).scheme}://{urlparse(root_url).netloc}"
        for guess in (f"{root}/kontakt", f"{root}/kontakty"):
            body, resolved = fetcher.get(guess)
            if body:
                pages.append((resolved, body, "contact"))

    for url, body, kind in pages:
        keep(archive, ico, url, body, run_id, kind)

    if all(looks_parked(to_text(body)) for _, body, _ in pages):
        return None

    weak = None
    for url, body, _kind in pages:
        text = to_text(body)

        quote = find_ico(text, ico)
        if quote:
            return {"domain": domain, "url": url, "evidence": "ico", "quote": quote,
                    "pages_read": len(pages)}

        quote = find_dic(text, ico)
        if quote:
            return {"domain": domain, "url": url, "evidence": "dic", "quote": quote,
                    "pages_read": len(pages)}

        # An inference is remembered but never returned early: a later
        # page on the same site may still carry the ICO and settle it.
        matched = find_name(text, name)
        if weak is None and matched:
            level = "name_city" if find_city(text, city) else "name"
            weak = {"domain": domain, "url": url, "evidence": level, "quote": None,
                    "matched": matched, "pages_read": len(pages)}

    return weak


def resolve(ico, name, city=None, seed_domains=(), fetcher=None, respect_robots=True,
            archive=None, run_id=None):
    """Find and prove the website of one company.

    Always returns a dict carrying `status`:

        proven     evidence is `ico` or `dic` - a register key on the page
        probable   evidence is `name_city` or `name` - an inference
        not_found  candidates existed, none could be tied to the company
        no_lead    nothing even resolved in DNS

    The caller must keep the two success states apart. A `probable`
    site may be used to read about the company, but nothing taken from
    it can be presented to the salesperson as a fact about *this* ICO.
    """
    ico = str(ico).strip().zfill(8)
    fetcher = fetcher or Fetcher(respect_robots=respect_robots)

    result = {
        "ico": ico,
        "name": name,
        "status": None,
        "domain": None,
        "url": None,
        "evidence": None,
        "quote": None,
        "matched": None,
        "candidates": 0,
        "live": 0,
        "checked": [],
        "retrieved_at": date.today().isoformat(),
    }

    tried = candidates(name, seed_domains)
    result["candidates"] = len(tried)

    live = [domain for domain in tried if resolves(domain)][:MAX_LIVE]
    result["live"] = len(live)
    if not live:
        result["status"] = "no_lead"
        return result

    best = None
    for domain in live:
        found = inspect(fetcher, domain, ico, name, city, archive, run_id)
        result["checked"].append({
            "domain": domain,
            "outcome": found["evidence"] if found else "no_match",
        })

        if found and found["evidence"] in ("ico", "dic"):
            result.update({k: found.get(k) for k in
                           ("domain", "url", "evidence", "quote", "matched")})
            result["status"] = "proven"
            return result

        # Rank inferences: a name plus the registered town beats a bare
        # name, and neither ever outranks a proof.
        if found and (best is None or found["evidence"] == "name_city"):
            best = found

    if best:
        result.update({k: best.get(k) for k in
                       ("domain", "url", "evidence", "quote", "matched")})
        result["status"] = "probable"
    else:
        result["status"] = "not_found"
    return result


# ---------------------------------------------------------------------------
# Batch run
# ---------------------------------------------------------------------------


# Applicant-tracking and job-board hosts. They turn up as the "company"
# domain in a vacancy because that is where the advert lives, but they
# are the recruiter's site, not the employer's.
NOT_A_COMPANY = {
    "teamio.net", "teamio.com", "jobs.cz", "prace.cz", "lmc.eu",
    "startupjobs.cz", "welcometothejungle.com", "profesia.cz",
    "indeed.com", "linkedin.com", "facebook.com", "seznam.cz",
    "uradprace.cz", "mpsv.cz", "google.com", "youtube.com",
}


def load_seeds(*paths):
    """ICO -> candidate domains gathered from the free sources.

    Two shapes are accepted because two different probes wrote them: a
    list of rows carrying `mpsv_domains`, and a plain {ico: [urls]} map
    scraped out of the free text of vacancy adverts.
    """
    seeds = {}

    def add(ico, domains):
        for domain in domains or []:
            domain = re.sub(r"^www\.", "", str(domain).strip().lower())
            if not domain or "." not in domain or domain in NOT_A_COMPANY:
                continue
            seeds.setdefault(ico, [])
            if domain not in seeds[ico]:
                seeds[ico].append(domain)

    for path in paths:
        if not path or not Path(path).exists():
            continue
        payload = json.loads(Path(path).read_text(encoding="utf-8"))

        if isinstance(payload, dict) and "sample" in payload:
            payload = payload["sample"]

        if isinstance(payload, dict):
            for ico, domains in payload.items():
                add(ico, domains)
        else:
            for row in payload:
                add(row["ico"], row.get("mpsv_domains"))

    return seeds


def run_all(limit=None, workers=8, seeds_path=None, respect_robots=True, archive=None):
    """Resolve every candidate on file, appending as it goes.

    Appends rather than collecting: a run over 3299 companies takes long
    enough that losing it to one exception would be its own bug. Already
    resolved ICOs are skipped, so the run resumes.
    """
    done = set()
    if OUTPUT.exists():
        with open(OUTPUT, encoding="utf-8") as handle:
            for line in handle:
                try:
                    done.add(json.loads(line)["ico"])
                except (json.JSONDecodeError, KeyError):
                    continue
        print(f"resuming: {len(done)} already done", file=sys.stderr)

    seeds = load_seeds(*(seeds_path or []))

    companies = []
    with open(CANDIDATES, encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record["ico"] in done or not record.get("name"):
                continue
            companies.append(record)
    if limit:
        companies = companies[:limit]

    print(f"resolving {len(companies)} companies with {workers} workers", file=sys.stderr)

    # One shared Fetcher: its session is thread-local, its robots cache
    # is not, which is exactly the split we want.
    fetcher = Fetcher(respect_robots=respect_robots)
    run_id = archive.start_run(note="website resolution") if archive else None

    def work(record):
        try:
            return resolve(
                record["ico"], record["name"], record.get("city"),
                seeds.get(record["ico"], []), fetcher=fetcher,
                archive=archive, run_id=run_id,
            )
        except Exception as error:  # one bad site must not end the run
            return {"ico": record["ico"], "name": record["name"],
                    "status": "error", "reason": f"{type(error).__name__}: {error}",
                    "retrieved_at": date.today().isoformat()}

    tally = {}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "a", encoding="utf-8") as sink:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, result in enumerate(pool.map(work, companies), 1):
                sink.write(json.dumps(result, ensure_ascii=False) + "\n")
                sink.flush()
                tally[result["status"]] = tally.get(result["status"], 0) + 1
                if index % 50 == 0:
                    print(f"  {index}/{len(companies)}  {tally}", file=sys.stderr)

    total = sum(tally.values()) or 1
    print("\nfinished:", file=sys.stderr)
    for status in ("proven", "probable", "not_found", "no_lead", "error"):
        count = tally.get(status, 0)
        print(f"  {status:10} {count:5}  {count / total * 100:5.1f} %", file=sys.stderr)



# ---------------------------------------------------------------------------
# Second pass: prove ownership through the domain registry
# ---------------------------------------------------------------------------


def registry_key(text):
    """Business name reduced to what two registries can be compared on.

    Legal form, punctuation and diacritics all vary between how a name
    is filed in ARES and how the owner typed it into the domain
    registry, so all three are removed before comparing.
    """
    plain = re.sub(r"[^a-z0-9 ]+", " ", fold(SUFFIX.sub("", text or "")))
    return "".join(word for word in plain.split() if word not in LEGAL_TOKENS)


def whois_evidence(record, name, people, postcode):
    """Grade what the domain registry says about an owner.

    Returns one of the proof tiers, a weak tier, or None.

    The strictness is not caution for its own sake. Matching the owner
    name loosely produced real false proofs on live data:

        H & M spol. s r.o.   -> h-m.cz   -> H&M Hennes & Mauritz AB
        BLIKA s.r.o.         -> blika.cz -> Blika A/S
        SETRA, spol. s r.o.  -> setra.cz -> SETRA Service Trading

    Every one of those is a different company that happens to share a
    short name. So a partial name match only counts when something
    independent agrees with it - the registered postcode, or a name long
    enough that a collision is not plausible.
    """
    if not record:
        return None

    owner_name = registry_key(record.get("org") or "")
    wanted = registry_key(name)
    postcode_matches = bool(postcode) and record.get("postcode") == postcode

    exact = bool(owner_name) and owner_name == wanted
    partial = bool(owner_name) and (wanted in owner_name or owner_name in wanted)

    # The registrant is a person who is on this company's board. Names
    # are specific enough that this needs no second signal.
    if record.get("person") and fold(record["person"]) in people:
        return "whois_person"

    if exact:
        return "whois_org"

    # A partial match is a different thing and gets its own name. Live
    # data: SMOLO Recycling s.r.o. -> smolo.cz owned by SMOLO a.s.,
    # Steelcase Czech Republic -> steelcase.cz owned by Steelcase Inc.
    # The site is almost certainly the right place to read about the
    # company, but the domain belongs to the group, not to this ICO -
    # and a claim sourced there is a claim about the group.
    if partial and (postcode_matches or len(wanted) >= 7):
        return "whois_org_group"

    # Right postcode, wrong or missing owner name. Suggestive, never
    # proof: a postcode covers a whole town.
    if postcode_matches:
        return "whois_postcode"
    return None


def load_company_facts(path=CANDIDATES, db_path=Path("data/ui/companies.db")):
    """ICO -> the register facts the WHOIS comparison needs.

    Postcode comes from the UI index rather than from ARES: the flat
    company dict keeps the town but not the PSC, and rebuilding it from
    the 517 MB export to read one column would be absurd.
    """
    facts = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            people = {
                fold(person["name"])
                for group in ("directors", "owners")
                for person in (record.get(group) or [])
                if person.get("name")
            }
            facts[record["ico"]] = {"people": people, "postcode": None}

    if Path(db_path).exists():
        connection = sqlite3.connect(db_path)
        for ico, postcode in connection.execute("SELECT ico, psc FROM company"):
            if ico in facts and postcode:
                facts[ico]["postcode"] = str(postcode).replace(" ", "")
        connection.close()

    return facts


# Evidence that identifies one legal entity. Several companies sharing a
# domain on any of these is an ordinary corporate group - pickering.cz
# prints the ICO of Pickering Connect, Interfaces and Electronics alike,
# and all three are right.
IDENTIFYING = ("ico", "dic", "whois_org", "whois_person", "whois_org_group")


def mark_contested(path=OUTPUT):
    """Flag domains claimed by several companies on a generic name alone.

    Found only by looking across the whole file, never at one company:
    nineteen separate municipal firms called "Technické služby <town>"
    were each handed technickesluzby.cz, because that is what their
    shared name spells and the domain resolves. At most one of them owns
    it. Four ČSAD companies split csad.cz the same way.

    Nothing is deleted. The domain stays, because it is still the best
    guess and a human may want to look - but `contested` says how many
    others claim it, so no fact read from that page can be attributed to
    this ICO without someone noticing.
    """
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

    claims = {}
    for row in rows:
        if row.get("domain"):
            claims.setdefault(row["domain"], []).append(row)

    flagged = 0
    for domain, holders in claims.items():
        if len(holders) < 2:
            continue
        # A group is fine; only the ones resting on a name are contested,
        # and only when they are not alone in resting on it.
        weak = [r for r in holders if r.get("evidence") not in IDENTIFYING]
        if len(weak) < 2:
            continue
        for row in weak:
            row["contested"] = len(weak)
            row["status"] = "probable"
            flagged += 1

    Path(path).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    print(f"contested domains flagged on {flagged} companies", file=sys.stderr)
    return flagged


def run_whois(limit=None):
    """Re-read websites.jsonl and try the registry on everything unproven.

    A separate pass on purpose. CZ.NIC refuses a burst - measured, twelve
    queries then a refusal - so this is serial at one query per second,
    and mixing it into the threaded HTTP pass would drag every worker
    down to that speed for the sake of half the companies.
    """
    from pipeline.sources import whois_cz

    rows = [json.loads(line) for line in open(OUTPUT, encoding="utf-8")]
    facts = load_company_facts()

    todo = [row for row in rows if row.get("status") != "proven"]
    if limit:
        todo = todo[:limit]
    print(f"{len(todo)} unproven of {len(rows)}; asking the domain registry",
          file=sys.stderr)

    upgraded = {}
    for index, row in enumerate(todo, 1):
        known = facts.get(row["ico"], {"people": set(), "postcode": None})

        # Candidates worth asking about: whatever the HTTP pass looked
        # at, best first. Only .cz - CZ.NIC knows nothing about .com.
        domains = [row["domain"]] if row.get("domain") else []
        domains += [entry["domain"] for entry in row.get("checked", [])]
        domains = [d for d in dict.fromkeys(domains) if d and d.endswith(".cz")][:2]

        for domain in domains:
            record = whois_cz.owner(domain)
            tier = whois_evidence(record, row["name"], known["people"], known["postcode"])
            if not tier:
                continue

            row["whois"] = {"domain": domain, "tier": tier, "org": record.get("org")}

            if tier == "whois_person":
                # The registrant is a private individual, so the address
                # WHOIS prints is their home - the jednatel of
                # petr-vojta.cz is registered at his own street address.
                # The name is kept because ARES already gave us the same
                # name; the postal data is new personal data about a
                # person and section 7 says the profile is of the
                # company, not of the human. So it is not stored.
                row["whois"]["person"] = record.get("person")
            else:
                row["whois"]["person"] = record.get("person")
                row["whois"]["postcode"] = record.get("postcode")
            if tier in ("whois_org", "whois_person"):
                row.update({"status": "proven", "evidence": tier, "domain": domain,
                            "url": row.get("url") or f"https://{domain}"})
            elif row.get("status") != "proven":
                row["status"] = "probable"
                row["evidence"] = row.get("evidence") or tier
                row["domain"] = row.get("domain") or domain
            upgraded[tier] = upgraded.get(tier, 0) + 1
            break

        if index % 50 == 0:
            print(f"  {index}/{len(todo)}  {upgraded}", file=sys.stderr)

    with open(OUTPUT, "w", encoding="utf-8") as sink:
        for row in rows:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")

    tally = {}
    for row in rows:
        tally[row["status"]] = tally.get(row["status"], 0) + 1
    total = len(rows)
    print("\nafter the registry pass:", file=sys.stderr)
    for status in ("proven", "probable", "not_found", "no_lead", "error"):
        count = tally.get(status, 0)
        print(f"  {status:10} {count:5}  {count / total * 100:5.1f} %", file=sys.stderr)
    print(f"  upgrades: {upgraded}", file=sys.stderr)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resolve company websites from an ICO.")
    parser.add_argument("ico", nargs="?", help="single ICO to resolve")
    parser.add_argument("name", nargs="?", help="business name for that ICO")
    parser.add_argument("--city", help="registered town, strengthens a name match")
    parser.add_argument("--all", action="store_true", help="run over ares_candidates.jsonl")
    parser.add_argument("--whois", action="store_true",
                        help="second pass: ask CZ.NIC about everything still unproven")
    parser.add_argument("--contested", action="store_true",
                        help="flag domains several companies claim on a generic name")
    parser.add_argument("--limit", type=int, help="stop after N companies")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seeds", nargs="*",
                        default=["data/raw/mpsv_domains.json",
                                 "data/raw/mpsv_text_urls.json"],
                        help="JSON files of known domains per ICO")
    parser.add_argument("--ignore-robots", action="store_true")
    parser.add_argument("--archive", action="store_true",
                        help="keep every page read in the evidence store")
    args = parser.parse_args()

    store = None
    if args.archive:
        from pipeline.evidence.archive import Archive
        store = Archive()

    if args.contested:
        mark_contested()
    elif args.whois:
        run_whois(args.limit)
    elif args.all:
        run_all(args.limit, args.workers, args.seeds, not args.ignore_robots, store)
    elif args.ico and args.name:
        print(json.dumps(
            resolve(args.ico, args.name, args.city,
                    respect_robots=not args.ignore_robots, archive=store),
            ensure_ascii=False, indent=2,
        ))
    else:
        parser.error("give an ICO and a name, or --all")
