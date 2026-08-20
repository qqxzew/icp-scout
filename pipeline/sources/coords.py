import requests


def get_coordinates(kod_adresniho_mista):
    url = "https://ags.cuzk.gov.cz/arcgis/rest/services/RUIAN/MapServer/1/query"

    params = {
        "where": f"kod={kod_adresniho_mista}",
        "outFields": "kod,adresa",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "json"
    }

    try:
        response = requests.get(url, params=params, timeout=10)

        if response.status_code != 200:
            print(
                f"Error at the RUIAN API: "
                f"{response.status_code} - {response.text}"
            )
            return None

        data = response.json()

        if not data.get("features"):
            print(f"RUIAN: coordinates not found for code {kod_adresniho_mista}")
            return None

        feature = data["features"][0]

        return {
            "lat": feature["geometry"]["y"],
            "lon": feature["geometry"]["x"]
        }

    except requests.RequestException as error:
        print(f"Connection error at the RUIAN API: {error}")
        return None
