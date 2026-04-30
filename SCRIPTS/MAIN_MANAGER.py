import os
import time
import random
import pymysql
import subprocess
import traceback

from datetime import datetime
from enum import Enum, auto
from fpdf import FPDF

from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger

from LOGIC import (
    initialize_ai_model,
    confirm_auger_position,
    generate_sampling_report,
    compress_pdf
)

# ── Database ──────────────────────────────────────────────────────────────────
DB_HOST = "127.0.0.1"
DB_USER = "root"
DB_PASS = "insightzz@123"
DB_NAME = "COAL_SAMPLING_DHAR"

# ── Tuning ────────────────────────────────────────────────────────────────────
DB_POLL_SEC     = 10 # Poll DB every x seconds while waiting for entry after RFID read
DB_WAIT_TIMEOUT = 600 # Wait up to x seconds for DB entry to appear after RFID read before aborting
TOTAL_CYCLES    = 3
HOME_POSITION_TIMEOUT = 200 # Wait up to x seconds for auger to return to home position before aborting
SAMPLE_CYCLE_TIMEOUT = 600
POSITION_CONFIRMATION_TIMEOUT = 300
CLOSE_CYCLE_WAIT_TIME = 50 # Wait time after close cycle command before checking for completion - allows PLC to process command and start movement
SET_BUCKET_WAIT_TIMEOUT = 120 # Wait up to x seconds for bucket set confirmation before aborting
MOVEMENT_DURATION = 2 # Duration to move in each direction during auger position confirmation loops (in seconds)

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
TEMP_IMG_PATH = os.path.join(BASE_FILE_PATH, "TEMP_IMG")
RESULT_IMG_PATH = os.path.join(BASE_FILE_PATH, "RESULT")
INF_IMG = os.path.join(BASE_FILE_PATH, "INF")
LOGS_PATH = os.path.join(BASE_FILE_PATH, "LOGS")

IN_TOPICS = (
    "camera/status",
    "plc_barrier/status",
    "plc_sampler/status",
    "rfid/status",
)

# Initialize logger
logger = initializeLogger("MAIN_MANAGER", LOGS_PATH=LOGS_PATH)

def build_rfid_key(rfids, uid=None):
    if isinstance(rfids, str):
        rfids = rfids.split("|")

    rfids = [r.strip() for r in rfids if r and r.strip()]
    rfids = sorted(set(rfids))

    if not rfids:
        return uid or ""

    if len(rfids) == 1:
        return f"{uid}|{rfids[0]}" if uid else rfids[0]

    return "|".join(rfids)

def get_sample_positions(used_areas=None, prev_points=None):
    if used_areas is None:
        used_areas = []

    if prev_points is None:
        prev_points = []

    # Define all possible areas
    all_areas = set(range(1, 4))
    available_areas = list(all_areas - set(used_areas))

    if not available_areas:
        print("All areas are already used")
        return None

    # Pick random available area
    area = random.choice(available_areas)

    # Global bounds
    x_min, x_max = 35, 100
    y_min, y_max = 45, 80

    # Grid split
    x_splits = 3
    y_splits = 1

    x_step = (x_max - x_min) / x_splits
    y_step = (y_max - y_min) / y_splits

    # Map area --> grid index
    row = (area - 1) // x_splits
    col = (area - 1) % x_splits

    # Cell bounds
    x_start = x_min + col * x_step
    x_end = x_start + x_step

    y_start = y_min + row * y_step
    y_end = y_start + y_step

    # Minimum separation (18% of total width)
    min_x_gap = int((x_max - x_min) * 0.18)

    # Try multiple times to satisfy separation
    for _ in range(10):
        x = random.randint(int(x_start) + 5, int(x_end) - 5)
        y = random.randint(int(y_start), int(y_end) - 1)

        # Check distance from previous points
        if all(abs(x - p["x"]) >= min_x_gap for p in prev_points):
            return {
                "x": x,
                "y": y,
                "area": area
            }

    # Fallback (if constraint fails after retries)
    return {
        "x": x,
        "y": y,
        "area": area
    }

# ── State machine ─────────────────────────────────────────────────────────────
class State(Enum):
    IDLE                     = auto()
    DB_CHECK                 = auto()
    WAITING_FOR_DB           = auto()
    OPEN_BARRIER             = auto()
    BARRIER_OPENING          = auto()
    SET_BUCKET               = auto()
    VEHICLE_PLACEMENT        = auto()
    CLOSE_BARRIER            = auto()
    BARRIER_CLOSING          = auto()
    AUGER_HOME_POS           = auto()
    CYCLE_POSITION           = auto()
    CYCLE_CONFIRM            = auto()
    CYCLE_CAPTURE            = auto()
    CYCLE_DONE               = auto()
    SAMPLE_COLLECTION        = auto()
    CYCLE_EMERGENCY_WAIT     = auto()
    RED_SIGNAL               = auto()
    COMPLETE_FINAL           = auto()
    GREEN_SIGNAL             = auto()
    COMPLETE                 = auto()
    ERROR                    = auto()

