"""Sbírka listin: turnover from published financial statements.

Turnover is the one ICP criterion with no register field behind it. It
lives in the výkaz zisku a ztráty, which companies file as a PDF into
Sbírka listin at or.justice.cz - free, public, no login.

Why this module is separate from the rest of sources/: it is the only
one that is *expensive and unreliable per company*, so the pipeline runs
it on the final five, not on the candidate list. Everything here is
built to fail loudly and cheaply rather than to guess.

The chain, verified by hand on 2026-08-21:

    IČO
      -> /ias/ui/rejstrik-$firma?ico=...        subjektId
      -> /ias/ui/vypis-sl-firma?subjektId=...   document list
      -> /ias/ui/vypis-sl-detail?dokument=...   file links
      -> /ias/content/download?id=...           the PDF itself

Two things about that chain are not obvious and cost a while to find:

* the download ids are single-use and tied to a session. They must be
  read from a freshly loaded detail page and fetched over the same
  cookie jar, or the server answers "Neplatný odkaz" as HTML with
  status 200. Hence Session below; hence nothing caches a download URL.
* the document type string carries the fiscal year - "účetní závěrka
  [2025]". The year filter therefore costs zero downloads, which
  matters: measured on 12 random candidates, half had nothing newer
  than 2024 and were dropped before a single PDF was fetched.

What this module does NOT do: decide whether the turnover fits the ICP,
and OCR scanned documents. A scan is reported as unknown with a reason.

Run manually:
    python -m pipeline.sources.sbirka 42766991
"""

import html
import io
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime
from http.cookiejar import CookieJar

BASE = "https://or.justice.cz"
TIMEOUT = 45
USER_AGENT = "icp-scout/0.1 (+https://github.com/qqxzew/icp-scout)"

# Statements older than this are not worth reading: a turnover from two
# closed years ago says little about a company we are about to call.
#
# Measured on 200 ICP-matching companies on 2026-08-21: raising this to
# 2025 discards 62 companies whose newest filing is 2024. Companies have
# most of the following year to file, so during 2026 the 2024 statement
# is still the newest many of them have.
MIN_YEAR = 2024

# Rows of the výkaz zisku a ztráty that make up turnover, by the row
# numbering of vyhláška 500/2002 Sb.:
#   1  I.  Tržby z prodeje výrobků a služeb
#   2  II. Tržby za prodej zboží
# Verified against the PDF of IČO 25470507, where XML row 1 kc_sled
# 131601 matches the printed "I. Tržby z prodeje výrobků a služeb
# 131601 149026" exactly.
REVENUE_ROWS = (1, 2)

# The XML carries no unit marker of its own. The form it follows is
# filed in thousands, and the cross-check above confirms it: 131601 in
# the XML is the same figure the PDF prints under "v celých tisících".
XML_SCALE = 1000

# Politeness between requests. Volume here is five companies a week, so
# there is nothing to gain by going faster.
DELAY = 0.4

# Row labels of the výkaz zisku a ztráty, druhové členění. Fixed by
# vyhláška 500/2002 Sb., which is what makes deterministic search
# possible at all - this is a form, not prose.
REVENUE_LABELS = (
    "Tržby z prodeje výrobků a služeb",
    "Tržby za prodej zboží",
)

# Wording used before the 2016 revision. A 2025 statement should not use
# it, but filers do reuse old templates.
LEGACY_LABELS = (
    "Tržby za prodej vlastních výrobků a služeb",
    "Výkony",
)

# "(v celých tisících CZK)" is the usual header; whole crowns happen.
# Getting this wrong is a factor-of-1000 error that still looks
# plausible, so the unit is read from the document, never assumed.
THOUSANDS_MARKERS = ("v celých tisících", "v tisících", "v tis. Kč", "(v tisících")
WHOLE_MARKERS = ("v celých Kč", "v Kč")

_NUMBER = re.compile(r"-?\d{1,3}(?:[\s ]\d{3})+|-?\d+")


