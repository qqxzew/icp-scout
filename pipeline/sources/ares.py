"""ARES data source: everything the pipeline needs about one company.

ARES (https://ares.gov.cz) is the Czech state register of economic
subjects. Four REST endpoints are read per company, all keyed by ICO
(the 8-digit registration number):

    ekonomicke-subjekty       profile: name, legal form, address, insolvency
    ekonomicke-subjekty-res   statistics: employee band, prevailing NACE
    ekonomicke-subjekty-vr    commercial register: directors, owners, dates
    ekonomicke-subjekty-rzp   trade licensing: trades, responsible persons

This module only fetches and reshapes data. It never decides whether a
company is a good lead - that is the scoring stage's job.

Entry point is get_company(ico), which returns one flat dict.

Run manually:
    python -m pipeline.sources.ares 29092540
"""

import sys
import json
import time

import requests

from pipeline.sources import coords
from pipeline.sources.codebooks import LEGAL_FORMS, decode_employee_category


BASE_URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"
TIMEOUT = 10  # seconds

# ARES rate-limits a burst of requests by answering 403 with an HTML
# block page instead of JSON. Measured on a run of 3299 companies with
# 10 threads: 23 failures, all of them inside the first ~300 requests,
# none in the remaining ~3000. So it lets go on its own - worth waiting
# out rather than losing the company.
MAX_ATTEMPTS = 4
BACKOFF = 2  # seconds, doubled on every further attempt
RETRY_STATUSES = (403, 429, 500, 502, 503, 504)


