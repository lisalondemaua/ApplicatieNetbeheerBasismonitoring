import logging
import requests
import json
import math
import random
from datetime import timedelta

import pandas as pd
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import HoverTool, Band, ColumnDataSource, DatetimeTickFormatter, Range1d
from bokeh.transform import jitter

from django.views import generic, View
from django.shortcuts import redirect
from django.contrib import messages
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from django.db.models import Avg, Count, Max, Min, OuterRef, Q, StdDev, Subquery

from .models import Meting, Sensor, Rapport, Net, Meetparameter, Infrastructuur

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTEN
# ─────────────────────────────────────────────────────────────────────────────

KWARTIER_MINUTEN = 15
SEED_DAGEN = 30
KWARTIEREN_PER_WEEK = 7 * 24 * 4  # 672


# ─────────────────────────────────────────────────────────────────────────────
# DEMO / SIMULATIE HELPERS (kwartierdata + voltage-afhankelijke MW)
# ─────────────────────────────────────────────────────────────────────────────

def floor_to_quarter(dt):
    dt = dt.replace(second=0, microsecond=0)
    minute = (dt.minute // KWARTIER_MINUTEN) * KWARTIER_MINUTEN
    return dt.replace(minute=minute)


def generate_realistic_infeed(voltage_kv, hour, seed=None):
    """
    Realistische MW-waarde gekoppeld aan spanningsniveau + uur van de dag.

    Conventie:
      - waarde < 0  => teruglevering/injectie
      - waarde >= 0 => afname/consumptie

    Optionele seed voor reproduceerbare simulaties (bv. hash van tijdstip).
    """
    rng = random.Random(seed)
    daily_factor = 0.6 + 0.4 * math.sin((hour - 6) / 24 * 2 * math.pi)

    if voltage_kv <= 1:
        base = rng.uniform(-0.2, 0.5)
    elif voltage_kv <= 15:
        base = rng.uniform(-20, 10) if 11 <= hour <= 15 else rng.uniform(-5, 15)
    elif voltage_kv <= 70:
        base = rng.uniform(20, 100)
    elif voltage_kv <= 150:
        base = rng.uniform(100, 500)
    else:
        base = rng.uniform(200, 1200)

    noise = rng.uniform(-0.1, 0.1) * abs(base)
    return round((base * daily_factor) + noise, 3)


def get_voltage_kv(sensor):
    if sensor.net and sensor.net.spanningsniveau is not None:
        try:
            return float(sensor.net.spanningsniveau)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def seed_last_30_days_quarterly_for_all_infeed(parameter_infeed):
    """
    Seed 30 dagen kwartiermetingen voor ALLE actieve Infeed-sensoren.
    Bedoeld om 1x te draaien na flush (of lege DB).
    """
    now = timezone.now()
    end_slot = floor_to_quarter(now)
    start_slot = end_slot - timedelta(days=SEED_DAGEN)

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
                tijdstip=slot,
                waarde=generate_realistic_infeed(voltage_kv, slot.hour, seed=hash((s.sensor_id, slot))),
                kwaliteit="in_spec",
            )
            for slot in slots
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
    Gebruikt bulk_create voor efficiëntie.
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
        next_slot = current_slot

    if next_slot > current_slot:
        return 0

    voltage_kv = get_voltage_kv(sensor)

    metingen = []
    ts = next_slot
    while ts <= current_slot and len(metingen) < max_points:
        metingen.append(Meting(
            sensor=sensor,
            parameter=parameter,
            tijdstip=ts,
            waarde=generate_realistic_infeed(voltage_kv, ts.hour, seed=hash((sensor.sensor_id, ts))),
            kwaliteit="in_spec",
        ))
        ts += timedelta(minutes=KWARTIER_MINUTEN)

    if metingen:
        Meting.objects.bulk_create(metingen, batch_size=2000)

    return len(metingen)


# ─────────────────────────────────────────────────────────────────────────────
# RAPPORTEN — GENERATOR (3 weken) + VIEW
# ─────────────────────────────────────────────────────────────────────────────

