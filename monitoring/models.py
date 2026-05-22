from django.db import models

class Net(models.Model):
    net_id = models.CharField(max_length=50, unique=True) # unique=True zodat er geen dubbele netten kunnen worden aangemaakt
    type = models.CharField(max_length=50)
    spanningsniveau = models.FloatField()
    freq_min = models.FloatField(default=49.5)
    freq_max = models.FloatField(default=50.5)

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
    communicatie_protocol = models.CharField(max_length=32, default='NB-IoT')
    status = models.CharField(max_length=32, default='actief')

    # Nieuwe velden uit de Elia API - bevatten stationsmetadata
    station = models.CharField(max_length=100, blank=True, default='')
    location = models.CharField(max_length=200, blank=True, default='')
    region = models.CharField(max_length=50, blank=True, default='')

    def __str__(self):
        return f"Sensor {self.sensor_id} ({self.type})"

    class Meta:
        verbose_name_plural = "Sensoren"

class Meetparameter(models.Model):
    naam = models.CharField(max_length=32)
    eenheid = models.CharField(max_length=10)
    drempel_onder = models.FloatField()
    drempel_boven = models.FloatField()

    def __str__(self):
        return f"{self.naam} ({self.eenheid})"

    class Meta:
        verbose_name_plural = "Meetparameters"


class Meting(models.Model):
    meting_id = models.AutoField(primary_key=True) # AutoField zorgt ervoor dat dit veld automatisch een unieke waarde krijgt bij het aanmaken van een nieuwe meting, primary_key=True maakt dit veld de primaire sleutel van het model
    tijdstip = models.DateTimeField(auto_now_add=True)
    waarde = models.FloatField()
    kwaliteit = models.CharField(max_length=20, default='in_spec')
    sensor = models.ForeignKey(Sensor, on_delete=models.CASCADE, related_name="metingen", null=True, blank=True)
    parameter = models.ForeignKey(Meetparameter, on_delete=models.CASCADE, related_name="metingen", blank=True, null=True)
    infeed_value = models.FloatField(default=0)

    def __str__(self):
        return f"Meting {self.meting_id}: {self.waarde} @ {self.tijdstip}"

    class Meta:
        verbose_name_plural = "Metingen"
        ordering = ["-tijdstip"]


class Netbelasting(models.Model):
    netbelasting_id = models.AutoField(primary_key=True)
    tijdstip_meting = models.DateTimeField()
    spanning = models.FloatField(null=True, blank=True)
    frequentie = models.FloatField(null=True, blank=True)
    sensor = models.ForeignKey(Sensor, on_delete=models.CASCADE, related_name="netbelastingen", blank=True, null=True)

    def __str__(self):
        return f"Netbelasting {self.netbelasting_id} op {self.tijdstip_meting}"

    class Meta:
        verbose_name_plural = "Netbelastingen"

class Operator(models.Model):
    medewerker_id = models.CharField(max_length=50, unique=True)
    naam = models.CharField(max_length=100)
    rol = models.CharField(max_length=50, choices=[("beheerder", "Beheerder"),("technicus", "Technicus"),("monitor", "Monitor"),] )
    emailadres = models.EmailField()
    telefoonnummer = models.CharField(max_length=20, blank=True)
    actief = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.naam} ({self.rol})"

    class Meta:
        verbose_name = "operator"
        verbose_name_plural = "operators"


class Rapport(models.Model):
    rapport_id = models.CharField(max_length=50, unique=True)
    titel = models.CharField(max_length=200)
    aangemaakt_op = models.DateTimeField(auto_now_add=True)
    periode_start = models.DateTimeField()
    periode_einde = models.DateTimeField()
    inhoud = models.TextField()
    operator = models.ForeignKey(Operator, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.titel} ({self.rapport_id})"

    class Meta:
        verbose_name = "rapport"
        verbose_name_plural = "rapporten"


