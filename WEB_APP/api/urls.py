from django.urls import path
from .views import *

urlpatterns = [
    path('dashboard/', APIDashboardView.as_view(), name='api_dashboard'),
    path('serve-file/', serve_file, name='serve-file'),

    path('fetch_history_data/', fetch_history_data, name='fetch_history_data'),
    path('add_vehicle/', add_vehicle, name='add_vehicle'),

    path('health_status/', health_status, name='health_status'),
    
    # New endpoints for state and emergency management
    path('get_current_status/', get_current_status, name='get_current_status'),
    path('reset_system/', reset_system, name='reset_system'),
    
    path('live-ip-camera', live_ip_camera, name='live-ip-camera'),
]