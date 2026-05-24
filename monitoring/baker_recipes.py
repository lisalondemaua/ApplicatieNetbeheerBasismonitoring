from django.utils import timezone
from model_bakery.recipe import Recipe, seq

from monitoring.models import Net, Infrastructuur, Sensor, Meetparameter, Meting, Rapport


net = Recipe(
    Net,
    net_id=seq("NET_", increment_by=1),
    type="Distributie",
    spanningsniveau=150.0,
    freq_min=49.50,
    freq_max=50.50,
)

infra = Recipe(
    Infrastructuur,
    infrastructuur_id=seq("INFRA_", increment_by=1),
    naam=seq("Station ", increment_by=1),
    type="Substation",
    locatie="Antwerpen",
    status="actief",
    beheerder="Elia",
)

parameter_freq = Recipe(
    Meetparameter,
    naam="frequentie",
    eenheid="Hz",
)

parameter_infeed = Recipe(
    Meetparameter,
    naam="infeedvalue",
    eenheid="MW",
)

parameter_load = Recipe(
    Meetparameter,
    naam="totalload",
    eenheid="MW",
)

parameter_spanning = Recipe(
    Meetparameter,
    naam="spanning",
    eenheid="V",
)

sensor = Recipe(
    Sensor,
    sensor_id=seq("SENSOR_", increment_by=1),
    type="Generieke sensor",
    status="actief",
    station=seq("INJ_", increment_by=1),
    location="Antwerpen",
)


meting = Recipe(
    Meting,
    tijdstip=timezone.now,
    waarde=seq(50.0, increment_by=0.01),
)


rapport = Recipe(
    Rapport,
    rapport_id=seq("RPT_", increment_by=1),
    titel=seq("Netkwaliteitsrapport ", increment_by=1),
    periode_start=timezone.now,
    periode_einde=timezone.now,
    inhoud="Automatisch gegenereerd testrapport.",
)