# ── Database helpers ──────────────────────────────────────────────────────────
def _db_connect():
    return pymysql.connect(
        host=DB_HOST, user=DB_USER, passwd=DB_PASS, db=DB_NAME,
        autocommit=False
    )

def db_resolve_bucket(rfid: str, vendor_code: str) -> int:
    db = None
    try:
        db = _db_connect()
        cur = db.cursor(pymysql.cursors.DictCursor)

        # Exact match instead of LIKE
        cur.execute(
            """
            SELECT vl.BUCKET_NO, vm.VENDOR_CODE
            FROM VEHICLE_LOGS vl
            LEFT JOIN VEHICLE_MASTER vm
                ON vl.RFIDS = vm.RFID
            WHERE vl.CREATE_TIME >= CURDATE()
              AND vl.CREATE_TIME < CURDATE() + INTERVAL 1 DAY
              AND vl.STATUS = 'COMPLETED'
            """
        )
        rows = cur.fetchall()

        if not rows:
            return 1

        used_buckets = set()
        vendor_bucket = None

        for row in rows:
            logger.debug(f"Bucket row: {row}")
            bucket = row.get("BUCKET_NO")
            vc = row.get("VENDOR_CODE")

            if bucket:
                bucket = int(bucket)
                used_buckets.add(bucket)

                # Same vendor → reuse bucket
                if vc == vendor_code and vendor_bucket is None:
                    vendor_bucket = bucket

        logger.debug(f"vendor bucket: {vendor_bucket} || used buckets: {used_buckets}")
        if vendor_bucket:
            return vendor_bucket

        # Next free bucket
        for i in range(1, 11):
            if i not in used_buckets:
                return i

        # Wrap around
        return (max(used_buckets) % 10) + 1

    except Exception as e:
        print(f"[DB] db_resolve_bucket error: {e}")
        logger.error(f"{traceback.format_exc()}")
        return 1

    finally:
        if db:
            db.close()

