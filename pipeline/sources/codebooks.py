"""Static code lists used to decode ARES fields.

Reference data only - no HTTP, no logic beyond dictionary lookups.
Kept separate from the fetching code so that a wrong label can never
be confused with a wrong request.
"""

# CSU codebook 579 (kategoriePoctuPracovniku).
# The codes are NOT a regular sequence: after 240 comes 310, there is
# no code 250. Never generate these by pattern - always look them up.
EMPLOYEE_CATEGORIES = {
    "000": "Neuvedeno",
    "110": "0 employees",
    "120": "1-5 employees",
    "130": "6-9 employees",
    "210": "10-19 employees",
    "220": "20-24 employees",
    "230": "25-49 employees",
    "240": "50-99 employees",
    "310": "100-199 employees",
    "320": "200-249 employees",
    "330": "250-499 employees",
    "340": "500-999 employees",
    "410": "1000-1499 employees",
    "420": "1500-1999 employees",
    "430": "2000-2499 employees",
    "440": "2500-2999 employees",
    "450": "3000-3999 employees",
    "460": "4000-4999 employees",
    "470": "5000-9999 employees",
    "510": "10000+ employees",
}

# Legal form codes (pravniForma). Only the two forms the ICP targets
# are decoded; anything else is reported by its raw code.
LEGAL_FORMS = {
    "112": "s.r.o.",
    "121": "a.s.",
}

# NUTS3 regions. The interface saves a brief as region CODES (they come
# from data/ui/regions.json, keyed by the RES district prefix), while
# ARES reports a company's region as a NAME (sidlo.nazevKraje). Filtering
# a candidate against a saved brief therefore needs one of the two
# translated, and this is where the translation lives rather than in the
# filter itself.
#
# Both sides were compared before writing this table down: the 14 names
# ARES uses across the whole candidate list are character-for-character
# the 14 names RUIAN returns for these codes, Prague included ("Hlavní
# město Praha", which is a city and not a "kraj" in either source). Had
# they differed anywhere, the filter would have needed folding rather
# than a dictionary - so the exact-match assumption is recorded here as
# a checked fact, not a hope.
REGIONS = {
    "CZ010": "Hlavní město Praha",
    "CZ020": "Středočeský kraj",
    "CZ031": "Jihočeský kraj",
    "CZ032": "Plzeňský kraj",
    "CZ041": "Karlovarský kraj",
    "CZ042": "Ústecký kraj",
    "CZ051": "Liberecký kraj",
    "CZ052": "Královéhradecký kraj",
    "CZ053": "Pardubický kraj",
    "CZ063": "Kraj Vysočina",
    "CZ064": "Jihomoravský kraj",
    "CZ071": "Olomoucký kraj",
    "CZ072": "Zlínský kraj",
    "CZ080": "Moravskoslezský kraj",
}


def region_names(codes):
    """NUTS3 codes -> the region names ARES uses. Unknown codes pass through.

    An unrecognised code is kept as given instead of being dropped: a
    brief that names a region this table does not know must not quietly
    turn into "no region filter at all", which is how a narrowing
    criterion becomes a widening one.
    """
    return {REGIONS.get(code, code) for code in codes or ()}


def decode_employee_category(code):
    """Translate a CSU 579 code into a readable range.

    Returns None when the code itself is missing, so that "the register
    has no record" stays distinguishable from "the register says 000".
    """
    if code is None:
        return None

    code = str(code).zfill(3)

    return EMPLOYEE_CATEGORIES.get(
        code,
        f"Unknown employee category: {code}"
    )
