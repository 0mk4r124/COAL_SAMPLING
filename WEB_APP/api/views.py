import json
import os
import shutil
import csv

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

def build_rfid_key(rfids, uid=None):
    if isinstance(rfids, str):
        rfids = rfids.split("|")

    rfids = [r.strip() for r in rfids if r and r.strip()]
    rfids = sorted(set(rfids))

    if not rfids:
        return uid or ""

    if len(rfids) == 1:
        return f"{rfids[0]}|{uid}" if uid else rfids[0]

    return "|".join(rfids)

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
    file_param = request.GET.get('file')
    if not file_param:
        return HttpResponse("File parameter missing", status=400)

    # Direct decode (NO urlparse)
    file_path = unquote(file_param)

    # Fix malformed paths
    file_path = file_path.replace("c://", "c:/")
    file_path = file_path.replace("//", "/")
    file_path = os.path.normpath(file_path)

    print("FINAL PATH:", file_path)  # debug

    if os.path.exists(file_path):
        try:
            ext = os.path.splitext(file_path)[1].lower()
            content_type = 'image/jpeg' if ext in ['.jpg', '.jpeg'] else 'application/octet-stream'
            return FileResponse(open(file_path, 'rb'), content_type=content_type)
        except Exception as e:
            return HttpResponse(f"Error reading file: {str(e)}", status=500)
    else:
        return HttpResponse(
            f"Image file not found on this server: {file_path}",
            status=404,
            content_type='text/plain'
        )
    
def live_ip_camera(request):
    try: 
        camera_paths = [
            "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM1_REDUCED.jpg",
            "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM2_REDUCED.jpg",
            "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM3_REDUCED.jpg",
        ]
        # camera_paths = [
        #     "/home/deepali/OMKAR/CODES/COAL_SAMPLING/COAL_SAMPLING/TEMP_IMG/CAM1/CAM1_1774424054126.jpg",
        #     "/home/deepali/OMKAR/CODES/COAL_SAMPLING/COAL_SAMPLING/TEMP_IMG/CAM2/CAM2_1773298529500.jpg",
        #     "/home/deepali/OMKAR/CODES/COAL_SAMPLING/COAL_SAMPLING/TEMP_IMG/CAM3/CAM3_1774424056125.jpg",
        # ]
        
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
                "create_time": timezone.now()
            }
        )

        # If vendor exists but name changed
        # Update only if explicitly provided (new vendor case)
        if vendor_name:
            if created:
                vendor_obj.vendor_name = vendor_name
            else:
                # Optional: update only if fields empty
                if not vendor_obj.vendor_name:
                    vendor_obj.vendor_name = vendor_name

            vendor_obj.save()

        # Create or update vehicle
        rfid_key = build_rfid_key(rfid)

        VEHICLE_MASTER.objects.update_or_create(
            rfid=rfid_key,
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
    page = int(request.GET.get("page", 1))
    per_page = int(request.GET.get("per_page", 12))

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
    vehicle_master_qs = VEHICLE_MASTER.objects.filter(
        rfid=OuterRef('rfids')
    )

    # ---- Subquery: Vendor from vehicle ----
    # vehicle_master_qs = VEHICLE_MASTER.objects.filter(
    #     rfid=OuterRef('rfids')
    # ).filter(match__gt=0)

    vendor_master_qs = VENDOR_MASTER.objects.filter(
        vendor_code=OuterRef('vendor_code')
    )

    # ---- Main Query ----
    queryset = VEHICLE_LOGS.objects.filter(status="COMPLETED").filter(
        create_time__date__gte=start_dt,
        create_time__date__lte=end_dt,
    ).annotate(
        vehicle_number=Subquery(vehicle_master_qs.values('vehicle_number')[:1]),
        vendor_code=Subquery(vehicle_master_qs.values('vendor_code')[:1]),
    ).annotate(
        vendor_name=Subquery(vendor_master_qs.values('vendor_name')[:1]),
    ).order_by("-create_time")

    # ---- Filters (after annotation) ----
    if vehicle_number:
        queryset = queryset.filter(vehicle_number__icontains=vehicle_number)

    if vendor_name:
        queryset = queryset.filter(vendor_name__icontains=vendor_name)

    total = queryset.count()
    start = (page - 1) * per_page
    end = start + per_page
    queryset = queryset[start:end]

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
            "report_path": row.report_path if row.report_path else None,
            "bucket_no": row.bucket_no,
        })

    return JsonResponse({
        "data": results,
        "vehicle_number": vehicle_number,
        "vendor_name": vendor_name,
        "total": total,
        "page": page,
        "per_page": per_page
    })

