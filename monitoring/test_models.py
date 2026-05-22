# ─────────────────────────────────────────────────────────────────────────────
# 1. IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import json
import random
import requests
from datetime import timedelta

from django.test import TransactionTestCase
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from model_bakery import baker
from monitoring.models import Net, Infrastructuur, Sensor, Meetparameter, Meting, Operator, Rapport

# ─────────────────────────────────────────────────────────────────────────────
# 2. GLOBALE VARIABELEN
# ─────────────────────────────────────────────────────────────────────────────
AMOUNT_GENERATED_DATA = 20
AMOUNT_INFEED_DATA = 500

AANTAL_METINGEN_PER_SENSOR = 24
INTERVAL_MINUTEN = 15
MIN_INFEED_MW = -20.0
MAX_INFEED_MW = 50.0

# ─────────────────────────────────────────────────────────────────────────────
# 3. FUNCTIES OM DATA TE IMPORTEREN VIA API
# ─────────────────────────────────────────────────────────────────────────────

def frequentieAPI():
    url = "https://opendata.elia.be/api/records/1.0/search/"
    params = {"dataset": "ods057", "rows": AMOUNT_GENERATED_DATA, "sort": "datetime"}
    with requests.get(url, params=params) as response:
        if response.status_code == 200:
            data = json.loads(response.text)
        else:
            print("Er trad een fout op bij het ophalen van de frequentie-API.")
            return []

    return [
        {
            "tijdstip": record.get("fields", {}).get("datetime"),
            "waarde": record.get("fields", {}).get("actualfrequency"),
        }
        for record in data.get("records", [])
        if record.get("fields", {}).get("datetime") and record.get("fields", {}).get("actualfrequency") is not None
    ]


def totalLoadAPI():
    url = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods001/records"
    params = {"limit": AMOUNT_GENERATED_DATA}
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

    with requests.get(url, params=params, headers=headers, timeout=30) as response:
        if response.status_code == 200:
            data = json.loads(response.text)
        else:
            print("Er trad een fout op bij het ophalen van de total-load-API.")
            return []

    return [
        {"tijdstip": r.get("datetime"), "waarde": r.get("totalload")}
        for r in data.get("results", [])
        if r.get("datetime") and r.get("totalload") is not None
    ]


def infeedPerStationAPI():
    """
    Haalt sensor-metadata op uit ods091.
    Returnt een lijst dicts. Maakt GEEN objecten aan in de DB.
    """
    url = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods091/records"
    params = {"limit": AMOUNT_INFEED_DATA}

    try:
        response = requests.get(url, params=params, timeout=30)
        if response.status_code != 200:
            print("Fout bij ophalen API")
            return []
        data = response.json()
    except requests.exceptions.RequestException as e:
        print(f"Netwerkfout: {e}")
        return []

    resultaten = []
    for r in data.get("results", []):
        ean = r.get("eancode")
        if not ean:
            continue

        resultaten.append({
            "ean": ean,
            "region": r.get("region") or "",
            "location": r.get("location") or "",
            "station": r.get("injectionstation") or r.get("injection_station") or r.get("station") or "",
            "dso": r.get("dso") or "Onbekend",
            "voltagelevel": r.get("voltagelevel"),
        })
    return resultaten


# ─────────────────────────────────────────────────────────────────────────────
# 4. TESTKLASSE
# ─────────────────────────────────────────────────────────────────────────────

