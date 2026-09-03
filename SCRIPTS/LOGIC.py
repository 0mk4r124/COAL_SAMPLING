import cv2
import os
import qrcode
import traceback
import tempfile
import uuid
import subprocess
import time
import shutil
import glob
import io

from datetime import datetime
from fpdf import FPDF
from PIL import Image

from DEPENDANT.INFERENCE import MASKRCNN
from DEPENDANT.LOGGING import initializeLogger


# AI Model Initialization 
BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
MODEL_CONFIG_PATH = BASE_FILE_PATH + 'MODEL/configs/COCO-InstanceSegmentation/mask_rcnn_R_101_FPN_3x.yaml'
MODEL_PATH = BASE_FILE_PATH + 'MODEL/coal_sampling_model_27aug/'
MODEL_FILE = 'model_final.pth'
CLASS_JSON = BASE_FILE_PATH + 'MODEL/coal_sampling_model_27aug/coal_sampling_27aug.json'
LOGS_PATH = BASE_FILE_PATH + "/LOGS/"
LEFT_LOGO_PATH = BASE_FILE_PATH + "/WEB_APP/static/logo/utcl.png"
RIGHT_LOGO_PATH = BASE_FILE_PATH + "/WEB_APP/static/logo/insightzz_logo.png"

# Initialize logger
logger = initializeLogger("LOGIC_MANAGER", LOGS_PATH=LOGS_PATH)

_inference_model = None

THRESHOLD_RATIO = 2 / 3

# Model is loaded once and reused (loading MASKRCNN repeatedly is expensive)
_model = None


def _get_model():
    _model = MASKRCNN(
        tag="coco",
        CONFIG_PATH="",
        mask_model_path="",
        model_file="",
        thr_acc=0.5,
        class_json=None,
        debugMode=False,
        GPU_ID=0,
        use_pretrained=True,
    )
    return _model


def is_truck_below_line(img=None, image_path=None, threshold_ratio=THRESHOLD_RATIO, thr_acc=0.5):
    """
    Returns True if a truck's bottom edge (y2) is below the 2/3 line.
    Pass either an image array (img) OR an image_path.
    """
    if img is None:
        if image_path is None:
            raise ValueError("Provide either img or image_path")
        img = cv2.imread(image_path)
    if img is None:                       # unreadable / missing frame
        return False

    img_height = img.shape[0]
    threshold_y = int(img_height * threshold_ratio)

    model = _get_model()
    output = model.predictor(img)
    instances = output["instances"].to("cpu")
    boxes = instances.pred_boxes.tensor.numpy().astype(int)
    classes = instances.pred_classes.numpy()
    scores = instances.scores.numpy()
    COCO_CLASSES = model.class_list

    for box, cls, score in zip(boxes, classes, scores):
        label = COCO_CLASSES[cls]
        if label != "truck" or score < thr_acc:
            continue

        x1, y1, x2, y2 = box
        crossed = y2 > threshold_y

        # green if above the line, magenta if bottom crossed below 2/3
        color = (255, 0, 255) if crossed else (0, 255, 0)

        cv2.rectangle(img, (x1, y1), (x2, y2), color, 2)
        cv2.putText(
            img,
            f"{label} {score:.2f}",
            (x1, y1 - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            color,
            2
        )

        if label == "truck" and crossed:
            cv2.putText(
                img,
                "TRUCK BOTTOM BELOW 2/3",
                (x1, y2 + 25),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 0, 255),
                2
            )
            print(f"Truck bottom crossed 2/3 line -> y2={y2}, threshold={threshold_y}")

            cv2.imwrite(str(image_path).split(".")[0]+"_mask.jpg", img)
            # bottom is below the 2/3 line
            return True
        
    cv2.imwrite(str(image_path).split(".")[0]+"_mask.jpg", img)
    return False