def db_find_vehicle(rfids: str, uid: str) -> dict | None:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor(pymysql.cursors.DictCursor)

        rfid_key = build_rfid_key(rfids, uid)
        print(f"[DB] Looking up vehicle with RFID key: {rfid_key}")

        cur.execute(
            """
            SELECT
                vm.RFID,
                vm.VEHICLE_NUMBER,
                vm.VENDOR_CODE,
                vr.VENDER_NAME
            FROM VEHICLE_MASTER vm
            LEFT JOIN VENDOR_MASTER vr 
                ON vr.VENDOR_CODE = vm.VENDOR_CODE
            WHERE vm.RFID = %s
            LIMIT 1
            """,
            (rfid_key,)
        )
        row = cur.fetchone()
        db.commit()

        return row

    except Exception as e:
        print(f"[DB] db_find_vehicle error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db:
            db.close()

def db_vehicle_already_in_front(rfids: list, uid: str) -> bool:
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()

        rfid_key = build_rfid_key(rfids, uid)

        query = """
            SELECT COUNT(*) FROM VEHICLE_LOGS
            WHERE STATUS = 'IN_PROGRESS'
              AND RFIDS = %s
        """
        cur.execute(query, (rfid_key,))
        row = cur.fetchone()
        db.commit()

        return (row[0] > 1) if row else False
    except Exception as e:
        print(f"[DB] db_vehicle_already_in_front error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db:
            db.close()

def db_create_log(uid: str, rfids: list, bucket_no: str, paths: dict) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        now = datetime.now()
        cur.execute(
            """
            INSERT INTO VEHICLE_LOGS
                (UID, RFIDS, STATUS, BUCKET_NO, CREATE_TIME, 
                VEHICLE_IMG_PATH, 
                SAMPLE_1_IMG_PATH, 
                SAMPLE_2_IMG_PATH, 
                SAMPLE_3_IMG_PATH, 
                REPORT_PATH)
            VALUES
                (
                %s, %s, %s, %s, %s, 
                %s, 
                %s, 
                %s,
                %s,
                %s
                )
            """,
            (uid, build_rfid_key(rfids, uid), "IN_PROGRESS", bucket_no,  now, 
                paths.get("VEHICLE_IMG_PATH"), 
                paths.get("SAMPLE_1_IMG_PATH"), 
                paths.get("SAMPLE_2_IMG_PATH"), 
                paths.get("SAMPLE_3_IMG_PATH"), 
                paths.get("REPORT_PATH")
            )
        )
        db.commit()
        print(f"[DB] Log created  uid={uid}  rfids={rfids}")
        return True
    except Exception as e:
        print(f"[DB] db_create_log error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()

def db_bucket_update_log(uid: str, bucket_no: int) -> bool:
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()

        cur.execute(
            """
            UPDATE VEHICLE_LOGS
            SET BUCKET_NO = %s,
                UPDATE_TIME = %s
            WHERE UID = %s
            ORDER BY CREATE_TIME DESC
            LIMIT 1
            """,
            (bucket_no, datetime.now(), uid)
        )

        db.commit()
        print(f"[DB] Bucket updated uid={uid} bucket={bucket_no}")
        return True

    except Exception as e:
        print(f"[DB] db_bucket_update_log error: {e}")
        logger.error(f"{traceback.format_exc()}")
        return False

    finally:
        if db:
            db.close()

def db_error_log(uid: str, msg: str) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        cur.execute(
            """
            UPDATE VEHICLE_LOGS
               SET STATUS = 'ERROR', ERROR_MESSAGE = %s, UPDATE_TIME = %s
            WHERE UID = %s
            """,
            (msg, datetime.now(), uid)
        )
        db.commit()
    except Exception as e:
        print(f"[DB] db_complete_log error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()

def db_complete_log(uid: str) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        cur.execute(
            """
            UPDATE VEHICLE_LOGS
               SET STATUS = 'COMPLETED', UPDATE_TIME = %s
            WHERE UID = %s
            """,
            (datetime.now(), uid)
        )
        db.commit()
    except Exception as e:
        print(f"[DB] db_complete_log error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()

def db_add_plc_comm(uid: str, state: str) -> bool:
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()
        now = datetime.now()
        
        cur.execute(
            """
            INSERT INTO PLC_COMM
                (UID, STATE, UPDATED)
            VALUES
                (%s, %s, %s)
            """,
            (uid, state, now)
        )
        
        db.commit()
        return True
    except Exception as e:
        print(f"[DB] db_update_plc_comm error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()
    
    return False

def db_update_plc_comm(uid: str, state: str, emergency: str = None, auto_manual: str = None) -> bool:
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()
        now = datetime.now()
        
        # Try to update first
        cur.execute(
            """
            UPDATE PLC_COMM
               SET STATE = %s, EMERGENCY = %s, AUTO_MANUAL = %s, UPDATED = %s
            WHERE UID = %s
            """,
            (state, emergency, auto_manual, now, uid)
        )
        
        db.commit()
        return True
    except Exception as e:
        print(f"[DB] db_update_plc_comm error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()
    
    return False

# ------------------------------------------------------------------------------
class Manager:

    HANDLERS = {
        State.IDLE                 : "_handle_idle",
        State.DB_CHECK             : "_handle_db_check",
        State.WAITING_FOR_DB       : "_handle_waiting_for_db",
        State.OPEN_BARRIER         : "_handle_open_barrier",
        State.BARRIER_OPENING      : "_handle_barrier_opening",
        State.SET_BUCKET           : "_handle_set_bucket",
        State.VEHICLE_PLACEMENT    : "_handle_vehicle_placement",
        State.RED_SIGNAL           : "_handle_red_signal",
        State.CLOSE_BARRIER        : "_handle_close_barrier",
        State.BARRIER_CLOSING      : "_handle_barrier_closing",
        State.AUGER_HOME_POS       : "_handle_auger_home_pos",
        State.CYCLE_POSITION       : "_handle_cycle_position",
        State.CYCLE_CONFIRM        : "_handle_cycle_confirm",
        State.CYCLE_CAPTURE        : "_handle_cycle_capture",
        State.CYCLE_DONE           : "_handle_cycle_done",
        State.SAMPLE_COLLECTION    : "_handle_all_samples_collection",
        State.CYCLE_EMERGENCY_WAIT : "_handle_cycle_emergency_wait",
        State.COMPLETE_FINAL       : "_handle_complete_final",
        State.GREEN_SIGNAL         : "_handle_green_signal",
        State.COMPLETE             : "_handle_complete",
        State.ERROR                : "_handle_error",
    }

    def __init__(self):
        self.mqtt = MQTT("MAIN_MANAGER")

        for topic in IN_TOPICS:
            self.mqtt.subscribe(topic)

        self.state     = State.IDLE
        self.uid       : str | None  = None
        self.rfids     : list        = []
        self.vehicle   : dict | None = None
        self.positions : list        = []
        self.cycle     : int         = 0
        self.date_file : str         = ""
        self.ai_model  : bool        = False
        self.bucket_no : int         = 1  # Store bucket number for the session
        self._emergency_return_state: State | None = State.CYCLE_CONFIRM  # Track which state to resume to after emergency

        self._state_entered_at: float = time.time()
        self._db_last_polled  : float = 0.0
        
        # AI Model initialization
        if initialize_ai_model():
            self.ai_model = True
            logger.debug("[MANAGER] Warning: AI Model initialized")
            print("[MANAGER] Warning: AI Model initialized")
        
        # Sampling position index tracker
        self._current_sample_index = 0
        self._successful_cycles = 0
        
        # Auger position confirmation tracking
        self._confirmation_loop_count = 0
        self._confirmation_results = []

    def _pop(self, topic: str) -> dict | None:
        return self.mqtt.pop(topic)

    def _cam(self, **kw):     self.mqtt.publish("manager/camera",      kw)
    def _barrier(self, **kw): self.mqtt.publish("manager/plc_barrier", kw)
    def _sampler(self, **kw): self.mqtt.publish("manager/plc_sampler", kw)
    def _printer(self, **kw): self.mqtt.publish("manager/printer",     kw)
    def _rfid(self, **kw):    self.mqtt.publish("manager/rfid",     kw)

    def _flush_topic(self, topic: str):
        flushed = 0
        while True:
            msg = self._pop(topic)
            if not msg:
                break
            flushed += 1
        print(f"[MANAGER] Flushed {flushed} messages from {topic}")
        logger.debug(f"[MANAGER] Flushed {flushed} messages from {topic}")

    def _goto(self, new_state: State):
        print(f"[MANAGER] State: {self.state.name} --> {new_state.name}")
        logger.info(f"State: {self.state.name} --> {new_state.name} -- {self._elapsed():.1f}s")
        self.state             = new_state
        self._state_entered_at = time.time()
        
        # Update database with new state
        if self.uid:
            db_update_plc_comm(self.uid, new_state.name)

    def _elapsed(self) -> float:
        return time.time() - self._state_entered_at

    def _resolve_rfid(self, rfids: list) -> dict | None:
        row = db_find_vehicle(rfids, self.uid)
        if row:
            logger.debug(f"[DB] Vehicle resolved: {row}")
            return row
        return None
    
    def _wait_for_file(self, check_path_1: str, check_path_3: str, timeout: float = 10.0) -> bool:
        start = time.time()
        cam1_done = False
        cam3_done = True

        while time.time() - start < timeout:
            msg = self._pop("camera/status")

            if msg:
                status = msg.get("status")
                path = msg.get("path")

                if status == "cam1_done":
                    if path == check_path_1 and os.path.exists(path):
                        cam1_done = True
                        print(f"[WAIT] CAM1 done: {path}")

                elif status == "cam3_done":
                    if path == check_path_3 and os.path.exists(path):
                        cam3_done = True
                        print(f"[WAIT] CAM3 done: {path}")

            # Exit early if both done
            if cam1_done and cam3_done:
                return True

            time.sleep(0.05)  # small yield, not 0.5

        print(f"[MANAGER] Timeout -> cam1={cam1_done}, cam3={cam3_done}")
        logger.debug(f"Timeout -> cam1={cam1_done}, cam3={cam3_done}")
        return False
    
    def _capture_images_for_confirmation(self, loop_num: int, movement_type: str = "") -> tuple[str, str] | None:
        try:
            inf_dir = os.path.join(INF_IMG, self.date_file, self.uid)
            os.makedirs(inf_dir, exist_ok=True)
            
            suffix = f"_loop{loop_num}_{movement_type}" if movement_type else f"_loop{loop_num}"
            cam1_path = os.path.join(inf_dir, f"CAM1{suffix}.jpg")
            cam3_path = os.path.join(inf_dir, f"CAM3{suffix}.jpg")

            print(f"[MANAGER] Capturing images (Loop {loop_num}, {movement_type})")

            # Trigger capture
            self._cam(action="cam1_single", cam="cam1", path=cam1_path)
            self._cam(action="cam3_single", cam="cam3", path=cam3_path)

            # Wait deterministically instead of sleep
            ok1 = self._wait_for_file(cam1_path, cam3_path)

            if ok1:
                print(f"[MANAGER] Capture OK: {cam1_path}, {cam3_path}")
                logger.debug(f"Capture OK: {cam1_path}, {cam3_path}")
                return (cam1_path, cam3_path)

            return None

        except Exception as e:
            print(f"[MANAGER] Error capturing images: {e}")
            logger.error(f"{traceback.format_exc()}")
            return None

    def _confirm_auger_position_with_movement_loop(self, target_area: int) -> bool:
        confirmations = []
        
        try:
            logger.debug(f"[MANAGER] Starting 5-loop auger position confirmation (Target Area: {target_area})")
            
            # ─── Loop 1: Original Position ─────────────────────────────────────────
            print("\n[MANAGER] --- Loop 1/5: Original Position ---")
            images = self._capture_images_for_confirmation(1, "original")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                logger.debug(f"[MANAGER] Loop 1 Result: {'PASS' if result else 'FAIL'}")
                print(f"[MANAGER] Loop 1 Result: {'PASS' if result else 'FAIL'}")
                if result: return True  # If original position is correct, no need to continue loops
            else:
                print("[MANAGER] Loop 1: Image capture failed")
                confirmations.append(False)
            
            # ─── Loop 2: Move Right ────────────────────────────────────────────────
            print("\n[MANAGER] --- Loop 2/5: Move Right ---")
            print(f"[MANAGER] Moving Y-axis right for {MOVEMENT_DURATION}s")
            self._sampler(action="move_y_right", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)  # Wait for movement to complete
            
            images = self._capture_images_for_confirmation(2, "right")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 2 Result: {'PASS' if result else 'FAIL'}")
                if result: return True  # If original position is correct, no need to continue loops
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving Y-axis left for {MOVEMENT_DURATION}s (returning to original)")
            self._sampler(action="move_y_left", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            # ─── Loop 3: Move Left ─────────────────────────────────────────────────
            print("\n[MANAGER] --- Loop 3/5: Move Left ---")
            print(f"[MANAGER] Moving Y-axis left for {MOVEMENT_DURATION}s")
            self._sampler(action="move_y_left", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            images = self._capture_images_for_confirmation(3, "left")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 3 Result: {'PASS' if result else 'FAIL'}")
                if result: return True  # If original position is correct, no need to continue loops
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving Y-axis right for {MOVEMENT_DURATION}s (returning to original)")
            self._sampler(action="move_y_right", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            # ─── Loop 4: Move Forward ──────────────────────────────────────────────
            print("\n[MANAGER] --- Loop 4/5: Move Forward ---")
            print(f"[MANAGER] Moving X-axis forward for {MOVEMENT_DURATION}s")
            self._sampler(action="move_x_forward", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            images = self._capture_images_for_confirmation(4, "forward")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 4 Result: {'PASS' if result else 'FAIL'}")
                if result: return True  # If original position is correct, no need to continue loops
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving X-axis reverse for {MOVEMENT_DURATION}s (returning to original)")
            self._sampler(action="move_x_reverse", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 1)
            
            # ─── Loop 5: Move Reverse ──────────────────────────────────────────────
            print("\n[MANAGER] --- Loop 5/5: Move Reverse ---")
            print(f"[MANAGER] Moving X-axis reverse for {MOVEMENT_DURATION}s")
            self._sampler(action="move_x_reverse", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 1)
            
            images = self._capture_images_for_confirmation(5, "reverse")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 5 Result: {'PASS' if result else 'FAIL'}")
                if result: return True  # If original position is correct, no need to continue loops
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving X-axis forward for {MOVEMENT_DURATION}s (returning to original)")
            self._sampler(action="move_x_forward", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 1)
            
            # ─── Summary ────────────────────────────────────────────────────────────
            self._confirmation_results = confirmations

            # Return True if any loop out of 5 loops pass
            final_result = any(confirmations)
            return final_result
        
        except Exception as e:
            print(f"[MANAGER] Error in auger position confirmation loop: {e}")
            logger.error(f"{traceback.format_exc()}")
            return False

    # ── State handlers ────────────────────────────────────────────────────────

    def _handle_idle(self):
        msg = self._pop("rfid/status")
        
        if not msg:
            return
        
        self.uid   = msg["uid"]
        self.date_file = self.uid[:8]  # Assuming UID starts with YYYYMMDD
        self.rfids = msg.get("rfids", [])
        self.paths = {
            "VEHICLE_IMG_PATH": normalize_path(os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "VEHICLE_IMG.jpg")),
            "SAMPLE_1_IMG_PATH": normalize_path(os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "SAMPLE_1_IMG.jpg")),
            "SAMPLE_2_IMG_PATH": normalize_path(os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "SAMPLE_2_IMG.jpg")),
            "SAMPLE_3_IMG_PATH": normalize_path(os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "SAMPLE_3_IMG.jpg")),
            "REPORT_PATH": normalize_path(os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, f"REPORT_{self.uid}.pdf")),
        }

        print(f"[MANAGER] RFID event uid={self.uid}  tags={self.rfids}")
        logger.debug(f"Started event uid={self.uid}  tags={self.rfids}")

        self._barrier(action="red_signal")
        time.sleep(1)
        self._goto(State.DB_CHECK)

    def _handle_db_check(self):
        msg = self._pop("plc_barrier/status")

        if msg and msg.get("action") == "red_sent":
            print("[MANAGER] Red Signal set !")
            logger.debug("Red Signal set !")
        
        vehicle = self._resolve_rfid(self.rfids)
        
        if vehicle:
            print(f"[MANAGER] Vehicle found in DB: {vehicle['VEHICLE_NUMBER']} ({vehicle['VENDER_NAME']})")
            logger.debug(f"[MANAGER] Vehicle found in DB: {vehicle['VEHICLE_NUMBER']} ({vehicle['VENDER_NAME']})")
            self.vehicle = vehicle
            vendor_code = vehicle.get("VENDOR_CODE")
            rfid_key = build_rfid_key(self.rfids, self.uid)
            self.bucket_no = db_resolve_bucket(rfid_key, vendor_code)
            
            if db_vehicle_already_in_front("|".join(self.rfids), self.uid):
                print("[MANAGER] Vehicle already in front — aborting.")
                logger.debug(f"Vehicle already in front {self.uid} — aborting.")
                self._reset()
                return
            
            # Create log entry
            db_create_log(self.uid, self.rfids, str(self.bucket_no), self.paths)
            db_add_plc_comm(self.uid, self.state.name)
            self._goto(State.OPEN_BARRIER)
        else:
            print("[MANAGER] RFID not in DB — will poll ")
            logger.info("RFID not in DB — will poll ")
            db_create_log(self.uid, self.rfids, str(self.bucket_no), self.paths)
            self._db_last_polled = time.time()
            self._goto(State.WAITING_FOR_DB)

    def _handle_waiting_for_db(self):

        if self._elapsed() > DB_WAIT_TIMEOUT:
            print("[MANAGER] DB wait timed out — resetting.")
            logger.debug(f"DB wait timed out {self.uid} — resetting.")
            self._reset("DB wait timed out — resetting")
            return

        if time.time() - self._db_last_polled < DB_POLL_SEC:
            return

        self._db_last_polled = time.time()
        print(f"[MANAGER] Polling DB for vehicle with RFID ... {build_rfid_key(self.rfids, self.uid)}")
        logger.info(f"Polling DB for vehicle with RFID ... {build_rfid_key(self.rfids, self.uid)}")

        vehicle = self._resolve_rfid(self.rfids)

        if vehicle:
            self.vehicle = vehicle
            vendor_code = vehicle.get("VENDOR_CODE")
            rfid_key = build_rfid_key(self.rfids, self.uid)
            self.bucket_no = db_resolve_bucket(rfid_key, vendor_code)
            
            db_bucket_update_log(self.uid, self.bucket_no)
            db_add_plc_comm(self.uid, self.state.name)
            time.sleep(1)

            print(f"[MANAGER] Vehicle now in DB: {vehicle['VEHICLE_NUMBER']} ({vehicle['VENDER_NAME']})")
            logger.debug(f"Vehicle now in DB: {vehicle['VEHICLE_NUMBER']} ({vehicle['VENDER_NAME']})")

            # refreshing vehicle to get correct bucket number
            vehicle = self._resolve_rfid(self.rfids)
            self.vehicle = vehicle
            self._barrier(action="green_signal")
            time.sleep(2)
            self._goto(State.OPEN_BARRIER)

    def _handle_open_barrier(self):
        msg = self._pop("plc_barrier/status")

        if msg and msg.get("action") == "green_sent":
            print("[MANAGER] Green Signal set !")
            logger.debug("Green Signal set !")

        print("[MANAGER] Sending open barrier command ")
        self._cam(action="cam2_single", path=self.paths["VEHICLE_IMG_PATH"])
        self._barrier(action="open_barrier")
        time.sleep(2)
        
        self._goto(State.BARRIER_OPENING)

    def _handle_barrier_opening(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        if status == "barrier_opened":
            print("[MANAGER] Barrier opened — waiting for AI confirmation ")
            self._goto(State.SET_BUCKET)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_set_bucket(self):
        bucket_no = self.bucket_no
        print(f"[MANAGER] Setting bucket to {bucket_no} ")
        logger.debug(f"Setting bucket to {bucket_no} ")
        self._barrier(action="set_bucket", bucket_no=bucket_no)
        time.sleep(5)
        
        # Wait for bucket confirmation
        now = time.time()
        while (time.time() - now) < SET_BUCKET_WAIT_TIMEOUT:
            msg = self._pop("plc_barrier/status")
            if msg and msg.get("status") == "bucket_set":
                self._barrier(action="check_truck")
                print(f"[MANAGER] Bucket {bucket_no} confirmed.")
                logger.debug(f"[MANAGER] Bucket {bucket_no} confirmed.")
                self._goto(State.VEHICLE_PLACEMENT)
                return
            
            time.sleep(0.1)
        
        print("[MANAGER] Bucket set timeout — continuing anyway")
        self._barrier(action="check_truck")
        self._goto(State.VEHICLE_PLACEMENT)

    def _handle_vehicle_placement(self):
        msg = self._pop("plc_barrier/status")
        
        # Check if truck is present at both positions
        if not msg or msg.get("status") == "truck_not_present":
            print("[MANAGER] Waiting for truck presence ")
            self._barrier(action="check_truck")
            time.sleep(3)
            return
        
        if msg.get("status") == "truck_present":
            print("[MANAGER] Truck placement confirmed.")

            # Check for AUTO/MANUAL signal
            self._sampler(action="auto_manual")
            time.sleep(3)
            msg = self._pop("plc_sampler/status")
            if msg:
                auto_manual_status = msg.get("status", True)
                if auto_manual_status == "auto_manual_off":
                    print("[MANAGER] Manual mode — waiting for user confirmation ")
                    logger.debug(f"Manual mode {self.uid} — waiting for user confirmation ")
                    # Update database to trigger popup
                    db_update_plc_comm(self.uid, self.state.name, auto_manual="ACTIVE")
                    time.sleep(5)
                    # Web app will handle this - popup will appear
                    return
            
            # Set red signal
            db_update_plc_comm(self.uid, self.state.name)
            self._barrier(action="red_signal")
            time.sleep(2)
            self._goto(State.CLOSE_BARRIER)

        self._barrier(action="check_truck")
        time.sleep(3)

    def _handle_close_barrier(self):
        msg = self._pop("plc_barrier/status")
        
        if msg and msg.get("status") == "red_sent":
            print("[MANAGER] Red signal confirmed.")
            logger.debug("Red signal confirmed.")

        time.sleep(10)
        self._barrier(action="close_barrier")
        time.sleep(2)

        # Sending data to printer with vendor and vehicle info for label printing
        vendor_name = self.vehicle.get("VENDER_NAME", "UNKNOWN")
        vehicle_number = self.vehicle.get("VEHICLE_NUMBER", "UNKNOWN")
        self._printer(action="send_data", vendor_name=vendor_name.replace(" ", "").upper(), vehicle_number=vehicle_number.replace(" ", "").upper(), dtstamp=self.uid.replace('_', ''))
        print(f"[MANAGER] Data Sent — {vendor_name.replace(' ', '').upper()} - {vehicle_number.replace(' ', '').upper()} - {self.uid.replace('_', '')}")
        logger.info(f"Data Sent — {vendor_name.replace(' ', '').upper()} - {vehicle_number.replace(' ', '').upper()} - {self.uid.replace('_', '')}")
        
        self._goto(State.BARRIER_CLOSING)

    def _handle_barrier_closing(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        if status == "barrier_closed":
            print("[MANAGER] Barrier closed — moving auger to home ")
            self._sampler(action="move_home")
            self._goto(State.AUGER_HOME_POS)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_auger_home_pos(self):
        print("[MANAGER] Setting auger to home position ")
        time.sleep(2)

        msg = self._pop("plc_sampler/status")
        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        if msg and msg.get("status") == "auger_home":
            print("[MANAGER] Auger at home position.")
            self._current_sample_index = 0
            self._successful_cycles = 0
            self._goto(State.CYCLE_POSITION)
        
        if self._elapsed() > HOME_POSITION_TIMEOUT:
            print("[MANAGER] Home position timeout — Going back to vehicle placement")
            logger.debug("Home position timeout — Going back to vehicle placement")
            self._current_sample_index = 0
            self._successful_cycles = 0
            self._goto(State.VEHICLE_PLACEMENT)

        self._sampler(action="move_home")
        time.sleep(10)

    def _handle_cycle_position(self):
        if self._successful_cycles >= TOTAL_CYCLES:
            self._barrier(action="green_signal")
            print(f"[MANAGER] All {TOTAL_CYCLES} cycles completed.")
            logger.debug(f"All {TOTAL_CYCLES} cycles completed.")
            self._goto(State.SAMPLE_COLLECTION)
        
        # Get sample positions
        areas = [p["area"] for p in self.positions]
        target_area = get_sample_positions(used_areas=areas, prev_points=self.positions)
        if target_area is not None: self.positions.append(target_area)
        else:
            target_area = get_sample_positions([], [])
            self.positions.append(target_area)

        pos = self.positions[self._current_sample_index]
        self._sampler(action="sample_cycle", x=pos["x"], y=pos["y"])
        print(f"[MANAGER] Sampling positions: {self.positions}")
        print(f"[MANAGER] Moving to sampling position: {pos}")
        logger.debug(f"Moving to sampling position: {pos}")
        self._goto(State.CYCLE_CONFIRM)

    def _handle_cycle_confirm(self):
        msg = self._pop("plc_sampler/status")
        print(f"[MANAGER] Cycle Waiting for position confirmation")
        time.sleep(2)

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
        
        if msg and msg.get("status") == "position_set":
            print("[MANAGER] Auger positioned — waiting for AI confirmation ")

            # if self.ai_model:
            #     try: self._confirm_auger_position_with_movement_loop(self.positions[self._current_sample_index]["area"])
            #     except: pass

            cycle_num = self._successful_cycles + 1
            self.cycle = cycle_num
            self._sampler(action="start_cycle", cycle=cycle_num)
            print("[MANAGER] CYCLE START GIVEN !!!")
            logger.debug("Position set, cycle start given")
            time.sleep(10)
            self._goto(State.CYCLE_CAPTURE)
            return
        
        if self._elapsed() > POSITION_CONFIRMATION_TIMEOUT:
            print("[MANAGER] Position confirmation timeout — starting cycle")
            logger.debug("Position confirmation timeout — starting cycle")
            print("[MANAGER] Retrying with different position ")
            self.positions.pop()
            time.sleep(2)
            self._goto(State.CYCLE_POSITION)

    def _handle_cycle_capture(self):
        msg = self._pop("plc_sampler/status")
        time.sleep(2)

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        if msg and msg.get("status") == "cycle_start_given":
            print("[MANAGER] Cycle start given and recieved !!")
            self._sampler(action="check_sample_cycle_complete")
            time.sleep(8)
            self._cam(action="cam3_single", path=self.paths[f"SAMPLE_{self.cycle}_IMG_PATH"])

        self._goto(State.CYCLE_DONE)

    def _handle_cycle_done(self):
        time.sleep(3)

        msg = self._pop("plc_sampler/status")
        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
        
        if msg and msg.get("status", "") == "sample_cycle_complete":
            
            print(f"[MANAGER] Cycle {self.cycle} completed")
            logger.debug(f"Cycle {self.cycle} completed")
            self._successful_cycles += 1
            self._current_sample_index += 1
            
            if self._successful_cycles >= TOTAL_CYCLES:
                self._barrier(action="green_signal")
                time.sleep(2)
                print(f"[MANAGER] All {TOTAL_CYCLES} successful cycles completed.")
                logger.debug(f"All {TOTAL_CYCLES} successful cycles completed.")
                self._sampler(action="check_all_samples_status")
                time.sleep(2)  # Ensure PLC has time to update status
                self._goto(State.SAMPLE_COLLECTION)
            else:
                print(f"[MANAGER] Moving to next sampling position ")
                self._goto(State.CYCLE_POSITION)
        elif msg and msg.get("status", "") == "cycle_error":
            print(f"[MANAGER] Cycle error: {msg.get('msg')}")
            print("[MANAGER] Retrying with different position ")
            self.positions.pop()
            self._goto(State.CYCLE_POSITION)
        if self._elapsed() > SAMPLE_CYCLE_TIMEOUT:
            print("[MANAGER] Cycle timeout — moving to next position")
            self.positions.pop()
            self._goto(State.CYCLE_POSITION)
        else:
            print("[MANAGER] Cycle inrpogress — waiting ")
            self._sampler(action="check_sample_cycle_complete")
            time.sleep(3)
            
    def _handle_all_samples_collection(self):
        msg = self._pop("plc_barrier/status")
        
        if msg and msg.get("status") == "red_sent":
            print("[MANAGER] Red signal confirmed.")
            logger.debug("Red signal confirmed.")

        msg = self._pop("plc_sampler/status")

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
        elif msg and msg.get("status") == "all_samples_collected":
            now = time.time()
            while (time.time() - now) < CLOSE_CYCLE_WAIT_TIME:
                time.sleep(10)
                print("[MANAGER] Waiting for Cycle Stop.")

            self._sampler(action="sample_cycle_stop")
            self._goto(State.COMPLETE_FINAL)
        else:
            self._sampler(action="check_all_samples_status")
            time.sleep(2)

    def _handle_cycle_emergency_wait(self):
        print("[MANAGER] Waiting for emergency stop clearance on Sampler PLC ")
        time.sleep(3)  # Poll every 3 seconds
        
        msg = self._pop("plc_sampler/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        if status == "emergency_cleared":
            db_update_plc_comm(self.uid, "VEHICLE_PLACEMENT")
            self._barrier(action="check_truck")
            time.sleep(2)
            self._flush_topic("plc_sampler/status")
            time.sleep(2)
            print("[MANAGER] Emergency stop cleared. Resuming operation ")
            
            # Reset successful cycles and return to CYCLE_CONFIRM
            self._successful_cycles = 0
            self._current_sample_index = 0
            self.positions = []
            self._emergency_return_state = None
            self._goto(State.VEHICLE_PLACEMENT)
            return

    def _handle_complete_final(self):
        msg = self._pop("plc_sampler/status")

        if msg and msg.get("status") == "sample_cycle_stop_comp": 
            print(f"[MANAGER] Sampling complete — generating QR code ")
            self._goto(State.COMPLETE)
        else:
            print(f"[MANAGER] Waiting for sample cycle stop confirmation ")
            return
        
    def _handle_complete(self):
        print(f"[MANAGER] Session {self.uid} complete.")
        try:
            
            report_data = {
                "pdf_path": self.paths.get("REPORT_PATH"),
                "uid": self.uid,
                "rfids": self.rfids,
                "vehicle": self.vehicle.get("VEHICLE_NUMBER"),
                "vendor_code": self.vehicle.get("VENDOR_CODE"),
                "vendor": self.vehicle.get("VENDER_NAME"),
                "paths": self.paths,
                "bucket_no": self.bucket_no
            }

            generate_sampling_report(report_data)
            compress_pdf(report_data["pdf_path"])
        except Exception as e: 
            print(f"[MANAGER] Error (generate_sampling_report) - {e}")
            logger.error(f"{traceback.format_exc()}")

        db_complete_log(self.uid)
        time.sleep(5)

        self._printer(action="stop")
        self._sampler(action="reset")
        self._barrier(action="reset")
        self._rfid(action="cycle_completed")
        
        self._reset("")
        self._cam(action="reset")

    def _handle_error(self):
        print("[MANAGER] Error state — resetting system.")
        self._printer(action="stop")
        # self._cam(action="sample_capture_stop")
        self._cam(action="reset")
        self._barrier(action="close_barrier")
        self._sampler(action="reset")
        self._reset("Handled error state !!")

    def _reset(self, msg: str = ""):
        if msg != "":
            db_error_log(self.uid, f"Session reset due to error : {msg}")
        
        # Signal RFID reader to resume reading
        self.mqtt.publish("rfid/control", {"action": "cycle_reset"})
        
        self.uid       = None
        self.rfids     = []
        self.vehicle   = None
        self.positions = []
        self.cycle     = 0
        self.bucket_no = 1
        self._current_sample_index = 0
        self._successful_cycles = 0
        self._goto(State.IDLE)
        print("[MANAGER] Ready for next vehicle")

    def run(self):
        print("[MANAGER] Coal Sampling Manager started.")

        while True:
            try:
                handler_name = self.HANDLERS.get(self.state)
                if handler_name:
                    getattr(self, handler_name)()
            except Exception as e:
                import traceback
                print(f"[MANAGER] Unhandled exception in {self.state.name}: {e}")
                logger.error(f"{traceback.format_exc()}")
                self._goto(State.ERROR)

            time.sleep(0.05)

def main():
    while True:
        try:
            mgr = Manager()
            mgr.run()
        except Exception as e:
            print(f"[MAIN] Unhandled exception in main loop: {e}")
            logger.error(f"{traceback.format_exc()}")
            time.sleep(5)  # Wait before restarting the manager

if __name__ == "__main__":
    main()