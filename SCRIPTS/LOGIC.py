import cv2
import os
import qrcode
from datetime import datetime
from DEPENDANT.INFERENCE import MASKRCNN

# AI Model Initialization 
BASE_DIR = '/home/omkar/INSIGHTZZ/PROJECTS/STANDARD_TEMPLATE/DJANGO_SCRIPTS_FRAMEWORK/STANDARD_FRAMEWORK/'
MODEL_CONFIG_PATH = BASE_DIR + 'SCRIPTS/configs/COCO-InstanceSegmentation/mask_rcnn_R_101_FPN_3x.yaml'
MODEL_PATH = BASE_DIR + 'SCRIPTS/MODEL/'
MODEL_FILE = 'model_final.pth'
CLASS_JSON = 'SCRIPTS/MODEL/JSON.json'

_inference_model = None

def initialize_ai_model():
    """
    Initialize the AI model for inference.
    Returns True if successful, False otherwise.
    """
    global _inference_model
    try:
        _inference_model = MASKRCNN(
            'Coal',
            MODEL_CONFIG_PATH,
            MODEL_PATH,
            MODEL_FILE,
            0.5,
            CLASS_JSON,
            debugMode=False
        )
        print("[LOGIC] AI Model initialized successfully")
        return True
    except Exception as e:
        print(f"[LOGIC] AI Model initialization failed: {e}")
        return False