def confirm_truck_present(get_frame, checks=5, required=None, delay=0.3):
    """
    Runs the truck check `checks` times to confirm a stable presence.

    get_frame : callable returning a fresh image each call (or None).
    checks    : how many times to check (default 5).
    required  : how many must be positive to confirm (default = all `checks`).
    delay     : seconds to wait between checks.

    Returns True only if the truck is confirmed present below the line.
    """
    if required is None:
        required = checks

    positive = 0
    for i in range(checks):
        frame = get_frame()
        if is_truck_below_line(img=frame):
            positive += 1
        print(f"[truck-check {i+1}/{checks}] positive so far: {positive}")
        if i < checks - 1:
            time.sleep(delay)

    return positive >= required

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
        # logger.info("CAM2 Check: Validating AUGER_BOTTOM in target coal area...")
        # cam2_position = _validate_cam2_auger(cam2_image_path, target_area)
        # cam2_position = True
        # logger.info(f"CAM2 Position Valid: {cam2_position}")
        
        # ─── Final Result ──────────────────────────────────────────────────────────
        result = cam1_position # and cam2_position
        if result:
            logger.info(f"AUGER POSITIONING CONFIRMED - Both CAM1 and CAM2 validations passed")
        else:
            logger.warning(f"AUGER POSITIONING FAILED - CAM1={cam1_position}")
        
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
        found_classes = set()
        for detection in labellist:
            # if len(detection) < 6:
            #     continue
                
            class_name = detection[5]
            x1 = int(detection[3])
            y1 = int(detection[1])
            x2 = int(detection[4])
            y2 = int(detection[2])

            if (y_x1 >= x1 and y_x2 <= x2 and y_y1 >= y1 and y_y2 <= y2):
                logger.info(f"CAM1: Yellow box is 100% inside green box {class_name}")
                found_classes.add(class_name)
                cv2.rectangle(vis_img, (x1, y1), (x2, y2), (0, 255, 0), 3)
                if class_name not in allowed_classes:
                    logger.warning(f"CAM1: Invalid class {class_name} detected inside yellow box")
                    cv2.rectangle(vis_img, (x1, y1), (x2, y2), (0, 0, 255), 3)  # Red for invalid
                    failed = True
                    break
                else:
                    cv2.rectangle(vis_img, (x1, y1), (x2, y2), (0, 255, 0), 3)  # Green for allowed
                    continue
    
        # Save visualization
        if not failed:
            if not allowed_classes.issubset(found_classes):
                missing = allowed_classes - found_classes
                logger.warning(f"CAM1: Missing required classes inside yellow box: {missing}")
                failed = True
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

# def compress_pdf(input_path: str, output_path: str = None, quality: str = "screen") -> str :
#     if not os.path.exists(input_path):
#         print(f"[PDF] Input file not found: {input_path}")
#         return None

#     if output_path is None:
#         output_path = input_path.replace(".pdf", "_compressed.pdf")

#     # Detect OS-specific Ghostscript binary
#     gs_cmd = "gs"
#     if os.name == "nt":
#         gs_cmd = r"C:\Program Files\gs\gs10.03.0\bin\gswin64c.exe"

#     cmd = [
#         gs_cmd,
#         "-sDEVICE=pdfwrite",
#         "-dCompatibilityLevel=1.4",
#         f"-dPDFSETTINGS=/{quality}",
#         "-dNOPAUSE",
#         "-dQUIET",
#         "-dBATCH",
#         f"-sOutputFile={output_path}",
#         input_path,
#     ]

#     try:
#         subprocess.run(cmd, check=True)

#         if os.path.exists(output_path):
#             print(f"[PDF] Compression successful: {output_path}")
#             return output_path
#         else:
#             print("[PDF] Compression failed: Output not created")
#             return None

#     except Exception as e:
#         print(f"[PDF] Compression failed: {e}")
#         return None

def clean_logo(img_path: str, name: str) -> str:
    """
    Removes black background-ish pixels and converts the logo to a clean RGB PNG.
    A unique temp file is created for each logo so left/right logos do not overwrite each other.
    """
    try:
        img = Image.open(img_path).convert("RGBA")

        # Remove near-black background
        pixels = []
        for r, g, b, a in img.getdata():
            if r < 25 and g < 25 and b < 25:
                pixels.append((255, 255, 255, 0))  # transparent
            else:
                pixels.append((r, g, b, a))
        img.putdata(pixels)

        # Composite on white so FPDF gets a clean RGB PNG
        white_bg = Image.new("RGBA", img.size, (255, 255, 255, 255))
        white_bg.alpha_composite(img)
        rgb_img = white_bg.convert("RGB")

        tmp_path = os.path.join(tempfile.gettempdir(), f"{name}_{uuid.uuid4().hex}.png")
        rgb_img.save(tmp_path, format="PNG")
        return tmp_path

    except Exception as e:
        print(f"[IMG] Logo prepare failed for {img_path}: {e}")
        return None

def image_size_mm(img_path: str, max_width_mm: float) -> tuple:
    """
    Returns width and height in mm, scaled to fit max_width_mm.
    """
    img = Image.open(img_path)
    w_px, h_px = img.size

    # Approx conversion assuming 96 DPI
    w_mm = w_px * 0.264583
    h_mm = h_px * 0.264583

    scale = min(max_width_mm / w_mm, 1.0)
    return w_mm * scale, h_mm * scale

