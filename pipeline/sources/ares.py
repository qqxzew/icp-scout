import requests
import coords


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
    print(f"Error at the ARES API: {response.status_code} - {response.text}")



URL = "https://ares.gov.cz/ekonomicke-subjekty-v-be/rest"

def fetch(endpoint, ico):
    url = f"{URL}/{endpoint}/{ico}"
    response = requests.get(url, timeout=5)

    if response.status_code == 200:
        return response.json()
    else:
        print(f"Error at the ARES API: {endpoint} - {ico} - {response.status_code}")
        return None