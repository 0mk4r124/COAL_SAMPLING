import cv2
import os
import qrcode
import traceback

from datetime import datetime

from DEPENDANT.INFERENCE import MASKRCNN
from DEPENDANT.LOGGING import initializeLogger

# AI Model Initialization 
BASE_DIR = 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/'
MODEL_CONFIG_PATH = BASE_DIR + 'MODEL/configs/COCO-InstanceSegmentation/mask_rcnn_R_101_FPN_3x.yaml'
MODEL_PATH = BASE_DIR + 'MODEL/COAL_SAMPLING_27MAR/'
MODEL_FILE = 'model_final.pth'
CLASS_JSON = BASE_DIR + 'MODEL/COAL_SAMPLING_27MAR/COAL_SAMPLING_27MAR.json'
LOGS_PATH = BASE_DIR + "/LOGS/"

# Initialize logger
logger = initializeLogger("LOGIC_MANAGER", LOGS_PATH=LOGS_PATH)

_inference_model = None

def initialize_ai_model():
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

def confirm_auger_position(cam1_image_path: str, cam2_image_path: str, target_area: int) -> bool:
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
    failed = False
    try:
        image = cv2.imread(image_path)
        if image is None:
            logger.error(f"Failed to read CAM1 image: {image_path}")
            return False
        
        vis_img = image.copy()
        height, width = image.shape[:2]
        
        # Define Yellow Box (Region of Interest)
        y_x1 = int(width * 0.35)
        y_x2 = int(width * 0.65)
        y_y1 = int(height * 0.50)
        y_y2 = int(height * 0.80)
        
        logger.debug(f"CAM1 Yellow Box: x=[{y_x1}-{y_x2}], y=[{y_y1}-{y_y2}]")
        cv2.rectangle(vis_img, (y_x1, y_y1), (y_x2, y_y2), (0, 255, 255), 4)
        
        # Run AI inference
        masked_img, labellist = _inference_model.run_inference(image)
        cv2.imwrite(f"{image_path.split('.')[0]}_masked.jpg", masked_img)
        
        if not labellist:
            logger.warning("CAM1: No objects detected")
            cv2.imwrite(f"{image_path.split('.')[0]}_vis.jpg", vis_img)
            return False
        
        allowed_classes = {"COAL", "TRUCK_BODY"}
        for detection in labellist:
            if len(detection) < 6:
                continue
                
            class_name = detection[5]
            x1 = int(detection[3])
            y1 = int(detection[1])
            x2 = int(detection[4])
            y2 = int(detection[2])

            if (y_x1 >= x1 and y_x2 <= x2 and y_y1 >= y1 and y_y2 <= y2):
                logger.info(f"CAM1: Yellow box is 100% inside green box {class_name}")
                if class_name not in allowed_classes:
                    logger.warning(f"CAM1: Invalid class {class_name} detected inside yellow box")
                    cv2.rectangle(vis_img, (x1, y1), (x2, y2), (0, 0, 255), 3)  # Red for invalid
                    failed = True
                    break
                else:
                    cv2.rectangle(vis_img, (x1, y1), (x2, y2), (0, 255, 0), 3)  # Green for allowed
                    continue
    
        # Save visualization
        cv2.imwrite(f"{image_path.split('.')[0]}_vis.jpg", vis_img)
        logger.debug(f"CAM1: Detection results saved to {image_path.split('.')[0]}_vis.jpg")
        return not failed
            
    except Exception as e:
        logger.error(f"Error in CAM1 validation: {e}", exc_info=True)
        print(f"ERROR: CAM1 validation error: {e}")

    return False

def _validate_cam2_auger(image_path: str, target_area_num: int) -> bool:
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
        cv2.imwrite(f"{image_path.split('.')[0]}_masked.jpg", masked_img)
        
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
            y_min = int(detection[1])
            y_max = int(detection[2])
            x_min = int(detection[3])
            x_max = int(detection[4])
            
            if class_name == "AUGER_BOTTOM":
                auger_bottom_detection = detection
                logger.debug(f"CAM2: AUGER_BOTTOM detected (bbox: x=[{x_min}-{x_max}], y=[{y_min}-{y_max}])")
            
            elif class_name == "AUGER":
                auger_detection = detection
                logger.debug(f"CAM2: AUGER detected (bbox: x=[{x_min}-{x_max}], y=[{y_min}-{y_max}])")
            
            elif class_name == "AUGER_FRAME":
                auger_frame_detection = detection
                logger.debug(f"CAM2: AUGER_FRAME detected (bbox: x=[{x_min}-{x_max}], y=[{y_min}-{y_max}])")
            
            elif class_name == target_coal_label:
                coal_area_bbox = {
                    'x_min': x_min, 'x_max': x_max,
                    'y_min': y_min, 'y_max': y_max,
                }
                cv2.rectangle(vis_img, (x_min, y_min), (x_max, y_max), (0, 255, 0), 2)
                logger.debug(f"CAM2: Target {target_coal_label} at x=[{x_min}-{x_max}], y=[{y_min}-{y_max}])")
        
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
                if auger_frame_detection is None:
                    logger.error("CAM2: Neither AUGER_BOTTOM nor AUGER detected", exc_info=True)
                    print(f"ERROR: CAM2: Neither AUGER_BOTTOM nor AUGER detected")
                    return False
                else:
                    auger_detection = auger_frame_detection
            
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
        logger.debug(f"{image_path.split('.')[0]}_vis.jpg")
        logger.debug(f"{labellist}")
        cv2.imwrite(f"{image_path.split('.')[0]}_vis.jpg", vis_img)
        logger.debug(f"{image_path.split('.')[0]}_vis.jpg")
        logger.debug(f"{labellist}")
        
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
        
        os.makedirs("".join(save_path.split("/")[:-1]), exist_ok=True)
        qr_img.save(save_path)
        
        logger.info(f"QR code generated: {save_path}")
        return save_path
    except Exception as e:
        logger.error(f"Error generating QR code: {e}", exc_info=True)
        print(f"ERROR: Error generating QR code: {e}")
        return ""

if __name__ == "__main__":
    # generate_qr_code("OMKAR", "1234", "345", "/home/omkar/INSIGHTZZ/PROJECTS/COAL_SAMPLING/COAL_SAMPLING/TEST_QR.png")
    # Test initialization
    if initialize_ai_model():
        logger.info("AI Model ready for use")
    else:
        logger.error("Failed to initialize AI Model", exc_info=True)
        print("ERROR: Failed to initialize AI Model")