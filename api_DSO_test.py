import requests
from pprint import pprint

amount_imported_data = 100  # maximaal 360 volgens total_count

# =================DOE API CALL==================
response = requests.get(
    "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods091/records",
    params={
        "limit": amount_imported_data
        # Voeg hier extra filters toe als gewenst
    }
)

# =================TOON RESPONSE==================
if response.status_code == 200:
    data = response.json()
    pprint(data, sort_dicts=False, indent=2)
else:
    print('An error occurred while attempting to retrieve data from the API.')

# =================RESPONSE OPSLAAN==================
if response.status_code == 200:
    records = data['results']  # Let op: dit is 'results', niet 'records'!
    metingen = [
        {
            'eancode': r['eancode'],
            'region': r['region'],
            'location': r['location'],
            'station': r['injectionstation'],
            'dso': r['dso'],
            'voltagelevel': r['voltagelevel'],
            'infeedvalue': r['infeedvalue']
        }
        for r in records
    ]

    # Toon resultaat netjes
    print("\nResultaat (mapping van response):")
    pprint(metingen, sort_dicts=False, indent=2)
else:
    print("Kon geen mapping doen omdat de API-call faalde.")