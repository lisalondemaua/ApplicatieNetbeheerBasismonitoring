import requests
import json
from pprint import pprint

amount_imported_data = 100

# =================DOE API CALL==================
response = requests.get(
    "https://opendata.elia.be/api/records/1.0/search/",
    params={
        "dataset": "ods057",  # dataset-id voor Elia frequentiedata
        "rows": amount_imported_data,
        "sort": "datetime"
    }
)

# =================TOON RESPONSE==================
if response.status_code == 200:
    data = response.json()  # json object
    # Print mooi (zoals in slides)
    pprint(data, sort_dicts=False, indent=2)
else:
    print('An error occurred while attempting to retrieve data from the API.')

# =================RESPONSE OPSLAAN==================
# Als de call gelukt is, maak een lijst van dicts (mapping):
if response.status_code == 200:
    records = data['records']
    metingen = [
        {
            'tijdstip': r['fields']['datetime'],
            'waarde': r['fields']['actualfrequency'],
            'fcrdemand': r['fields'].get('fcrdemand'),
            'fcrrequested': r['fields'].get('fcrrequested'),
            'resolutioncode': r['fields'].get('resolutioncode')
        }
        for r in records
    ]

    # Toon resultaat netjes
    print("\nResultaat (mapping van response):")
    pprint(metingen, sort_dicts=False, indent=2)
else:
    print("Kon geen mapping doen omdat de API-call faalde.")
