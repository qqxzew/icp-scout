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
else:
    print(f"Error at the ARES API: ekonomicke-subjekty-res - {response.status_code} - {response.text}")

