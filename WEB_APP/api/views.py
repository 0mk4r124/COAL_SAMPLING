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

def live_ip_camera(request):
    """API endpoint for all 4 IP camera live images"""
    try: 
        camera_paths = [
            "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM1_REDUCED.jpg",
            "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM2_REDUCED.jpg",
            "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/CAM3_REDUCED.jpg",
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
        if not created and vendor_name and vendor_obj.vendor_name != vendor_name:
            vendor_obj.vendor_name = vendor_name
            vendor_obj.save()

        # Create or update vehicle
        vehicle_obj, created = VEHICLE_MASTER.objects.update_or_create(
            rfid=rfid,
            defaults={
                "vehicle_number": vehicle_number,
                "vendor_code": vendor_code,
                "create_time": timezone.now()
            }
        )

        return JsonResponse({
            "success": True,
            "message": "Vehicle added/updated successfully",
            "vehicle_id": vehicle_obj.id
        })

    except Exception as e:
        print(e)
        return JsonResponse({
            "success": False,
            "error": str(e)
        }, status=500)

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

@csrf_exempt
def check_for_vehicle(request):
    status = "new"
    vehicle_number = ""
    vendor_name = ""
    rfid_value = ""

    current_vehicle = VEHICLE_LOGS.objects.filter(status="IN_PROGRESS").first()

    if current_vehicle:
        rfids = (current_vehicle.rfids or "").split("|")

        for rfid in rfids:
            rfid_value = rfid
            vehicle = VEHICLE_MASTER.objects.filter(rfid=rfid).first()

            if vehicle:
                vendor = VENDOR_MASTER.objects.filter(
                    vendor_code=vehicle.vendor_code
                ).first()

                status = "present"
                vehicle_number = vehicle.vehicle_number
                vendor_name = vendor.vendor_name if vendor else ""
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


@csrf_exempt
def get_current_status(request):
    """Get current vehicle, state, and emergency/auto_manual status"""
    try:
        # Get the current in-progress vehicle
        current_vehicle = VEHICLE_LOGS.objects.filter(status="IN_PROGRESS").first()
        
        if not current_vehicle:
            return JsonResponse({
                "status": "idle",
                "uid": None,
                "vehicle_number": "NOT_FOUND",
                "vendor_name": "NOT_FOUND",
                "current_state": "IDLE",
                "emergency": None,
                "auto_manual": None,
                "emergency_acknowledged": False,
                "auto_manual_acknowledged": False,
            })
        
        # Get PLC communication record
        plc_comm = PLC_COMM.objects.filter(uid=current_vehicle.uid).first()
        
        # Get vehicle details
        rfids = (current_vehicle.rfids or "").split("|")
        vehicle_number = "NOT_FOUND"
        vendor_name = "NOT_FOUND"
        
        for rfid in rfids:
            vehicle = VEHICLE_MASTER.objects.filter(rfid=rfid).first()
            if vehicle:
                vendor = VENDOR_MASTER.objects.filter(vendor_code=vehicle.vendor_code).first()
                vehicle_number = vehicle.vehicle_number
                vendor_name = vendor.vendor_name if vendor else "NOT_FOUND"
                break
        
        if plc_comm:
            return JsonResponse({
                "status": "in_progress",
                "uid": current_vehicle.uid,
                "vehicle_number": vehicle_number,
                "vendor_name": vendor_name,
                "current_state": plc_comm.state,
                "emergency": plc_comm.emergency,
                "auto_manual": plc_comm.auto_manual,
                "emergency_acknowledged": plc_comm.emergency_acknowledged,
                "auto_manual_acknowledged": plc_comm.auto_manual_acknowledged,
                "user_approved_skip_cycles": plc_comm.user_approved_skip_cycles,
            })
        else:
            return JsonResponse({
                "status": "in_progress",
                "uid": current_vehicle.uid,
                "vehicle_number": vehicle_number,
                "vendor_name": vendor_name,
                "current_state": "UNKNOWN",
                "emergency": None,
                "auto_manual": None,
                "emergency_acknowledged": False,
                "auto_manual_acknowledged": False,
                "user_approved_skip_cycles": False,
            })
    
    except Exception as e:
        print(f"Error in get_current_status: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def acknowledge_emergency(request):
    """Handle emergency popup acknowledgment - user wants to retake cycles"""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"}, status=405)
    
    try:
        data = json.loads(request.body)
        uid = data.get("uid")
        retake_cycles = data.get("retake_cycles", True)
        
        plc_comm = PLC_COMM.objects.filter(uid=uid).first()
        if plc_comm:
            plc_comm.emergency_acknowledged = True
            plc_comm.user_approved_skip_cycles = not retake_cycles
            plc_comm.updated = timezone.now()
            plc_comm.save()
            
            return JsonResponse({
                "success": True,
                "message": "Emergency acknowledged",
                "retake_cycles": retake_cycles
            })
        else:
            return JsonResponse({"success": False, "error": "No PLC record found"}, status=404)
    
    except Exception as e:
        print(f"Error in acknowledge_emergency: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@csrf_exempt
def acknowledge_auto_manual(request):
    """Handle auto_manual popup acknowledgment - user confirms or skips cycles"""
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid request method"}, status=405)
    
    try:
        data = json.loads(request.body)
        uid = data.get("uid")
        user_action = data.get("user_action")  # "continue" or "skip_all_cycles"
        
        plc_comm = PLC_COMM.objects.filter(uid=uid).first()
        if plc_comm:
            plc_comm.auto_manual_acknowledged = True
            if user_action == "skip_all_cycles":
                plc_comm.user_approved_skip_cycles = True
            plc_comm.updated = timezone.now()
            plc_comm.save()
            
            return JsonResponse({
                "success": True,
                "message": "Auto manual acknowledged",
                "user_action": user_action
            })
        else:
            return JsonResponse({"success": False, "error": "No PLC record found"}, status=404)
    
    except Exception as e:
        print(f"Error in acknowledge_auto_manual: {e}")
        return JsonResponse({"success": False, "error": str(e)}, status=500)


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
