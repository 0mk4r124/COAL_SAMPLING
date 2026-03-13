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
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    
    vehicle_number = request.GET.get('vehicle_number')
    vendor_name = request.GET.get('vendor_name')

    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except:
        return JsonResponse({'error': 'Invalid date format'}, status=400)

    queryset = VEHICLE_LOGS.objects.filter(
            create_time__date__gte=start_dt,
            create_time__date__lte=end_dt,
        ).annotate(

        ).order_by(
            "create_time"
        )
    
    if vehicle_number:
        queryset = queryset.filter(
            vehicle_number__icontains = vehicle_number,
        )
    if vendor_name:
        queryset = queryset.filter(
            vendor_name__icontains = vendor_name,
        )

    results = []
    for idx, row in enumerate(queryset, start=1):
        results.append({
            "sno": idx,
            "anode_number": row["anode_number"],
            "bunch_name": row["bunch_number"],
            "capture_time": row["create_time"].strftime("%Y-%m-%d %H:%M:%S"),
            "view_image": f"/api/serve-file?file={row['image_path']}" if row["image_path"] else None,
            "view_image": f"/api/serve-file?file={row['image_path']}" if row["image_path"] else None,
            "view_image": f"/api/serve-file?file={row['image_path']}" if row["image_path"] else None,
        })

    return JsonResponse({
        "data": results,
        "vehicle_number": vehicle_number,
        "vendor_name": vendor_name,
    })

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt
def check_for_vehicle(request):
    status = "new"
    vehicle_number = ""
    vendor_name = ""
    rfid_value = ""

    current_vehicle = VEHICLE_LOGS.objects.filter(status="INPROGRESS").first()

    if current_vehicle:
        rfids = (current_vehicle.rfids or "").split("|")

        for rfid in rfids:
            vehicle = VEHICLE_MASTER.objects.filter(rfid=rfid).first()

            if vehicle:
                vendor = VENDOR_MASTER.objects.filter(
                    vendor_code=vehicle.vendor_code
                ).first()

                status = "present"
                vehicle_number = vehicle.vehicle_number
                vendor_name = vendor.vendor_name if vendor else ""
                rfid_value = rfid
                break

    else:
        current_vehicle = VEHICLE_LOGS.objects.order_by("-create_time").first()
        status = "present"
        vehicle_number = "NOT_FOUND"
        vendor_name = "NOT_FOUND"

        if current_vehicle:
            rfids = (current_vehicle.rfids or "").split("|")

            for rfid in rfids:
                vehicle = VEHICLE_MASTER.objects.filter(rfid=rfid).first()

                if vehicle:
                    vendor = VENDOR_MASTER.objects.filter(
                        vendor_code=vehicle.vendor_code
                    ).first()

                    status = "present"
                    vehicle_number = vehicle.vehicle_number if vehicle else "NOT_FOUND"
                    vendor_name = vendor.vendor_name if vendor else "NOT_FOUND"
                    break

    return JsonResponse({
        "status": status,
        "vehicle_number": vehicle_number,
        "vendor_name": vendor_name,
        "rfid": rfid_value,
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
