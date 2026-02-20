import json
import os
import shutil

from django.contrib.auth.decorators import login_required
from django.db.models import Max, Subquery, OuterRef, Value, DateTimeField
from django.http import JsonResponse, HttpResponse, FileResponse
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.generic import TemplateView
from django.views.decorators.csrf import csrf_exempt
from django.db.models.functions import Greatest
from django.utils.timezone import is_aware, make_naive, make_aware
from django.contrib import messages

from api.models import *
from accounts.decorators import password_expiry_required
from datetime import datetime
from urllib.parse import unquote, urlparse

# Create your views here.

def to_naive(dt):
    if dt is None:
        return None
    return make_naive(dt) if is_aware(dt) else dt

def max_datetime(a, b):
    a = to_naive(a)
    b = to_naive(b)

    if a is None and b is None:
        return None
    if a is None:
        return b
    if b is None:
        return a
    return a if a > b else b

def parse_last_ping(value):
    if not value:
        return None
    try:
        dt = make_naive(value) if is_aware(value) else value
        return dt.strftime("%d-%m-%Y %I:%M%p")
    except ValueError:
        return None
    
@method_decorator([login_required, password_expiry_required], name='dispatch')
class APIDashboardView(TemplateView):
    template_name = 'dashboards/api_dashboard.html'

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['message'] = 'Welcome to the API Dashboard'
        return context
    
def serve_file(request):
    # Get the full URL parameter
    file_param = request.GET.get('file')
    if not file_param:
        return HttpResponse("File parameter missing", status=400)

    # Remove query string if any (like ?t=...)
    parsed = urlparse(file_param)
    file_path = unquote(parsed.path)  # decode spaces and URL chars

    # Check if file exists
    if os.path.exists(file_path):
        try:
            # Determine MIME type based on extension
            ext = os.path.splitext(file_path)[1].lower()
            content_type = 'image/jpeg' if ext in ['.jpg', '.jpeg'] else 'application/octet-stream'
            return FileResponse(open(file_path, 'rb'), content_type=content_type)
        except Exception as e:
            return HttpResponse(f"Error reading file: {str(e)}", status=500)
    else:
        # Return a placeholder response instead of 404
        return HttpResponse(f"Image file not found on this server: {os.path.basename(file_path)}", 
                        status=404, content_type='text/plain')

@csrf_exempt
def fetch_history_data(request):
    bunch_id = request.GET.get('bunch_id')

    queryset = (
        VEHICLE_LOGS.objects.filter(
        )
        .values()
        .order_by("capture_time")
    )

    results = []
    for idx, row in enumerate(queryset, start=1):
        results.append({
            "sno": idx,
            "anode_number": row["anode_number"],
            "bunch_name": row["bunch_number"],
            "capture_time": row["capture_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "view_image": f"/api/serve-file?file={row['image_path']}" if row["image_path"] else None,
            "view_image": f"/api/serve-file?file={row['image_path']}" if row["image_path"] else None,
            "view_image": f"/api/serve-file?file={row['image_path']}" if row["image_path"] else None,
        })

    return JsonResponse({
        "data": results,
        "bunch_id": bunch_id,
    })

@csrf_exempt
def health_status(request):
    queryset = (
        HEALTH_STATUS.objects.filter()
        .values()
        .order_by("ip")
    )
    results = {}
    devices = {}
    for idx, row in enumerate(queryset, start=1):
        if row["location"] not in list(devices.keys()):
            devices[row["location"]] = {}
            if row["device_type"] not in devices[row["location"]]:
                devices[row["location"]][row["device_type"]] = [row["ip"]]
            else:
                devices[row["location"]][row["device_type"]].append(row["ip"])
        else:
            if row["device_type"] not in devices[row["location"]]:
                devices[row["location"]][row["device_type"]] = [row["ip"]]
            else:
                devices[row["location"]][row["device_type"]].append(row["ip"])

        results[f'{row["location"]} {row["device_type"]} {len(devices[row["location"]][row["device_type"]])}'] = {
            "type": row.get("device_type"),
            "ip": row.get("ip"),
            "cam_serial": row.get("camera_serial_number") or None,
            "status": row.get("status"),
            "last_ping": parse_last_ping(row.get("last_ping")),
            "top": row.get("top"),
            "left": row.get("left"),
        }

    return JsonResponse({
        "data": results,
    })
