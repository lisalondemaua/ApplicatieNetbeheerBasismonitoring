import math
import random
from datetime import timedelta

import requests
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from monitoring.models import Infrastructuur, Meetparameter, Meting, Net, Operator, Rapport, Sensor

AMOUNT_GENERATED_DATA = 20
AMOUNT_INFEED_DATA = 500
AANTAL_METINGEN_PER_SENSOR = 7 * 24
INTERVAL_MINUTEN = 60
MAX_INFEED_STATIONS = 30


def generate_realistic_infeed(voltage_kv, hour):
    daily_factor = 0.6 + 0.4 * math.sin((hour - 6) / 24 * 2 * math.pi)
    if voltage_kv <= 1:
        base = random.uniform(-0.2, 0.5)
    elif voltage_kv <= 15:
        base = random.uniform(-20, 10) if 11 <= hour <= 15 else random.uniform(-5, 15)
    elif voltage_kv <= 70:
        base = random.uniform(20, 100)
    elif voltage_kv <= 150:
        base = random.uniform(100, 500)
    else:
        base = random.uniform(200, 1200)
    noise = random.uniform(-0.1, 0.1) * abs(base)
    return round((base * daily_factor) + noise, 3)


def _aware_datetime(value):
    dt = parse_datetime(value) if isinstance(value, str) else value
    if not dt:
        return None
    if timezone.is_naive(dt):
        return timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def frequentie_api():
    try:
        response = requests.get(
            "https://opendata.elia.be/api/records/1.0/search/",
            params={"dataset": "ods057", "rows": AMOUNT_GENERATED_DATA, "sort": "datetime"},
            timeout=30,
        )
        if response.status_code != 200:
            return []
        payload = response.json()
    except requests.RequestException:
        return []
    return [
        {
            "tijdstip": record.get("fields", {}).get("datetime"),
            "waarde": record.get("fields", {}).get("actualfrequency"),
        }
        for record in payload.get("records", [])
    ]


def total_load_api():
    try:
        response = requests.get(
            "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods001/records",
            params={"limit": AMOUNT_GENERATED_DATA},
            timeout=30,
        )
        if response.status_code != 200:
            return []
        payload = response.json()
    except requests.RequestException:
        return []
    return [
        {"tijdstip": row.get("datetime"), "waarde": row.get("totalload")}
        for row in payload.get("results", [])
    ]


def infeed_per_station_api():
    try:
        response = requests.get(
            "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods091/records",
            params={"limit": AMOUNT_INFEED_DATA},
            timeout=30,
        )
        if response.status_code != 200:
            return []
        payload = response.json()
    except requests.RequestException:
        return []

    resultaten = []
    for r in payload.get("results", []):
        ean = r.get("eancode")
        if not ean:
            continue
        resultaten.append(
            {
                "ean": ean,
                "region": r.get("region") or "",
                "location": r.get("location") or "",
                "station": r.get("injectionstation") or r.get("injection_station") or r.get("station") or "",
                "dso": r.get("dso") or "Onbekend",
                "voltagelevel": r.get("voltagelevel"),
            }
        )
    return resultaten


def _synthetic_freq_rows(now):
    return [
        {"tijdstip": now - timedelta(minutes=15 * i), "waarde": 50.0 + random.uniform(-0.08, 0.08)}
        for i in range(AMOUNT_GENERATED_DATA)
    ]


def _synthetic_load_rows(now):
    return [
        {"tijdstip": now - timedelta(minutes=15 * i), "waarde": 8500 + random.uniform(-300, 300)}
        for i in range(AMOUNT_GENERATED_DATA)
    ]


def _synthetic_infeed_sensors():
    voltages = [0.4, 11, 15, 30, 70, 110, 150]
    rows = []
    for i in range(1, 11):
        v = voltages[(i - 1) % len(voltages)]
        rows.append(
            {
                "ean": f"EAN-DEMO-{i:03d}",
                "region": f"Regio {(i % 3) + 1}",
                "location": f"Locatie {i}",
                "station": f"Station {i}",
                "dso": f"DSO-{(i % 4) + 1}",
                "voltagelevel": v,
            }
        )
    return rows


