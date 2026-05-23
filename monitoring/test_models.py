# ─────────────────────────────────────────────────────────────────────────────
# 1. IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import json
import math
import random
import requests

from datetime import timedelta

from django.test import TransactionTestCase
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from model_bakery import baker

from monitoring.models import (
    Net,
    Infrastructuur,
    Sensor,
    Meetparameter,
    Meting,
    Operator,
    Rapport,
)

# ─────────────────────────────────────────────────────────────────────────────
# 2. GLOBALE VARIABELEN
# ─────────────────────────────────────────────────────────────────────────────
AMOUNT_GENERATED_DATA = 20
AMOUNT_INFEED_DATA = 500

# Kies uurlijkse data voor 7 dagen (minder punten, sneller, toch mooie grafiek)
AANTAL_METINGEN_PER_SENSOR = 7 * 24   # 168 metingen per sensor
INTERVAL_MINUTEN = 60                 # 1 meting per uur

# Beperk aantal infeed-stations zodat tests/DB niet te zwaar worden
MAX_INFEED_STATIONS = 30

# ─────────────────────────────────────────────────────────────────────────────
# 3. REALISTISCHE INFEED GENERATIE
# ─────────────────────────────────────────────────────────────────────────────

def generate_realistic_infeed(voltage_kv, hour):
    """
    Genereert realistische MW-infeed op basis van:
    - spanningsniveau
    - tijdstip van de dag
    - willekeurige variatie

    Conventie (zoals je UI eerder gebruikte):
    - negatief = teruglevering/injectie
    - positief = afname/consumptie
    """

    # Dagcurve (nacht lager, overdag hoger) — blijft altijd positief (0.2..1.0)
    daily_factor = 0.6 + 0.4 * math.sin((hour - 6) / 24 * 2 * math.pi)

    # ── Laagspanning ────────────────────────────────────────────────────────
    if voltage_kv <= 1:
        base = random.uniform(-0.2, 0.5)

    # ── Middenspanning (6-15 kV) ────────────────────────────────────────────
    elif voltage_kv <= 15:
        # Middag = mogelijk sterke PV-injectie (negatief)
        if 11 <= hour <= 15:
            base = random.uniform(-20, 10)
        else:
            base = random.uniform(-5, 15)

    # ── Regionale hoogspanning (30-70 kV) ───────────────────────────────────
    elif voltage_kv <= 70:
        base = random.uniform(20, 100)

    # ── 110-150 kV ──────────────────────────────────────────────────────────
    elif voltage_kv <= 150:
        base = random.uniform(100, 500)

    # ── 220-380 kV transmissie ──────────────────────────────────────────────
    else:
        # Iets gematigder dan 3000 MW om realistischer te blijven per "node"
        base = random.uniform(200, 1200)

    # Willekeurige ruis (10% van de grootte)
    noise = random.uniform(-0.1, 0.1) * abs(base)

    value = (base * daily_factor) + noise
    return round(value, 3)


# ─────────────────────────────────────────────────────────────────────────────
# 4. FUNCTIES OM DATA TE IMPORTEREN VIA API
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
            "waarde": record.get("fields", {}).get("actualfrequency"),
        }
        for record in data.get("records", [])
        if record.get("fields", {}).get("datetime")
        and record.get("fields", {}).get("actualfrequency") is not None
    ]


def totalLoadAPI():

    url = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods001/records"

    params = {
        "limit": AMOUNT_GENERATED_DATA
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json"
    }

    with requests.get(
        url,
        params=params,
        headers=headers,
        timeout=30
    ) as response:

        if response.status_code == 200:
            data = json.loads(response.text)
        else:
            print("Er trad een fout op bij het ophalen van de total-load-API.")
            return []

    return [
        {
            "tijdstip": r.get("datetime"),
            "waarde": r.get("totalload")
        }
        for r in data.get("results", [])
        if r.get("datetime") and r.get("totalload") is not None
    ]


def infeedPerStationAPI():
    """
    Haalt sensor-metadata op uit ods091.
    Returnt een lijst dicts.
    """

    url = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods091/records"

    params = {
        "limit": AMOUNT_INFEED_DATA
    }

    try:

        response = requests.get(
            url,
            params=params,
            timeout=30
        )

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
            "station": (
                r.get("injectionstation")
                or r.get("injection_station")
                or r.get("station")
                or ""
            ),
            "dso": r.get("dso") or "Onbekend",
            "voltagelevel": r.get("voltagelevel"),
        })

    return resultaten


# ─────────────────────────────────────────────────────────────────────────────
# 5. TESTKLASSE
# ─────────────────────────────────────────────────────────────────────────────

