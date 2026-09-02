import math

import requests


# Mean Earth radius in kilometres. The great-circle distance it produces
# is a few tenths of a percent off the real driving distance, which is
# irrelevant here: the number answers "is this a day trip from Plzen or
# the other end of the country", not "how much fuel".
EARTH_RADIUS_KM = 6371.0


def distance_km(origin, point):
    """Great-circle distance between two {"lat", "lon"} points, or None.

    None when either point is missing a coordinate - and the caller has
    to keep that apart from a large distance. A company whose address
    RUIAN could not place is not far away; it is unplaced, and the two
    must not be collapsed (hypothesis E of the brief).
    """
    if not origin or not point:
        return None
    try:
        lat1, lon1 = float(origin["lat"]), float(origin["lon"])
        lat2, lon2 = float(point["lat"]), float(point["lon"])
    except (KeyError, TypeError, ValueError):
        return None

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2)
    return round(2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a)), 1)


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