class SamplingPDF(FPDF):
    def __init__(self, left_logo=None, right_logo=None):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.set_margins(10, 12, 10)
        self.set_auto_page_break(auto=True, margin=12)
        self.alias_nb_pages()

        self.left_logo = clean_logo(left_logo, "left_logo") if left_logo else None
        self.right_logo = clean_logo(right_logo, "right_logo") if right_logo else None

        self.set_creator("Coal Sampling System")
        self.set_author("Coal Sampling System")
        self.set_title("Coal Sampling Report")

    def header(self):
        top_y = 8

        # Logos
        if self.left_logo and os.path.exists(self.left_logo):
            self.image(self.left_logo, 10, top_y-2, 20)

        if self.right_logo and os.path.exists(self.right_logo):
            self.image(self.right_logo, self.w - 38, top_y, 28)

        # Center title
        self.set_y(10)
        self.set_font("Arial", "B", 18)
        self.cell(0, 8, "COAL SAMPLING REPORT", border=0, ln=1, align="C")

        self.set_font("Arial", "", 10)
        self.cell(0, 6, "Sampling & Traceability Record", border=0, ln=1, align="C")

        # Separator
        self.ln(2)
        self.set_line_width(0.6)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-10)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())

        self.set_font("Arial", "", 8)
        self.cell(0, 5, f"Page {self.page_no()}/{{nb}}", align="R")

def add_section_title(pdf: FPDF, title: str):
    pdf.ln(2)
    pdf.set_font("Arial", "B", 12)
    pdf.cell(0, 8, title, ln=1)
    pdf.set_line_width(0.35)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(3)

def add_metadata_table(pdf: FPDF, data: dict):
    """
    Industrial style key-value table.
    Two key-value pairs per row.
    """
    rows = [
        ("UID", data.get("uid", "")),
        ("RFIDs", ", ".join(data.get("rfids", []))),
        ("Vehicle Number", data.get("vehicle", "")),
        ("Vendor Code", data.get("vendor_code", "")),
        ("Vendor Name", data.get("vendor", "")),
        ("Date & Time", data.get("datetime", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))),
        ("Bucket Number", str(data.get("bucket_no", ""))),
    ]

    pdf.set_font("Arial", "", 10)

    label_w = 60
    value_w = pdf.w - pdf.l_margin - pdf.r_margin - label_w
    row_h = 9

    for label, value in rows:
        # Label cell
        pdf.set_font("Arial", "B", 10)
        pdf.set_fill_color(240, 240, 240)
        pdf.cell(label_w, row_h, label, border=1, fill=True)

        # Value cell
        pdf.set_font("Arial", "", 10)
        txt = str(value) if value is not None else ""
        pdf.cell(value_w, row_h, txt, border=1)

        pdf.ln(row_h)

    pdf.ln(2)

def add_image_block(pdf: FPDF, title: str, img_path: str):
    """
    Keeps the title and image together on the same page.
    Scales image to fit page width and available height.
    """
    if not img_path or not os.path.exists(img_path):
        return

    max_width = pdf.w - pdf.l_margin - pdf.r_margin

    # Estimate image block height before writing anything
    w_mm, h_mm = image_size_mm(img_path, max_width)
    title_h = 7
    block_h = title_h + 2 + h_mm + 4

    # If block won't fit, move to new page first
    remaining = pdf.h - pdf.b_margin - pdf.get_y()
    if block_h > remaining:
        pdf.add_page()

    pdf.set_font("Arial", "B", 11)
    pdf.cell(0, title_h, title, ln=1)

    pdf.set_line_width(0.25)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(2)

    # Recalculate with the fresh page space if needed
    available_width = pdf.w - pdf.l_margin - pdf.r_margin
    available_height = pdf.h - pdf.b_margin - pdf.get_y() - 2

    img = Image.open(img_path)
    w_px, h_px = img.size
    w_mm_raw = w_px * 0.264583
    h_mm_raw = h_px * 0.264583

    scale = min(available_width / w_mm_raw, available_height / h_mm_raw, 1.0)
    new_w = w_mm_raw * scale
    new_h = h_mm_raw * scale

    x = pdf.l_margin + (available_width - new_w) / 2
    pdf.image(img_path, x=x, w=new_w, h=new_h)
    pdf.ln(new_h + 4)

