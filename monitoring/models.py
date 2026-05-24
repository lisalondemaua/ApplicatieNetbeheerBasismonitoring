from django.db import models
from django.utils import timezone

class Net(models.Model):
    net_id = models.CharField(max_length=50, unique=True) # unique=True zodat er geen dubbele netten kunnen worden aangemaakt
    type = models.CharField(max_length=50)
    spanningsniveau = models.FloatField()

# Bepaalt hoe het object wordt weergegeven in de admin interface en andere contexten --> Bijvoorbeeld 'Net 1 (Hoogspanningsnet)'
    def __str__(self):
        return f"{self.net_id} ({self.type})"

# Bepaalt de meervoudsvorm van het model in de admin interface --> Bijvoorbeeld 'Netten' in plaats van 'Nets'
    class Meta:
        verbose_name_plural = "Netten"


class Infrastructuur(models.Model):
    infrastructuur_id = models.CharField(max_length=50, unique=True)
    naam = models.CharField(max_length=150)
    type = models.CharField(max_length=150)
    locatie = models.CharField(max_length=150)
    status = models.CharField(max_length=150, default="actief", blank=True) # blank=True maakt veld optioneel, default="actief" zodat nieuwe infrastructuren standaard actief zijn
    beheerder = models.CharField(max_length=150, blank=True, null=True) # null=True laat lege waarde toe in databank

    def __str__(self):
        return f"{self.naam} ({self.type})"

    class Meta:
        verbose_name_plural = "Infrastructuren"


class Sensor(models.Model):
    sensor_id = models.CharField(max_length=150, unique=True)
    type = models.CharField(max_length=32)
    net = models.ForeignKey(Net, on_delete=models.CASCADE, related_name="sensoren", blank=True, null=True)
    # ForeignKey naar Infrastructuur zodat een sensor aan een specifieke infrastructuur kan worden gekoppeld
    # on_delete=models.CASCADE zorgt ervoor dat als een net wordt verwijderd, de bijbehorende sensoren ook worden verwijderd
    # related_name="sensoren" maakt het mogelijk om via een net object alle bijbehorende sensoren op te halen
    infrastructuur = models.ForeignKey(Infrastructuur, on_delete=models.CASCADE, related_name="sensoren", blank=True, null=True)
    status = models.CharField(max_length=32, default='actief')

    # Nieuwe velden uit de Elia API - bevatten stationsmetadata
    station = models.CharField(max_length=100, blank=True, default='')
    location = models.CharField(max_length=200, blank=True, default='')

    laatste_waarde = models.FloatField(blank=True, null=True)
    laatste_tijdstip = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return f"Sensor {self.sensor_id} ({self.type})"

    class Meta:
        verbose_name_plural = "Sensoren"

class Meetparameter(models.Model):
    naam = models.CharField(max_length=32, unique=True)
    eenheid = models.CharField(max_length=10)

    def __str__(self):
        return f"{self.naam} ({self.eenheid})"

    class Meta:
        verbose_name_plural = "Meetparameters"


class Meting(models.Model):
    tijdstip = models.DateTimeField(default=timezone.now, db_index=True)
    waarde = models.FloatField()
    sensor = models.ForeignKey(Sensor, on_delete=models.CASCADE, related_name="metingen")
    parameter = models.ForeignKey(Meetparameter, on_delete=models.CASCADE, related_name="metingen")

    def __str__(self):
        return f"Meting {self.id}: {self.waarde} @ {self.tijdstip}"

    class Meta:
        verbose_name_plural = "Metingen"
        ordering = ["-tijdstip"]


class Rapport(models.Model):
    rapport_id = models.CharField(max_length=50, unique=True)
    titel = models.CharField(max_length=200)
    aangemaakt_op = models.DateTimeField(auto_now_add=True)
    periode_start = models.DateTimeField()
    periode_einde = models.DateTimeField()
    inhoud = models.TextField()


    def __str__(self):
        return f"{self.titel} ({self.rapport_id})"

    class Meta:
        verbose_name = "rapport"
        verbose_name_plural = "rapporten"