def _start_of_iso_week(dt):
    """Maandag 00:00 van de ISO-week waarin dt valt (timezone-aware)."""
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    dt_local = timezone.localtime(dt)
    monday = dt_local - timedelta(days=dt_local.isoweekday() - 1)
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def generate_dso_reports_for_last_n_weeks(n_weeks=3, now=None):
    """
    Genereert weekrapporten per DSO voor de laatste n volledige ISO-weken.
    """
    now = now or timezone.now()

    parameter_infeed = Meetparameter.objects.filter(naam="infeedvalue").first()
    if not parameter_infeed:
        return 0

    def _generate_for_week(week_start, week_end):
        created = 0

        iso_year, iso_week, _ = week_start.isocalendar()
        iso_year = int(iso_year)
        iso_week = int(iso_week)

        dsos = (
            Infrastructuur.objects
            .filter(sensoren__metingen__parameter=parameter_infeed)
            .distinct()
            .order_by("naam")
        )

        for dso in dsos:
            dso_name = dso.naam or "Onbekend"
            dso_slug = slugify(dso_name) or "onbekend"

            rapport_id = f"DSO_{dso_slug}_{iso_year}-W{iso_week:02d}"
            titel = f"Weekrapport {dso_name} ({iso_year}-W{iso_week:02d})"

            qs = Meting.objects.filter(
                parameter=parameter_infeed,
                sensor__infrastructuur=dso,
                tijdstip__gte=week_start,
                tijdstip__lt=week_end,
            ).select_related("sensor")

            agg = qs.aggregate(
                aantal=Count("meting_id"),
                min=Min("waarde"),
                max=Max("waarde"),
                gemiddelde=Avg("waarde"),
                std=StdDev("waarde"),
                teruglevering=Count("meting_id", filter=Q(waarde__lt=0)),
            )

            if not agg["aantal"]:
                continue

            teruglevering_pct = (agg["teruglevering"] / agg["aantal"]) * 100.0

            # DATAKWALITEIT
            sensor_count = qs.values_list("sensor_id", flat=True).distinct().count()
            expected_total = sensor_count * KWARTIEREN_PER_WEEK if sensor_count else 0
            completeness_pct = (agg["aantal"] / expected_total * 100.0) if expected_total else 0.0

            # GAPS (grote tijdsprongen > 30 min)
            gap_threshold = timedelta(minutes=30)
            times = list(qs.order_by("tijdstip").values_list("tijdstip", flat=True))
            gaps = 0
            max_gap = timedelta(0)
            for idx in range(1, len(times)):
                delta = times[idx] - times[idx - 1]
                if delta > gap_threshold:
                    gaps += 1
                    if delta > max_gap:
                        max_gap = delta

            # PIEKEN
            std = float(agg["std"]) if agg["std"] is not None else 0.0
            mean = float(agg["gemiddelde"])
            peak_threshold = mean + (3.0 * std) if std > 0 else None

            top_peaks = list(
                qs.order_by("-waarde")
                  .values("tijdstip", "waarde", "sensor__sensor_id")[:5]
            )

            peaks_over_threshold = (
                qs.filter(waarde__gt=peak_threshold).count()
                if peak_threshold is not None else 0
            )

            # VERGELIJKING MET WEEK ERVOOR
            prev_end = week_start
            prev_start = prev_end - timedelta(days=7)

            qs_prev = Meting.objects.filter(
                parameter=parameter_infeed,
                sensor__infrastructuur=dso,
                tijdstip__gte=prev_start,
                tijdstip__lt=prev_end,
            )
            agg_prev = qs_prev.aggregate(
                aantal=Count("meting_id"),
                min=Min("waarde"),
                max=Max("waarde"),
                gemiddelde=Avg("waarde"),
                teruglevering=Count("meting_id", filter=Q(waarde__lt=0)),
            )

            def _fmt_delta(curr, prev, decimals=3):
                if curr is None or prev is None:
                    return "n.v.t."
                return f"{(float(curr) - float(prev)):+.{decimals}f}"

            teruglevering_pct_prev = (
                (agg_prev["teruglevering"] / agg_prev["aantal"]) * 100.0
                if agg_prev["aantal"] else None
            )

            inhoud_lines = [
                f"DSO: {dso_name}",
                f"Periode: {timezone.localtime(week_start).strftime('%d-%m-%Y %H:%M')} "
                f"t/m {timezone.localtime(week_end).strftime('%d-%m-%Y %H:%M')} (einde exclusief)",
                "Parameter: infeedvalue (MW)",
                "",
                "STATISTIEKEN",
                f"Aantal metingen: {agg['aantal']}",
                f"Min (MW): {float(agg['min']):.3f}",
                f"Max (MW): {float(agg['max']):.3f}",
                f"Gemiddelde (MW): {float(agg['gemiddelde']):.3f}",
                f"Std dev (MW): {std:.3f}",
                f"Teruglevering (% metingen < 0): {teruglevering_pct:.1f}%",
                "",
                "DATAKWALITEIT",
                f"Aantal sensoren met data: {sensor_count}",
                f"Verwacht # metingen (schatting): {expected_total}",
                f"Completeness: {completeness_pct:.1f}%",
                f"Aantal gaps (>30 min): {gaps}",
                f"Grootste gap: {max_gap}",
                "",
                "PIEKEN",
            ]

            if peak_threshold is not None:
                inhoud_lines.append(f"Piekdrempel (gem + 3*std): {peak_threshold:.3f} MW")
                inhoud_lines.append(f"Aantal metingen boven drempel: {peaks_over_threshold}")
            else:
                inhoud_lines.append("Piekdrempel (gem + 3*std): n.v.t. (std=0 of te weinig data)")

            if top_peaks:
                inhoud_lines.append("Top 5 pieken:")
                for p in top_peaks:
                    ts_str = timezone.localtime(p["tijdstip"]).strftime("%d-%m-%Y %H:%M")
                    inhoud_lines.append(
                        f" - {ts_str} | {float(p['waarde']):.3f} MW | sensor {p['sensor__sensor_id']}"
                    )
            else:
                inhoud_lines.append("Top 5 pieken: geen data")

            inhoud_lines += [
                "",
                "WIJZIGINGEN T.O.V. VORIGE WEEK",
                f"Δ gemiddelde (MW): {_fmt_delta(agg['gemiddelde'], agg_prev['gemiddelde'])}",
                f"Δ max (MW): {_fmt_delta(agg['max'], agg_prev['max'])}",
                f"Δ min (MW): {_fmt_delta(agg['min'], agg_prev['min'])}",
                (
                    "Δ teruglevering (%): "
                    + (
                        "n.v.t."
                        if teruglevering_pct_prev is None
                        else f"{(teruglevering_pct - float(teruglevering_pct_prev)):+.1f}%"
                    )
                ),
            ]

            inhoud = "\n".join(inhoud_lines)

            _, was_created = Rapport.objects.update_or_create(
                rapport_id=rapport_id,
                defaults={
                    "titel": titel,
                    "periode_start": week_start,
                    "periode_einde": week_end,
                    "inhoud": inhoud,
                    "operator": None,
                },
            )
            if was_created:
                created += 1

        return created

    this_week_start = _start_of_iso_week(now)

    total_created = 0
    for i in range(1, n_weeks + 1):
        week_end = this_week_start - timedelta(days=7 * (i - 1))
        week_start = week_end - timedelta(days=7)
        try:
            created = _generate_for_week(week_start, week_end)
            logger.info("Week %d (W%s): %d rapporten aangemaakt", i, week_start.isocalendar()[1], created)
            total_created += created
        except Exception:
            logger.exception("Fout bij genereren rapporten voor week %d", i)

    return total_created


