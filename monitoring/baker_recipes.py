from model_bakery.recipe import Recipe, seq
from monitoring.models import Net, Infrastructuur, Sensor, Meetparameter, Meting, Netbelasting, Afwijking, Operator, Rapport

# Net
net = Recipe(
    Net,
    net_id=seq('NET_', increment_by=1),
    type='Distributie',
    spanningsniveau=150.0,
    freq_min=49.99,
    freq_max=50.01,
)

# Infrastructuur
infra = Recipe(
    Infrastructuur,
    infrastructuur_id=seq('INFRA_', increment_by=1),
    naam=seq('Station ', increment_by=1),
    type="Substation",
    locatie="Antwerpen",
    status="actief",
    beheerder="Elia"
)

# Meetparameters frequentie en spanning
parameter_freq = Recipe(
    Meetparameter,
    naam="Frequentie",
    eenheid="Hz",
    drempel_onder=49.8,
    drempel_boven=50.2
)
parameter_spanning = Recipe(
    Meetparameter,
    naam="Spanning",
    eenheid="V",
    drempel_onder=220,
    drempel_boven=240
)

# Sensor
sensor = Recipe(
    Sensor,
    sensor_id=seq('SENSOR_', increment_by=1),
    type='Frequentie',
    communicatie_protocol='NB-IoT',
    status='actief',
)

# Meting
meting = Recipe(
    Meting,
    tijdstip='2026-05-01T10:00:00Z',
    waarde=seq(50.0, increment_by=0.01),
    kwaliteit='in_spec',
)

# Netbelasting
netbelasting = Recipe(
    Netbelasting,
    tijdstip_meting='2026-05-01T10:05:00Z',
    spanning=seq(230.0, increment_by=0.5),
    frequentie=seq(50.0, increment_by=0.01),
)

# Operator
operator = Recipe(
    Operator,
    medewerker_id=seq('MED_', increment_by=1),
    naam=seq('Operator ', increment_by=1),
    rol="beheerder",
    emailadres=seq("operator", increment_by=1, suffix="@elia.be"),
    telefoonnummer=seq('048000000', increment_by=1),
    actief=True
)

# Rapport
rapport = Recipe(
    Rapport,
    rapport_id=seq('RPT_', increment_by=1),
    titel=seq('Netkwaliteitsrapport ', increment_by=1),
    periode_start='2026-05-01T00:00:00Z',
    periode_einde='2026-05-02T00:00:00Z',
    inhoud="Automatisch gegenereerd testrapport.",
)

# Afwijking
afwijking = Recipe(
    Afwijking,
    type="hoogspanningsdip",
    omschrijving="Testafwijking door bakery",
    duur=10.0,
    begintijd='2026-05-01T11:00:00Z',
    eindtijd='2026-05-01T11:00:10Z',
)