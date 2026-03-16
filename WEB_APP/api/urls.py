from django.urls import path
from .views import *

urlpatterns = [
    path('dashboard/', APIDashboardView.as_view(), name='api_dashboard'),

    path('fetch_history_data/', fetch_history_data, name='fetch_history_data'),
    path('check_for_vehicle/', check_for_vehicle, name='check_for_vehicle'),
    path('add_vehicle/', add_vehicle, name='add_vehicle'),

    path('health_status/', health_status, name='health_status'),
]