@require_POST
def genereer_rapporten_view(request):
    created = generate_dso_reports_for_last_n_weeks(n_weeks=3)
    messages.success(request, f"Nieuwe weekrapporten aangemaakt: {created}.")
    return redirect("monitoring:rapport_lijst")


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD — helpers
# ─────────────────────────────────────────────────────────────────────────────

FREQUENTIE_API_URL = "https://opendata.elia.be/api/records/1.0/search/"
FREQUENTIE_MAX_RESULTATEN = 500
FREQUENTIE_BATCH = 100


def _refresh_frequentie_data(sensor_freq, parameter_freq):
    """
    Haalt de laatste frequentiemetingen op van de Elia API en slaat
    ontbrekende op in de DB. Retourneert het aantal nieuwe records.
    """
    should_fetch = True
    offset = 0
    api_resultaten = []

    while should_fetch:
        try:
            response = requests.get(
                FREQUENTIE_API_URL,
                params={
                    "dataset": "ods057",
                    "rows": FREQUENTIE_BATCH,
                    "start": offset,
                    "sort": "datetime",
                },
                timeout=10,
            )
        except requests.RequestException:
            logger.exception("Fout bij ophalen frequentie-API")
            break

        if response.status_code != 200:
            logger.warning("Frequentie-API antwoordde met status %s", response.status_code)
            break

        batch = response.json().get("records", [])
        resterende = FREQUENTIE_MAX_RESULTATEN - len(api_resultaten)
        api_resultaten.extend(batch[:resterende])

        if len(api_resultaten) >= FREQUENTIE_MAX_RESULTATEN or len(batch) < FREQUENTIE_BATCH:
            should_fetch = False
        else:
            offset += FREQUENTIE_BATCH

    aangemaakt = 0
    for r in api_resultaten:
        fields = r.get("fields", {})
        tijdstip_str = fields.get("datetime")
        waarde = fields.get("actualfrequency")

        if not tijdstip_str or waarde is None:
            continue

        tijdstip = parse_datetime(tijdstip_str)
        if not tijdstip:
            continue

        _, created = Meting.objects.get_or_create(
            sensor=sensor_freq,
            parameter=parameter_freq,
            tijdstip=tijdstip,
            defaults={"waarde": float(waarde), "kwaliteit": "in_spec"},
        )
        if created:
            aangemaakt += 1

    return aangemaakt


