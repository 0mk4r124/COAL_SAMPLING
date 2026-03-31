import cv2
import os
import qrcode
import traceback
from datetime import datetime
from DEPENDANT.INFERENCE import MASKRCNN
from LOGGING_CONFIG import initializeLogger

# Initialize logger
logger = initializeLogger("LOGIC")

# AI Model Initialization 
BASE_DIR = 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/'
MODEL_CONFIG_PATH = BASE_DIR + 'MODEL/configs/COCO-InstanceSegmentation/mask_rcnn_R_101_FPN_3x.yaml'
MODEL_PATH = BASE_DIR + 'MODEL/COAL_SAMPLING_27MAR/'
MODEL_FILE = 'model_final.pth'
CLASS_JSON = BASE_DIR + 'MODEL/COAL_SAMPLING_27MAR/COAL_SAMPLING_27MAR.json'

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
        logger.info("AI Model initialized successfully")
        return True
    except Exception as e:
        logger.error(f"AI Model initialization failed: {e}", exc_info=True)
        print(f"ERROR: AI Model initialization failed: {e}")
        return False

def check_vehicle_front_present(image) -> bool:
    """
    AI Model confirmation: Check if vehicle front is present in the image.
    Input: image from CAM2 (vehicle arrival camera)
    Returns: True if vehicle front detected, False otherwise
    """
    try:
        if _inference_model is None:
            logger.warning("AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if any vehicle-related objects detected
        if labellist and len(labellist) > 0:
            logger.info(f"Vehicle detected: {len(labellist)} objects found")
            return True
        
        logger.debug("No vehicle detected in image")
        return False
    except Exception as e:
        logger.error(f"Error checking vehicle front: {e}", exc_info=True)
        print(f"ERROR: Error checking vehicle front: {e}")
        return False

def confirm_barrier_opening(image) -> bool:
    """
    AI Model confirmation: Verify barrier opening from image.
    Returns: True if barrier opening confirmed, False otherwise
    """
    try:
        if _inference_model is None:
            logger.warning("AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if barrier opening detected
        if labellist and len(labellist) > 0:
            logger.info("Barrier opening confirmed by AI")
            return True
        
        return False
    except Exception as e:
        logger.error(f"Error confirming barrier opening: {e}", exc_info=True)
        print(f"ERROR: Error confirming barrier opening: {e}")
        return False

def confirm_barrier_closing(image) -> bool:
    """
    AI Model confirmation: Verify barrier closing from image.
    Returns: True if barrier closing confirmed, False otherwise
    """
    try:
        if _inference_model is None:
            logger.warning("AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if barrier closing detected (barrier should be at closed position)
        if labellist and len(labellist) > 0:
            logger.info("Barrier closing confirmed by AI")
            return True
        
        return False
    except Exception as e:
        logger.error(f"Error confirming barrier closing: {e}", exc_info=True)
        print(f"ERROR: Error confirming barrier closing: {e}")
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
            logger.warning("AI Model not initialized for auger position confirmation")
            return False
        
        # ─── CAM1 Validation ──────────────────────────────────────────────────────
        logger.info("CAM1 Check: Validating bottom-middle region has only COAL...")
        cam1_position = _validate_cam1_region(cam1_image_path)
        logger.info(f"CAM1 Position Valid: {cam1_position}")
        
        # ─── CAM2 Validation ──────────────────────────────────────────────────────
        logger.info("CAM2 Check: Validating AUGER_BOTTOM in target coal area...")
        cam2_position = _validate_cam2_auger(cam2_image_path, target_area)
        logger.info(f"CAM2 Position Valid: {cam2_position}")
        
        # ─── Final Result ──────────────────────────────────────────────────────────
        result = cam1_position and cam2_position
        if result:
            logger.info(f"AUGER POSITIONING CONFIRMED - Both CAM1 and CAM2 validations passed")
        else:
            logger.warning(f"AUGER POSITIONING FAILED - CAM1={cam1_position}, CAM2={cam2_position}")
        
        return result
    
    except Exception as e:
        logger.error(f"Error in auger position confirmation: {e}", exc_info=True)
        print(f"ERROR: Error in auger position confirmation: {e}")
        return False

def _validate_cam1_region(image_path: str) -> bool:
    """
    Validate that ONLY COAL and TRUCK_BODY objects overlap with the region of interest.
    
    The region box contains the auger placement area. We want to ensure that within this
    region, there are no unwanted objects - only COAL and optionally TRUCK_BODY.
    
    Logic:
        1. Define region of interest (bottom-middle box)
        2. For each detected object, check if it overlaps with the region
        3. Only allow COAL and TRUCK_BODY objects to overlap
        4. If ANY other object overlaps the region, return False
        5. If only COAL/TRUCK_BODY overlap (or no overlap at all), return True
    
    Returns:
        True if region contains only COAL/TRUCK_BODY (allowed objects)
        False if any other object type overlaps the region
    """
    try:
        image = cv2.imread(image_path)
        if image is None:
            logger.error(f"Failed to read CAM1 image: {image_path}", exc_info=True)
            print(f"ERROR: Failed to read CAM1 image: {image_path}")
            return False
        
        vis_img = image.copy()
        height, width = image.shape[:2]
        logger.debug(f"CAM1 Image dimensions: {width}x{height}")
        
        # Define bottom-middle region bounds (region of interest for auger placement)
        x_start = int(width * 0.35)      # 35% from left
        x_end = int(width * 0.65)        # 65% from left
        y_start = int(height * 0.50)     # 50% from top
        y_end = int(height * 0.80)       # 80% from top
        
        region_width = x_end - x_start
        region_height = y_end - y_start
        
        logger.debug(f"CAM1: Region of interest x=[{x_start}-{x_end}], y=[{y_start}-{y_end}]")
        logger.debug(f"CAM1: Region size: {region_width}x{region_height}")
        cv2.rectangle(vis_img, (x_start, y_start), (x_end, y_end), (0, 255, 255), 3)
        
        # Run inference on full image
        masked_img, labellist = _inference_model.run_inference(image)
        cv2.imwrite(f"{image_path.split('.')[0]}_masked.jpg", vis_img)
        
        if not labellist:
            logger.info("CAM1: No objects detected - region is clear")
            cv2.imwrite("test.jpg", vis_img)
            return True
        
        # Allowed object types in the region
        allowed_classes = {"COAL", "TRUCK_BODY"}
        
        # Check each detection for overlap with region
        has_coal_or_truck = False
        has_forbidden_objects = False
        forbidden_objects_in_region = []
        
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
            
            has_overlap = (overlap_x_min < overlap_x_max) and (overlap_y_min < overlap_y_max)
            
            if has_overlap:
                overlap_area = (overlap_x_max - overlap_x_min) * (overlap_y_max - overlap_y_min)
                
                if class_name in allowed_classes:
                    # Draw in green for allowed objects
                    cv2.rectangle(vis_img, (x_min, y_min), (x_max, y_max), (0, 255, 0), 2)
                    has_coal_or_truck = True
                    logger.debug(f"CAM1: ALLOWED '{class_name}' overlaps region - area: {overlap_area}px (confidence: {score:.2f})")
                else:
                    # Draw in red for forbidden objects
                    cv2.rectangle(vis_img, (x_min, y_min), (x_max, y_max), (0, 0, 255), 2)
                    has_forbidden_objects = True
                    forbidden_objects_in_region.append((class_name, score, overlap_area))
                    logger.warning(f"CAM1: FORBIDDEN '{class_name}' overlaps region - area: {overlap_area}px (confidence: {score:.2f})")
            else:
                # Object detected but doesn't overlap region - draw in cyan
                cv2.rectangle(vis_img, (x_min, y_min), (x_max, y_max), (255, 255, 0), 1)
                logger.debug(f"CAM1: '{class_name}' detected but outside region (confidence: {score:.2f})")
        
        # cv2.imwrite("test.jpg", vis_img)
        cv2.imwrite(f"{image_path.split('.')[0]}_masked.jpg", vis_img)
        
        # Result: Pass only if NO forbidden objects overlap the region
        if has_forbidden_objects:
            logger.warning(f"CAM1: VALIDATION FAILED - Forbidden objects in region: {forbidden_objects_in_region}")
            return False
        else:
            logger.info(f"CAM1: VALIDATION PASSED - Region contains only allowed objects or is clear")
            return True
    
    except Exception as e:
        logger.error(f"Error in CAM1 validation: {e}", exc_info=True)
        print(f"ERROR: Error in CAM1 validation: {e}")
        return False

def _validate_cam2_auger(image_path: str, target_area_num: int) -> bool:
    """
    Validate AUGER positioning using CAM2.
    
    Primary: Look for AUGER_BOTTOM (label 23) detection - use its lowest mask point
    Fallback: If AUGER_BOTTOM not detected, use AUGER (label 22) frame - use center of bottom edge
    
    Then verify the reference point is inside the target COAL_AREA box.
    """ 
    try:
        image = cv2.imread(image_path)
        if image is None:
            logger.error(f"Failed to read CAM2 image: {image_path}", exc_info=True)
            print(f"ERROR: Failed to read CAM2 image: {image_path}")
            return False
        
        vis_img = image.copy()
        height, width = image.shape[:2]
        logger.debug(f"CAM2 Image dimensions: {width}x{height}")
        
        # Run inference
        masked_img, labellist = _inference_model.run_inference(image)
        cv2.imwrite("masked_img.jpg", masked_img)
        
        if not labellist:
            logger.info("CAM2: No objects detected")
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
        
        # Find AUGER_BOTTOM, AUGER, and COAL_AREA detections
        auger_bottom_detection = None
        auger_detection = None
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
                logger.debug(f"CAM2: AUGER_BOTTOM detected (bbox: x=[{x_min}-{x_max}], y=[{y_min}-{y_max}], confidence: {score:.2f})")
            
            elif class_name == "AUGER":
                auger_detection = detection
                logger.debug(f"CAM2: AUGER detected (bbox: x=[{x_min}-{x_max}], y=[{y_min}-{y_max}], confidence: {score:.2f})")
            
            elif class_name == target_coal_label:
                coal_area_bbox = {
                    'x_min': x_min, 'x_max': x_max,
                    'y_min': y_min, 'y_max': y_max,
                    'score': score
                }
                cv2.rectangle(vis_img, (x_min, y_min), (x_max, y_max), (0, 255, 0), 2)
                logger.debug(f"CAM2: Target {target_coal_label} at x=[{x_min}-{x_max}], y=[{y_min}-{y_max}] (confidence: {score:.2f})")
        
        # Validate that target coal area is detected
        if coal_area_bbox is None:
            logger.warning(f"CAM2: Target coal area {target_coal_label} not detected")
            return False
        
        # ─── Determine Reference Point ─────────────────────────────────────────
        reference_point = None
        reference_source = None
        
        # Primary: Try to use AUGER_BOTTOM's lowest mask point
        if auger_bottom_detection is not None:
            mask_points = auger_bottom_detection[-1]
            
            if mask_points is not None and len(mask_points) > 0:
                # Find the very bottom point (maximum y value) from mask points
                bottom_point = max(mask_points, key=lambda p: p[1])
                reference_point = (int(bottom_point[0]), int(bottom_point[1]))
                reference_source = "AUGER_BOTTOM mask"
                
                logger.debug(f"CAM2: Using AUGER_BOTTOM lowest mask point as reference: {reference_point}")
                cv2.circle(vis_img, reference_point, 8, (0, 0, 255), -1)
            else:
                logger.debug("CAM2: AUGER_BOTTOM detected but has no mask points - falling back to AUGER")
                auger_bottom_detection = None
        
        # Fallback: If AUGER_BOTTOM unavailable, use center of AUGER bottom edge
        if reference_point is None:
            if auger_detection is None:
                logger.error("CAM2: Neither AUGER_BOTTOM nor AUGER detected", exc_info=True)
                print(f"ERROR: CAM2: Neither AUGER_BOTTOM nor AUGER detected")
                return False
            
            # Extract AUGER bounding box
            y_min = int(auger_detection[1])
            y_max = int(auger_detection[2])
            x_min = int(auger_detection[3])
            x_max = int(auger_detection[4])
            
            # Center point of bottom edge
            center_x = (x_min + x_max) // 2
            bottom_y = y_max
            reference_point = (center_x, bottom_y)
            reference_source = "AUGER bottom edge center"
            
            logger.debug(f"CAM2: AUGER_BOTTOM not available - Using {reference_source} as reference: {reference_point}")
            cv2.rectangle(vis_img, (x_min, y_min), (x_max, y_max), (255, 0, 0), 2)
            cv2.circle(vis_img, reference_point, 8, (255, 165, 0), -1)
        
        # cv2.imwrite("test.jpg", vis_img)
        cv2.imwrite(f"{image_path.split('.')[0]}_masked.jpg", vis_img)
        
        # ─── Validate Reference Point Inside Coal Area ───────────────────────
        coal_x_min = coal_area_bbox['x_min']
        coal_x_max = coal_area_bbox['x_max']
        coal_y_min = coal_area_bbox['y_min']
        coal_y_max = coal_area_bbox['y_max']
        
        ref_x = reference_point[0]
        ref_y = reference_point[1]
        
        inside_x = coal_x_min <= ref_x <= coal_x_max
        inside_y = coal_y_min <= ref_y <= coal_y_max
        
        result = inside_x and inside_y
        
        logger.debug(f"CAM2: Reference point ({ref_x}, {ref_y}) from {reference_source}")
        logger.debug(f"CAM2: COAL_AREA bounds x=[{coal_x_min}-{coal_x_max}], y=[{coal_y_min}-{coal_y_max}]")
        logger.debug(f"CAM2: X alignment: {inside_x} (ref_x={ref_x} in range [{coal_x_min}-{coal_x_max}])")
        logger.debug(f"CAM2: Y alignment: {inside_y} (ref_y={ref_y} in range [{coal_y_min}-{coal_y_max}])")
        
        if result:
            logger.info(f"CAM2: AUGER is positioned correctly inside {target_coal_label}")
        else:
            logger.warning(f"CAM2: AUGER is NOT positioned correctly inside {target_coal_label}")
        
        return result
    
    except Exception as e:
        logger.error(f"Error in CAM2 validation: {e}", exc_info=True)
        print(f"ERROR: Error in CAM2 validation: {e}")
        return False

def check_sampling_cycle_completion(image) -> bool:
    """
    AI Model confirmation: Verify sampling cycle completed successfully.
    Returns: True if cycle completed, False otherwise
    """
    try:
        if _inference_model is None:
            logger.warning("AI Model not initialized")
            return False
        
        _, labellist = _inference_model.run_inference(image)
        
        # Check if sampling action completed
        if labellist and len(labellist) > 0:
            logger.info("Sampling cycle completion confirmed")
            return True
        
        return False
    except Exception as e:
        logger.error(f"Error checking sampling cycle completion: {e}", exc_info=True)
        print(f"ERROR: Error checking sampling cycle completion: {e}")
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
        
        logger.info(f"QR code generated: {save_path}")
        return save_path
    except Exception as e:
        logger.error(f"Error generating QR code: {e}", exc_info=True)
        print(f"ERROR: Error generating QR code: {e}")
        return ""

if __name__ == "__main__":
    # Test initialization
    if initialize_ai_model():
        logger.info("AI Model ready for use")
    else:
        logger.error("Failed to initialize AI Model", exc_info=True)
        print("ERROR: Failed to initialize AI Model")