class Session:
    """One cookie-backed HTTP session against or.justice.cz.

    A plain urllib call is not enough: download links are bound to the
    session that rendered the page they came from.
    """

    def __init__(self):
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        self._opener.addheaders = [("User-Agent", USER_AGENT)]

    def get(self, url):
        """Fetch one URL and return raw bytes."""
        time.sleep(DELAY)
        with self._opener.open(url, timeout=TIMEOUT) as response:
            return response.read()

    def get_html(self, url):
        """Fetch one URL and return decoded markup."""
        return self.get(url).decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def find_subject_id(session, ico):
    """Resolve an IČO to the internal subjektId the register indexes by.

    Returns None when the company has no Sbírka listin entry at all.
    """
    markup = session.get_html(f"{BASE}/ias/ui/rejstrik-$firma?ico={ico}")
    match = re.search(r"vypis-sl-firma\?subjektId=(\d+)", markup)
    return match.group(1) if match else None


def list_documents(session, subject_id):
    """List everything filed for one subject.

    Each entry is {ref, kind, years, href}. `years` comes from the type
    string ("účetní závěrka [2025]"), which is why the whole date filter
    happens here rather than after downloading anything.
    """
    markup = session.get_html(f"{BASE}/ias/ui/vypis-sl-firma?subjektId={subject_id}")
    documents = []

    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", markup, re.S):
        link = re.search(r'href="([^"]*vypis-sl-detail[^"]*)"', row)
        if not link:
            continue

        cells = [_plain(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) < 2:
            continue

        documents.append({
            "ref": cells[0],
            "kind": cells[1],
            "years": sorted({int(y) for y in re.findall(r"\[(\d{4})\]", cells[1])}, reverse=True),
            "href": html.unescape(link.group(1)),
        })

    return documents


def pick_statement(documents, min_year=MIN_YEAR):
    """Choose the newest účetní závěrka not older than min_year.

    Returns None when there is none - which is a fact about the company,
    not a failure to look properly.
    """
    statements = [
        doc for doc in documents
        if "závěrka" in doc["kind"].lower() and doc["years"]
    ]
    if not statements:
        return None

    newest = max(statements, key=lambda doc: doc["years"][0])
    return newest if newest["years"][0] >= min_year else None


def document_files(session, href):
    """List the files attached to one document as (url, label) pairs.

    The urls returned here are single-use. Fetch them now, over the same
    session, or they expire into an HTML error page.
    """
    url = f"{BASE}/ias/ui/" + href.lstrip("./")
    markup = session.get_html(url)

    files = []
    for match in re.finditer(
        r'<a[^>]*href="([^"]*content/download[^"]*)"[^>]*>(.*?)</a>', markup, re.S
    ):
        label = _plain(match.group(2))
        files.append((BASE + html.unescape(match.group(1)), label))
    return files


# ---------------------------------------------------------------------------
# Reading the document
# ---------------------------------------------------------------------------


def parse_statement_xml(xml_bytes):
    """Read turnover out of the machine-readable copy of the statement.

    Some filings carry a UZ-*.xml next to the PDF - the same statement
    as data. It removes both traps the PDF has: the period is named
    (`kc_sled` current, `kc_min` previous) instead of being guessed from
    the order text came out in, and rows are numbered instead of
    matched by label.

    Record types, worked out by matching values against a printed
    statement: VetaUA and VetaUD are the two sides of the balance sheet,
    VetaUB is the P&L. Returns None when there is no P&L in the file,
    which is the case for a company filing a balance sheet only.
    """
    text = xml_bytes.decode("utf-8", "replace")

    header = re.search(r"<VetaD\b([^>]*)/>", text)
    attributes = dict(re.findall(r'(\w+)="([^"]*)"', header.group(1))) if header else {}

    rows = {}
    for match in re.finditer(r"<VetaUB\b([^>]*)/>", text):
        row = dict(re.findall(r'(\w+)="([^"]*)"', match.group(1)))
        try:
            rows[int(row["c_radku"])] = {
                "current": int(row["kc_sled"]),
                "previous": int(row["kc_min"]),
            }
        except (KeyError, ValueError):
            continue

    if not rows:
        return None

    present = [rows[n] for n in REVENUE_ROWS if n in rows]
    if not present:
        return None

    return {
        "value_czk": sum(r["current"] for r in present) * XML_SCALE,
        "previous_czk": sum(r["previous"] for r in present) * XML_SCALE,
        "rows": {n: rows[n] for n in REVENUE_ROWS if n in rows},
        "period_end": attributes.get("d_uv"),
        "currency": attributes.get("uv_mena"),
        "scale": XML_SCALE,
    }


def extract_text(pdf_bytes):
    """Return the text layer of a PDF, or "" when there is none.

    Scanned filings come back empty or near-empty. That is left as
    unknown on purpose: OCR of a numeric table produces wrong figures
    that look right, which is the one failure this project cannot
    afford.
    """
    try:
        from pypdf import PdfReader
    except ImportError:  # keep the module importable without the dependency
        raise RuntimeError("pypdf is required to read statements: pip install pypdf")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        raw = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""
    return normalize(raw)


def normalize(text):
    """Collapse every run of whitespace into a single ordinary space.

    Typesetting inside these PDFs uses non-breaking spaces between the
    words of a row label - "Tržby\\xa0z\\xa0prodeje\\xa0výrobků" - so a
    plain search for the label finds nothing at all. Normalising once,
    here, means every later step sees the same shape of text.

    The evidence layer has to apply this same function before looking
    for a quote, or a quote taken from here will not be found in the
    archived document.
    """
    return re.sub(r"\s+", " ", text)


def detect_scale(text, before=None):
    """Return how many crowns one printed figure stands for.

    Statements are usually filed in thousands. Reading 261 426 as crowns
    instead of thousands understates a 261M company as a 261k one, and
    the wrong answer looks entirely plausible - so the unit is taken
    from the document and left as None when the document does not say.

    `before` is the offset of the row being read. One filing bundles a
    balance sheet, a P&L and an equity statement, each with its own unit
    header, so the marker that counts is the last one *above* the row -
    not the first in the file.
    """
    limit = len(text) if before is None else before
    best = (None, None, -1)

    for scale, markers in ((1000, THOUSANDS_MARKERS), (1, WHOLE_MARKERS)):
        for marker in markers:
            position = text.rfind(marker, 0, limit)
            if position > best[2]:
                best = (scale, marker, position)

    return best[0], best[1]


def revenue_windows(text, width=140):
    """Cut out the text around every revenue row of the P&L.

    The window, not the figure, is the unit of work here. PDF extraction
    interleaves the columns - a real line comes out as

        262 201Tržby z prodeje výrobků a služeb 261 4261I.

    where 262 201 is the *previous* period sitting in front of the label
    and 261 426 the current one behind it. Any rule of the form "take
    the number after the label" quietly returns last year's figure, so
    this returns the whole neighbourhood and lets the caller decide.
    """
    windows = []
    for label in REVENUE_LABELS + LEGACY_LABELS:
        for match in re.finditer(re.escape(label), text):
            start = max(0, match.start() - width)
            end = min(len(text), match.end() + width)
            windows.append({
                "label": label,
                "legacy": label in LEGACY_LABELS,
                "position": match.start(),
                # Kept verbatim: this is the string verify.py will look
                # for in the archived document.
                "quote": _collapse(text[start:end]),
                "numbers": [_to_int(n) for n in _NUMBER.findall(text[start:end])],
            })
    return windows


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def get_turnover(ico, min_year=MIN_YEAR, session=None):
    """Collect what Sbírka listin says about one company's turnover.

    Returns a dict that always carries `status` and `reason`:

        no_subject       not in Sbírka listin
        no_statement     nothing filed for min_year or later
        no_pdf           filing holds no readable PDF
        scanned          PDF has no text layer, would need OCR
        no_revenue_row   readable, but no P&L row - the normal case for
                         a small company filing only a balance sheet
        found            turnover in crowns, taken from the XML copy
        found_rows       rows located in the PDF, but which column is
                         the current period is still open

    The two success states are deliberately different. `found` is a
    number this module stands behind: the XML names the period per
    figure. `found_rows` is evidence only - the PDF text layer shuffles
    the two periods together, so picking one is a judgement, and it
    belongs to the LLM stage where the quote can be checked against the
    archived document.
    """
    session = session or Session()
    result = {
        "ico": str(ico),
        "status": None,
        "reason": None,
        "year": None,
        "document_ref": None,
        "source_url": None,
        "scale": None,
        "scale_marker": None,
        "windows": [],
        "retrieved_at": date.today().isoformat(),
    }

    subject_id = find_subject_id(session, ico)
    if not subject_id:
        return _fail(result, "no_subject", "IČO has no Sbírka listin entry")

    statement = pick_statement(list_documents(session, subject_id), min_year)
    if not statement:
        return _fail(result, "no_statement", f"no účetní závěrka for {min_year} or later")

    result["year"] = statement["years"][0]
    result["document_ref"] = statement["ref"]

    files = document_files(session, statement["href"])

    # The XML copy is tried first and, when present, ends the job: it
    # states the period per figure, so nothing downstream has to guess a
    # column. The PDF path exists only because most filings have no XML.
    for url, label in files:
        if ".xml" not in label.lower():
            continue
        data = session.get(url)
        if not data.lstrip().startswith(b"<?xml"):
            continue
        parsed = parse_statement_xml(data)
        if parsed:
            result.update({
                "status": "found",
                "method": "xml",
                "source_url": url,
                "file_label": label,
                "scale": parsed["scale"],
                "value_czk": parsed["value_czk"],
                "previous_czk": parsed["previous_czk"],
                "period_end": parsed["period_end"],
                "rows": parsed["rows"],
            })
            return result

    pdfs = [(url, label) for url, label in files if ".pdf" in label.lower()]
    if not pdfs:
        return _fail(result, "no_pdf", "filing contains no PDF")

    # Several files hang off one filing - the statement itself, the
    # notes, the auditor's report. Which one holds the P&L is not
    # knowable from the label, so read them until the rows turn up.
    best_text = ""
    for url, label in pdfs:
        data = session.get(url)
        if not data.startswith(b"%PDF-"):
            continue

        text = extract_text(data)
        if len(text) > len(best_text):
            best_text, result["source_url"], result["file_label"] = text, url, label

        windows = revenue_windows(text)
        if windows:
            scale, marker = detect_scale(text, before=windows[0]["position"])
            result.update({
                "status": "found_rows",
                "reason": "column choice pending: PDF text interleaves the periods",
                "method": "pdf",
                "source_url": url,
                "file_label": label,
                "scale": scale,
                "scale_marker": marker,
                "windows": windows,
            })
            return result

    if len(best_text) < 500:
        return _fail(result, "scanned", "no text layer, OCR would be needed")
    return _fail(result, "no_revenue_row", "readable, but no P&L row present")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fail(result, status, reason):
    result["status"] = status
    result["reason"] = reason
    return result


def _plain(markup):
    """Strip tags and squeeze whitespace out of one HTML fragment."""
    return _collapse(html.unescape(re.sub(r"<[^>]+>", " ", markup)))


def _collapse(text):
    return re.sub(r"\s+", " ", text).strip()


def _to_int(token):
    """Parse a Czech-formatted integer, tolerating grouped spaces."""
    try:
        return int(re.sub(r"[\s ]", "", token))
    except ValueError:
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.sources.sbirka <ICO> [MIN_YEAR]")
        sys.exit(1)

    year = int(sys.argv[2]) if len(sys.argv) > 2 else MIN_YEAR
    print(json.dumps(get_turnover(sys.argv[1], year), ensure_ascii=False, indent=2))
