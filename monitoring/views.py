import requests
import random
from datetime import timedelta
import pandas as pd
from bokeh.plotting import figure
from bokeh.embed import components
from bokeh.models import HoverTool, ColumnDataSource, DatetimeTickFormatter, Range1d
from django.views import generic
from django.shortcuts import redirect
from django.contrib import messages
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.utils.text import slugify
from django.db.models import Avg, Count, Max, Min, StdDev
from .models import Meting, Sensor, Rapport, Net, Meetparameter, Infrastructuur

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTEN
# ─────────────────────────────────────────────────────────────────────────────

KWARTIER_MINUTEN = 15
SEED_DAGEN = 30
KWARTIEREN_PER_WEEK = 7 * 24 * 4  # 672
FREQ_MIN_STANDAARD = 49.50
FREQ_MAX_STANDAARD = 50.50

# ─────────────────────────────────────────────────────────────────────────────
# GENEREER KWARTIERMETINGEN
# ─────────────────────────────────────────────────────────────────────────────

# zorgt ervoor dat de tijd mooi per kwartier wordt afgerond
def rond_af_op_kwartier(datum_tijd):
    datum_tijd = datum_tijd.replace(second=0, microsecond=0)
    minuut = (datum_tijd.minute // KWARTIER_MINUTEN) * KWARTIER_MINUTEN
    return datum_tijd.replace(minute=minuut)


# Genereert een willekeurig getal voor MW-waarden, consistent per sensor_id + tijdstip combinatie
# Je krijgt dus altijd dezelfde waarde voor dezelfde sensor en hetzelfde tijdstip, anders zou de data elke keer anders zijn bij het verversen van het dashboard
def kwartier_data(sensor_id, tijdstip):
    startwaarde = str(sensor_id) + str(tijdstip)  # bv. ELIA_12345 2024-06-01 12:00:00
    generator = random.Random(startwaarde)
    willekeurig_getal = generator.uniform(-40, 120)
    return round(willekeurig_getal, 2)


# Vul de laatste 30 dagen kwartiermetingen voor alle actieve Infeed-sensoren aan.
# Bedoelt om dataset te vullen
def vul_laatste_30_dagen_infeed_aan(parameter_infeed):
    nu = timezone.now()
    eind_kwartier = rond_af_op_kwartier(nu)
    start_kwartier = eind_kwartier - timedelta(days=SEED_DAGEN)  # zorgt ervoor dat we precies 30 dagen terug gaan

    kwartieren = []
    huidig_tijdstip = start_kwartier
    while huidig_tijdstip <= eind_kwartier:
        kwartieren.append(huidig_tijdstip)
        huidig_tijdstip += timedelta(minutes=KWARTIER_MINUTEN)

    # haal alle sensoren op + info over het net
    sensoren = Sensor.objects.all().select_related("net")
    totaal_aangemaakt = 0

    # loop om voor elke sensor en elk kwartier een meting aan te maken met de gegenereerde waarde, en deze in bulk op te slaan in de database
    for s in sensoren:
        metingen = [
            Meting(
                sensor=s,
                parameter=parameter_infeed,
                tijdstip=kwartier,
                waarde=kwartier_data(s.sensor_id, kwartier),
            )
            for kwartier in kwartieren
        ]
        Meting.objects.bulk_create(metingen,
                                   batch_size=2880)  # bulk_create is een snelle manier om veel records aan te maken in de database
        totaal_aangemaakt += len(metingen)

        if metingen:
            s.laatste_waarde = metingen[-1].waarde
            s.laatste_tijdstip = metingen[-1].tijdstip
            s.save(update_fields=['laatste_waarde', 'laatste_tijdstip'])

    return {"total_created": totaal_aangemaakt}


# functie om ontbrekende kwartiermetingen aan te vullen vanaf de laatste meting tot nu
def inhalen_kwartiermetingen(sensor, parameter, nu=None, max_aantal=2880):
    nu = nu or timezone.now()
    huidig_kwartier = rond_af_op_kwartier(nu)

    laatste_tijdstip = (
        Meting.objects.filter(sensor=sensor, parameter=parameter)  # metingen ophalen voor deze sensor EN parameter
        .order_by("-tijdstip")
        .values_list("tijdstip", flat=True)  # haal enkel lijst met tijdstip op
        .first()  # pak het nieuwste (laatste) tijdstip
    )

    # bepaal vanaf welk kwartier we moeten beginnen met aanmaken: het kwartier na de laatste meting, of het huidige kwartier als er nog geen metingen zijn
    if laatste_tijdstip:
        laatste_kwartier = rond_af_op_kwartier(laatste_tijdstip)
        volgend_kwartier = laatste_kwartier + timedelta(minutes=KWARTIER_MINUTEN)
    else:
        volgend_kwartier = huidig_kwartier

    if volgend_kwartier > huidig_kwartier:
        return 0

    # maak metingen aan voor elk kwartier tussen volgend_kwartier en huidig_kwartier, maar stop als we het max_aantal bereiken
    metingen = []
    huidig_tijdstip = volgend_kwartier  # huidig_tijdstip begint bij het volgende kwartier na de laatste meting, of bij het huidige kwartier als er nog geen metingen zijn
    while huidig_tijdstip <= huidig_kwartier and len(metingen) < max_aantal:
        metingen.append(Meting(
            sensor=sensor,
            parameter=parameter,
            tijdstip=huidig_tijdstip,
            waarde=kwartier_data(sensor.sensor_id, huidig_tijdstip),
        ))
        huidig_tijdstip += timedelta(minutes=KWARTIER_MINUTEN)

    if metingen:
        Meting.objects.bulk_create(metingen, batch_size=2880)
        sensor.laatste_waarde = metingen[-1].waarde
        sensor.laatste_tijdstip = metingen[-1].tijdstip
        sensor.save(update_fields=['laatste_waarde', 'laatste_tijdstip'])

    return len(metingen)


# ─────────────────────────────────────────────────────────────────────────────
# GENEREER RAPPORTEN (3 weken)
# ─────────────────────────────────────────────────────────────────────────────

# bepaalt het begin van de week
def _start_of_week(dt):
    maandag = dt - timedelta(days=dt.weekday())
    return maandag.replace(hour=0, minute=0, second=0, microsecond=0)  # zet klok op 00:00:00


#
def _generate_for_week(week_start, week_end, parameter_infeed):
    # Hulpfunctie om het verschil netjes af te drukken
    def _formatteer_verschil(huidig, vorig, decimalen=3):
        if huidig is None or vorig is None:
            return "n.v.t."
        return f"{(float(huidig) - float(vorig)):+.{decimalen}f}"

    aantal_aangemaakt = 0
    week_label = week_start.strftime("%G-W%V")  # bv. 2026-W20

    # lijst van DSO's
    dsos = (
        Infrastructuur.objects
        .filter(
            sensoren__metingen__parameter=parameter_infeed)  # filter op infrastructuren die sensoren hebben met metingen voor deze parameter
        .distinct()  # zorgt ervoor dat we elke DSO, maar 1 keer krijgen, ook al heeft hij meerdere sensoren met metingen
        .order_by("naam")
    )

    for dso in dsos:
        dso_naam = dso.naam or "Onbekend"
        dso_slug = slugify(
            dso_naam) or "onbekend"  # slugify zorgt ervoor dat we een nette string krijgen zonder spaties of speciale tekens, bv. "DSO_Enexis_W2026"

        rapport_id = f"DSO_{dso_slug}_{week_label}"
        titel = f"Weekrapport {dso_naam} ({week_label})"

        # queryset van metingen op te halen uit de databank voor de DSO, parameter en periode.
        qs = Meting.objects.filter(
            parameter=parameter_infeed,
            sensor__infrastructuur=dso,
            tijdstip__gte=week_start,  # gte = greater than or equal, we willen metingen vanaf het begin van de week
            tijdstip__lt=week_end,
            # lt = less than, we willen metingen tot het einde van de week, exclusief het exacte tijdstip van week_end
        ).select_related("sensor")

        # aggregaties uitvoeren op de queryset om de statistieken te berekenen: aantal metingen, min/max/avg/std dev, percentage teruglevering
        samenvatting = qs.aggregate(
            aantal=Count("meting_id"),
            min=Min("waarde"),
            max=Max("waarde"),
            gemiddelde=Avg("waarde"),
            standaardafwijking=StdDev("waarde"),
        )

        aantal = samenvatting["aantal"] or 0
        standaardafwijking = float(samenvatting["standaardafwijking"] or 0)

        aantal_teruglevering = 0
        for meting in qs:
            if meting.waarde is not None and meting.waarde < 0:
                aantal_teruglevering += 1

        teruglevering_pct = (
            (aantal_teruglevering / aantal) * 100.0
            if aantal else 0.0
        )

        # DATAKWALITEIT
        # Bepaal hoeveel sensoren er zijn en wat we maximaal verwachten
        aantal_sensoren = qs.values_list("sensor_id", flat=True).distinct().count()
        verwacht_totaal = aantal_sensoren * KWARTIEREN_PER_WEEK

        if verwacht_totaal == 0:
            volledigheid_pct = 0.0
            ontbrekend_pct = 100.0
        else:
            volledigheid_pct = (samenvatting["aantal"] / verwacht_totaal) * 100.0
            ontbrekend_pct = 100.0 - volledigheid_pct

        # Labels voor datakwaliteit bepalen op basis van percentage ontbrekende data
        if ontbrekend_pct > 10:
            datakwaliteit = "ONBETROUWBAAR"
        else:
            datakwaliteit = "BETROUWBAAR"

        # tijdsprongen (grote tijdsprongen > 30 min)
        tijdsprong_drempel = timedelta(minutes=30)
        tijden = list(qs.order_by("tijdstip").values_list("tijdstip", flat=True))

        aantal_tijdsprongen = 0
        grootste_tijdsprong = timedelta(0)
        vorige_tijd = None

        for huidige_tijd in tijden:
            if vorige_tijd is not None:
                tijdsverschil = huidige_tijd - vorige_tijd

                if tijdsverschil > tijdsprong_drempel:
                    aantal_tijdsprongen += 1

                    if tijdsverschil > grootste_tijdsprong:
                        grootste_tijdsprong = tijdsverschil

            vorige_tijd = huidige_tijd  # Update vorige_tijd naar huidige_tijd voor volgende iteratie

        # PIEKEN
        gemiddelde_waarde = float(samenvatting["gemiddelde"]) if samenvatting["gemiddelde"] is not None else 0.0
        piek_drempel = gemiddelde_waarde + (3.0 * standaardafwijking) if standaardafwijking > 0 else None

        # haal de top 5 pieken op (hoogste waarden) voor deze DSO en periode, inclusief tijdstip, waarde en sensor_id
        top_pieken = list(
            qs.order_by("-waarde")
            .values("tijdstip", "waarde", "sensor__sensor_id")[:5]
        )

        if piek_drempel is None:
            pieken_boven_drempel = 0
        else:
            pieken_boven_drempel = qs.filter(waarde__gt=piek_drempel).count()

        # VERGELIJKING MET WEEK ERVOOR
        vorige_eind = week_start
        vorige_start = vorige_eind - timedelta(days=7)

        qs_vorig = Meting.objects.filter(
            parameter=parameter_infeed,
            sensor__infrastructuur=dso,
            tijdstip__gte=vorige_start,
            tijdstip__lt=vorige_eind,
        )

        samenvatting_vorig = qs_vorig.aggregate(
            aantal=Count("meting_id"),
            min=Min("waarde"),
            max=Max("waarde"),
            gemiddelde=Avg("waarde"),
        )

        aantal_vorig = samenvatting_vorig["aantal"] or 0

        aantal_teruglevering_vorig = 0
        for meting in qs_vorig:
            if meting.waarde is not None and meting.waarde < 0:
                aantal_teruglevering_vorig += 1

        teruglevering_pct_vorig = (
            (aantal_teruglevering_vorig / aantal_vorig) * 100.0
            if aantal_vorig else None
        )

        # mooie weergave van getallen in rapport
        def formatteer_getal(waarde):
            if waarde is None:
                return "n.v.t."
            return f"{float(waarde):.3f}"

        min_str = formatteer_getal(samenvatting['min'])
        max_str = formatteer_getal(samenvatting['max'])
        gem_str = formatteer_getal(samenvatting['gemiddelde'])

        inhoud_regels = [
            f"Netbeheerder (DSO): {dso_naam}",
            f"Periode: {timezone.localtime(week_start).strftime('%d-%m-%Y %H:%M')} "
            f"t/m {timezone.localtime(week_end).strftime('%d-%m-%Y %H:%M')} (einde exclusief)",
            "Parameter: infeedvalue (MW)",
            "",
            "STATISTIEKEN",
            f"Aantal metingen: {samenvatting['aantal']}",
            f"Min (MW): {min_str}",
            f"Max (MW): {max_str}",
            f"Gemiddelde (MW): {gem_str}",
            f"Standaardafwijking (MW): {standaardafwijking:.3f}",
            f"Teruglevering (% metingen < 0): {teruglevering_pct:.1f}%",
            "",
            "DATAKWALITEIT",
            f"Aantal sensoren met data: {aantal_sensoren}",
            f"Verwacht aantal metingen (schatting): {verwacht_totaal}",
            f"Volledigheid: {volledigheid_pct:.1f}%",
            f"Ontbrekend: {ontbrekend_pct:.1f}%",
            f"Aantal tijdsprongen (>30 min): {aantal_tijdsprongen}",
            f"Grootste tijdsprong: {grootste_tijdsprong}",
            f"Datakwaliteit: {datakwaliteit}",
            "",
            "PIEKEN",
        ]

        if piek_drempel is not None:
            inhoud_regels.append(f"Piekdrempel (gem + 3*standaardafwijking): {piek_drempel:.3f} MW")
            inhoud_regels.append(f"Aantal metingen boven drempel: {pieken_boven_drempel}")
        else:
            inhoud_regels.append(
                "Piekdrempel (gem + 3*standaardafwijking): n.v.t. (standaardafwijking is 0 of te weinig data)")

        if top_pieken:
            inhoud_regels.append("Top 5 pieken:")
            for piek in top_pieken:
                tijdstip_str = timezone.localtime(piek["tijdstip"]).strftime("%d-%m-%Y %H:%M")
                inhoud_regels.append(
                    f" - {tijdstip_str} | {float(piek['waarde']):.3f} MW | sensor {piek['sensor__sensor_id']}"
                )
        else:
            inhoud_regels.append("Top 5 pieken: geen data")

        inhoud_regels += [
            "",
            "WIJZIGINGEN T.O.V. VORIGE WEEK",
            f"Δ gemiddelde (MW): {_formatteer_verschil(samenvatting['gemiddelde'], samenvatting_vorig['gemiddelde'])}",
            f"Δ max (MW): {_formatteer_verschil(samenvatting['max'], samenvatting_vorig['max'])}",
            f"Δ min (MW): {_formatteer_verschil(samenvatting['min'], samenvatting_vorig['min'])}",
            (
                    "Δ teruglevering (%): "
                    + (
                        "n.v.t."
                        if teruglevering_pct_vorig is None
                        else f"{(teruglevering_pct - float(teruglevering_pct_vorig)):+.1f}%"
                    )
            ),
        ]

        inhoud = "\n".join(inhoud_regels)

        _, was_created = Rapport.objects.update_or_create(
            rapport_id=rapport_id,
            defaults={
                "titel": titel,
                "periode_start": week_start,
                "periode_einde": week_end,
                "inhoud": inhoud,
            },
        )
        if was_created:
            aantal_aangemaakt += 1

    return aantal_aangemaakt

# functie om rapporten te genereren voor de laatste n weken
def genereer_dso_rapporten_voor_laatste_n_weken(n_weken=3, nu=None):
    nu = nu or timezone.now()

    parameter_infeed = Meetparameter.objects.filter(naam="infeedvalue").first()
    if not parameter_infeed:
        return 0

    huidige_week_start = _start_of_week(nu)

    totaal_aangemaakt = 0
    for i in range(1, n_weken + 1):
        week_einde = huidige_week_start - timedelta(days=7 * (i - 1))
        week_start = week_einde - timedelta(days=7)
        try:
            aantal = _generate_for_week(week_start, week_einde, parameter_infeed)
            week_nr = week_start.strftime("%V")
            print(f"Week {i} (W{week_nr}): {aantal} rapporten aangemaakt")
            totaal_aangemaakt += aantal
        except Exception as e:
            print(f"Fout bij genereren rapporten voor week {i}: {e}")

    return totaal_aangemaakt


def genereer_rapporten_view(request):
    aantal = genereer_dso_rapporten_voor_laatste_n_weken(n_weken=3)
    messages.success(request, f"Nieuwe weekrapporten aangemaakt: {aantal}.")
    return redirect("monitoring:rapport_lijst")



# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD - FREQUENTIE OPHALEN UIT API & INFEED OPHALEN UIT DATABASE
# ─────────────────────────────────────────────────────────────────────────────

FREQUENTIE_API_URL = "https://opendata.elia.be/api/records/1.0/search/"
FREQUENTIE_MAX_RESULTATEN = 500
FREQUENTIE_BATCH = 100

# functie om bij refresh van de pagina de laatste frequentiegegevens op te halen uit de Elia API en op te slaan in de database
def _refresh_frequentie_data(sensor_freq, parameter_freq):
    should_fetch = True
    offset = 0 # houdt bij hoeveel resultaten we al hebben opgehaald, voor de paginering van de API
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
            print("Fout bij ophalen frequentie-API")
            break

        if response.status_code != 200:
            print(f"Frequentie-API antwoordde met status {response.status_code}")
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

# er wordt eerst gecontroleerd of er al een meting bestaat voor deze sensor, parameter en tijdstip
# als er al een meting bestaat voor deze sensor, parameter en tijdstip, dan maken we geen nieuwe aan
        _, created = Meting.objects.get_or_create(
            sensor=sensor_freq,
            parameter=parameter_freq,
            tijdstip=tijdstip,
            defaults={"waarde": float(waarde)},
        )
        if created:
            aangemaakt += 1

    return aangemaakt


# functie om de infeed-waarden op te halen uit de database en klaar te maken voor weergave in het dashboard
def _get_infeed_rows(parameter_infeed):
    if not parameter_infeed:
        return []

    sensors = (
        Sensor.objects
        .filter(type="Infeed-sensor", status="actief")
        .select_related("net", "infrastructuur") # select_related zorgt voor een snelle SQL-join
    )

    rows = []
    for s in sensors:
        try:
            waarde = float(s.laatste_waarde) if s.laatste_waarde is not None else None
        except (TypeError, ValueError):
            waarde = None

        spanningsniveau = s.net.spanningsniveau if s.net else "–"

        rows.append({
            "ean_code": s.sensor_id,
            "region": s.infrastructuur.locatie if s.infrastructuur else "–",  # We halen de regio uit het gekoppelde Infrastructuur
            "location": s.location,
            "injection_station": s.station,
            "dso": s.infrastructuur.beheerder if s.infrastructuur else "–",
            "voltage_level": spanningsniveau,
            "infeed_value": waarde,
            "tijdstip": s.laatste_tijdstip,
            "waarde": waarde,
            "is_teruglevering": waarde is not None and waarde < 0,
            "sensor_id": s.sensor_id,
            "station": s.station,
        })

    return rows

# ─────────────────────────────────────────────────────────────────────────────
# LANDINGSPAGINA
# ─────────────────────────────────────────────────────────────────────────────

class LandingPageView(generic.TemplateView):
    template_name = 'monitoring/index.html'


# ─────────────────────────────────────────────────────────────────────────────
# HULPFUNCTIE: ONDERHOUD
# ─────────────────────────────────────────────────────────────────────────────

# functie om bij het laden van het dashboard automatisch onderhoudstaken uit te voeren, zoals het verversen van de frequentiegegevens en het aanvullen van ontbrekende infeedmetingen
def voer_dashboard_onderhoud_uit(parameter_infeed, parameter_freq):
# Ververs frequentiegegevens uit de Elia API en sla deze op in de database
    try:
        sensor_freq, _ = Sensor.objects.get_or_create(
            sensor_id='ELIA_FREQ', defaults={"type": "Frequentiesensor", "status": "actief"}
        )
        nieuw = _refresh_frequentie_data(sensor_freq, parameter_freq)
        if nieuw:
            print(f"[Onderhoud] {nieuw} nieuwe frequentiemetingen opgeslagen")
    except Exception as e:
        print(f"[Onderhoud] Fout bij verversen frequentie: {e}")

# Controleer of er al voldoende infeedmetingen zijn in de database, zo niet, vul dan de laatste 30 dagen aan met gegenereerde data
    if Meting.objects.filter(parameter=parameter_infeed).count() < 2880:
        vul_laatste_30_dagen_infeed_aan(parameter_infeed)

# Vul ontbrekende kwartiermetingen aan voor alle actieve Infeed-sensoren, vanaf de laatste meting tot nu
    for s in Sensor.objects.all().select_related("net"):
        inhalen_kwartiermetingen(s, parameter_infeed, max_aantal=2880)


# ─────────────────────────────────────────────────────────────────────────────
# DASHBOARD VIEW
# ─────────────────────────────────────────────────────────────────────────────

class DashboardView(generic.TemplateView):
    template_name = 'monitoring/dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # ophalen of aanmaken van de Meetparameter objecten voor frequentie, infeedvalue en totalload
        # [0] zorgt ervoor dat we het Meetparameter object krijgen, niet de boolean of tuple die get_or_create teruggeeft
        p_freq = Meetparameter.objects.get_or_create(naam='frequentie')[0]
        p_infeed = Meetparameter.objects.get_or_create(naam='infeedvalue')[0]
        p_load = Meetparameter.objects.get_or_create(naam='totalload')[0]

        # voer onderhoudstaken uit
        voer_dashboard_onderhoud_uit(p_infeed, p_freq)

        # Gebruik de constanten voor de drempelwaarden
        freq_min = FREQ_MIN_STANDAARD
        freq_max = FREQ_MAX_STANDAARD

        # Frequentie rijen
        meting_rows = []
        for m in Meting.objects.filter(parameter=p_freq).select_related("sensor").order_by("-tijdstip")[:20]:
            waarde = float(m.waarde) if m.waarde is not None else None
            meting_rows.append({
                "tijdstip": m.tijdstip,
                "waarde": waarde,
                "in_spec": waarde is not None and freq_min <= waarde <= freq_max,
                "sensor_id": m.sensor.sensor_id if m.sensor else "–",
            })

        # Infeed samenvatting
        infeed_rows = _get_infeed_rows(p_infeed)
        dso_samenvatting = {}
        for row in infeed_rows:
            dso = row["dso"]
            entry = dso_samenvatting.setdefault(dso, {"totaal_mw": 0.0, "aantal_stations": 0, "teruglevering": 0})
            if row["waarde"] is not None:
                entry["totaal_mw"] += row["waarde"]
                entry["aantal_stations"] += 1
                if row["is_teruglevering"]:
                    entry["teruglevering"] += 1

        # Total load rijen
        laatste_load_metingen = []
        for m in Meting.objects.filter(parameter=p_load).select_related("sensor").order_by("-tijdstip")[:20]:
            waarde = float(m.waarde) if m.waarde is not None else None
            laatste_load_metingen.append({
                "tijdstip": m.tijdstip,
                "waarde": waarde,
                "sensor_id": m.sensor.sensor_id if m.sensor else "–",
            })

        context.update({
            "laatste_metingen": meting_rows,
            "infeed_rows": infeed_rows,
            "dso_samenvatting": dso_samenvatting.items(),
            "totaal_infeed_mw": sum(r["waarde"] for r in infeed_rows if r["waarde"] is not None),
            "aantal_teruglevering": sum(1 for r in infeed_rows if r["is_teruglevering"]),
            "sensoren_totaal": Sensor.objects.count(),
            "laatste_load_metingen": laatste_load_metingen,
            "totaal_load_mw": laatste_load_metingen[0]["waarde"] if laatste_load_metingen else None,
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
        return Sensor.objects.select_related(
            'net',
            'infrastructuur'
        ).order_by('sensor_id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        sensoren = context['sensoren']

        context['aantal_inactief'] = sensoren.filter(status='inactief').count()

        context['aantal_actief'] = sensoren.filter(status='actief').count()

        return context


class SensorDetailView(generic.DetailView):
    template_name = 'monitoring/sensor_detail.html'
    model = Sensor
    context_object_name = 'sensor'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sensor = self.get_object()

        parameter_infeed = Meetparameter.objects.filter(naam="infeedvalue").first()
        if parameter_infeed and sensor.status == "actief" and sensor.type == "Infeed-sensor":
            toegevoegd = inhalen_kwartiermetingen(sensor, parameter_infeed, max_aantal=500)
            if toegevoegd:
                print(f"[SensorDetail] inhalen {sensor.sensor_id}: +{toegevoegd} kwartiermetingen")

        # laatste metingen ophalen
        metingen = []
        for m in sensor.metingen.select_related("parameter").order_by("-tijdstip")[:50]:
            param = m.parameter

            metingen.append({
                "tijdstip": m.tijdstip,
                "waarde": m.waarde,
                "parameter_naam": param.naam if param else "onbekend",
                "eenheid": param.eenheid if param else "",
            })

    # BOKEH GRAFIEK ANALYSE LAATSTE 7 DAGEN
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


                if pd.api.types.is_datetime64tz_dtype(df["tijdstip"]):
                    df["tijdstip"] = df["tijdstip"].dt.tz_convert("UTC").dt.tz_localize(None)

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
            print(f"Bokeh sensor-analyse mislukt voor {sensor.sensor_id}")

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
    aantal_aangemaakt = 0
    aantal_bijgewerkt = 0
    fouten = []

    # Pagination instellingen
    limit = 100
    offset = 0
    alle_resultaten = []
    doorgaan = True

    # Stap 1: Haal alle data op via paginering
    while doorgaan:
        try:
            params = {"limit": limit, "offset": offset}
            with requests.get(url, params=params, timeout=30) as response:
                if response.status_code == 200:
                    data = response.json()
                    batch = data.get("results", [])
                    alle_resultaten.extend(batch)

                    # Als we minder resultaten krijgen dan de limit, zijn we klaar
                    if len(batch) < limit:
                        doorgaan = False
                    else:
                        offset += limit  # Volgende ronde 100 records verder
                else:
                    messages.error(request, f"API fout: {response.status_code}")
                    return redirect("monitoring:dashboard")
        except Exception as e:
            messages.error(request, f"Fout bij API call: {e}")
            return redirect("monitoring:dashboard")

    # Stap 2: Verwerk alle verzamelde resultaten
    parameter_definities = [
        ("infeedvalue", "infeedvalue", "MW"),
        ("actualfrequency", "frequentie", "Hz"),
        ("totalload", "totalload", "MW"),
        ("load", "totalload", "MW"),
    ]


    for r in alle_resultaten:
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
                    "status": "actief",
                    "station": injection_station,
                    "location": r.get("location") or "",
                }
            )
            if created:
                aantal_aangemaakt += 1
            else:
                aantal_bijgewerkt += 1

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
                    }
                )

                Meting.objects.update_or_create(
                    sensor=obj,
                    parameter=parameter_obj,
                    tijdstip=tijdstip,
                    defaults={
                        "waarde": waarde,
                    },
                )

                if parameter_naam == "infeedvalue":
                    obj.laatste_waarde = waarde
                    obj.laatste_tijdstip = tijdstip
                    obj.save(update_fields=['laatste_waarde', 'laatste_tijdstip'])

        except Exception as e:
            fouten.append(f"Sensor {ean}: {e}")
            print(f"Fout bij importeren sensor {ean}: {e}")

    if fouten:
        for fout in fouten:
            messages.warning(request, fout)
    messages.success(
        request,
        f"Import klaar. Aangemaakt: {aantal_aangemaakt}, Bijgewerkt: {aantal_bijgewerkt}, Fouten: {len(fouten)}",
    )
    return redirect("monitoring:dashboard")
