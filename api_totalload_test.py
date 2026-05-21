import requests
from pprint import pprint

amount_imported_data = 20  # start klein voor testing

# ================= DOE API CALL ==================
response = requests.get(
    "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods001/records",
    params={
        "limit": amount_imported_data
    }
)

# ================= TOON RESPONSE ==================
if response.status_code == 200:
    data = response.json()
    pprint(data, sort_dicts=False, indent=2)
else:
    print("An error occurred while attempting to retrieve data from the API.")

# ================= RESPONSE OPSLAAN ==================
if response.status_code == 200:
    records = data.get("results", [])

    metingen = [
        {
            "datetime": r.get("datetime"),
            "totalload": r.get("totalload"),
            "mostrecentforecast": r.get("mostrecentforecast"),
            "dayaheadforecast": r.get("dayaheadforecast"),
            "weekaheadforecast": r.get("weekaheadforecast"),
            "confidence10": r.get("mostrecentconfidence10"),
            "confidence90": r.get("mostrecentconfidence90"),
        }
        for r in records
    ]

    # ================= NETJES RESULTAAT ==================
    print("\nResultaat (mapping van ods001 load data):")
    pprint(metingen, sort_dicts=False, indent=2)

else:
    print("Kon geen mapping doen omdat de API-call faalde.")