def generate_demo_data(use_api=False, stdout=print):
    Meting.objects.all().delete()
    Operator.objects.all().delete()
    Rapport.objects.all().delete()
    stdout("Metingen/rapporten/operators verwijderd.")
    stdout("Sensoren blijven behouden.")

    now = timezone.now()
    net_freq, _ = Net.objects.get_or_create(
        net_id="ELIA_NET",
        defaults={"type": "Transmissienet", "spanningsniveau": 380.0},
    )
    infra_freq, _ = Infrastructuur.objects.get_or_create(
        infrastructuur_id="NATIONAAL",
        defaults={
            "naam": "Belgisch transmissienet",
            "type": "Transmissienet",
            "locatie": "België",
            "status": "actief",
            "beheerder": "Elia",
        },
    )
    sensor_freq, _ = Sensor.objects.update_or_create(
        sensor_id="ELIA-NATIONAAL",
        defaults={
            "type": "Frequentie (landelijk)",
            "net": net_freq,
            "infrastructuur": infra_freq,
            "communicatie_protocol": "N.v.t.",
            "status": "actief",
        },
    )
    param_freq, _ = Meetparameter.objects.get_or_create(
        naam="frequentie",
        defaults={"eenheid": "Hz", "drempel_onder": 49.5, "drempel_boven": 50.5},
    )

    net_load, _ = Net.objects.get_or_create(
        net_id="ELIA_LOAD_NET",
        defaults={"type": "Transmissienet", "spanningsniveau": 380.0},
    )
    infra_load, _ = Infrastructuur.objects.get_or_create(
        infrastructuur_id="NATIONAAL_LOAD",
        defaults={
            "naam": "Belgisch load monitoring systeem",
            "type": "Load monitoring",
            "locatie": "België",
            "status": "actief",
            "beheerder": "Elia",
        },
    )
    sensor_load, _ = Sensor.objects.update_or_create(
        sensor_id="ELIA-LOAD",
        defaults={
            "type": "Netbelasting (nationaal)",
            "net": net_load,
            "infrastructuur": infra_load,
            "communicatie_protocol": "N.v.t.",
            "status": "actief",
        },
    )
    param_load, _ = Meetparameter.objects.get_or_create(
        naam="totalload",
        defaults={"eenheid": "MW", "drempel_onder": 0.0, "drempel_boven": 15000.0},
    )
    param_infeed, _ = Meetparameter.objects.get_or_create(
        naam="infeedvalue",
        defaults={"eenheid": "MW", "drempel_onder": -5000.0, "drempel_boven": 10000.0},
    )

    freq_rows = frequentie_api() if use_api else _synthetic_freq_rows(now)
    if not freq_rows:
        freq_rows = _synthetic_freq_rows(now)
    for row in freq_rows:
        tijdstip = _aware_datetime(row.get("tijdstip"))
        waarde = row.get("waarde")
        if tijdstip is None or waarde is None:
            continue
        Meting.objects.update_or_create(
            sensor=sensor_freq,
            parameter=param_freq,
            tijdstip=tijdstip,
            defaults={"waarde": float(waarde), "kwaliteit": "in_spec"},
        )

    load_rows = total_load_api() if use_api else _synthetic_load_rows(now)
    if not load_rows:
        load_rows = _synthetic_load_rows(now)
    for row in load_rows:
        tijdstip = _aware_datetime(row.get("tijdstip"))
        waarde = row.get("waarde")
        if tijdstip is None or waarde is None:
            continue
        waarde = float(waarde)
        Meting.objects.update_or_create(
            sensor=sensor_load,
            parameter=param_load,
            tijdstip=tijdstip,
            defaults={"waarde": waarde, "kwaliteit": "in_spec" if waarde < 15000 else "waarschuwing"},
        )

    api_sensors = infeed_per_station_api() if use_api else _synthetic_infeed_sensors()
    if not api_sensors:
        api_sensors = _synthetic_infeed_sensors()
    api_sensors = api_sensors[:MAX_INFEED_STATIONS]

    infeed_created = 0
    for r in api_sensors:
        try:
            voltage = float(str(r.get("voltagelevel", "")).replace(",", ".").replace("kV", "").replace("KV", "").strip())
        except ValueError:
            voltage = 0.0

        net_id = f"ELIA_{voltage}kV" if voltage else "ELIA_onbekend"
        net_infeed, _ = Net.objects.get_or_create(
            net_id=net_id,
            defaults={"type": "Transmissienet" if voltage >= 110 else "Distributienet", "spanningsniveau": voltage},
        )
        infra_infeed, _ = Infrastructuur.objects.get_or_create(
            infrastructuur_id=f"DSO_{r.get('dso')}",
            defaults={
                "naam": r.get("dso") or "Onbekend",
                "type": "Distributiestation",
                "locatie": r.get("region") or "",
                "status": "actief",
                "beheerder": r.get("dso") or "Onbekend",
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
                "station": r.get("station") or "",
                "location": r.get("location") or "",
                "region": r.get("region") or "",
            },
        )

        for i in range(AANTAL_METINGEN_PER_SENSOR):
            timestamp = now - timedelta(minutes=INTERVAL_MINUTEN * i)
            waarde = generate_realistic_infeed(voltage, timestamp.hour)
            kwaliteit = "kritiek" if abs(waarde) > 5000 else "waarschuwing" if abs(waarde) > 1000 else "in_spec"
            Meting.objects.update_or_create(
                sensor=sensor_infeed,
                parameter=param_infeed,
                tijdstip=timestamp,
                defaults={"waarde": waarde, "kwaliteit": kwaliteit},
            )
            infeed_created += 1

    for i in range(AMOUNT_GENERATED_DATA):
        operator, _ = Operator.objects.update_or_create(
            medewerker_id=f"OP-{i + 1:03d}",
            defaults={
                "naam": f"Operator {i + 1}",
                "rol": "monitor",
                "emailadres": f"operator{i + 1}@example.com",
                "actief": True,
            },
        )
        rapport_id = f"RAPP-{timezone.localtime(now).strftime('%Y%m%d')}-{i + 1:03d}"
        Rapport.objects.update_or_create(
            rapport_id=rapport_id,
            defaults={
                "titel": f"Demo rapport {i + 1}",
                "periode_start": now - timedelta(days=7),
                "periode_einde": now,
                "inhoud": "Datakwaliteit: BETROUWBAAR",
                "operator": operator,
            },
        )

    summary = {
        "freq_metingen": Meting.objects.filter(parameter=param_freq).count(),
        "load_metingen": Meting.objects.filter(parameter=param_load).count(),
        "infeed_metingen": Meting.objects.filter(parameter=param_infeed).count(),
        "sensoren_totaal": Sensor.objects.count(),
        "infeed_metingen_aangeraakt": infeed_created,
    }
    return summary
