import requests
import json
import math
import random
from collections import defaultdict
from datetime import timedelta

import pandas as pd
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import HoverTool, Band, ColumnDataSource, DatetimeTickFormatter, Range1d
from bokeh.transform import jitter

from django.views import generic
from django.shortcuts import redirect
from django.contrib import messages
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from .models import Meting, Sensor, Rapport, Net, Meetparameter, Infrastructuur


# ─────────────────────────────────────────────────────────────────────────────
# DEMO / SIMULATIE HELPERS (kwartierdata + voltage-afhankelijke MW)
# ─────────────────────────────────────────────────────────────────────────────

KWARTIER_MINUTEN = 15
SEED_DAGEN = 7


def floor_to_quarter(dt):
    dt = dt.replace(second=0, microsecond=0)
    minute = (dt.minute // KWARTIER_MINUTEN) * KWARTIER_MINUTEN
    return dt.replace(minute=minute)


def generate_realistic_infeed(voltage_kv, hour):
    """
    Realistische MW-waarde gekoppeld aan spanningsniveau + uur van de dag.

    Conventie (zoals je UI eerder gebruikte):
      - waarde < 0  => teruglevering/injectie
      - waarde >= 0 => afname/consumptie
    """
    daily_factor = 0.6 + 0.4 * math.sin((hour - 6) / 24 * 2 * math.pi)  # 0.2..1.0

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


def get_voltage_kv(sensor):
    if sensor.net and sensor.net.spanningsniveau is not None:
        try:
            return float(sensor.net.spanningsniveau)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def seed_last_7_days_quarterly_for_all_infeed(parameter_infeed):
    """
    Seed 7 dagen kwartiermetingen voor ALLE actieve Infeed-sensoren.
    Wordt bedoeld om 1x te draaien na flush (of lege DB).
    """
    now = timezone.now()
    end_slot = floor_to_quarter(now)
    start_slot = end_slot - timedelta(days=SEED_DAGEN)

    # kwartier slots
    slots = []
    ts = start_slot
    while ts <= end_slot:
        slots.append(ts)
        ts += timedelta(minutes=KWARTIER_MINUTEN)

    sensors = Sensor.objects.filter(type="Infeed-sensor", status="actief").select_related("net")
    total = 0

    for s in sensors:
        voltage_kv = get_voltage_kv(s)
        metingen = [
            Meting(
                sensor=s,
                parameter=parameter_infeed,
                tijdstip=ts,
                waarde=generate_realistic_infeed(voltage_kv, ts.hour),
                kwaliteit="in_spec",
            )
            for ts in slots
        ]
        Meting.objects.bulk_create(metingen, batch_size=2000)
        total += len(metingen)

    return {
        "sensors": sensors.count(),
        "slots_per_sensor": len(slots),
        "total_created": total,
        "start": start_slot,
        "end": end_slot,
    }


def catch_up_quarterly_measurements(sensor, parameter, now=None, max_points=5000):
    """
    Voeg ontbrekende kwartiermetingen toe vanaf de laatste bestaande meting
    tot en met het huidige kwartier-slot (floor).
    """
    now = now or timezone.now()
    current_slot = floor_to_quarter(now)

    last_ts = (
        Meting.objects.filter(sensor=sensor, parameter=parameter)
        .order_by("-tijdstip")
        .values_list("tijdstip", flat=True)
        .first()
    )

    if last_ts:
        last_slot = floor_to_quarter(last_ts)
        next_slot = last_slot + timedelta(minutes=KWARTIER_MINUTEN)
    else:
        # als er geen data is, start op current_slot
        next_slot = current_slot

    if next_slot > current_slot:
        return 0

    voltage_kv = get_voltage_kv(sensor)

    created = 0
    ts = next_slot
    while ts <= current_slot and created < max_points:
        Meting.objects.create(
            sensor=sensor,
            parameter=parameter,
            tijdstip=ts,
            waarde=generate_realistic_infeed(voltage_kv, ts.hour),
            kwaliteit="in_spec",
        )
        created += 1
        ts += timedelta(minutes=KWARTIER_MINUTEN)

    return created


# ─────────────────────────────────────────────────────────────────────────────
# LANDINGSPAGINA
# ─────────────────────────────────────────────────────────────────────────────

class LandingPageView(generic.TemplateView):
    template_name = 'monitoring/index.html'


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

class DashboardView(generic.TemplateView):
    template_name = 'monitoring/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # ── FREQUENTIES OPHALEN ──────────────────────────────────────────────
        url_frequentie = "https://opendata.elia.be/api/records/1.0/search/"

        parameter_freq = Meetparameter.objects.filter(naam='frequentie').first()
        sensor_freq = Sensor.objects.filter(sensor_id='ELIA_FREQ').first()

        if not sensor_freq:
            sensor_freq = Sensor.objects.create(
                sensor_id='ELIA_FREQ',
                type='Frequentiesensor',
                communicatie_protocol='N.v.t.',
                status='actief',
            )

        should_fetch_next_page = True
        offset = 0
        maximum_limit = 100
        maximum_totaal_resultaten = 500
        api_resultaten = []

        while should_fetch_next_page:
            response = requests.get(
                url_frequentie,
                params={
                    "dataset": "ods057",
                    "rows": maximum_limit,
                    "start": offset,
                    "sort": "datetime",
                },
            )

            if response.status_code == 200:
                data = json.loads(response.text)
                batch = data.get("records", [])
                resterende_slots = maximum_totaal_resultaten - len(api_resultaten)
                if resterende_slots > 0:
                    api_resultaten.extend(batch[:resterende_slots])

                if len(api_resultaten) >= maximum_totaal_resultaten:
                    should_fetch_next_page = False
                elif len(batch) == maximum_limit:
                    offset += maximum_limit
                else:
                    should_fetch_next_page = False
            else:
                print("Er liep iets fout bij het ophalen van de frequenties:", response.status_code)
                should_fetch_next_page = False

        for r in api_resultaten:
            fields = r.get("fields", {})
            tijdstip_str = fields.get("datetime")
            waarde = fields.get("actualfrequency")

            if not tijdstip_str or waarde is None:
                continue

            tijdstip = parse_datetime(tijdstip_str)
            if not tijdstip:
                continue

            Meting.objects.get_or_create(
                sensor=sensor_freq,
                parameter=parameter_freq,
                tijdstip=tijdstip,
                defaults={
                    "waarde": float(waarde),
                    "kwaliteit": "in_spec",
                }
            )

        # ── EINDE API REFRESH ─────────────────────────────────────────────────

        net = Net.objects.first()
        freq_min = net.freq_min if net else 49.50
        freq_max = net.freq_max if net else 50.50

        parameter_freq = Meetparameter.objects.filter(naam='frequentie').first()
        parameter_infeed = Meetparameter.objects.filter(naam='infeedvalue').first()
        parameter_load = Meetparameter.objects.filter(naam='totalload').first()

        # ─────────────────────────────────────────────────────────────────────
        # INFEED: SEED 7 DAGEN (1x) + CATCH-UP PER KWARTIER BIJ REFRESH
        # ─────────────────────────────────────────────────────────────────────
        if parameter_infeed:
            # 1) Seed als er nog geen infeed metingen bestaan (bv. na flush)
            if not Meting.objects.filter(parameter=parameter_infeed).exists():
                info = seed_last_7_days_quarterly_for_all_infeed(parameter_infeed)
                print(
                    f"[Seed] {info['total_created']} metingen aangemaakt "
                    f"({info['slots_per_sensor']} slots/sensor, {info['sensors']} sensoren) "
                    f"van {info['start']} tot {info['end']}"
                )

            # 2) Catch-up bij elke refresh
            now = timezone.now()
            total_added = 0
            sensors = Sensor.objects.filter(type="Infeed-sensor", status="actief").select_related("net")
            for s in sensors:
                total_added += catch_up_quarterly_measurements(s, parameter_infeed, now=now, max_points=500)
            if total_added:
                print(f"[Dashboard] catch-up: +{total_added} kwartiermetingen")

        # ── FREQUENTIE tabel ────────────────────────────────────────────────
        laatste_metingen_qs = (
            Meting.objects.filter(parameter=parameter_freq)
            .select_related('sensor')
            .order_by('-tijdstip')[:20]
        ) if parameter_freq else Meting.objects.none()

        meting_rows = []
        for m in laatste_metingen_qs:
            try:
                waarde = float(m.waarde)
            except (TypeError, ValueError):
                waarde = None

            meting_rows.append({
                "tijdstip": m.tijdstip,
                "waarde": waarde,
                "in_spec": waarde is not None and freq_min <= waarde <= freq_max,
                "sensor_id": m.sensor.sensor_id if m.sensor else "–",
            })

        # ── BOKEH GRAFIEK — frequentie over LAATSTE WEEK ─────────────────────
        bokeh_script = ""
        bokeh_div = ""
        try:
            nu = timezone.now()
            een_week_geleden = nu - timedelta(days=7)

            grafiek_metingen = (
                Meting.objects.filter(
                    parameter=parameter_freq,
                    tijdstip__gte=een_week_geleden
                )
                .order_by('tijdstip')
            )

            if grafiek_metingen.exists():
                df = pd.DataFrame([{
                    "tijdstip": m.tijdstip,
                    "waarde": float(m.waarde),
                } for m in grafiek_metingen]).sort_values("tijdstip")

                source = ColumnDataSource(df)

                p = figure(
                    x_axis_type="datetime",
                    height=320,
                    sizing_mode="stretch_width",
                    toolbar_location="above",
                    title="Netwerkfrequentie (Hz) — Laatste 7 dagen",
                    background_fill_color="#ffffff",
                    border_fill_color="#ffffff",
                )

                band_source = ColumnDataSource(pd.DataFrame({
                    "tijdstip": df["tijdstip"],
                    "lower": [freq_min] * len(df),
                    "upper": [freq_max] * len(df),
                }))
                band = Band(
                    base="tijdstip",
                    lower="lower",
                    upper="upper",
                    source=band_source,
                    level="underlay",
                    fill_alpha=0.10,
                    fill_color="#f5a623",
                    line_alpha=0.0
                )
                p.add_layout(band)

                p.scatter(
                    x=jitter("tijdstip", 0.04),
                    y="waarde",
                    source=source,
                    size=6,
                    color="#1a73e8",
                    alpha=0.65,
                )

                y_min = min(df["waarde"].min(), freq_min) - 0.01
                y_max = max(df["waarde"].max(), freq_max) + 0.01
                p.y_range = Range1d(y_min, y_max)

                p.grid.grid_line_color = "#eaecef"
                p.outline_line_color = "#e1e4e8"

                p.xaxis.axis_label = "Tijdstip"
                p.yaxis.axis_label = "Frequentie (Hz)"

                p.xaxis.formatter = DatetimeTickFormatter(
                    hours="%d-%m %H:%M",
                    days="%d-%m",
                    months="%m-%Y",
                )

                p.add_tools(HoverTool(
                    tooltips=[
                        ("Tijdstip", "@tijdstip{%d-%m-%Y %H:%M:%S}"),
                        ("Frequentie", "@waarde{0.0000} Hz"),
                    ],
                    formatters={"@tijdstip": "datetime"},
                ))

                bokeh_script, bokeh_div = components(p)
        except Exception as e:
            print(f"Bokeh grafiek mislukt: {e}")

        # ── INFEED (uit DB) ──────────────────────────────────────────────────
        infeed_rows = []
        if parameter_infeed:
            infeed_metingen_qs = (
                Meting.objects.filter(parameter=parameter_infeed)
                .select_related('sensor__infrastructuur', 'sensor__net')
                .order_by('-tijdstip')[:5000]
            )

            geziene_sensors = set()
            for m in infeed_metingen_qs:
                sensor_id = m.sensor.sensor_id if m.sensor else None
                if sensor_id in geziene_sensors:
                    continue
                geziene_sensors.add(sensor_id)

                try:
                    waarde = float(m.waarde)
                except (TypeError, ValueError):
                    waarde = None

                infeed_rows.append({
                    "ean_code": sensor_id or "–",
                    "region": m.sensor.region if m.sensor else "–",
                    "location": m.sensor.location if m.sensor else "–",
                    "injection_station": m.sensor.station if m.sensor else "–",
                    "dso": m.sensor.infrastructuur.naam if m.sensor and m.sensor.infrastructuur else "–",
                    "voltage_level": m.sensor.net.spanningsniveau if m.sensor and m.sensor.net else "–",
                    "infeed_value": waarde,

                    "tijdstip": m.tijdstip,
                    "waarde": waarde,
                    "is_teruglevering": waarde is not None and waarde < 0,
                    "sensor_id": sensor_id or "–",
                    "station": m.sensor.station if m.sensor else "–",
                    "voltagelevel": m.sensor.net.spanningsniveau if m.sensor and m.sensor.net else "–",
                })

        # ── DSO SAMENVATTING ─────────────────────────────────────────────────
        dso_samenvatting = {}
        for row in infeed_rows:
            dso = row['dso']
            if dso not in dso_samenvatting:
                dso_samenvatting[dso] = {
                    "totaal_mw": 0.0,
                    "aantal_stations": 0,
                    "teruglevering": 0
                }
            if row['waarde'] is not None:
                dso_samenvatting[dso]["totaal_mw"] += row['waarde']
                dso_samenvatting[dso]["aantal_stations"] += 1
                if row['is_teruglevering']:
                    dso_samenvatting[dso]["teruglevering"] += 1

        # ── TOTAL LOAD ───────────────────────────────────────────────────────
        laatste_load_metingen = []
        totaal_load_mw = None

        if parameter_load:
            load_qs = (
                Meting.objects.filter(parameter=parameter_load)
                .select_related('sensor')
                .order_by('-tijdstip')[:20]
            )

            for m in load_qs:
                try:
                    waarde = float(m.waarde)
                except (TypeError, ValueError):
                    waarde = None

                laatste_load_metingen.append({
                    "tijdstip": m.tijdstip,
                    "waarde": waarde,
                    "sensor_id": m.sensor.sensor_id if m.sensor else "–",
                })

            if load_qs.exists():
                try:
                    totaal_load_mw = float(load_qs.first().waarde)
                except (TypeError, ValueError):
                    totaal_load_mw = None

        sensoren_totaal = Sensor.objects.count()

        context.update({
            "laatste_metingen": meting_rows,
            "infeed_rows": infeed_rows,
            "dso_samenvatting": dso_samenvatting.items(),
            "freq_min": freq_min,
            "freq_max": freq_max,
            "net": net,
            "totaal_infeed_mw": sum(r['waarde'] for r in infeed_rows if r['waarde'] is not None),
            "aantal_teruglevering": sum(1 for r in infeed_rows if r['is_teruglevering']),
            "laatste_load_metingen": laatste_load_metingen,
            "totaal_load_mw": totaal_load_mw,
            "bokeh_script": bokeh_script,
            "bokeh_div": bokeh_div,
            "sensoren_totaal": sensoren_totaal,
        })

        return context


# ─────────────────────────────────────────────────────────────────────────────
# SENSOREN
# ─────────────────────────────────────────────────────────────────────────────

class SensorListView(generic.ListView):
    template_name = 'monitoring/sensor_lijst.html'
    model = Sensor
    context_object_name = 'sensoren'

    def get_queryset(self):
        return Sensor.objects.select_related('net', 'infrastructuur').order_by('sensor_id')


class SensorDetailView(generic.DetailView):
    template_name = 'monitoring/sensor_detail.html'
    model = Sensor
    context_object_name = 'sensor'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sensor = self.get_object()

        parameter_infeed = Meetparameter.objects.filter(naam="infeedvalue").first()
        if parameter_infeed and sensor.status == "actief" and sensor.type == "Infeed-sensor":
            added = catch_up_quarterly_measurements(sensor, parameter_infeed, max_points=500)
            if added:
                print(f"[SensorDetail] catch-up {sensor.sensor_id}: +{added} kwartiermetingen")

        # ── LAATSTE METINGEN (tabel) ─────────────────────────────────────────
        metingen = []
        for m in sensor.metingen.select_related('parameter').order_by('-tijdstip')[:50]:
            metingen.append({
                "tijdstip": m.tijdstip,
                "waarde": m.waarde,
                "parameter_naam": (m.parameter.naam if m.parameter else "onbekend"),
                "eenheid": (m.parameter.eenheid if m.parameter else ""),
            })

        # ── BOKEH ANALYSE (MW) — laatste 7 dagen ────────────────────────────
        bokeh_script = ""
        bokeh_div = ""
        analyse = {}

        try:
            nu = timezone.now()
            start = nu - timedelta(days=7)

            qs = (
                sensor.metingen
                .select_related("parameter")
                .filter(tijdstip__gte=start)
                .order_by("tijdstip")
            )

            rows = []
            for m in qs:
                try:
                    v = float(m.waarde) if m.waarde is not None else None
                except (TypeError, ValueError):
                    v = None
                if v is None:
                    continue
                rows.append({"tijdstip": m.tijdstip, "waarde": v})

            analyse["aantal_metingen_7d"] = len(rows)

            if rows:
                df = pd.DataFrame(rows).sort_values("tijdstip")

                analyse.update({
                    "laatste_waarde": float(df["waarde"].iloc[-1]),
                    "laatste_tijdstip": df["tijdstip"].iloc[-1],
                    "min": float(df["waarde"].min()),
                    "max": float(df["waarde"].max()),
                    "gemiddelde": float(df["waarde"].mean()),
                    "teruglevering_pct": float((df["waarde"] < 0).mean() * 100.0),
                })

                source = ColumnDataSource(df)

                p = figure(
                    x_axis_type="datetime",
                    height=320,
                    sizing_mode="stretch_width",
                    toolbar_location="above",
                    title=f"Vermogen (MW) — laatste 7 dagen ({sensor.sensor_id})",
                    background_fill_color="#ffffff",
                    border_fill_color="#ffffff",
                )

                p.line("tijdstip", "waarde", source=source, line_width=2, color="#1a73e8", alpha=0.9)
                p.scatter("tijdstip", "waarde", source=source, size=5, color="#1a73e8", alpha=0.55)

                y_min = float(df["waarde"].min())
                y_max = float(df["waarde"].max())
                marge = max(0.02 * (y_max - y_min), 1.0)
                p.y_range = Range1d(y_min - marge, y_max + marge)

                p.grid.grid_line_color = "#eaecef"
                p.outline_line_color = "#e1e4e8"
                p.xaxis.axis_label = "Tijdstip"
                p.yaxis.axis_label = "MW"

                p.xaxis.formatter = DatetimeTickFormatter(
                    hours="%d-%m %H:%M",
                    days="%d-%m",
                    months="%m-%Y",
                )

                p.add_tools(HoverTool(
                    tooltips=[
                        ("Tijdstip", "@tijdstip{%d-%m-%Y %H:%M}"),
                        ("MW", "@waarde{0.000}"),
                    ],
                    formatters={"@tijdstip": "datetime"},
                ))

                bokeh_script, bokeh_div = components(p)

        except Exception as e:
            print(f"Bokeh analyse mislukt: {e}")

        context.update({
            "metingen": metingen,
            "bokeh_script": bokeh_script,
            "bokeh_div": bokeh_div,
            "analyse": analyse,
        })
        return context


# ─────────────────────────────────────────────────────────────────────────────
# RAPPORTEN
# ─────────────────────────────────────────────────────────────────────────────

class RapportListView(generic.ListView):
    template_name = 'monitoring/rapport_lijst.html'
    model = Rapport
    context_object_name = 'rapporten'

    def get_queryset(self):
        return Rapport.objects.select_related('operator').order_by('-aangemaakt_op')


class RapportDetailView(generic.DetailView):
    template_name = 'monitoring/rapport_detail.html'
    model = Rapport
    context_object_name = 'rapport'
    slug_field = 'rapport_id'
    slug_url_kwarg = 'rapport_id'


# ─────────────────────────────────────────────────────────────────────────────
# SENSOR IMPORT VAN API – handmatige actie!
# ─────────────────────────────────────────────────────────────────────────────

def importeer_sensors_api_view(request):
    url = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods091/records"
    params = {"limit": 500}
    aantal_aangemaakt = 0
    aantal_bijgewerkt = 0
    fouten = []

    try:
        with requests.get(url, params=params, timeout=30) as response:
            if response.status_code == 200:
                data = response.json()
            else:
                messages.error(request, "API call mislukt met status: " + str(response.status_code))
                return redirect("monitoring:dashboard")
    except Exception as e:
        messages.error(request, f"Fout tijdens API call: {e}")
        return redirect("monitoring:dashboard")

    resultaten = data.get("results", [])
    for r in resultaten:
        ean = r.get("eancode")
        if not ean:
            continue

        try:
            net_id = f"ELIA_{r.get('voltagelevel')}kV" if r.get("voltagelevel") else "ELIA_onbekend"
            net_infeed, _ = Net.objects.get_or_create(
                net_id=net_id,
                defaults={
                    "type": "Distributienet",
                    "spanningsniveau": float(r["voltagelevel"]) if r.get("voltagelevel") else 0.0,
                }
            )
            infra_infeed, _ = Infrastructuur.objects.get_or_create(
                infrastructuur_id=f"DSO_{r.get('dso') or 'Onbekend'}",
                defaults={
                    "naam": r.get('dso') or 'Onbekend',
                    "type": "Distributiestation",
                    "locatie": r.get('region') or "",
                    "status": "actief",
                    "beheerder": r.get('dso') or "Onbekend",
                }
            )

            injection_station = (
                r.get("injectionstation")
                or r.get("injection_station")
                or r.get("station")
                or ""
            )

            obj, created = Sensor.objects.update_or_create(
                sensor_id=ean,
                defaults={
                    "type": "Infeed-sensor",
                    "net": net_infeed,
                    "infrastructuur": infra_infeed,
                    "communicatie_protocol": "N.v.t.",
                    "status": "actief",
                    "station": injection_station,
                    "location": r.get("location") or "",
                    "region": r.get("region") or "",
                }
            )
            if created:
                aantal_aangemaakt += 1
            else:
                aantal_bijgewerkt += 1

            parameter_definities = [
                ("infeedvalue", "infeedvalue", "MW"),
                ("actualfrequency", "frequentie", "Hz"),
                ("totalload", "totalload", "MW"),
                ("load", "totalload", "MW"),
            ]
            kwaliteit = (
                r.get("quality")
                or r.get("qualitystatus")
                or r.get("quality_status")
                or "in_spec"
            )

            tijdstip_raw = (
                r.get("datetime")
                or r.get("timestamp")
                or r.get("tijdstip")
                or r.get("datehour")
            )
            tijdstip = parse_datetime(tijdstip_raw) if tijdstip_raw else None
            if not tijdstip:
                continue
            if timezone.is_naive(tijdstip):
                tijdstip = timezone.make_aware(tijdstip, timezone.get_current_timezone())

            for bronveld, parameter_naam, eenheid in parameter_definities:
                ruwe_waarde = r.get(bronveld)
                if ruwe_waarde is None:
                    continue

                try:
                    waarde = float(ruwe_waarde)
                except (TypeError, ValueError):
                    continue

                parameter_obj, _ = Meetparameter.objects.get_or_create(
                    naam=parameter_naam,
                    defaults={
                        "eenheid": eenheid,
                        "drempel_onder": -999999999.0,
                        "drempel_boven": 999999999.0,
                    }
                )

                bestaand = Meting.objects.filter(
                    sensor=obj,
                    parameter=parameter_obj,
                    tijdstip=tijdstip,
                ).first()

                if bestaand:
                    bestaand.waarde = waarde
                    bestaand.kwaliteit = kwaliteit
                    bestaand.infeed_value = waarde if parameter_naam == "infeedvalue" else 0
                    bestaand.save(update_fields=["waarde", "kwaliteit", "infeed_value"])
                else:
                    nieuwe_meting = Meting.objects.create(
                        sensor=obj,
                        parameter=parameter_obj,
                        waarde=waarde,
                        kwaliteit=kwaliteit,
                        infeed_value=waarde if parameter_naam == "infeedvalue" else 0,
                    )
                    Meting.objects.filter(pk=nieuwe_meting.pk).update(tijdstip=tijdstip)
        except Exception as e:
            fouten.append(f"Sensor {ean}: {e}")

    if fouten:
        for fout in fouten:
            messages.warning(request, fout)
    messages.success(
        request,
        f"Import klaar. Aangemaakt: {aantal_aangemaakt}, Bijgewerkt: {aantal_bijgewerkt}, Fouten: {len(fouten)}",
    )
    return redirect("monitoring:dashboard")