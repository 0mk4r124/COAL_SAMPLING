from django.urls import path
from .views import *

urlpatterns = [
    path('dashboard/', APIDashboardView.as_view(), name='api_dashboard'),
    path('serve-file/', serve_file, name='serve-file'),

    path('fetch_history_data/', fetch_history_data, name='fetch_history_data'),
    path('download_history_data/', download_history_data, name='download_history_data'),
    path('add_vehicle/', add_vehicle, name='add_vehicle'),
    path('vehicle_master/', vehicle_master, name='vehicle_master'),
    path('edit_vehicle_master/', edit_vehicle_master, name='edit_vehicle_master'),
    path('upload_vehicle_master/', upload_vehicle_master, name='upload_vehicle_master'),
    path('download_vehicle_master/', download_vehicle_master, name='download_vehicle_master'),
    path('get_current_status/', get_current_status, name='get_current_status'),
    path('reset_system/', reset_system, name='reset_system'),
    path('send_print_data/', send_print_data, name='send_print_data'),
    path('print_current_vehicle/', print_current_vehicle, name='print_current_vehicle'),
    path('stop_print_job/', stop_print_job, name='stop_print_job'),
    path('retake_failed_cycle/', retake_failed_cycle, name='retake_failed_cycle'),
    path('api/ai_position_decision/', ai_position_decision, name='ai_position_decision'),

    path('health_status/', health_status, name='health_status'),
    path('live-ip-camera', live_ip_camera, name='live-ip-camera'),
]