def _get_infeed_rows(parameter_infeed):
    """
    Haalt per sensor de meest recente infeed-meting op via een efficiënte
    subquery (geen Python-side deduplicatie van 5000 records).
    """
    if not parameter_infeed:
        return []

    laatste_tijdstip = (
        Meting.objects
        .filter(parameter=parameter_infeed, sensor=OuterRef("pk"))
        .order_by("-tijdstip")
        .values("tijdstip")[:1]
    )
    laatste_waarde = (
        Meting.objects
        .filter(parameter=parameter_infeed, sensor=OuterRef("pk"))
        .order_by("-tijdstip")
        .values("waarde")[:1]
    )

    sensors = (
        Sensor.objects
        .filter(type="Infeed-sensor", status="actief")
        .select_related("net", "infrastructuur")
        .annotate(
            laatste_tijdstip=Subquery(laatste_tijdstip),
            laatste_waarde=Subquery(laatste_waarde),
        )
    )

    rows = []
    for s in sensors:
        try:
            waarde = float(s.laatste_waarde) if s.laatste_waarde is not None else None
        except (TypeError, ValueError):
            waarde = None

        rows.append({
            "ean_code": s.sensor_id,
            "region": s.region,
            "location": s.location,
            "injection_station": s.station,
            "dso": s.infrastructuur.naam if s.infrastructuur else "–",
            "voltage_level": s.net.spanningsniveau if s.net else "–",
            "infeed_value": waarde,
            "tijdstip": s.laatste_tijdstip,
            "waarde": waarde,
            "is_teruglevering": waarde is not None and waarde < 0,
            "sensor_id": s.sensor_id,
            "station": s.station,
            "voltagelevel": s.net.spanningsniveau if s.net else "–",
        })

    return rows