class GenereerData(TransactionTestCase):
    databases = ["default"]

    def test_genereer_data(self):

        # ── Bestaande data verwijderen (BELANGRIJK: sensoren behouden) ───────
        Meting.objects.all().delete()
        Operator.objects.all().delete()
        Rapport.objects.all().delete()
        print("Metingen/rapporten/operators verwijderd. Sensoren blijven behouden.")

        print("Data generatie gestart, even geduld...")

        # ── Vaste structuurobjecten: frequentie ───────────────────────────────
        net_freq = baker.make_recipe(
            "monitoring.net",
            net_id="ELIA_NET",
            type="Transmissienet",
            spanningsniveau=380.0,
        )
        infra_freq = baker.make_recipe(
            "monitoring.infra",
            infrastructuur_id="NATIONAAL",
            naam="Belgisch transmissienet",
            type="Transmissienet",
            locatie="België",
            status="actief",
            beheerder="Elia",
        )
        sensor_freq = baker.make_recipe(
            "monitoring.sensor",
            sensor_id="ELIA-NATIONAAL",
            type="Frequentie (landelijk)",
            net=net_freq,
            infrastructuur=infra_freq,
            communicatie_protocol="N.v.t.",
            status="actief",
        )
        param_freq, _ = Meetparameter.objects.get_or_create(
            naam="frequentie",
            defaults={"eenheid": "Hz", "drempel_onder": 49.5, "drempel_boven": 50.5},
        )

        # ── Vaste structuurobjecten: total load ───────────────────────────────
        net_load = baker.make_recipe(
            "monitoring.net",
            net_id="ELIA_LOAD_NET",
            type="Transmissienet",
            spanningsniveau=380.0,
        )
        infra_load = baker.make_recipe(
            "monitoring.infra",
            infrastructuur_id="NATIONAAL_LOAD",
            naam="Belgisch load monitoring systeem",
            type="Load monitoring",
            locatie="België",
            status="actief",
            beheerder="Elia",
        )
        sensor_load = baker.make_recipe(
            "monitoring.sensor",
            sensor_id="ELIA-LOAD",
            type="Netbelasting (nationaal)",
            net=net_load,
            infrastructuur=infra_load,
            communicatie_protocol="N.v.t.",
            status="actief",
        )
        param_load, _ = Meetparameter.objects.get_or_create(
            naam="totalload",
            defaults={"eenheid": "MW", "drempel_onder": 0.0, "drempel_boven": 15000.0},
        )

        # ── Meetparameter infeed ──────────────────────────────────────────────
        param_infeed, _ = Meetparameter.objects.get_or_create(
            naam="infeedvalue",
            defaults={"eenheid": "MW", "drempel_onder": -500.0, "drempel_boven": 500.0},
        )

        # ── Frequentie-metingen via API ───────────────────────────────────────
        freq_data = frequentieAPI()
        print(f"\n--- Frequentie-metingen ({len(freq_data)} records) ---")
        for item in freq_data:
            tijdstip = parse_datetime(item["tijdstip"])
            if tijdstip is None:
                continue
            baker.make_recipe(
                "monitoring.meting",
                sensor=sensor_freq,
                parameter=param_freq,
                tijdstip=tijdstip,
                waarde=float(item["waarde"]),
                kwaliteit="in_spec",
            )

        # ── Total-load-metingen via API ───────────────────────────────────────
        load_data = totalLoadAPI()
        print(f"\n--- Total load-metingen ({len(load_data)} records) ---")
        for item in load_data:
            tijdstip = parse_datetime(item["tijdstip"])
            if tijdstip is None:
                continue
            baker.make_recipe(
                "monitoring.meting",
                sensor=sensor_load,
                parameter=param_load,
                tijdstip=tijdstip,
                waarde=float(item["waarde"]),
                kwaliteit="in_spec" if float(item["waarde"]) < 15000 else "waarschuwing",
            )

        # ── Infeed: gebruik bestaande (API) sensoren + genereer meerdere waarden ──
        api_sensors = infeedPerStationAPI()
        print(f"\n--- Infeed stations metadata uit API ({len(api_sensors)} records) ---")

        start = timezone.now()

        for r in api_sensors:
            # Zorg dat de sensor in DB bestaat + metadata heeft (update_or_create)
            voltage = r.get("voltagelevel")
            try:
                voltage_float = float(str(voltage).replace("kV", "").replace("KV", "").strip()) if voltage else 0.0
            except ValueError:
                voltage_float = 0.0

            net_id = f"ELIA_{voltage_float}kV" if voltage_float else "ELIA_onbekend"
            net_infeed, _ = Net.objects.get_or_create(
                net_id=net_id,
                defaults={"type": "Distributienet", "spanningsniveau": voltage_float},
            )
            infra_infeed, _ = Infrastructuur.objects.get_or_create(
                infrastructuur_id=f"DSO_{r.get('dso')}",
                defaults={
                    "naam": r.get("dso"),
                    "type": "Distributiestation",
                    "locatie": r.get("region"),
                    "status": "actief",
                    "beheerder": r.get("dso"),
                },
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
                },
            )

            # Maak meerdere metingen per sensor (zelf gegenereerde waarden)
            tijden = [start - timedelta(minutes=INTERVAL_MINUTEN * i) for i in range(AANTAL_METINGEN_PER_SENSOR)]
            basis = random.uniform(MIN_INFEED_MW, MAX_INFEED_MW)
            waarden = [round(basis + random.uniform(-1.0, 1.0), 3) for _ in range(AANTAL_METINGEN_PER_SENSOR)]

            baker.make_recipe(
                "monitoring.meting",
                _quantity=AANTAL_METINGEN_PER_SENSOR,
                sensor=sensor_infeed,
                parameter=param_infeed,
                tijdstip=tijden,
                waarde=waarden,
                kwaliteit="in_spec",
            )

        # ── Opvuldata: operators, rapporten ──────────────────────
        print("\n--- Overige objecten via baker_recipes ---")
        for i in range(AMOUNT_GENERATED_DATA):
            operator = baker.make_recipe("monitoring.operator")
            baker.make_recipe("monitoring.rapport", operator=operator)
            _ = random.choice(Meting.objects.all())
            print(f"  Operator/Rapport {i + 1}/{AMOUNT_GENERATED_DATA} aangemaakt.")