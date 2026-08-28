"""Quality certificates from a company's own site - ICP sign 5.

The ICP lists "procesy jsou popsané" last and the log gives it a low
weight, for a measured reason: at 50-200 employees almost every company
has ISO 9001, so its presence barely separates one candidate from
another. That is an argument about WEIGHT, not about whether to collect
it - and this module exists because of the property that makes this
sign different from the other four:

    A certificate is issued by a third party. Everything else on a
    company website is the company talking about itself.

So this is the one pain sign that can be a hard fact rather than a
reading of marketing copy - and the strongest version of it is the
certificate PDF, which carries the standard, the issuer and a
registration number that a salesperson can repeat out loud without
risk. Two tiers, and the difference is deliberate:

    pdf     the certificate document itself was found and read.
            Standard + number + issuer, quoted from the PDF text.
    page    the site only says "certifikace ISO 9001" in prose. True,
            useful, but nobody's number - no third-party document was
            actually seen.

When neither turns up the field stays empty. An absent certificate is
not evidence of anything (see hypothesis E) - plenty of certified
companies simply do not publish the document - so nothing is inferred
from the silence and no placeholder is invented to fill the card.

Scanned certificates are common and are left as `scanned` rather than
OCR'd, for the same reason sbirka.py refuses to OCR a figure table: an
OCR'd registration number that is wrong by one digit looks exactly as
convincing as a right one.

Run:
    python -m pipeline.sources.certificates 29181241
    python -m pipeline.sources.certificates --all --archive
"""

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

from pipeline.sources.sbirka import extract_text
from pipeline.sources.website import Fetcher, USER_AGENT

OUTPUT = Path("data/raw/certificates.jsonl")

# Standards worth recognising, in the wording the ICP and the log use.
# IATF 16949 (automotive) and EN 1090 (structural steel) matter more
# than ISO 9001 here precisely because they are NOT universal: they say
# what the company actually makes, which ISO 9001 does not.
STANDARDS = (
    ("IATF 16949", re.compile(r"(?i)\bIATF\s*[-: ]?\s*16949")),
    ("EN 1090",    re.compile(r"(?i)\b(?:[ČC]SN\s+)?EN\s*[-: ]?\s*1090(?:-\d)?")),
    ("ISO 9001",   re.compile(r"(?i)\b(?:[ČC]SN\s+)?(?:EN\s+)?ISO\s*[-: ]?\s*9001")),
    ("ISO 14001",  re.compile(r"(?i)\b(?:[ČC]SN\s+)?(?:EN\s+)?ISO\s*[-: ]?\s*14001")),
    ("ISO 45001",  re.compile(r"(?i)\b(?:[ČC]SN\s+)?(?:EN\s+)?ISO\s*[-: ]?\s*45001")),
    ("ISO 50001",  re.compile(r"(?i)\b(?:[ČC]SN\s+)?(?:EN\s+)?ISO\s*[-: ]?\s*50001")),
    ("ISO 3834",   re.compile(r"(?i)\b(?:[ČC]SN\s+)?(?:EN\s+)?ISO\s*[-: ]?\s*3834(?:-\d)?")),
    ("ISO 27001",  re.compile(r"(?i)\bISO(?:/IEC)?\s*[-: ]?\s*27001")),
    ("ISO 13485",  re.compile(r"(?i)\b(?:[ČC]SN\s+)?(?:EN\s+)?ISO\s*[-: ]?\s*13485")),
)

# A link is worth downloading if either the URL or the anchor text looks
# like a certificate. Both are tested because Czech sites label the file
# "Certifikát ISO 9001" while the href is an opaque /media/1234.pdf.
CERT_LINK = re.compile(
    r"(?i)certifik|certificate|osvedcen|osv[eě]d[cč]en|zertifik|iso[-_ ]?\d{4}|iatf|en[-_ ]?1090"
)

