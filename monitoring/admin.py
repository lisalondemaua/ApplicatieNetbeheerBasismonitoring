from django.contrib import admin
from .models import Net, Infrastructuur, Meetparameter, Sensor, Meting, Rapport

class SensorAdmin(admin.ModelAdmin):
    list_display = ['sensor_id', 'type', 'status']
    search_fields = ['sensor_id', 'type']

class RapportAdmin(admin.ModelAdmin):
    list_display = ['rapport_id', 'titel', 'aangemaakt_op']
    search_fields = ['rapport_id', 'titel']

admin.site.register(Net)
admin.site.register(Infrastructuur)
admin.site.register(Meetparameter)
admin.site.register(Sensor, SensorAdmin)
admin.site.register(Meting)
admin.site.register(Rapport, RapportAdmin)

