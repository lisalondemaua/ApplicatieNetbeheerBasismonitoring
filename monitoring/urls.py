from django.urls import path
from . import views

app_name = "monitoring"

urlpatterns = [
    path('', views.LandingPageView.as_view(), name='index'),
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    path('sensoren/', views.SensorListView.as_view(), name='sensor_lijst'),
    path('sensor/<int:pk>/', views.SensorDetailView.as_view(), name='sensor_detail'),
    path('sensoren/zoeken/', views.SensorZoekenView.as_view(), name='sensor_zoeken'),
    path('importeer-sensoren/', views.importeer_sensors_api_view, name='importeer_sensoren'),
    path('sensoren/analyse/', views.SensorAnalyseView.as_view(), name='sensor_analyse'),
    path('afwijkingen/', views.AfwijkingenListView.as_view(), name='afwijkingen_lijst'),
    path('rapporten/', views.RapportListView.as_view(), name='rapport_lijst'),
    path('rapport/<str:rapport_id>/', views.RapportDetailView.as_view(), name='rapport_detail'),
]
