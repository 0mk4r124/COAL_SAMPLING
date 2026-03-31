import json
import os
import shutil

from django.contrib.auth.decorators import login_required
from django.db.models import F, Func, Max, Subquery, OuterRef, Value, DateTimeField, CharField
from django.http import JsonResponse, HttpResponse, FileResponse
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.generic import TemplateView
from django.views.decorators.csrf import csrf_exempt
from django.db.models.functions import Greatest, Concat
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

def live_ip_camera(request):
    """API endpoint for all 4 IP camera live images"""
    try: 
        # camera_paths = [
        #     "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM1_REDUCED.jpg",
        #     "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM2_REDUCED.jpg",
        #     "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM3_REDUCED.jpg",
        # ]
        camera_paths = [
            "/home/deepali/OMKAR/CODES/COAL_SAMPLING/COAL_SAMPLING/TEMP_IMG/CAM1/CAM1_1774424054126.jpg",
            "/home/deepali/OMKAR/CODES/COAL_SAMPLING/COAL_SAMPLING/TEMP_IMG/CAM2/CAM2_1773298529500.jpg",
            "/home/deepali/OMKAR/CODES/COAL_SAMPLING/COAL_SAMPLING/TEMP_IMG/CAM3/CAM3_1774424056125.jpg",
        ]
        
        cameras = []
        for i, path in enumerate(camera_paths, start=1):
            if os.path.exists(path):
                cameras.append({
                    "camera_id": i,
                    "camera_name": f"Camera {i}",
                    "image_url": f'/api/serve-file?file={path}&t={timezone.now().timestamp()}',
                    "has_image": True,
                    "status": "online"
                })
            else:
                cameras.append({
                    "camera_id": i,
                    "camera_name": f"Camera {i}",
                    "image_url": "",
                    "has_image": False,
                    "status": "no_image"
                })
        
        return JsonResponse({
            "status": "success", 
            "cameras": cameras,
            "timestamp": timezone.now().isoformat()
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
def add_vehicle(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"}, status=405)

    try:
        data = json.loads(request.body)

        rfid = data.get("rfid")
        vehicle_number = data.get("vehicleNumber")
        vendor_name = data.get("vendorName")
        vendor_code = data.get("vendorCode")
        bucketNo = data.get("bucketNo")

        if not rfid or not vehicle_number or not vendor_code:
            return JsonResponse({
                "success": False,
                "error": "Missing required fields"
            }, status=400)

        # Create or update vendor
        vendor_obj, created = VENDOR_MASTER.objects.get_or_create(
            vendor_code=vendor_code,
            defaults={
                "vendor_name": vendor_name,
                "bucket_no": bucketNo,
                "create_time": timezone.now()
            }
        )

        # If vendor exists but name changed
        # Update only if explicitly provided (new vendor case)
        if vendor_name:
            if created:
                vendor_obj.vendor_name = vendor_name
                vendor_obj.bucket_no = bucketNo
            else:
                # Optional: update only if fields empty
                if not vendor_obj.vendor_name:
                    vendor_obj.vendor_name = vendor_name
                if not vendor_obj.bucket_no:
                    vendor_obj.bucket_no = bucketNo

            vendor_obj.save()

        # Create or update vehicle
        rfid_list = rfid.split("|")

        for r in rfid_list:
            VEHICLE_MASTER.objects.update_or_create(
                rfid=r.strip(),
                defaults={
                    "vehicle_number": vehicle_number,
                    "vendor_code": vendor_code,
                    "create_time": timezone.now()
                }
            )

        return JsonResponse({
            "success": True,
            "message": "Vehicle added/updated successfully",
            "vehicle_no": vehicle_number
        })

    except Exception as e:
        print(e)
        return JsonResponse({
            "success": False,
            "error": str(e)
        }, status=500)

# Custom LOCATE function for MySQL
class Locate(Func):
    function = 'LOCATE'
    arity = 2

@csrf_exempt
def fetch_history_data(request):
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    vehicle_number = request.GET.get('vehicle_number')
    vendor_name = request.GET.get('vendor_name')

    # ---- Date Parsing ----
    try:
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    except Exception:
        return JsonResponse({'error': 'Invalid date format'}, status=400)

    # ---- Subquery: Match RFID inside pipe-separated string ----
    vehicle_master_qs = VEHICLE_MASTER.objects.annotate(
        rfid_wrapped=Concat(
            Value('|'),
            F('rfid'),
            Value('|'),
            output_field=CharField()
        ),
        rfids_wrapped=Concat(
            Value('|'),
            OuterRef('rfids'),
            Value('|'),
            output_field=CharField()
        )
    ).annotate(
        match=Locate(F('rfid_wrapped'), F('rfids_wrapped'))
    ).filter(match__gt=0)

    # ---- Subquery: Vendor from vehicle ----
    vehicle_master_qs = VEHICLE_MASTER.objects.annotate(
        rfid_wrapped=Concat(
            Value('|'), F('rfid'), Value('|'),
            output_field=CharField()
        ),
        rfids_wrapped=Concat(
            Value('|'), OuterRef('rfids'), Value('|'),
            output_field=CharField()
        )
    ).annotate(
        match=Locate(F('rfid_wrapped'), F('rfids_wrapped'))
    ).filter(match__gt=0)

    vendor_master_qs = VENDOR_MASTER.objects.filter(
        vendor_code=OuterRef('vendor_code')
    )

    # ---- Main Query ----
    queryset = VEHICLE_LOGS.objects.filter(
        create_time__date__gte=start_dt,
        create_time__date__lte=end_dt,
    ).annotate(
        vehicle_number=Subquery(vehicle_master_qs.values('vehicle_number')[:1]),
        vendor_code=Subquery(vehicle_master_qs.values('vendor_code')[:1]),
    ).annotate(
        vendor_name=Subquery(vendor_master_qs.values('vendor_name')[:1]),
        bucket_no=Subquery(vendor_master_qs.values('bucket_no')[:1]),
    ).order_by("create_time")

    # ---- Filters (after annotation) ----
    if vehicle_number:
        queryset = queryset.filter(vehicle_number__icontains=vehicle_number)

    if vendor_name:
        queryset = queryset.filter(vendor_name__icontains=vendor_name)

    results = []
    for idx, row in enumerate(queryset, start=1):
        results.append({
            "sno": idx,
            "datetimestamp": row.create_time.strftime("%Y-%m-%d %H:%M:%S") if row.create_time else None,
            "vehicle_number": row.vehicle_number,
            "vendor_name": row.vendor_name,
            "vendor_code": row.vendor_code,
            "vehicle_image": f"/api/serve-file?file={row.vehicle_img_path}" if row.vehicle_img_path else None,
            "sample_1_image": f"/api/serve-file?file={row.sample_1_img_path}" if row.sample_1_img_path else None,
            "sample_2_image": f"/api/serve-file?file={row.sample_2_img_path}" if row.sample_2_img_path else None,
            "sample_3_image": f"/api/serve-file?file={row.sample_3_img_path}" if row.sample_3_img_path else None,
            "qr_code": f"/api/serve-file?file={row.QR_code_path}" if row.QR_code_path else None,
            "bucket_no": row.bucket_no,
        })

    return JsonResponse({
        "data": results,
        "vehicle_number": vehicle_number,
        "vendor_name": vendor_name,
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

@csrf_exempt
def get_current_status(request):
    try:
        # 1. Get active vehicle
        current_vehicle = VEHICLE_LOGS.objects.filter(status="IN_PROGRESS").first()

        if not current_vehicle:
            return JsonResponse({
                "status": "idle",
                "uid": None,
                "current_state": "IDLE",
                "vehicle_number": "NOT_FOUND",
                "vendor_name": "NOT_FOUND",
                "vendor_code": None,
                "add_vehicle": "NO",
                "emergency": None,
                "auto_manual": None,
            })

        # 2. Extract RFIDs
        rfids = (current_vehicle.rfids or "").split("|")

        vehicle_obj = None
        vendor_obj = None

        # 3. Find first matching RFID
        for rfid in rfids:
            rfid = rfid.strip()
            if not rfid:
                continue

            vehicle_obj = VEHICLE_MASTER.objects.filter(rfid=rfid).first()
            if vehicle_obj:
                vendor_obj = VENDOR_MASTER.objects.filter(
                    vendor_code=vehicle_obj.vendor_code
                ).first()
                break

        # 4. If NO vehicle found → trigger ADD VEHICLE FLOW
        if not vehicle_obj:
            vendors = list(
                VENDOR_MASTER.objects.values("vendor_code", "vendor_name", "bucket_no")
            )

            return JsonResponse({
                "status": "in_progress",
                "uid": current_vehicle.uid,
                "rfids": current_vehicle.rfids,
                "add_vehicle": "YES", 
                "vendors": vendors, 
                "current_state": "WAITING_FOR_VEHICLE_MASTER",
                "vehicle_number": None,
                "vendor_name": None,
                "vendor_code": None,
                "emergency": None,
                "auto_manual": None,
            })

        # 5. Get PLC state
        plc = PLC_COMM.objects.filter(uid=current_vehicle.uid).first()

        current_state = "UNKNOWN"
        emergency = None
        auto_manual = None

        if plc:
            current_state = plc.state
            emergency = plc.emergency
            auto_manual = plc.auto_manual

            if emergency == "ACTIVE":
                return JsonResponse({
                    "status": "blocked",
                    "reason": "EMERGENCY_ACTIVE",
                    "uid": current_vehicle.uid,
                    "current_state": current_state,
                    "vehicle_number": vehicle_obj.vehicle_number,
                    "vendor_name": vendor_obj.vendor_name if vendor_obj else None,
                })

            if auto_manual == "ACTIVE":
                return JsonResponse({
                    "status": "blocked",
                    "reason": "AUTO_MANUAL_ACTIVE",
                    "uid": current_vehicle.uid,
                    "current_state": current_state,
                    "vehicle_number": vehicle_obj.vehicle_number,
                    "vendor_name": vendor_obj.vendor_name if vendor_obj else None,
                })

        # 6. Normal flow
        return JsonResponse({
            "status": "in_progress",
            "uid": current_vehicle.uid,
            "add_vehicle": "NO",
            "current_state": current_state,
            "vehicle_number": vehicle_obj.vehicle_number,
            "vendor_name": vendor_obj.vendor_name if vendor_obj else "NOT_FOUND",
            "vendor_code": vehicle_obj.vendor_code,
            "emergency": emergency,
            "auto_manual": auto_manual,
        })

    except Exception as e:
        print(f"[ERROR] get_current_status: {e}")
        return JsonResponse({
            "status": "error",
            "message": str(e)
        }, status=500)

@csrf_exempt
def reset_system(request):
    """Reset entire system - mark in-progress as ERROR and reset state"""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"}, status=405)
    
    try:
        data = json.loads(request.body)
        uid = data.get("uid")
        
        # Mark current vehicle log as ERROR
        current_vehicle = VEHICLE_LOGS.objects.filter(uid=uid, status="IN_PROGRESS").first()
        if current_vehicle:
            current_vehicle.status = "ERROR"
            current_vehicle.error_message = "System reset by user"
            current_vehicle.update_time = timezone.now()
            current_vehicle.save()
        
        # Reset PLC_COMM
        plc_comm = PLC_COMM.objects.filter(uid=uid).first()
        if plc_comm:
            plc_comm.state = "IDLE"
            plc_comm.emergency = None
            plc_comm.auto_manual = None
            plc_comm.emergency_acknowledged = False
            plc_comm.auto_manual_acknowledged = False
            plc_comm.user_approved_skip_cycles = False
            plc_comm.updated = timezone.now()
            plc_comm.save()
        
        return JsonResponse({
            "success": True,
            "message": "System reset successfully"
        })
    
    except Exception as e:
        print(f"Error in reset_system: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)