def _build_frequentie_chart(parameter_freq, freq_min, freq_max):
    """
    Bouwt de Bokeh-grafiek voor de netwerkfrequentie van de laatste 7 dagen.
    Retourneert (script, div) of ("", "") bij een fout.
    """
    try:
        nu = timezone.now()
        een_week_geleden = nu - timedelta(days=7)

        grafiek_metingen = (
            Meting.objects
            .filter(parameter=parameter_freq, tijdstip__gte=een_week_geleden)
            .order_by("tijdstip")
        )

        if not grafiek_metingen.exists():
            return "", ""

        df = pd.DataFrame([
            {"tijdstip": m.tijdstip, "waarde": float(m.waarde)}
            for m in grafiek_metingen
        ]).sort_values("tijdstip")

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
        p.add_layout(Band(
            base="tijdstip",
            lower="lower",
            upper="upper",
            source=band_source,
            level="underlay",
            fill_alpha=0.10,
            fill_color="#f5a623",
            line_alpha=0.0,
        ))

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

        return components(p)

    except Exception:
        logger.exception("Bokeh frequentiegrafiek mislukt")
        return "", ""


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

        # ── FREQUENTIESENSOR ────────────────────────────────────────────────
        parameter_freq = Meetparameter.objects.filter(naam='frequentie').first()
        sensor_freq, _ = Sensor.objects.get_or_create(
            sensor_id='ELIA_FREQ',
            defaults={
                "type": "Frequentiesensor",
                "communicatie_protocol": "N.v.t.",
                "status": "actief",
            },
        )

        if parameter_freq:
            nieuw = _refresh_frequentie_data(sensor_freq, parameter_freq)
            if nieuw:
                logger.info("[Dashboard] %d nieuwe frequentiemetingen opgeslagen", nieuw)

        # ── PARAMETERS ──────────────────────────────────────────────────────
        parameter_infeed = Meetparameter.objects.filter(naam='infeedvalue').first()
        parameter_load = Meetparameter.objects.filter(naam='totalload').first()

        net = Net.objects.first()
        freq_min = net.freq_min if net else 49.50
        freq_max = net.freq_max if net else 50.50

        # ── INFEED SEED + CATCH-UP ───────────────────────────────────────────
        if parameter_infeed:
            if not Meting.objects.filter(parameter=parameter_infeed).exists():
                info = seed_last_30_days_quarterly_for_all_infeed(parameter_infeed)
                logger.info(
                    "[Seed] %d metingen aangemaakt (%d slots/sensor, %d sensoren) van %s tot %s",
                    info["total_created"], info["slots_per_sensor"], info["sensors"],
                    info["start"], info["end"],
                )

            now = timezone.now()
            total_added = 0
            for s in Sensor.objects.filter(type="Infeed-sensor", status="actief").select_related("net"):
                total_added += catch_up_quarterly_measurements(s, parameter_infeed, now=now, max_points=500)
            if total_added:
                logger.info("[Dashboard] catch-up: +%d kwartiermetingen", total_added)

        # ── FREQUENTIE TABEL ────────────────────────────────────────────────
        meting_rows = []
        if parameter_freq:
            for m in (
                Meting.objects.filter(parameter=parameter_freq)
                .select_related("sensor")
                .order_by("-tijdstip")[:20]
            ):
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

        # ── BOKEH GRAFIEK ────────────────────────────────────────────────────
        bokeh_script, bokeh_div = _build_frequentie_chart(parameter_freq, freq_min, freq_max)

        # ── INFEED RIJEN (efficiënt via subquery) ────────────────────────────
        infeed_rows = _get_infeed_rows(parameter_infeed)

        # ── DSO SAMENVATTING ─────────────────────────────────────────────────
        dso_samenvatting = {}
        for row in infeed_rows:
            dso = row["dso"]
            entry = dso_samenvatting.setdefault(dso, {"totaal_mw": 0.0, "aantal_stations": 0, "teruglevering": 0})
            if row["waarde"] is not None:
                entry["totaal_mw"] += row["waarde"]
                entry["aantal_stations"] += 1
                if row["is_teruglevering"]:
                    entry["teruglevering"] += 1

        # ── TOTAL LOAD ───────────────────────────────────────────────────────
        laatste_load_metingen = []
        totaal_load_mw = None

        if parameter_load:
            load_qs = (
                Meting.objects.filter(parameter=parameter_load)
                .select_related("sensor")
                .order_by("-tijdstip")[:20]
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
            "freq_min": freq_min,
            "freq_max": freq_max,
            "net": net,
            "totaal_infeed_mw": sum(r["waarde"] for r in infeed_rows if r["waarde"] is not None),
            "aantal_teruglevering": sum(1 for r in infeed_rows if r["is_teruglevering"]),
            "laatste_load_metingen": laatste_load_metingen,
            "totaal_load_mw": totaal_load_mw,
            "bokeh_script": bokeh_script,
            "bokeh_div": bokeh_div,
            "sensoren_totaal": Sensor.objects.count(),
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
                logger.info("[SensorDetail] catch-up %s: +%d kwartiermetingen", sensor.sensor_id, added)

        # ── LAATSTE METINGEN (tabel) ─────────────────────────────────────────
        metingen = [
            {
                "tijdstip": m.tijdstip,
                "waarde": m.waarde,
                "parameter_naam": m.parameter.naam if m.parameter else "onbekend",
                "eenheid": m.parameter.eenheid if m.parameter else "",
            }
            for m in sensor.metingen.select_related("parameter").order_by("-tijdstip")[:50]
        ]

        # ── BOKEH ANALYSE (MW) — laatste 7 dagen ────────────────────────────
        bokeh_script = ""
        bokeh_div = ""
        analyse = {}

        try:
            nu = timezone.now()
            start = nu - timedelta(days=7)

            rows = []
            for m in (
                sensor.metingen
                .select_related("parameter")
                .filter(tijdstip__gte=start)
                .order_by("tijdstip")
            ):
                try:
                    v = float(m.waarde) if m.waarde is not None else None
                except (TypeError, ValueError):
                    v = None
                if v is not None:
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

        except Exception:
            logger.exception("Bokeh sensor-analyse mislukt voor %s", sensor.sensor_id)

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
# SENSOR IMPORT VAN API
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

                Meting.objects.update_or_create(
                    sensor=obj,
                    parameter=parameter_obj,
                    tijdstip=tijdstip,
                    defaults={
                        "waarde": waarde,
                        "kwaliteit": kwaliteit,
                        "infeed_value": waarde if parameter_naam == "infeedvalue" else 0,
                    },
                )

        except Exception as e:
            fouten.append(f"Sensor {ean}: {e}")
            logger.warning("Fout bij importeren sensor %s: %s", ean, e)

    if fouten:
        for fout in fouten:
            messages.warning(request, fout)
    messages.success(
        request,
        f"Import klaar. Aangemaakt: {aantal_aangemaakt}, Bijgewerkt: {aantal_bijgewerkt}, Fouten: {len(fouten)}",
    )
    return redirect("monitoring:dashboard")