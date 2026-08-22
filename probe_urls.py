"""Throwaway measurement: how many candidates can we get a website for?

Not part of the pipeline. Answers one question before sources/web.py is
written: for a company that passed the ICP filter, can code find its
website at all - and prove the site belongs to that company?

"Found" here means verified, not guessed. A candidate URL only counts
when the company's ICO appears in the text of the page. Czech companies
put it in the footer or on /kontakty, and it is the one string that ties
a domain to a register entry without a judgement call.

Two routes are measured separately:

    mpsv    urlAdresa and contact e-mail domains from the vacancy export
    guess   the company name turned into a .cz domain

Run:
    python probe_urls.py mpsv     # build the sample, pull MPSV contacts
    python probe_urls.py verify   # fetch candidates, check for the ICO
"""

import gzip
import json
import random
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CANDIDATES = Path("data/raw/ares_candidates.jsonl")
SAMPLE_FILE = Path("data/raw/url_probe_sample.json")
MPSV_URL = "https://data.mpsv.cz/od/soubory/volna-mista/volna-mista.json.gz"

SAMPLE_SIZE = 200
SEED = 42
TIMEOUT = 12
USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"

# A mail domain only points at a company website when the company owns
# it. These are the Czech and international free providers - an address
# there says nothing about a domain.
FREEMAIL = {
    "gmail.com", "seznam.cz", "email.cz", "centrum.cz", "volny.cz",
    "post.cz", "atlas.cz", "tiscali.cz", "quick.cz", "outlook.com",
    "hotmail.com", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com",
    "protonmail.com", "proton.me", "azet.sk", "zoznam.sk", "mail.com",
    "gmail.cz", "googlemail.com", "live.com", "epo.cz", "iol.cz",
}

# Legal-form suffixes to strip before turning a name into a domain.
SUFFIX = re.compile(
    r"[\s,]*(a\.?\s?s\.?|s\.?\s?r\.?\s?o\.?|spol\.?|v\.?o\.?s\.?|k\.?s\.?|"
    r"z\.?\s?s\.?|o\.?p\.?s\.?|se|akciov[aá]\s+spole[cč]nost)\s*$",
    re.IGNORECASE,
)