def fetch(endpoint, ico):
    """One GET to ARES. Returns parsed JSON, or None when the ICO
    is not present in that particular register.

    404 is an answer, not an error: sole traders have no commercial
    register record, some subjects hold no trade licences. The caller
    must be able to tell "not in this register" from "request failed",
    so only genuine failures raise.
    """
    url = f"{BASE_URL}/{endpoint}/{ico}"

    for attempt in range(MAX_ATTEMPTS):
        response = requests.get(url, timeout=TIMEOUT)

        if response.status_code == 404:
            return None
        if response.status_code == 200:
            return response.json()

        if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS - 1:
            delay = BACKOFF * (2 ** attempt)
            print(
                f"ares: HTTP {response.status_code} for {endpoint}/{ico}, "
                f"retry {attempt + 1}/{MAX_ATTEMPTS - 1} in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)
            continue

        raise RuntimeError(
            f"ARES {endpoint}/{ico}: HTTP {response.status_code} - {response.text[:200]}"
        )

    raise RuntimeError(f"ARES {endpoint}/{ico}: still failing after {MAX_ATTEMPTS} attempts")


def full_name(person):
    """Join title + first + last name, skipping whatever is missing.

    Returns None instead of an empty string when there is no name at
    all, so the absence stays visible in the output.
    """
    parts = [
        person.get("titulPredJmenem"),
        person.get("jmeno"),
        person.get("prijmeni"),
    ]
    name = " ".join(part for part in parts if part)
    return name or None


def first_record(data):
    """Unwrap the zaznamy envelope used by the res/vr/rzp endpoints.

    Returns the primary record, or None when the payload is missing
    or carries no records at all.

    Taking zaznamy[0] is wrong: a company that was re-registered keeps
    its old entries in the same list, and they can come first. Checked
    live on 00543551 - three records, the first two are HISTORICKY
    (deleted in 1991), the active one is third. The register marks the
    current one with primarniZaznam, so that is what decides.
    """
    if data is None:
        return None

    records = data.get("zaznamy") or []
    if not records:
        return None

    for record in records:
        if record.get("primarniZaznam"):
            return record

    # No record flagged as primary: fall back to the first one, but say
    # so - it means this endpoint broke its own convention.
    print(
        f"ares: no primarniZaznam among {len(records)} records, using the first",
        file=sys.stderr,
    )
    return records[0]


# ---------------------------------------------------------------------------
# Parsers: raw endpoint payload in, flat dict out. No HTTP in here,
# so any of them can be re-run on a saved JSON file while debugging.
# ---------------------------------------------------------------------------


def parse_summary(data):
    """ekonomicke-subjekty: identity, address, insolvency flag."""
    if data is None:
        return {
            "name": None,
            "legal_form": None,
            "legal_form_name": None,
            "city": None,
            "district": None,
            "region": None,
            "insolvency_state": None,
            "insolvent": None,
            "file_number": None,
            "address_code": None,
            "established": None,
            "updated": None,
        }

    # Prague has no district (nazevOkresu) - .get() everywhere,
    # a missing part of the address is normal, not an error.
    sidlo = data.get("sidlo", {})

    legal_form = data.get("pravniForma")

    insolvency_state = data.get("seznamRegistraci", {}).get("stavZdrojeIr")

    # File number lives inside dalsiUdaje, in the entry that comes
    # from the commercial register (datovyZdroj == "vr").
    file_number = None
    for entry in data.get("dalsiUdaje", []):
        if entry.get("datovyZdroj") == "vr":
            file_number = entry.get("spisovaZnacka")
            break

    return {
        "name": data.get("obchodniJmeno"),
        "legal_form": legal_form,
        "legal_form_name": LEGAL_FORMS.get(legal_form),
        "city": sidlo.get("nazevObce"),
        "district": sidlo.get("nazevOkresu"),
        "region": sidlo.get("nazevKraje"),
        "insolvency_state": insolvency_state,
        "insolvent": insolvency_state == "AKTIVNI" if insolvency_state else None,
        "file_number": file_number,
        "address_code": sidlo.get("kodAdresnihoMista"),
        # Needed to read the director dates correctly: a company founded
        # last year has an all-new board because it is new, not because
        # anyone was replaced. Without this the two look identical.
        "established": data.get("datumVzniku"),
        "updated": data.get("datumAktualizace"),
    }


def parse_res(data):
    """ekonomicke-subjekty-res: employee count band and prevailing NACE."""
    record = first_record(data)
    if record is None:
        return {
            "employee_code": None,
            "employee_range": None,
            "nace": None,
        }

    employee_code = record.get("statistickeUdaje", {}).get("kategoriePoctuPracovniku")

    return {
        "employee_code": employee_code,
        "employee_range": decode_employee_category(employee_code),
        "nace": record.get("czNacePrevazujici"),
    }


def parse_vr(data):
    """ekonomicke-subjekty-vr: current directors and owners.

    People sit two levels deep: statutarniOrgany[] -> clenoveOrganu[]
    and spolecnici[] -> spolecnik[]. An entry with datumVymazu is
    historical - the person already left.
    """
    record = first_record(data)
    if record is None:
        return {
            "directors": None,
            "owners": None,
        }

    directors = []
    for organ in record.get("statutarniOrgany", []):
        for member in organ.get("clenoveOrganu", []):
            if member.get("datumVymazu"):
                continue

            directors.append({
                "name": full_name(member.get("fyzickaOsoba", {})),
                "role": member.get("clenstvi", {}).get("funkce", {}).get("nazev"),
                "since": member.get("datumZapisu"),
            })

    owners = []
    for organ in record.get("spolecnici", []):
        for owner in organ.get("spolecnik", []):
            if owner.get("datumVymazu"):
                continue

            # An owner can be a person or another company.
            osoba = owner.get("osoba", {})
            name = (
                full_name(osoba.get("fyzickaOsoba", {}))
                or osoba.get("pravnickaOsoba", {}).get("obchodniJmeno")
            )

            owners.append({
                "name": name,
                "since": owner.get("datumZapisu"),
            })

    return {
        "directors": directors,
        "owners": owners,
    }


def parse_rzp(data):
    """ekonomicke-subjekty-rzp: trades, responsible persons, establishments."""
    record = first_record(data)
    if record is None:
        return {
            "trades": None,
            "responsible_representatives": None,
            "involved_persons": None,
            "establishments_active": None,
            "establishments_total": None,
        }

    # datumZaniku present means the licence was terminated.
    trades = [
        trade.get("predmetPodnikani")
        for trade in record.get("zivnosti", [])
        if not trade.get("datumZaniku")
    ]

    # Responsible representatives live inside each zivnost, not at the
    # top level. platnostDo present means the mandate already ended.
    representatives = []
    for trade in record.get("zivnosti", []):
        for person in trade.get("odpovedniZastupci", []):
            if person.get("platnostDo"):
                continue

            representatives.append({
                "name": full_name(person),
                "trade": trade.get("predmetPodnikani"),
            })

    involved = [
        {
            "name": full_name(person),
            "engagement": person.get("typAngazma"),
        }
        for person in record.get("angazovaneOsoby", [])
    ]

    # ARES never returns the establishments themselves, only counts in
    # provozovnyStav (verified live on several ICOs). Counts are still
    # a signal: "0 active of 2" means two sites were closed down.
    establishments = record.get("provozovnyStav", {})

    return {
        "trades": trades,
        "responsible_representatives": representatives,
        "involved_persons": involved,
        "establishments_active": establishments.get("pocetAktivnich"),
        "establishments_total": establishments.get("pocetCelkem"),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def get_company(ico):
    """Fetch all four ARES endpoints and merge them into one flat dict.

    Keys parsed from a register the company is absent from are set to
    None - "no record in that register" is itself a fact and must not
    look like an empty record.
    """
    ico = str(ico).strip().zfill(8)  # ARES only matches the padded form

    company = {"ico": ico}
    company.update(parse_summary(fetch("ekonomicke-subjekty", ico)))
    company.update(parse_res(fetch("ekonomicke-subjekty-res", ico)))
    company.update(parse_vr(fetch("ekonomicke-subjekty-vr", ico)))
    company.update(parse_rzp(fetch("ekonomicke-subjekty-rzp", ico)))

    # Coordinates come from RUIAN, not ARES - a separate call that
    # only makes sense when the address code is known.
    if company["address_code"]:
        company["coordinates"] = coords.get_coordinates(company["address_code"])
    else:
        company["coordinates"] = None

    return company


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m pipeline.sources.ares <ICO>")
        sys.exit(1)

    result = get_company(sys.argv[1])
    print(json.dumps(result, ensure_ascii=False, indent=2))
