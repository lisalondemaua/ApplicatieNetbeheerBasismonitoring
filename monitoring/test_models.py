# ─────────────────────────────────────────────────────────────────────────────
# 1. IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import json
import random
import requests

from django.test import TransactionTestCase
from django.utils.dateparse import parse_datetime
from model_bakery import baker
from monitoring.models import Net, Infrastructuur, Sensor, Meetparameter, Meting, Operator, Rapport

# ─────────────────────────────────────────────────────────────────────────────
# 2. GLOBALE VARIABELEN
# ─────────────────────────────────────────────────────────────────────────────
AMOUNT_GENERATED_DATA = 20
AMOUNT_INFEED_DATA = 500


# ─────────────────────────────────────────────────────────────────────────────
# 3. FUNCTIES OM DATA TE IMPORTEREN VIA API
# ─────────────────────────────────────────────────────────────────────────────

def frequentieAPI():
    url = "https://opendata.elia.be/api/records/1.0/search/"
    params = {
        "dataset": "ods057",
        "rows": AMOUNT_GENERATED_DATA,
        "sort": "datetime"
    }
    with requests.get(url, params=params) as response:
        if response.status_code == 200:
            data = json.loads(response.text)
        else:
            print("Er trad een fout op bij het ophalen van de frequentie-API.")
            return []

    return [
        {
            "tijdstip": record.get("fields", {}).get("datetime"),
            "waarde":   record.get("fields", {}).get("actualfrequency"),
        }
        for record in data.get("records", [])
        if record.get("fields", {}).get("datetime") and record.get("fields", {}).get("actualfrequency")
    ]


from django.utils import timezone

def infeedPerStationAPI():
    url = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods091/records"

    params = {
        "limit": AMOUNT_INFEED_DATA
    }

    try:
        response = requests.get(url, params=params, timeout=30)

        if response.status_code != 200:
            print("Fout bij ophalen API")
            return

        data = response.json()

    except requests.exceptions.RequestException as e:
        print(f"Netwerkfout: {e}")
        return

    now = timezone.now()

    for r in data.get("results", []):

        ean = r.get("eancode")

        if not ean:
            continue

        sensor, created = Sensor.objects.get_or_create(
            sensor_id=ean,
            defaults={
                "type": "Injectiestation",
                "status": "actief"
            }
        )

        Meting.objects.create(
            sensor=sensor,
            tijdstip=now,
            infeed_value=r.get("infeedvalue") or 0
        )

# ─────────────────────────────────────────────────────────────────────────────
# 4. TESTKLASSE
# ─────────────────────────────────────────────────────────────────────────────

class GenereerData(TransactionTestCase):

    # Django gebruikt standaard een aparte lege testdatabank.
    # Met databases instellen we dat de echte databank gebruikt wordt.
    databases = ['default']

    def test_genereer_data(self):

        # ── Bestaande data verwijderen ────────────────────────────────────────
        for model in [Meting, Sensor, Meetparameter, Infrastructuur, Net, Operator, Rapport]:
            model.objects.all().delete()
            print(f"Alle objecten van {model.__name__} verwijderd.")

        print("Data generatie gestart, even geduld...")

        # ── Vaste structuurobjecten: frequentie ───────────────────────────────
        net_freq = baker.make_recipe(
            'monitoring.net',
            net_id="ELIA_NET",
            type="Transmissienet",
            spanningsniveau=380.0,
        )
        infra_freq = baker.make_recipe(
            'monitoring.infra',
            infrastructuur_id="NATIONAAL",
            naam="Belgisch transmissienet",
            type="Transmissienet",
            locatie="België",
            status="actief",
            beheerder="Elia",
        )
        sensor_freq = baker.make_recipe(
            'monitoring.sensor',
            sensor_id="ELIA-NATIONAAL",
            type="Frequentie (landelijk)",
            net=net_freq,
            infrastructuur=infra_freq,
            communicatie_protocol="N.v.t.",
            status="actief",
        )
        param_freq = baker.make_recipe(
            'monitoring.parameter_freq',
            naam="frequentie",
            eenheid="Hz",
            drempel_onder=49.5,
            drempel_boven=50.5,
        )

        # ── Meetparameter infeed ──────────────────────────────────────────────
        param_infeed, _ = Meetparameter.objects.get_or_create(
            naam="infeedvalue",
            defaults={
                "eenheid": "MW",
                "drempel_onder": -500.0,
                "drempel_boven": 500.0,
            }
        )

        # ── Frequentie-metingen via API ───────────────────────────────────────
        freq_data = frequentieAPI()
        print(f"\n--- Frequentie-metingen ({len(freq_data)} records) ---")
        for item in freq_data:
            tijdstip = parse_datetime(item["tijdstip"])
            if tijdstip is None:
                continue
            baker.make_recipe(
                'monitoring.meting',
                sensor=sensor_freq,
                parameter=param_freq,
                tijdstip=tijdstip,
                waarde=item["waarde"],
                kwaliteit="in_spec",
            )
            print(f"  {tijdstip} → {item['waarde']} Hz")

        # ── Infeed per station via API ────────────────────────────────────────
        infeed_data = infeedPerStationAPI()

        if infeed_data is None:
            print("\n--- Infeed per station (0 records) ---")
            print("Infeed API ophalen faalde: infeedPerStationAPI() gaf None terug.")
            infeed_data = []

        print(f"\n--- Infeed per station ({len(infeed_data)} records) ---")
        for r in infeed_data:
            tijdstip = parse_datetime(r["tijdstip"]) if r.get("tijdstip") else None
            if tijdstip is None or r.get("waarde") is None:
                print(f"  EAN {r.get('ean')}: overgeslagen (geen tijdstip of waarde).")
                continue

            net_id = f"ELIA_{r.get('voltage')}kV" if r.get("voltage") else "ELIA_onbekend"
            net_infeed, _ = Net.objects.get_or_create(
                net_id=net_id,
                defaults={
                    "type": "Distributienet",
                    "spanningsniveau": float(r["voltage"]) if r.get("voltage") else 0.0,
                }
            )
            infra_infeed, _ = Infrastructuur.objects.get_or_create(
                infrastructuur_id=f"DSO_{r.get('dso')}",
                defaults={
                    "naam": r.get("dso"),
                    "type": "Distributiestation",
                    "locatie": r.get("region"),
                    "status": "actief",
                    "beheerder": r.get("dso"),
                }
            )

            sensor_infeed, _ = Sensor.objects.update_or_create(
                sensor_id=r.get("ean"),
                defaults={
                    "type": "Infeed-sensor",
                    "net": net_infeed,
                    "infrastructuur": infra_infeed,
                    "communicatie_protocol": "N.v.t.",
                    "status": "actief",
                    "station": r.get("station"),
                    "location": r.get("location"),
                    "region": r.get("region"),
                }
            )
            baker.make_recipe(
                "monitoring.meting",
                sensor=sensor_infeed,
                parameter=param_infeed,
                tijdstip=tijdstip,
                waarde=r["waarde"],
                kwaliteit="teruglevering" if r["waarde"] < 0 else "in_spec",
            )
            print(f"  EAN {r.get('ean')} → {r['waarde']} MW")

        # ── Opvuldata: operators, rapporten ──────────────────────
        print("\n--- Overige objecten via baker_recipes ---")
        for i in range(AMOUNT_GENERATED_DATA):
            operator = baker.make_recipe('monitoring.operator')
            baker.make_recipe('monitoring.rapport', operator=operator)
            meting = random.choice(Meting.objects.all())
            print(f"  Operator/Rapport {i + 1}/{AMOUNT_GENERATED_DATA} aangemaakt.")