@csrf_exempt
def download_history_data(request):
    try:
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

        # ---- SAME QUERY AS fetch_history_data ----
        vehicle_master_qs = VEHICLE_MASTER.objects.annotate(
            rfid=OuterRef('rfids')
        )

        vendor_master_qs = VENDOR_MASTER.objects.filter(
            vendor_code=OuterRef('vendor_code')
        )

        queryset = VEHICLE_LOGS.objects.filter(status="COMPLETED").filter(
            create_time__date__gte=start_dt,
            create_time__date__lte=end_dt,
        ).annotate(
            vehicle_number=Subquery(vehicle_master_qs.values('vehicle_number')[:1]),
            vendor_code=Subquery(vehicle_master_qs.values('vendor_code')[:1]),
        ).annotate(
            vendor_name=Subquery(vendor_master_qs.values('vendor_name')[:1]),
        ).order_by("create_time")

        if vehicle_number:
            queryset = queryset.filter(vehicle_number__icontains=vehicle_number)

        if vendor_name:
            queryset = queryset.filter(vendor_name__icontains=vendor_name)

        # ---- CSV RESPONSE ----
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="history_data.csv"'

        writer = csv.writer(response)

        # Header (same as UI)
        writer.writerow([
            "SNo",
            "Datetime",
            "Vehicle Number",
            "Vendor Name",
            "Vendor Code",
            "Bucket Number"
        ])

        for idx, row in enumerate(queryset, start=1):
            writer.writerow([
                idx,
                row.create_time.strftime("%Y-%m-%d %H:%M:%S") if row.create_time else "",
                row.vehicle_number,
                row.vendor_name,
                row.vendor_code,
                row.bucket_no
            ])

        return response

    except Exception as e:
        return HttpResponse(str(e), status=500)

@csrf_exempt
def vehicle_master(request):
    try:
        page = int(request.GET.get("page", 1))
        per_page = int(request.GET.get("per_page", 12))

        queryset = VEHICLE_MASTER.objects.all().order_by("rfid")

        total = queryset.count()
        start = (page - 1) * per_page
        end = start + per_page

        vehicles = queryset[start:end]

        data = []
        for idx, v in enumerate(vehicles, start=1):
            vendor = VENDOR_MASTER.objects.filter(vendor_code=v.vendor_code).first()

            data.append({
                "sno": idx,
                "rfid": v.rfid,
                "vehicle_number": v.vehicle_number,
                "vendor_code": v.vendor_code,
                "vendor_name": vendor.vendor_name if vendor else None
            })

        return JsonResponse({
            "data": data,
            "total": total,
            "page": page,
            "per_page": per_page
        })

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
    
@csrf_exempt
def edit_vehicle_master(request):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    try:
        data = json.loads(request.body)

        rfid = data.get("rfid")

        vehicle = VEHICLE_MASTER.objects.filter(rfid=rfid).first()
        if not vehicle:
            return JsonResponse({"success": False, "error": "RFID not found"})

        vehicle.vehicle_number = data.get("vehicle_number")
        vehicle.vendor_code = data.get("vendor_code")
        vehicle.save()

        # update vendor
        vendor, _ = VENDOR_MASTER.objects.update_or_create(
            vendor_code=data.get("vendor_code"),
            defaults={"vendor_name": data.get("vendor_name")}
        )

        return JsonResponse({"success": True})

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})
    
@csrf_exempt
def upload_vehicle_master(request):
    if request.method != "POST":
        return JsonResponse({"success": False}, status=405)

    try:
        rows = json.loads(request.body)

        for row in rows:
            vendor, _ = VENDOR_MASTER.objects.update_or_create(
                vendor_code=row["vendor_code"],
                defaults={"vendor_name": row.get("vendor_name")}
            )

            VEHICLE_MASTER.objects.update_or_create(
                rfid=row["rfid"],
                defaults={
                    "vehicle_number": row["vehicle_number"],
                    "vendor_code": row["vendor_code"]
                }
            )

        return JsonResponse({"success": True})

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)})

@csrf_exempt
def download_vehicle_master(request):
    try:
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="vehicle_master.csv"'

        writer = csv.writer(response)

        # Header
        writer.writerow(["RFID", "Vehicle Number", "Vendor Name", "Vendor Code"])

        vehicles = VEHICLE_MASTER.objects.all().order_by("rfid")

        for v in vehicles:
            vendor = VENDOR_MASTER.objects.filter(vendor_code=v.vendor_code).first()

            writer.writerow([
                v.rfid,
                v.vehicle_number,
                vendor.vendor_name if vendor else "",
                v.vendor_code
            ])

        return response

    except Exception as e:
        return HttpResponse(str(e), status=500)

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

        vehicle_obj = None
        vendor_obj = None

        # 2. Extract RFIDs
        rfid_key =  rfid_key = build_rfid_key(current_vehicle.rfids, current_vehicle.uid)
        vehicle_obj = VEHICLE_MASTER.objects.filter(rfid=rfid_key).first()

        # 3. Find first matching RFID
        vendor_obj = None
        if vehicle_obj:
            vendor_obj = VENDOR_MASTER.objects.filter(
                vendor_code=vehicle_obj.vendor_code
            ).first()

        # 4. If NO vehicle found → trigger ADD VEHICLE FLOW
        if not vehicle_obj:
            vendors = list(
                VENDOR_MASTER.objects.values("vendor_code", "vendor_name")
            )

            return JsonResponse({
                "status": "in_progress",
                "uid": current_vehicle.uid,
                "rfids": current_vehicle.rfids,
                "bucket_number": current_vehicle.bucket_no,
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
            "bucket_number": current_vehicle.bucket_no,
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
