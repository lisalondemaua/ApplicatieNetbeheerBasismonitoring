import requests
import json

from django.views import generic
from django.db.models import Q, Count
from django.shortcuts import render, redirect
from django.contrib import messages
from django.utils.dateparse import parse_datetime
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import HoverTool, Band, ColumnDataSource, DatetimeTickFormatter, Range1d
from bokeh.transform import jitter
import pandas as pd
from datetime import timedelta
from django.utils import timezone

from .models import Meting, Afwijking, Sensor, Rapport, Net, Meetparameter, Infrastructuur

# LANDINGSPAGINA

class LandingPageView(generic.TemplateView):
    template_name = 'monitoring/index.html'

# DASHBOARD

class DashboardView(generic.TemplateView):
    template_name = 'monitoring/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # ── TOTAAL AANTAL SENSOREN VAN ELIA API ──────────────────────────────
        url_sensoren = "https://opendata.elia.be/api/explore/v2.1/catalog/datasets/ods091/records"
        sensoren_totaal = 0
        try:
            api_response = requests.get(url_sensoren, params={"limit": 1, "offset": 0})
            if api_response.status_code == 200:
                sensoren_totaal = api_response.json().get("total_count", 0)
            else:
                print("Kon sensoren totaal niet ophalen van API:", api_response.status_code)
        except Exception as e:
            print("Fout tijdens API-call voor sensoren totaal:", e)

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
                print(f"Frequenties refresh klaar: {len(api_resultaten)} resultaten ontvangen")

                if len(api_resultaten) >= maximum_totaal_resultaten:
                    should_fetch_next_page = False
                elif len(batch) == maximum_limit:
                    offset += maximum_limit
                else:
                    should_fetch_next_page = False

            else:
                print("Er liep iets fout bij het ophalen van de frequenties:", response.status_code)
                should_fetch_next_page = False
                break

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

        # FREQUENTIE
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

        # BOKEH GRAFIEK — frequentie over LAATSTE WEEK (met jitter voor betere zichtbaarheid)
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

            print(f"Aantal metingen voor grafiek (7 dagen): {grafiek_metingen.count()}")
            print(f"parameter_freq: {parameter_freq}")

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
                print("Bokeh grafiek aangemaakt (7 dagen, met jitter)")
        except Exception as e:
            print(f"Bokeh grafiek mislukt: {e}")

        # INFEED (ALLEEN UIT JE EIGEN DB)
        infeed_rows = []
        if parameter_infeed:
            infeed_metingen_qs = (
                Meting.objects.filter(parameter=parameter_infeed)
                .select_related('sensor__infrastructuur', 'sensor__net')
                .order_by('-tijdstip')[:500]
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
                    "tijdstip": m.tijdstip,
                    "waarde": waarde,
                    "is_teruglevering": waarde is not None and waarde < 0,
                    "sensor_id": sensor_id or "–",
                    "station": m.sensor.station if m.sensor else "–",
                    "location": m.sensor.location if m.sensor else "–",
                    "region": m.sensor.region if m.sensor else "–",
                    "dso": m.sensor.infrastructuur.naam if m.sensor and m.sensor.infrastructuur else "–",
                    "voltagelevel": m.sensor.net.spanningsniveau if m.sensor and m.sensor.net else "–",
                })

        # DSO SAMENVATTING (LOKAAL VIA DB)
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

        afwijkingen = Afwijking.objects.select_related('meting__sensor').order_by('-begintijd')[:10]

        # TOTAL LOAD
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

        context.update({
            "laatste_metingen": meting_rows,
            "infeed_rows": infeed_rows,
            "dso_samenvatting": dso_samenvatting.items(),
            "afwijkingen": afwijkingen,
            "freq_min": freq_min,
            "freq_max": freq_max,
            "net": net,
            "totaal_infeed_mw": sum(r['waarde'] for r in infeed_rows if r['waarde'] is not None),
            "aantal_teruglevering": sum(1 for r in infeed_rows if r['is_teruglevering']),
            "laatste_load_metingen": laatste_load_metingen,
            "totaal_load_mw": totaal_load_mw,
            "bokeh_script": bokeh_script,
            "bokeh_div": bokeh_div,
            "sensoren_totaal": sensoren_totaal,   # <-- alleen display, GEEN DB-bijwerken hier!
        })

        return context

# SENSOREN (ListView + DetailView)

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
        freq_min = sensor.net.freq_min if sensor.net else 49.50
        freq_max = sensor.net.freq_max if sensor.net else 50.50
        metingen = sensor.metingen.order_by('-tijdstip')[:50]
        afwijkingen = Afwijking.objects.filter(meting__sensor=sensor).order_by('-begintijd')
        context.update({
            'metingen': metingen,
            'afwijkingen': afwijkingen,
            'freq_min': freq_min,
            'freq_max': freq_max,
        })
        return context

# AFWIJKINGEN (ListView)

class AfwijkingenListView(generic.ListView):
    template_name = 'monitoring/afwijkingen_lijst.html'
    context_object_name = 'afwijkingen'

    def get_queryset(self):
        net = Net.objects.first()
        freq_min = net.freq_min if net else 49.50
        freq_max = net.freq_max if net else 50.50
        return Meting.objects.filter(
            Q(waarde__lt=freq_min) | Q(waarde__gt=freq_max)
        ).select_related('sensor').order_by('-tijdstip')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        net = Net.objects.first()
        context.update({
            "freq_min": net.freq_min if net else 49.50,
            "freq_max": net.freq_max if net else 50.50,
            "net": net,
        })
        return context


# RAPPORTEN (ListView + DetailView)

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

# SENSOR IMPORT VAN API – handmatige actie!

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
            obj, created = Sensor.objects.update_or_create(
                sensor_id=ean,
                defaults={
                    "type": "Infeed-sensor",
                    "net": net_infeed,
                    "infrastructuur": infra_infeed,
                    "communicatie_protocol": "N.v.t.",
                    "status": "actief",
                    "station": r.get("station") or "",
                    "location": r.get("location") or "",
                    "region": r.get("region") or "",
                }
            )
            if created:
                aantal_aangemaakt += 1
            else:
                aantal_bijgewerkt += 1
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