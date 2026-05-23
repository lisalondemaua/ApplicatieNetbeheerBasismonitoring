from django.urls import path
from . import views

app_name = "monitoring"

urlpatterns = [
    path('', views.LandingPageView.as_view(), name='index'),
    path('dashboard/', views.DashboardView.as_view(), name='dashboard'),
    path('sensoren/', views.SensorListView.as_view(), name='sensor_lijst'),
    path('sensor/<int:pk>/', views.SensorDetailView.as_view(), name='sensor_detail'), # overbodig??
    path('importeer-sensoren/', views.importeer_sensors_api_view, name='importeer_sensoren'),
    path('rapporten/', views.RapportListView.as_view(), name='rapport_lijst'),
    path('rapport/<str:rapport_id>/', views.RapportDetailView.as_view(), name='rapport_detail'),
]