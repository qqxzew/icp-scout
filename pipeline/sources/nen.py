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
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime
from http.cookiejar import CookieJar
from pathlib import Path

BASE = "https://nen.nipez.cz"
LISTING = f"{BASE}/verejne-zakazky"
OUTPUT = Path("data/raw/nen.jsonl")
CANDIDATES = Path("data/raw/ares_candidates_v2.jsonl")

USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"
TIMEOUT = 45
DELAY = 0.6          # seconds between requests; nothing here is urgent

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

# What makes a tender interesting for RTsoft. Deliberately wider than the
# four words RTsoft named for subsidies (ERP, informační systém, MES,
# WMS): they said "dala by se hledat i jiná klíčová slova, ale teď stačí
# takto", and a tender's name is more specific than a subsidy's, so
# there is room to catch the shop-floor wording the ICP actually cares
# about - terminals, data collection, production planning.
SUBJECT = re.compile(
    r"(?i)\bERP\b|\bMES\b|\bWMS\b|\bAPS\b|informa[cč]n[ií]\s*syst[ée]m|"
    r"[rř][ií]zen[ií]\s*v[ýy]roby|pl[aá]nov[aá]n[ií]\s*v[ýy]roby|"
    r"v[ýy]robn[ií]\s*syst[ée]m|termin[aá]l|[cč]&#x00E1;rov|čárov[ée]\s*k[oó]dy|"
    r"sb[eě]r\s*dat|dispe[cč]|sklado[vw]"
)


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
        wait = DELAY - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        with self.opener.open(url, timeout=TIMEOUT) as response:
            body = response.read()
        self._last = time.monotonic()
        return body.decode("utf-8", "replace")


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
    html = session.get(f"{LISTING}/p:vz:zadavatelICO={ico}")
    rows = parse_rows(html)
    for row in rows:
        row["ico"] = ico
        row["relevant"] = bool(SUBJECT.search(row.get("name") or ""))
        row["retrieved_at"] = date.today().isoformat()
    return rows


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

    found = with_tender = 0
    with open(output, "w", encoding="utf-8") as sink:
        for index, ico in enumerate(icos, 1):
            try:
                rows = tenders_for(ico, session)
            except Exception as error:
                print(f"  {ico}: {type(error).__name__} {error}", file=sys.stderr)
                continue
            if rows:
                with_tender += 1
                found += len(rows)
                for row in rows:
                    sink.write(json.dumps(row, ensure_ascii=False) + "\n")
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
