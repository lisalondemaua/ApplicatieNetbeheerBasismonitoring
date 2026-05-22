from django.utils import timezone
from model_bakery.recipe import Recipe, seq

from monitoring.models import Net, Infrastructuur, Sensor, Meetparameter, Meting, Operator, Rapport


# ─────────────────────────────────────────────────────────────────────────────
# NET
# ─────────────────────────────────────────────────────────────────────────────
net = Recipe(
    Net,
    net_id=seq("NET_", increment_by=1),
    type="Distributie",
    spanningsniveau=150.0,
    freq_min=49.50,
    freq_max=50.50,
)

# ─────────────────────────────────────────────────────────────────────────────
# INFRASTRUCTUUR
# ─────────────────────────────────────────────────────────────────────────────
infra = Recipe(
    Infrastructuur,
    infrastructuur_id=seq("INFRA_", increment_by=1),
    naam=seq("Station ", increment_by=1),
    type="Substation",
    locatie="Antwerpen",
    status="actief",
    beheerder="Elia",
)

# ─────────────────────────────────────────────────────────────────────────────
# MEETPARAMETERS (namen matchen je code + zijn unique=True)
# ─────────────────────────────────────────────────────────────────────────────
parameter_freq = Recipe(
    Meetparameter,
    naam="frequentie",
    eenheid="Hz",
    drempel_onder=49.50,
    drempel_boven=50.50,
)

parameter_infeed = Recipe(
    Meetparameter,
    naam="infeedvalue",
    eenheid="MW",
    drempel_onder=-999999999.0,
    drempel_boven=999999999.0,
)

parameter_load = Recipe(
    Meetparameter,
    naam="totalload",
    eenheid="MW",
    drempel_onder=0.0,
    drempel_boven=15000.0,
)

# (optioneel) Als je écht nog een spanningsparameter nodig hebt
parameter_spanning = Recipe(
    Meetparameter,
    naam="spanning",
    eenheid="V",
    drempel_onder=220.0,
    drempel_boven=240.0,
)

# ─────────────────────────────────────────────────────────────────────────────
# SENSOR
# (Voor losse tests. Voor jouw "API-sensoren behouden" ga je meestal Sensor.objects.all() gebruiken.)
# ─────────────────────────────────────────────────────────────────────────────
sensor = Recipe(
    Sensor,
    sensor_id=seq("SENSOR_", increment_by=1),
    type="Generieke sensor",
    communicatie_protocol="NB-IoT",
    status="actief",
    station=seq("INJ_", increment_by=1),
    location="Antwerpen",
    region="Flanders",
)

# ─────────────────────────────────────────────────────────────────────────────
# METING
# sensor/parameter/waarde/tijdstip ga je meestal overschrijven in je test_models.py
# ─────────────────────────────────────────────────────────────────────────────
meting = Recipe(
    Meting,
    tijdstip=timezone.now,
    waarde=seq(50.0, increment_by=0.01),
    kwaliteit="in_spec",
)

# ─────────────────────────────────────────────────────────────────────────────
# OPERATOR
# ─────────────────────────────────────────────────────────────────────────────
operator = Recipe(
    Operator,
    medewerker_id=seq("MED_", increment_by=1),
    naam=seq("Operator ", increment_by=1),
    rol="beheerder",
    emailadres=seq("operator", increment_by=1, suffix="@elia.be"),
    telefoonnummer=seq("048000000", increment_by=1),
    actief=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# RAPPORT
# operator ga je meestal overschrijven bij het aanmaken in tests
# ─────────────────────────────────────────────────────────────────────────────
rapport = Recipe(
    Rapport,
    rapport_id=seq("RPT_", increment_by=1),
    titel=seq("Netkwaliteitsrapport ", increment_by=1),
    periode_start=timezone.now,
    periode_einde=timezone.now,
    inhoud="Automatisch gegenereerd testrapport.",
)