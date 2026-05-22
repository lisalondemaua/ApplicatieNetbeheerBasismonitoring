from django.contrib import admin
from .models import Net, Infrastructuur, Meetparameter, Netbelasting, Operator, Sensor, Meting, Rapport

class MetingInline(admin.TabularInline):
    model = Meting
    extra = 0

class SensorAdmin(admin.ModelAdmin):
    list_display = ['sensor_id', 'type', 'status']
    search_fields = ['sensor_id', 'type']
    inlines = [MetingInline]

class RapportAdmin(admin.ModelAdmin):
    list_display = ['rapport_id', 'titel', 'aangemaakt_op', 'operator']
    search_fields = ['rapport_id', 'titel']

admin.site.register(Net)
admin.site.register(Infrastructuur)
admin.site.register(Meetparameter)
admin.site.register(Netbelasting)
admin.site.register(Operator)
admin.site.register(Sensor, SensorAdmin)
admin.site.register(Meting)
admin.site.register(Rapport, RapportAdmin)

