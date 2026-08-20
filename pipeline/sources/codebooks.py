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