class GenereerData(TransactionTestCase):

    databases = ["default"]

    def test_genereer_data(self):

        # ── Bestaande data verwijderen ───────────────────────────────────────
        Meting.objects.all().delete()
        Operator.objects.all().delete()
        Rapport.objects.all().delete()

        print("Metingen/rapporten/operators verwijderd.")
        print("Sensoren blijven behouden.")

        print("\nData generatie gestart...\n")

        # ─────────────────────────────────────────────────────────────────────
        # FREQUENTIE SENSOR
        # ─────────────────────────────────────────────────────────────────────

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
            defaults={
                "eenheid": "Hz",
                "drempel_onder": 49.5,
                "drempel_boven": 50.5,
            },
        )

        # ─────────────────────────────────────────────────────────────────────
        # TOTAL LOAD SENSOR
        # ─────────────────────────────────────────────────────────────────────

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
            defaults={
                "eenheid": "MW",
                "drempel_onder": 0.0,
                "drempel_boven": 15000.0,
            },
        )

        # ─────────────────────────────────────────────────────────────────────
        # INFEED PARAMETER
        # ─────────────────────────────────────────────────────────────────────

        param_infeed, _ = Meetparameter.objects.get_or_create(
            naam="infeedvalue",
            defaults={
                "eenheid": "MW",
                "drempel_onder": -5000.0,
                "drempel_boven": 10000.0,
            },
        )

        # ─────────────────────────────────────────────────────────────────────
        # FREQUENTIE DATA
        # ─────────────────────────────────────────────────────────────────────

        freq_data = frequentieAPI()

        print(f"Frequentie records: {len(freq_data)}")

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

        # ─────────────────────────────────────────────────────────────────────
        # TOTAL LOAD DATA
        # ─────────────────────────────────────────────────────────────────────

        load_data = totalLoadAPI()

        print(f"Total load records: {len(load_data)}")

        for item in load_data:

            tijdstip = parse_datetime(item["tijdstip"])

            if tijdstip is None:
                continue

            waarde = float(item["waarde"])

            baker.make_recipe(
                "monitoring.meting",
                sensor=sensor_load,
                parameter=param_load,
                tijdstip=tijdstip,
                waarde=waarde,
                kwaliteit=(
                    "in_spec"
                    if waarde < 15000
                    else "waarschuwing"
                ),
            )

        # ─────────────────────────────────────────────────────────────────────
        # INFEED DATA
        # ─────────────────────────────────────────────────────────────────────

        api_sensors = infeedPerStationAPI()
        api_sensors = api_sensors[:MAX_INFEED_STATIONS]

        print(f"Infeed stations gebruikt: {len(api_sensors)} (max {MAX_INFEED_STATIONS})")

        start = timezone.now()

        for r in api_sensors:

            voltage = r.get("voltagelevel")

            try:
                voltage_str = str(voltage).replace(",", ".") if voltage else ""
                voltage_float = float(
                    voltage_str
                    .replace("kV", "")
                    .replace("KV", "")
                    .strip()
                ) if voltage_str else 0.0

            except ValueError:
                voltage_float = 0.0

            # ── NET ──────────────────────────────────────────────────────────

            net_id = (
                f"ELIA_{voltage_float}kV"
                if voltage_float
                else "ELIA_onbekend"
            )

            net_infeed, _ = Net.objects.get_or_create(
                net_id=net_id,
                defaults={
                    "type": (
                        "Transmissienet"
                        if voltage_float >= 110
                        else "Distributienet"
                    ),
                    "spanningsniveau": voltage_float,
                },
            )

            # ── INFRASTRUCTUUR ───────────────────────────────────────────────

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

            # ── SENSOR ───────────────────────────────────────────────────────

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

            # ─────────────────────────────────────────────────────────────────
            # REALISTISCHE METINGEN GENEREREN (uurlijkse metingen over 7 dagen)
            # ─────────────────────────────────────────────────────────────────

            tijden = []
            waarden = []
            kwaliteiten = []

            for i in range(AANTAL_METINGEN_PER_SENSOR):

                timestamp = (
                    start
                    - timedelta(minutes=INTERVAL_MINUTEN * i)
                )

                waarde = generate_realistic_infeed(
                    voltage_float,
                    timestamp.hour
                )

                tijden.append(timestamp)
                waarden.append(waarde)

                # kwaliteitslabel
                if abs(waarde) > 5000:
                    kwaliteiten.append("kritiek")
                elif abs(waarde) > 1000:
                    kwaliteiten.append("waarschuwing")
                else:
                    kwaliteiten.append("in_spec")

            baker.make_recipe(
                "monitoring.meting",
                _quantity=AANTAL_METINGEN_PER_SENSOR,
                sensor=sensor_infeed,
                parameter=param_infeed,
                tijdstip=tijden,
                waarde=waarden,
                kwaliteit=kwaliteiten,
            )

            print(
                f"Sensor {sensor_infeed.sensor_id} "
                f"({voltage_float} kV) "
                f"=> {len(waarden)} metingen"
            )

        # ─────────────────────────────────────────────────────────────────────
        # OVERIGE DATA
        # ─────────────────────────────────────────────────────────────────────

        print("\nOperators en rapporten genereren...\n")

        for i in range(AMOUNT_GENERATED_DATA):

            operator = baker.make_recipe(
                "monitoring.operator"
            )

            baker.make_recipe(
                "monitoring.rapport",
                operator=operator
            )

            print(
                f"Operator/Rapport "
                f"{i + 1}/{AMOUNT_GENERATED_DATA}"
            )

        # ─────────────────────────────────────────────────────────────────────

        print("\nDatageneratie voltooid.\n")