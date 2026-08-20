import requests
import coords


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


def decode_employee_category(code):
    code = str(code).zfill(3)

    return EMPLOYEE_CATEGORIES.get(
        code,
        f"Unknown employee category: {code}"
    )

ico = input("Enter the ICO: ")
url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty/{ico}"

response = requests.get(url, timeout=5)

if response.status_code == 200:
    data = response.json()

    kod_adresniho_mista = data["sidlo"]["kodAdresnihoMista"]

    print(data["obchodniJmeno"])
    LEGAL_FORM = data.get("pravniForma")

    if LEGAL_FORM == "112":
        print("s.r.o.")
    elif LEGAL_FORM == "121":
        print("a.s.")
    else:
        print(f"not eligible (legal form {LEGAL_FORM})")

    sidlo = data["sidlo"]
    print(sidlo.get("nazevObce"), "|", sidlo.get("nazevOkresu"), "|", sidlo.get("nazevKraje"))

    if data["seznamRegistraci"]["stavZdrojeIr"].upper() == "AKTIVNI":
        print("the company is bankrupt")
        exit()
    else:
        print(data["seznamRegistraci"]["stavZdrojeIr"])
    print(coords.get_coordinates(kod_adresniho_mista))

    for zapis in data.get("dalsiUdaje", []):
        if zapis.get("datovyZdroj") == "vr":
            spisova_znacka = zapis.get("spisovaZnacka")
            print(f"Spisová značka: {spisova_znacka}")
            break
    print(data["datumAktualizace"])

else:
    print(f"Error at the ARES API: ekonomicke-subjekty - {response.status_code} - {response.text}")


url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-res/{ico}"
response = requests.get(url, timeout=5)

if response.status_code == 200:
    data = response.json()
    category_code = (
        data["zaznamy"][0]
        ["statistickeUdaje"]
        ["kategoriePoctuPracovniku"]
    )

    print(decode_employee_category(category_code))
    print(data["zaznamy"][0]["czNacePrevazujici"])
else:
    print(f"Error at the ARES API: ekonomicke-subjekty-res - {response.status_code} - {response.text}")


url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-vr/{ico}"
response = requests.get(url, timeout=5)

if response.status_code == 200:
    data = response.json()
    zaznam = data["zaznamy"][0]

    # statutarniOrgany[] -> clenoveOrganu[], not statutarniOrgan[] directly.
    # Two levels of nesting: an "organ" (e.g. "Statutární orgán") holds members.
    print("Directors:")
    for organ in zaznam.get("statutarniOrgany", []):
        for clen in organ.get("clenoveOrganu", []):

            # datumVymazu present means this person already left the office
            if clen.get("datumVymazu"):
                continue

            osoba = clen.get("fyzickaOsoba", {})
            funkce = clen.get("clenstvi", {}).get("funkce", {}).get("nazev")

            jmeno_parts = [osoba.get("titulPredJmenem"), osoba.get("jmeno"), osoba.get("prijmeni")]
            jmeno = " ".join(part for part in jmeno_parts if part)

            print("  ", jmeno, f"| {funkce}", f"| since {clen.get('datumZapisu')}")

    # spolecnici[] -> spolecnik[], same two-level nesting as directors above
    print("Owners:")
    for organ in zaznam.get("spolecnici", []):
        for spolecnik in organ.get("spolecnik", []):

            if spolecnik.get("datumVymazu"):
                continue

            osoba = spolecnik.get("osoba", {}).get("fyzickaOsoba", {})

            jmeno_parts = [osoba.get("titulPredJmenem"), osoba.get("jmeno"), osoba.get("prijmeni")]
            jmeno = " ".join(part for part in jmeno_parts if part)

            print("  ", jmeno, f"| since {spolecnik.get('datumZapisu')}")
else:
    print(f"Error at the ARES API: ekonomicke-subjekty-vr - {response.status_code} - {response.text}")


url = f"https://ares.gov.cz/ekonomicke-subjekty-v-be/rest/ekonomicke-subjekty-rzp/{ico}"
response = requests.get(url, timeout=5)

if response.status_code == 200:
    data = response.json()
    zaznam = data["zaznamy"][0]

    # datumZaniku present means this trade licence is no longer active
    print("Trades:")
    for zivnost in zaznam.get("zivnosti", []):
        if zivnost.get("datumZaniku"):
            continue
        print("  ", zivnost.get("predmetPodnikani"))

    # ODPOVEDNY_ZASTUPCE_RZP lives inside each zivnost, not at the top level
    print("Responsible representatives:")
    for zivnost in zaznam.get("zivnosti", []):
        for zastupce in zivnost.get("odpovedniZastupci", []):
            if zastupce.get("platnostDo"):
                continue

            jmeno_parts = [zastupce.get("titulPredJmenem"), zastupce.get("jmeno"), zastupce.get("prijmeni")]
            jmeno = " ".join(part for part in jmeno_parts if part)

            print("  ", jmeno, f"| {zivnost.get('predmetPodnikani')}")

    print("Angazovane osoby:")
    for osoba in zaznam.get("angazovaneOsoby", []):
        print(
            "  ",
            osoba.get("jmeno"),
            osoba.get("prijmeni"),
            f"| {osoba.get('typAngazma')}",
        )

    # NOTE: there is no "provozovny" key with the actual establishments,
    # only "provozovnyStav" with counts (pocetCelkem/pocetAktivnich/...).
    # Checked live on several IČO - the list itself never came back.
    provozovny_stav = zaznam.get("provozovnyStav", {})
    print(f"Establishments: {provozovny_stav.get('pocetAktivnich', 0)} active of {provozovny_stav.get('pocetCelkem', 0)}")
else:
    print(f"Error at the ARES API: ekonomicke-subjekty-rzp - {response.status_code} - {response.text}")