# The certification body. This is the part of sign 5 that actually
# carries weight: "we have ISO 9001" is the company talking, "Bureau
# Veritas issued it" names a third party who can be asked. Found on
# OK Záchlumí, whose certificates are published only as images - the
# page text still named the issuer, so the fact survived even though
# no document could be read.
ISSUERS = re.compile(
    r"(?i)\b(Bureau\s+Veritas|T[UÜ]V\s*(?:S[UÜ]D|NORD|Rheinland|SUD)?|DNV(?:\s*GL)?|"
    r"LRQA|Lloyd'?s\s+Register|SGS|DEKRA|DQS|CQS|ITC\s+Zl[ií]n|EZ[UÚ]|Certline|"
    r"QUALIFORM|Strojírenský\s+zku[sš]ebn[ií]\s+[uú]stav|TZ[UÚ])\b"
)

# Certificates published as an image are the "no number" case, and it is
# worth recording as its own state rather than as silence: it says the
# company IS certified and that the number was simply not machine
# readable - different from a company that publishes nothing at all.
CERT_IMAGE = re.compile(r"(?i)\.(?:jpe?g|png|gif|webp)(?:[?#]|$)")

LINK = re.compile(r"""<a\b[^>]*?href=["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
TAG = re.compile(r"<[^>]+>")

# Finding the registration number is a two-step job, not one regex, and
# the reason is the same one sbirka.py hit in financial statements: a
# PDF text layer flattens a table, so the label and its value are not
# adjacent. On ZKL's real certificate the text reads
#
#   "Registrační číslo certifikátu Platí od Platí do Datum certifikace
#    31150274 OHS18 2022-05-19 2025-05-18 2022-05-19"
#
# - the whole header row comes first, then the whole value row. A
# pattern that simply grabs what follows the label captures the next
# column heading instead of the number.
#
# So: find the label, then scan forward for the first token SHAPED like
# a registration number, rejecting the dates that sit beside it.
NUMBER_LABEL = re.compile(
    r"(?i)\b(?:registra[cč]n[ií]\s+[cč][ií]sl[oa]|[cč][ií]slo\s+certifik[aá]tu|"
    r"registra[cč]n[ií]\s+[cč]\.|certificate\s+(?:registration\s+)?(?:no|number)|"
    r"reg\.?\s*no|zertifikat-?registrier)"
)

# A registration number: mostly digits, at least six characters, and it
# may carry a short alphanumeric suffix ("31150274 OHS18", "CZ12 3456").
NUMBER_TOKEN = re.compile(
    r"\b(\d[\d\s/\-]{4,}\d(?:\s+[A-Z][A-Z0-9]{1,7})?|[A-Z]{1,4}[-/ ]?\d{4,}[-/A-Z0-9]*)\b"
)

# Dates and years live in the same table row and match the shape above,
# so they are excluded explicitly rather than by hoping they will not.
NOT_A_NUMBER = re.compile(r"^(?:(?:19|20)\d{2}(?:[-./]\d{1,2}){0,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4})$")

# How far past the label to look for the value. One flattened table row
# of headings is comfortably under this; two tables are not, which is
# the point - a number found far from its label is not its value.
NUMBER_SEARCH_WINDOW = 220

# How much of the PDF to keep as the quote around a standard.
WINDOW = 160

MAX_PDFS_PER_COMPANY = 4
MIN_TEXT_LAYER = 200          # shorter than this means a scan, not a document


def strip_tags(markup):
    return TAG.sub(" ", markup or "").replace("&nbsp;", " ").strip()


def certificate_links(html, base_url):
    """Absolute URLs of PDFs on this page that look like certificates.

    Only PDFs. A certificate rendered as an HTML page is already covered
    by the page tier - this tier exists specifically to get hold of the
    issued document.
    """
    found = []
    for href, label in LINK.findall(html or ""):
        text = strip_tags(label)
        if ".pdf" not in href.lower():
            continue
        if not (CERT_LINK.search(href) or CERT_LINK.search(text)):
            continue
        url = urljoin(base_url, href)
        if url not in [u for u, _ in found]:
            found.append((url, text))
    return found[:MAX_PDFS_PER_COMPANY]


def certificate_images(html, base_url):
    """Certificate scans published as images - present but unreadable.

    Not downloaded and not OCR'd. Recording the URL is enough: it lets a
    salesperson open the scan themselves, and it distinguishes "the
    number exists but only as pixels" from "nothing was published".
    """
    found = []
    for href, label in LINK.findall(html or ""):
        text = strip_tags(label)
        if not CERT_IMAGE.search(href):
            continue
        if not (CERT_LINK.search(href) or CERT_LINK.search(text)):
            continue
        url = urljoin(base_url, href)
        if url not in found:
            found.append(url)
    # Images are also embedded rather than linked, so <img src> counts too.
    for src in re.findall(r"""<img\b[^>]*?src=["']([^"']+)["']""", html or "", re.I):
        if CERT_IMAGE.search(src) and CERT_LINK.search(src):
            url = urljoin(base_url, src)
            if url not in found:
                found.append(url)
    return found[:MAX_PDFS_PER_COMPANY]


def issuer_in(text):
    """The certification body named in `text`, or None."""
    match = ISSUERS.search(text or "")
    return " ".join(match.group(1).split()) if match else None


def standards_in(text):
    """Every standard named in `text`, each with the sentence around it.

    The quote is what makes this checkable downstream: evidence/verify.py
    will look for it in the archived document, so it has to be taken
    from the text verbatim rather than rebuilt from the match.
    """
    out = []
    for name, pattern in STANDARDS:
        match = pattern.search(text)
        if not match:
            continue
        start = max(0, match.start() - WINDOW // 2)
        end = min(len(text), match.end() + WINDOW // 2)
        out.append({"standard": name, "quote": text[start:end].strip()})
    return out


def certificate_number(text):
    """The registration number printed on a certificate, or None.

    None is a normal answer, not a failure: plenty of certificates print
    the number only inside the scanned image, and guessing one from
    surrounding digits is exactly the kind of plausible-looking
    invention this project is built to avoid. The first version of this
    function did exactly that - it returned "Konrad-Adenauer-Allee 8-10"
    from the certifier's letterhead, because `nr\\.?` matched the "nr"
    inside "Konrad" and the address followed. Hence word boundaries on
    the label, and a shape test on the value.
    """
    label = NUMBER_LABEL.search(text or "")
    if not label:
        return None

    window = text[label.end():label.end() + NUMBER_SEARCH_WINDOW]
    for match in NUMBER_TOKEN.finditer(window):
        number = " ".join(match.group(1).split()).strip(" .-/")
        # The numeric head is tested separately from any letter suffix:
        # "2024-01-01 CZ" is a validity date that happens to be followed
        # by a country code, and testing the whole string would let it
        # through because the suffix makes it stop looking like a date.
        head = re.match(r"[\d\s/\-.]+", number)
        if head and NOT_A_NUMBER.match(head.group(0).strip()):
            continue
        if NOT_A_NUMBER.match(number):
            continue
        if len(re.sub(r"\D", "", number)) < 5:      # too few digits to be a registration
            continue
        return number
    return None


def fetch_pdf(fetcher, url):
    """Raw PDF bytes, or None. Fetcher.get() decodes to text, so it cannot
    be reused here - a decoded PDF is destroyed before pypdf sees it."""
    import requests
    if not fetcher.allowed(url):
        return None
    try:
        response = fetcher.session.get(
            url, timeout=45, allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException:
        return None
    if response.status_code >= 400:
        return None
    body = response.content
    return body if body.startswith(b"%PDF-") else None


def from_archive(archive, ico, fetcher=None, run_id=None):
    """Certificates for one company, read off what website.py already stored.

    The page tier costs nothing - the harvested pages are on disk. The
    PDF tier costs one request per certificate document, and only for
    companies whose pages actually link to one.
    """
    result = {
        "ico": str(ico).zfill(8),
        "certificates": [],
        "tier": None,
        "retrieved_at": date.today().isoformat(),
    }

    documents = archive.documents(ico, source="website")
    if not documents:
        result["tier"] = "no_pages"
        return result

    # -- page tier: what the site says in prose -----------------------
    seen = set()
    for row, text in documents:
        if not text:
            continue
        page_issuer = issuer_in(text)
        for hit in standards_in(text):
            if hit["standard"] in seen:
                continue
            seen.add(hit["standard"])
            result["certificates"].append({
                "standard": hit["standard"],
                "number": None,
                "issuer": page_issuer,
                "tier": "page",
                "source_url": row["url"],
                "snapshot_id": row["id"],
                "quote": hit["quote"],
            })

    if result["certificates"]:
        result["tier"] = "page"

    # -- pdf tier: the issued document itself -------------------------
    if fetcher is None:
        return result

    for row, _text in documents:
        # website.py archives readable text, not markup, so links have to
        # be re-read from the live page. Only pages classified as
        # certificates/about are worth the request.
        if (row["kind"] or "") not in ("certificates", "about", "production"):
            continue
        html, final_url = fetcher.get(row["url"])
        if not html:
            continue

        for image_url in certificate_images(html, final_url or row["url"]):
            result["certificates"].append({
                "standard": None, "number": None, "issuer": None,
                "tier": "image", "source_url": image_url,
                "snapshot_id": None, "quote": None,
            })

        for url, label in certificate_links(html, final_url or row["url"]):
            pdf = fetch_pdf(fetcher, url)
            if not pdf:
                continue
            text = extract_text(pdf)
            if len(text) < MIN_TEXT_LAYER:
                result["certificates"].append({
                    "standard": None, "number": None, "issuer": None,
                    "tier": "scanned", "source_url": url, "snapshot_id": None,
                    "quote": None, "label": label,
                })
                continue

            # The PDF text is archived unconditionally: a certificate a
            # salesperson may quote has to have a stored source behind
            # it, and archive.store() deduplicates by content hash, so
            # re-reading an unchanged certificate costs nothing.
            snapshot_id, _ = archive.store(
                ico, "certificate", text, url=url, run_id=run_id, kind="certificate",
            )

            number = certificate_number(text)
            pdf_issuer = issuer_in(text)
            for hit in standards_in(text):
                result["certificates"].append({
                    "standard": hit["standard"],
                    "number": number,
                    "issuer": pdf_issuer,
                    "tier": "pdf",
                    "source_url": url,
                    "snapshot_id": snapshot_id,
                    "quote": hit["quote"],
                    "label": label,
                })
            result["tier"] = "pdf"

    if not result["certificates"]:
        result["tier"] = "none"
    return result


def record(archive, ico, result, run_id=None):
    """Write each certificate as a claim, verified like any other statement.

    A certificate found in a PDF carries a quote from that PDF and goes
    through the same substring check as a model's citation - the source
    being a document rather than a model changes nothing about how it is
    proved.
    """
    from pipeline.evidence.verify import check

    recorded = []
    for cert in result["certificates"]:
        if not cert.get("snapshot_id") or not cert.get("standard"):
            continue
        value = cert["standard"]
        if cert.get("number"):
            value = f"{cert['standard']} (č. {cert['number']})"
        recorded.append(check(
            archive, ico, f"certificate:{cert['tier']}", value,
            cert.get("quote"), cert["snapshot_id"], run_id=run_id,
        ))
    return recorded


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ISO/IATF certificates from company sites.")
    parser.add_argument("ico", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--archive", action="store_true", help="record claims")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-pdf", action="store_true", help="page tier only, no requests")
    args = parser.parse_args()

    from pipeline.evidence.archive import Archive

    archive = Archive()
    fetcher = None if args.no_pdf else Fetcher()
    run_id = archive.start_run(note="certificates") if args.archive else None

    if args.all:
        icos = [r["ico"] for r in archive.db.execute(
            "SELECT DISTINCT ico FROM snapshot WHERE source='website'"
        ).fetchall()][:args.limit]
    else:
        icos = args.ico

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    sink = open(OUTPUT, "w", encoding="utf-8") if args.all else None

    with_cert = 0
    for ico in icos:
        result = from_archive(archive, ico, fetcher, run_id)
        if args.archive:
            record(archive, ico, result, run_id)
        if result["certificates"]:
            with_cert += 1
        if sink:
            sink.write(json.dumps(result, ensure_ascii=False) + "\n")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))

    if sink:
        sink.close()
        print(f"{with_cert} of {len(icos)} companies have at least one certificate",
              file=sys.stderr)

    if run_id:
        archive.finish_run(run_id)
    archive.close()