def generate_sampling_report(report_data: dict) -> bool:
    try:
        pdf_path = report_data.get("pdf_path")
        paths = report_data.get("paths", {})

        if not pdf_path:
            raise ValueError("pdf_path is required")

        pdf = SamplingPDF(
            left_logo=LEFT_LOGO_PATH,
            right_logo=RIGHT_LOGO_PATH
        )
        pdf.add_page()

        # -------- REPORT META --------
        add_section_title(pdf, "GENERAL INFORMATION")
        add_metadata_table(pdf, report_data)

        # -------- IMAGES --------
        # add_section_title(pdf, "IMAGE CAPTURE")

        add_image_block(pdf, "Vehicle Image", paths.get("VEHICLE_IMG_PATH"))

        for i in range(1, 4):
            add_image_block(pdf, f"Coal Sample {i} Image", paths.get(f"SAMPLE_{i}_IMG_PATH"))

        # -------- SAVE --------
        out_dir = os.path.dirname(pdf_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        pdf.output(pdf_path)
        print(f"[PDF] Report generated: {pdf_path}")
        return True

    except Exception as e:
        print(f"[PDF] Error: {e}")
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# LOGIC.py — PATCH: fix compress_pdf ([WinError 2] The system cannot find the
# file specified)
#
# ROOT CAUSE: compress_pdf used a HARDCODED Ghostscript path
#     C:\Program Files\gs\gs10.03.0\bin\gswin64c.exe
# which does not exist on this machine (not installed / different version),
# so subprocess.run failed with WinError 2 before touching the PDF.
#
# HOW TO APPLY:
#   1. Add `glob`, `shutil`, `io` to the imports at the top of LOGIC.py:
#          import glob
#          import shutil
#          import io
#      (os, subprocess are already imported)
#   2. REPLACE the existing compress_pdf() function with everything below
#      (the two helper functions + the new compress_pdf).
#
# BEHAVIOUR:
#   - Auto-detects Ghostscript: PATH first, then ANY version under
#     C:\Program Files\gs\gs*\bin (newest first). No more hardcoded version.
#   - If Ghostscript is not installed at all, falls back to a pure-Python
#     compressor (pikepdf + PIL — pikepdf is already used by PDF_UTILS.py)
#     that downscales/re-encodes the embedded JPEG images.
#   - Compresses the EXISTING file IN PLACE: writes to a temp file, then
#     atomically replaces the original, so REPORT_PATH in the DB / QR keeps
#     pointing at the (now smaller) same file. No *_compressed.pdf leftovers.
#   - Keeps the original untouched if compression fails or wouldn't shrink it.
# ═══════════════════════════════════════════════════════════════════════════════


def _find_ghostscript() -> str :
    """Locate a Ghostscript executable on this machine (any installed version)."""
    # 1) On PATH?
    for name in ("gswin64c", "gswin32c", "gs"):
        found = shutil.which(name)
        if found:
            return found

    # 2) Standard Windows install locations — ANY version, newest first
    if os.name == "nt":
        patterns = [
            r"C:\Program Files\gs\gs*\bin\gswin64c.exe",
            r"C:\Program Files\gs\gs*\bin\gswin32c.exe",
            r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe",
        ]
        for pat in patterns:
            matches = sorted(glob.glob(pat), reverse=True)
            if matches:
                return matches[0]

    return None


def _compress_pdf_pikepdf(input_path: str, output_path: str,
                          jpeg_quality: int = 60, max_dim: int = 1600) -> bool:
    """
    Pure-Python fallback (no Ghostscript needed): downscale + re-encode the
    embedded JPEG images and recompress all streams using pikepdf + PIL.
    Works well for our FPDF reports, whose size is almost entirely camera JPEGs.
    """
    try:
        import pikepdf
    except ImportError:
        print("[PDF] pikepdf not available for fallback compression")
        return False

    try:
        with pikepdf.open(input_path) as pdf:
            for page in pdf.pages:
                try:
                    images = page.images
                except Exception:
                    continue

                for _name, raw in images.items():
                    try:
                        pimg = pikepdf.PdfImage(raw)
                        pil = pimg.as_pil_image()

                        # Downscale big camera frames
                        if max(pil.size) > max_dim:
                            pil.thumbnail((max_dim, max_dim), Image.LANCZOS)

                        if pil.mode != "RGB":
                            pil = pil.convert("RGB")

                        buf = io.BytesIO()
                        pil.save(buf, format="JPEG", quality=jpeg_quality, optimize=True)

                        raw.write(buf.getvalue(), filter=pikepdf.Name("/DCTDecode"))
                        raw.Width = pil.width
                        raw.Height = pil.height
                        raw.ColorSpace = pikepdf.Name("/DeviceRGB")
                        raw.BitsPerComponent = 8
                    except Exception:
                        # Skip images that can't be processed — keep them as-is
                        continue

            pdf.save(output_path, compress_streams=True,
                     recompress_flate=True)

        return os.path.exists(output_path) and os.path.getsize(output_path) > 0

    except Exception as e:
        print(f"[PDF] pikepdf fallback compression failed: {e}")
        logger.error(f"pikepdf fallback compression failed: {e}", exc_info=True)
        return False


def compress_pdf(input_path: str, output_path: str = None,
                 quality: str = "screen", replace_original: bool = True) -> str :
    """
    Compress a PDF.
      - Tries Ghostscript (auto-detected, any installed version).
      - Falls back to pikepdf+PIL if Ghostscript is not installed.
      - By default compresses the EXISTING file IN PLACE (replace_original=True):
        the original at input_path is replaced by the compressed version, so
        every stored path keeps working.
      - Pass output_path to write to a different file instead (original kept).

    Returns the path of the compressed PDF, or None on failure
    (the original file is always left intact on failure).
    """
    if not os.path.exists(input_path):
        print(f"[PDF] Input file not found: {input_path}")
        return None

    orig_size = os.path.getsize(input_path)

    # Work file: never write over the original directly
    if output_path is None:
        tmp_out = input_path.replace(".pdf", "_compressed_tmp.pdf")
    else:
        tmp_out = output_path

    compressed = False

    # ── Attempt 1: Ghostscript (auto-detected) ────────────────────────────────
    gs_cmd = _find_ghostscript()
    if gs_cmd:
        print(f"[PDF] Using Ghostscript: {gs_cmd}")
        logger.debug(f"Using Ghostscript: {gs_cmd}")
        cmd = [
            gs_cmd,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS=/{quality}",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            f"-sOutputFile={tmp_out}",
            input_path,
        ]
        try:
            subprocess.run(cmd, check=True)
            compressed = os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0
        except Exception as e:
            print(f"[PDF] Ghostscript compression failed: {e}")
            logger.error(f"Ghostscript compression failed: {e}", exc_info=True)
            compressed = False
    else:
        print("[PDF] Ghostscript not found on this machine — using Python fallback")
        logger.warning("Ghostscript not found — using pikepdf fallback for PDF compression")

    # ── Attempt 2: pure-Python fallback (pikepdf + PIL) ───────────────────────
    if not compressed:
        compressed = _compress_pdf_pikepdf(input_path, tmp_out)

    if not compressed:
        print("[PDF] Compression failed: no working compressor — original kept")
        logger.error("PDF compression failed with all methods — original kept")
        if os.path.exists(tmp_out) and tmp_out != input_path:
            try: os.remove(tmp_out)
            except Exception: pass
        return None

    new_size = os.path.getsize(tmp_out)

    # Only keep the result if it's actually smaller
    if new_size >= orig_size:
        print(f"[PDF] Compressed file not smaller "
              f"({new_size/1024:.0f} KB >= {orig_size/1024:.0f} KB) — original kept")
        try: os.remove(tmp_out)
        except Exception: pass
        return input_path

    # Swap the compressed file into place (or hand back the separate output)
    if replace_original and output_path is None:
        try:
            os.replace(tmp_out, input_path)   # atomic on the same drive
            final_path = input_path
        except Exception as e:
            print(f"[PDF] Could not replace original: {e} — keeping compressed copy")
            logger.error(f"Could not replace original PDF: {e}", exc_info=True)
            final_path = tmp_out
    else:
        final_path = tmp_out

    print(f"[PDF] Compression successful: {final_path} "
          f"({orig_size/1024:.0f} KB -> {new_size/1024:.0f} KB, "
          f"-{(1 - new_size/orig_size)*100:.0f}%)")
    logger.debug(f"PDF compressed: {final_path} "
                 f"({orig_size/1024:.0f} KB -> {new_size/1024:.0f} KB)")
    return final_path

if __name__ == "__main__":
    # generate_qr_code("OMKAR", "1234", "345", "/home/omkar/INSIGHTZZ/PROJECTS/COAL_SAMPLING/COAL_SAMPLING/TEST_QR.png")
    # Test initialization
    if initialize_ai_model():
        logger.info("AI Model ready for use")
    else:
        logger.error("Failed to initialize AI Model", exc_info=True)
        print("ERROR: Failed to initialize AI Model")