def check_vehicle_front_present(image) -> bool:
    """
    AI Model confirmation: Check if vehicle front is present in the image.
    Input: image from CAM2 (vehicle arrival camera)
    Returns: True if vehicle front detected, False otherwise
    """
    try:
        if _inference_model is None:
            print("[LOGIC] AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if any vehicle-related objects detected
        if labellist and len(labellist) > 0:
            print(f"[LOGIC] Vehicle detected: {len(labellist)} objects found")
            return True
        
        print("[LOGIC] No vehicle detected in image")
        return False
    except Exception as e:
        print(f"[LOGIC] Error checking vehicle front: {e}")
        return False

def confirm_barrier_opening(image) -> bool:
    """
    AI Model confirmation: Verify barrier opening from image.
    Returns: True if barrier opening confirmed, False otherwise
    """
    try:
        if _inference_model is None:
            print("[LOGIC] AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if barrier opening detected
        if labellist and len(labellist) > 0:
            print(f"[LOGIC] Barrier opening confirmed by AI")
            return True
        
        return False
    except Exception as e:
        print(f"[LOGIC] Error confirming barrier opening: {e}")
        return False

def confirm_barrier_closing(image) -> bool:
    """
    AI Model confirmation: Verify barrier closing from image.
    Returns: True if barrier closing confirmed, False otherwise
    """
    try:
        if _inference_model is None:
            print("[LOGIC] AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if barrier closing detected (barrier should be at closed position)
        if labellist and len(labellist) > 0:
            print(f"[LOGIC] Barrier closing confirmed by AI")
            return True
        
        return False
    except Exception as e:
        print(f"[LOGIC] Error confirming barrier closing: {e}")
        return False

def confirm_auger_position(cam1_image_path: str, cam2_image_path: str, target_area: int) -> bool:
    """
    AI Model confirmation: Verify auger positioning using dual-camera validation.
    
    Args:
        cam1_image_path: Path to CAM1 image (checks bottom-middle region for COAL only)
        cam2_image_path: Path to CAM2 image (checks AUGER_BOTTOM inside COAL_AREA)
        target_area: Int (1-6) specifying which coal area to validate
    
    Returns:
        True if BOTH cam1_position AND cam2_position validations pass
    
    Validation Logic:
        CAM1 Check:
            - Region: bottom-middle box (x: 35%-65%, y: 50%-80% of image)
            - Validate: Only COAL (label 6) detected in region, no other overlaps
            - Pass Condition: >=80% of region pixels are COAL mask
        
        CAM2 Check:
            - Find AUGER_BOTTOM (label 23) lowest point of mask
            - Find target COAL_AREA (15-20) bounding box
            - Validate: AUGER_BOTTOM lowest point is inside COAL_AREA box
            - Pass Condition: Bottom point within coal area bounds
    
    Label Mapping:
        COAL = 6
        COAL_AREA_1 = 15, ..., COAL_AREA_6 = 20
        AUGER = 22, AUGER_BOTTOM = 23
    """
    try:
        if _inference_model is None:
            print("[LOGIC] AI Model not initialized for auger position confirmation")
            return False
        
        # ─── CAM1 Validation ──────────────────────────────────────────────────────
        print(f"[LOGIC] CAM1 Check: Validating bottom-middle region has only COAL...")
        cam1_position = _validate_cam1_region(cam1_image_path)
        print(f"[LOGIC] CAM1 Position Valid: {cam1_position}")
        
        # ─── CAM2 Validation ──────────────────────────────────────────────────────
        print(f"[LOGIC] CAM2 Check: Validating AUGER_BOTTOM in target coal area...")
        cam2_position = _validate_cam2_auger(cam2_image_path, target_area)
        print(f"[LOGIC] CAM2 Position Valid: {cam2_position}")
        
        # ─── Final Result ──────────────────────────────────────────────────────────
        result = cam1_position and cam2_position
        if result:
            print(f"[LOGIC] AUGER POSITIONING CONFIRMED - Both CAM1 and CAM2 validations passed")
        else:
            print(f"[LOGIC] AUGER POSITIONING FAILED - CAM1={cam1_position}, CAM2={cam2_position}")
        
        return result
    
    except Exception as e:
        print(f"[LOGIC] Error in auger position confirmation: {e}")
        return False

def _validate_cam1_region(image_path: str) -> bool:
    try:
        image = cv2.imread(image_path)
        if image is None:
            print(f"[LOGIC] Failed to read CAM1 image: {image_path}")
            return False
        
        height, width = image.shape[:2]
        print(f"[LOGIC] CAM1 Image dimensions: {width}x{height}")
        
        # Define bottom-middle region bounds
        x_start = int(width * 0.35)      # 35% from left
        x_end = int(width * 0.65)        # 65% from left
        y_start = int(height * 0.50)     # 50% from top
        y_end = int(height * 0.80)       # 80% from top
        
        region_width = x_end - x_start
        region_height = y_end - y_start
        region_area = region_width * region_height
        
        print(f"[LOGIC] Bottom-middle region: x=[{x_start}-{x_end}], y=[{y_start}-{y_end}]")
        print(f"[LOGIC] Region dimensions: {region_width}x{region_height} ({region_area} pixels)")
        
        # Run inference on full image
        masked_img, labellist = _inference_model.run_inference(image)
        
        if not labellist:
            print("[LOGIC] CAM1: No objects detected")
            return False
        
        # Count COAL (label 6) pixels in the region
        coal_count = 0
        non_coal_count = 0
        
        for detection in labellist:
            if len(detection) < 6:
                continue
            
            # Detection format: (score, y_min, y_max, x_min, x_max, class_name, ...)
            y_min = int(detection[1])
            y_max = int(detection[2])
            x_min = int(detection[3])
            x_max = int(detection[4])
            class_name = detection[5]
            score = detection[0]
            
            # Check if bounding box overlaps with region
            overlap_x_min = max(x_min, x_start)
            overlap_x_max = min(x_max, x_end)
            overlap_y_min = max(y_min, y_start)
            overlap_y_max = min(y_max, y_end)
            
            if overlap_x_min < overlap_x_max and overlap_y_min < overlap_y_max:
                overlap_area = (overlap_x_max - overlap_x_min) * (overlap_y_max - overlap_y_min)
                
                if class_name == "COAL":
                    coal_count += overlap_area
                    print(f"[LOGIC] CAM1: COAL detected in region - overlap area: {overlap_area}px (confidence: {score:.2f})")
                else:
                    non_coal_count += overlap_area
                    print(f"[LOGIC] CAM1: {class_name} detected in region - overlap area: {overlap_area}px (confidence: {score:.2f})")
        
        total_masked = coal_count + non_coal_count
        if total_masked == 0:
            print("[LOGIC] CAM1: No mask detections in region")
            return False
        
        coal_percentage = (coal_count / total_masked) * 100
        print(f"[LOGIC] CAM1: Region analysis - COAL: {coal_count}px ({coal_percentage:.1f}%), Others: {non_coal_count}px")
        
        # Success if >=80% of detected pixels in region are COAL
        threshold = 0.80
        result = (coal_count / total_masked) >= threshold
        return result
    
    except Exception as e:
        print(f"[LOGIC] Error in CAM1 validation: {e}")
        return False

def _validate_cam2_auger(image_path: str, target_area_num: int) -> bool:
    try:
        image = cv2.imread(image_path)
        if image is None:
            print(f"[LOGIC] Failed to read CAM2 image: {image_path}")
            return False
        
        height, width = image.shape[:2]
        print(f"[LOGIC] CAM2 Image dimensions: {width}x{height}")
        
        # Run inference
        masked_img, labellist = _inference_model.run_inference(image)
        
        if not labellist:
            print("[LOGIC] CAM2: No objects detected")
            return False
        
        # Map target area number to label name
        target_coal_labels = {
            1: "COAL_AREA_1",
            2: "COAL_AREA_2",
            3: "COAL_AREA_3",
            4: "COAL_AREA_4",
            5: "COAL_AREA_5",
            6: "COAL_AREA_6"
        }
        target_coal_label = target_coal_labels.get(target_area_num, "COAL_AREA_1")
        
        # Find AUGER_BOTTOM and COAL_AREA detections
        auger_bottom_detection = None
        coal_area_bbox = None
        
        for detection in labellist:
            if len(detection) < 6:
                continue
            
            class_name = detection[5]
            score = detection[0]
            y_min = int(detection[1])
            y_max = int(detection[2])
            x_min = int(detection[3])
            x_max = int(detection[4])
            
            if class_name == "AUGER_BOTTOM":
                auger_bottom_detection = detection
                print(f"[LOGIC] CAM2: AUGER_BOTTOM detected (bounding box: x=[{x_min}-{x_max}], y=[{y_min}-{y_max}], confidence: {score:.2f})")
            
            elif class_name == target_coal_label:
                coal_area_bbox = {
                    'x_min': x_min, 'x_max': x_max,
                    'y_min': y_min, 'y_max': y_max,
                    'score': score
                }
                print(f"[LOGIC] CAM2: Target {target_coal_label} at x=[{x_min}-{x_max}], y=[{y_min}-{y_max}] (confidence: {score:.2f})")
        
        # Validate that both detections exist
        if auger_bottom_detection is None:
            print("[LOGIC] CAM2: AUGER_BOTTOM not detected")
            return False
        
        if coal_area_bbox is None:
            print(f"[LOGIC] CAM2: Target coal area {target_coal_label} not detected")
            return False
        
        # Extract mask points from AUGER_BOTTOM detection (last element is the mask points array)
        mask_points = auger_bottom_detection[-1]
        
        if mask_points is None or len(mask_points) == 0:
            print("[LOGIC] CAM2: AUGER_BOTTOM has no mask points")
            return False
        
        # Find the very bottom point (maximum y value) from mask points
        bottom_point = max(mask_points, key=lambda p: p[1])
        bottom_x = int(bottom_point[0])
        bottom_y = int(bottom_point[1])
        
        print(f"[LOGIC] CAM2: AUGER_BOTTOM mask has {len(mask_points)} points")
        print(f"[LOGIC] CAM2: AUGER_BOTTOM lowest mask point at ({bottom_x}, {bottom_y})")
        
        # Get COAL_AREA bounds
        coal_x_min = coal_area_bbox['x_min']
        coal_x_max = coal_area_bbox['x_max']
        coal_y_min = coal_area_bbox['y_min']
        coal_y_max = coal_area_bbox['y_max']
        
        print(f"[LOGIC] CAM2: COAL_AREA box x=[{coal_x_min}-{coal_x_max}], y=[{coal_y_min}-{coal_y_max}]")
        
        # Check if AUGER_BOTTOM's bottom mask point is inside COAL_AREA bounds
        inside_x = coal_x_min <= bottom_x <= coal_x_max
        inside_y = coal_y_min <= bottom_y <= coal_y_max
        
        result = inside_x and inside_y
        
        if result:
            print(f"[LOGIC] CAM2: AUGER_BOTTOM lowest mask point is inside {target_coal_label}")
        else:
            print(f"[LOGIC] CAM2: AUGER_BOTTOM lowest mask point is NOT inside {target_coal_label}")
            print(f"[LOGIC] X alignment: {inside_x} (point_x={bottom_x} in range [{coal_x_min}-{coal_x_max}])")
            print(f"[LOGIC] Y alignment: {inside_y} (point_y={bottom_y} in range [{coal_y_min}-{coal_y_max}])")
        
        return result
    
    except Exception as e:
        print(f"[LOGIC] Error in CAM2 validation: {e}")
        import traceback
        traceback.print_exc()
        return False

def check_sampling_cycle_completion(image) -> bool:
    """
    AI Model confirmation: Verify sampling cycle completed successfully.
    Returns: True if cycle completed, False otherwise
    """
    try:
        if _inference_model is None:
            print("[LOGIC] AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if sampling action completed
        if labellist and len(labellist) > 0:
            print(f"[LOGIC] Sampling cycle completion confirmed")
            return True
        
        return False
    except Exception as e:
        print(f"[LOGIC] Error checking sampling cycle completion: {e}")
        return False

def generate_qr_code(vendor_name: str, vehicle_number: str, uid: str, save_path: str) -> str:
    try:
        qr_data = f"Vendor:{vendor_name}|Vehicle:{vehicle_number}|UID:{uid}|Time:{datetime.now().isoformat()}"
        
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)
        
        qr_img = qr.make_image(fill_color="black", back_color="white")
        
        os.makedirs(save_path, exist_ok=True)
        qr_img.save(save_path)
        
        print(f"[LOGIC] QR code generated: {save_path}")
        return save_path
    except Exception as e:
        print(f"[LOGIC] Error generating QR code: {e}")
        return ""

if __name__ == "__main__":
    # Test initialization
    if initialize_ai_model():
        print("AI Model ready for use")
    else:
        print("Failed to initialize AI Model")