def strip_diacritics(text):
    import unicodedata
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def load_sample():
    """Pick the same 200 companies every run."""
    rows = []
    with open(CANDIDATES, encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("name"):
                rows.append({"ico": record["ico"], "name": record["name"]})

    random.seed(SEED)
    return random.sample(rows, SAMPLE_SIZE)


def domain_of(address):
    """Company mail domain, or None for a free provider."""
    if not address or "@" not in address:
        return None
    domain = address.rsplit("@", 1)[1].strip().lower().strip(".")
    if not domain or domain in FREEMAIL or "." not in domain:
        return None
    return domain


def guess_domains(name):
    """Turn a business name into plausible .cz domains.

    Deliberately crude - the point of the measurement is to find out
    whether guessing is worth anything once verification is applied.
    """
    base = SUFFIX.sub("", name).strip()
    base = strip_diacritics(base).lower()
    base = re.sub(r"[^a-z0-9\s-]", " ", base)
    words = [w for w in base.split() if w]
    if not words:
        return []

    joined = "".join(words)
    dashed = "-".join(words)
    out = [f"{joined}.cz"]
    if dashed != joined:
        out.append(f"{dashed}.cz")
    if len(words) > 1:
        out.append(f"{words[0]}.cz")
    return out[:3]


# ---------------------------------------------------------------------------
# Route: MPSV
# ---------------------------------------------------------------------------


def collect_mpsv(wanted):
    """Stream the vacancy export, keep contacts for the sampled ICOs."""
    print(f"downloading {MPSV_URL} ...", file=sys.stderr)
    request = urllib.request.Request(MPSV_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = gzip.GzipFile(fileobj=response).read()
    print(f"  {len(payload) // 1024 // 1024} MB decompressed", file=sys.stderr)

    records = json.loads(payload)["polozky"]
    print(f"  {len(records)} vacancies", file=sys.stderr)

    found = {}
    employers = set()

    for item in records:
        employer = item.get("zamestnavatel") or {}
        ico = str(employer.get("ico") or "").zfill(8)
        if not ico.strip("0"):
            continue
        employers.add(ico)
        if ico not in wanted:
            continue

        entry = found.setdefault(ico, {"vacancies": 0, "urls": set(), "domains": set()})
        entry["vacancies"] += 1

        if item.get("urlAdresa"):
            entry["urls"].add(item["urlAdresa"].strip())

        contact = (item.get("prvniKontaktSeZamestnavatelem") or {})
        addresses = [
            (contact.get("komuSeHlasit") or {}).get("email"),
            (contact.get("kdeSeHlasit") or {}).get("email"),
        ]
        place = (item.get("mistoVykonuPrace") or {}).get("pracoviste") or []
        addresses += [p.get("email") for p in place]

        for address in addresses:
            domain = domain_of(address)
            if domain:
                entry["domains"].add(domain)

    return found, len(employers)


# ---------------------------------------------------------------------------
# Verification: does the page carry this ICO?
# ---------------------------------------------------------------------------


TAGS = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
MARKUP = re.compile(r"(?s)<[^>]+>")


def fetch(url):
    """Fetch one page, returning (html, final_url) or (None, None).

    Markup is kept rather than flattened straight away: the links out of
    a homepage are needed, and they do not survive tag stripping.
    """
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read(1_500_000).decode("utf-8", "replace"), response.geturl()
    except Exception:
        return None, None


def to_text(html):
    """Visible text of a page, whitespace collapsed."""
    return re.sub(r"\s+", " ", MARKUP.sub(" ", TAGS.sub(" ", html)))


def page_text(url):
    """Convenience wrapper kept for ad-hoc inspection."""
    html, final = fetch(url)
    return (to_text(html) if html else None), final


def carries_ico(text, ico):
    """Look for the ICO in the page, in the shapes Czech sites print it.

    The register form is zero-padded to eight digits, the printed form
    usually is not, and thousands separators are common - so the digits
    are compared after stripping everything that is not a digit.
    """
    if not text:
        return False

    bare = ico.lstrip("0")
    for match in re.finditer(r"\d[\d\s .]{6,12}\d", text):
        digits = re.sub(r"\D", "", match.group())
        if digits == ico or digits == bare:
            return True
    return False


DEAD = "dead"          # nothing answers on that host
MISMATCH = "mismatch"  # a site is there, but nothing ties it to this company

# Anchor text and hrefs that lead to the page carrying the ICO. Guessing
# /kontakt as a path finds almost nothing - the link is on the homepage,
# so it is read rather than invented.
CONTACT_HINT = re.compile(
    r"kontakt|contact|o-nas|o_nas|onas|o-firme|o-spolecnosti|about|impressum|"
    r"footer|udaje|firma",
    re.IGNORECASE,
)
LINK = re.compile(r'(?is)<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>')


def internal_links(html, domain, limit=5):
    """Contact-ish links from one page, as absolute URLs on that host."""
    from urllib.parse import urljoin, urlparse

    found = []
    seen = set()
    for href, anchor in LINK.findall(html):
        label = MARKUP.sub(" ", anchor)
        if not CONTACT_HINT.search(href) and not CONTACT_HINT.search(label):
            continue

        url = urljoin(f"https://{domain}/", href.strip())
        host = urlparse(url).netloc.lower()
        # Stay on the same site: an "Impressum" pointing at a parent
        # group would be somebody else's ICO.
        if domain.lstrip("www.") not in host:
            continue
        if url in seen:
            continue
        seen.add(url)
        found.append(url)
        if len(found) >= limit:
            break
    return found


def name_tokens(name):
    """Distinctive part of a business name, folded for comparison."""
    base = SUFFIX.sub("", name).strip()
    base = strip_diacritics(base).lower()
    base = re.sub(r"[^a-z0-9]+", " ", base).strip()
    if not base:
        return []
    return [base.replace(" ", ""), base.replace(" ", "-")]


def verify(ico, name, domain):
    """Decide whether `domain` is this company's website.

    Three outcomes, deliberately not two - the same split the pipeline
    uses for facts:

        match     the ICO is printed on the site. Proof.
        probable  no ICO, but the business name is on the page. Inference.
        mismatch  neither. Some other company's site, or a parked domain.

    Collapsing `probable` into `match` would be the exact failure this
    project is built against: a plausible guess presented as a fact.
    """
    raw = None
    for scheme in ("https://", "http://"):
        raw, final = fetch(f"{scheme}{domain}")
        if raw is not None:
            break
    if raw is None:
        return DEAD

    pages = [(final, raw)]
    for url in internal_links(raw, domain):
        body, resolved = fetch(url)
        if body:
            pages.append((resolved, body))

    for url, body in pages:
        text = to_text(body)
        if carries_ico(text, ico):
            return {"domain": domain, "url": url, "evidence": "ico"}

    tokens = name_tokens(name)
    for url, body in pages:
        folded = strip_diacritics(to_text(body)).lower()
        squeezed = re.sub(r"[^a-z0-9]+", "", folded)
        if any(t and (t in folded or t.replace("-", "") in squeezed) for t in tokens):
            return {"domain": domain, "url": url, "evidence": "name"}

    return MISMATCH


# ---------------------------------------------------------------------------


def cmd_mpsv():
    sample = load_sample()
    wanted = {row["ico"] for row in sample}
    contacts, employer_count = collect_mpsv(wanted)

    for row in sample:
        hit = contacts.get(row["ico"])
        row["mpsv_vacancies"] = hit["vacancies"] if hit else 0
        row["mpsv_urls"] = sorted(hit["urls"]) if hit else []
        row["mpsv_domains"] = sorted(hit["domains"]) if hit else []
        row["guessed"] = guess_domains(row["name"])

    SAMPLE_FILE.write_text(
        json.dumps({"employers_in_mpsv": employer_count, "sample": sample},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )

    with_vacancy = sum(1 for r in sample if r["mpsv_vacancies"])
    with_url = sum(1 for r in sample if r["mpsv_urls"])
    with_domain = sum(1 for r in sample if r["mpsv_domains"])
    either = sum(1 for r in sample if r["mpsv_urls"] or r["mpsv_domains"])

    print(f"\ndistinct employers in MPSV export : {employer_count}")
    print(f"sample size                       : {len(sample)}")
    print(f"  present in MPSV at all          : {with_vacancy:3}  {with_vacancy/2:.1f} %")
    print(f"  with urlAdresa                  : {with_url:3}  {with_url/2:.1f} %")
    print(f"  with a company mail domain      : {with_domain:3}  {with_domain/2:.1f} %")
    print(f"  with either                     : {either:3}  {either/2:.1f} %")
    print(f"\nwrote {SAMPLE_FILE}")


def cmd_verify():
    data = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    sample = data["sample"]

    def work(row):
        tried = []
        # MPSV domains first: they come from a document the employer
        # filed, not from a pattern we invented.
        best = None
        for source in ("mpsv_domains", "guessed"):
            route = "mpsv" if source == "mpsv_domains" else "guess"
            for domain in row[source]:
                result = verify(row["ico"], row["name"], domain)
                outcome = result if isinstance(result, str) else result["evidence"]
                tried.append({"domain": domain, "route": route, "outcome": outcome})

                if outcome == "ico":
                    return {**row, "route": route, "evidence": "ico",
                            "hit": result, "tried": tried}
                # A name match is worth keeping, but not worth stopping
                # for: a later domain may still prove itself with an ICO.
                if outcome == "name" and best is None:
                    best = {**row, "route": route, "evidence": "name", "hit": result}

        if best:
            return {**best, "tried": tried}
        return {**row, "route": None, "evidence": None, "hit": None, "tried": tried}

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(work, sample))

    Path("data/raw/url_probe_result.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    total = len(results)

    def count(**match):
        return sum(
            1 for r in results
            if all(r.get(k) == v for k, v in match.items())
        )

    proven = count(evidence="ico")
    probable = count(evidence="name")
    print(f"\nsample: {total} companies that passed the ICP filter\n")
    print(f"  proven   (ICO printed on the site) : {proven:3}  {proven / total * 100:.1f} %")
    print(f"  probable (name only, no ICO)       : {probable:3}  {probable / total * 100:.1f} %")
    print(f"  nothing                            : {total - proven - probable:3}  "
          f"{(total - proven - probable) / total * 100:.1f} %")

    print("\nby route:")
    for route in ("mpsv", "guess"):
        print(f"  {route:6} : proven {count(route=route, evidence='ico'):3}"
              f"   probable {count(route=route, evidence='name'):3}")

    # The number that matters for the design: a guessed domain that is
    # live and belongs to someone else. Without verification every one of
    # these would have been scraped as if it were the target company.
    print("\noutcome of every domain tried:")
    for route in ("mpsv", "guess"):
        tally = {}
        for row in results:
            for attempt in row["tried"]:
                if attempt["route"] == route:
                    tally[attempt["outcome"]] = tally.get(attempt["outcome"], 0) + 1
        tries = sum(tally.values()) or 1
        parts = "  ".join(f"{k}={v} ({v / tries * 100:.0f}%)" for k, v in sorted(tally.items()))
        print(f"  {route:6} tried {tries:4} : {parts}")

    print("\nwrote data/raw/url_probe_result.json")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "mpsv"
    {"mpsv": cmd_mpsv, "verify": cmd_verify}[command]()
