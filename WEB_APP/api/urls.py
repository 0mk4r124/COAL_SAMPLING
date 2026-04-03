from django.urls import path
from .views import *

urlpatterns = [
    path('dashboard/', APIDashboardView.as_view(), name='api_dashboard'),
    path('serve-file/', serve_file, name='serve-file'),

    path('fetch_history_data/', fetch_history_data, name='fetch_history_data'),
    path('api/download_history_data/', download_history_data, name='download_history_data'),
    path('add_vehicle/', add_vehicle, name='add_vehicle'),
    path('api/vehicle_master/', vehicle_master, name='vehicle_master'),
    path('api/edit_vehicle_master/', edit_vehicle_master, name='edit_vehicle_master'),
    path('api/upload_vehicle_master/', upload_vehicle_master, name='upload_vehicle_master'),
    path('api/download_vehicle_master/', download_vehicle_master, name='download_vehicle_master'),

    path('health_status/', health_status, name='health_status'),
    
    # New endpoints for state and emergency management
    path('get_current_status/', get_current_status, name='get_current_status'),
    path('reset_system/', reset_system, name='reset_system'),
    
    path('live-ip-camera', live_ip_camera, name='live-ip-camera'),
]