# ═══════════════════════════════════════════════════════════════════════════════
# MAIN_MANAGER.py — HARD MATERIAL CHANGES
#
# This file contains the COMPLETE replacement versions of every section of
# MAIN_MANAGER.py that changes for the MATERIAL_HARD_STATUS feature.
# Paste each section over the matching original section — everything else in
# MAIN_MANAGER.py stays exactly as it is.
#
# Sections:
#   1. Tuning constants           -> add below the existing "# ── Tuning ──" block
#   2. get_nearby_position()      -> add right after get_sample_positions()
#   3. State-variable init        -> add wherever self.positions / _force_home_next
#                                    are initialised (per-vehicle reset)
#   4. _handle_cycle_position()   -> FULL replacement
#   5. _handle_cycle_done()       -> FULL replacement
# ═══════════════════════════════════════════════════════════════════════════════

import os
import time
import random
import pymysql
import subprocess
import traceback
import threading

from datetime import datetime
from enum import Enum, auto
from fpdf import FPDF

from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger
# from DEPENDANT.EMAIL_ALERTS import send_new_vehicle_alert
from DEPENDANT.VEHICLE_HOOKS import on_new_vehicle, resolve_pdf_url

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
CLOSE_CYCLE_WAIT_TIME = 45    # Wait 0.45 minute AFTER auger is home + CYCLE_COMPLETE=1, then cycle stop
FINAL_HOME_TIMEOUT    = 120   # Max wait for the auger to come home by itself after the last cycle
HOME_POLL_SEC         = 2     # How often to poll the home FBs / CYCLE_COMPLETE tag
SET_BUCKET_WAIT_TIMEOUT = 120 # Wait up to x seconds for bucket set confirmation before aborting
MOVEMENT_DURATION = 2 # Duration to move in each direction during auger position confirmation loops (in seconds)
HARD_RETRY_MAX_SHIFT      = 10  # Max % offset on X and Y for the nearby retry point (user spec: not more than 10%)
HARD_MATERIAL_MAX_RETRIES = 3   # After this many consecutive hard hits, give up "nearby" and pick a fresh area via home
# ── Sampling geometry ─────────────────────────────────────────────────────────
X_MIN, X_MAX = 35, 100   # X travel bounds, % of full travel
Y_MIN, Y_MAX = 40, 80    # Y travel bounds, % of full travel
MIN_X_GAP    = 20        # Minimum X separation between the 3 sample points (%)

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

def normalize_path(path: str) -> str:
    return path.replace("\\", "/")

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
    """
    Return the next sampling point.

    Areas are taken IN ORDER: 1, then 2, then 3 (no random choice).
    X moves left-to-right with at least MIN_X_GAP between consecutive points,
    e.g. (41, 72, 92). The upper bound reserves MIN_X_GAP for each point still
    to come, so the last area can never be squeezed out of range.
    Y is free anywhere in its bounds.
    """
    if used_areas is None:
        used_areas = []
    if prev_points is None:
        prev_points = []

    all_areas = set(range(1, TOTAL_CYCLES + 1))
    available = sorted(all_areas - set(used_areas))

    if not available:
        print("All areas are already used")
        return None

    area = available[0]                       # sequential: 1 -> 2 -> 3

    # Lower bound: MIN_X_GAP past the right-most point already taken
    x_lo = X_MIN
    if prev_points:
        x_lo = max(x_lo, max(p["x"] for p in prev_points) + MIN_X_GAP)

    # Upper bound: leave room for the areas still to come
    remaining_after = TOTAL_CYCLES - area
    x_hi = X_MAX - (remaining_after * MIN_X_GAP)

    # Safety net: only trips if a retry/fallback pushed a previous point high
    if x_lo > x_hi:
        print(f"[MANAGER] X range collapsed for area {area} "
              f"(lo={x_lo}, hi={x_hi}) — clamping")
        logger.warning(f"X range collapsed for area {area} (lo={x_lo}, hi={x_hi})")
        x_lo = x_hi = min(max(x_lo, X_MIN), X_MAX)

    return {
        "x": random.randint(int(x_lo), int(x_hi)),
        "y": random.randint(Y_MIN, Y_MAX),
        "area": area,
    }

def db_deactivate_manuals() -> list[str]:
    """Clear all PLC_COMM rows where auto_manual='ACTIVE'. Returns list of affected UIDs."""
    db = None
    affected = []
    try:
        db  = _db_connect()
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            "SELECT UID FROM PLC_COMM WHERE AUTO_MANUAL = 'ACTIVE'"
        )
        rows = cur.fetchall()
        affected = [r["UID"] for r in rows]
        if affected:
            cur.execute(
                """
                UPDATE PLC_COMM
                   SET AUTO_MANUAL = NULL, UPDATED = %s
                WHERE AUTO_MANUAL = 'ACTIVE'
                """,
                (datetime.now(),)
            )
            db.commit()
    except Exception as e:
        print(f"[DB] db_deactivate_manuals error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()
    return affected

def get_nearby_position(pos, prev_points=None, max_shift=HARD_RETRY_MAX_SHIFT):
    """
    Hard material was hit at `pos` — return a new point within ±max_shift %
    on both axes, still respecting MIN_X_GAP against the other sample points.
    """
    prev_points = prev_points or []

    # Keep the retry inside this area's slot
    x_lo = max(X_MIN, pos["x"] - max_shift)
    x_hi = min(X_MAX - (TOTAL_CYCLES - pos["area"]) * MIN_X_GAP, pos["x"] + max_shift)
    others = [p["x"] for p in prev_points if p["area"] != pos["area"]]
    for ox in others:
        if ox < pos["x"]:
            x_lo = max(x_lo, ox + MIN_X_GAP)
        else:
            x_hi = min(x_hi, ox - MIN_X_GAP)
    if x_lo > x_hi:
        x_lo = x_hi = pos["x"]        # no room to shift X — move Y only

    y_lo = max(Y_MIN, pos["y"] - max_shift)
    y_hi = min(Y_MAX, pos["y"] + max_shift)

    for _ in range(10):
        nx = random.randint(int(x_lo), int(x_hi))
        ny = random.randint(int(y_lo), int(y_hi))
        if (nx, ny) != (pos["x"], pos["y"]):
            return {"x": nx, "y": ny, "area": pos["area"]}

    return {"x": int(x_lo), "y": int(y_hi), "area": pos["area"]}


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
                vm.PDF_URL,
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
        self._new_vehicle_mail_sent = False
        self._final_home_confirmed = False   # auger home + CYCLE_COMPLETE=1 seen
        self._final_home_at        = 0.0     # timestamp that happened
        self._last_home_poll       = 0.0     # last check_home_status poll
        
        # AI Model initialization
        if initialize_ai_model():
            self.ai_model = True
            logger.debug("[MANAGER] Warning: AI Model initialized")
            print("[MANAGER] Warning: AI Model initialized")
        
        # Sampling position index tracker
        self._current_sample_index = 0
        self._successful_cycles = 0
        # Force the next positioning move to go via HOME (used for cycle 1 and
        # after errors/timeouts so the auger recovers from a known reference)
        self._force_home_next = True
        self._hard_retry_pos = None   # nearby point to use on the next CYCLE_POSITION pass (hard-material retry)
        self._hard_retries   = 0      # consecutive hard-material hits for the CURRENT cycle number

        # Auger position confirmation tracking
        self._confirmation_loop_count = 0
        self._confirmation_results = []
        # When move_home was last commanded (for re-send throttling)
        self._last_home_cmd = 0.0

        # ── Manual watchdog ───────────────────────────────────────────────────
        self._watchdog_mqtt = MQTT("MAIN_MANAGER_WATCHDOG")
        self._watchdog_stop  = threading.Event()
        self._watchdog_thread = threading.Thread(
            target=self._manual_watchdog_loop,
            name="ManualWatchdog",
            daemon=True
        )
        self._watchdog_thread.start()
        print("[MANAGER] Manual watchdog thread started.")

    # ── Helpers ───────────────────────────────────────────────────────────────

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

        if new_state == State.SAMPLE_COLLECTION:
            self._final_home_confirmed = False
            self._final_home_at        = 0.0
            self._last_home_poll       = 0.0
        
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
            on_new_vehicle(self.uid, self.vehicle.get("VEHICLE_NUMBER"), self.rfids)      # mail alert (deduped, threaded)
            self._goto(State.OPEN_BARRIER)

    def _handle_open_barrier(self):
        msg = self._pop("plc_barrier/status")

        if msg and msg.get("action") == "green_sent":
            print("[MANAGER] Green Signal set !")
            logger.debug("Green Signal set !")

        print("[MANAGER] Sending open barrier command ")
        self._cam(action="cam2_single", path=self.paths["VEHICLE_IMG_PATH"])
        self._barrier(action="open_barrier")
        # self._cam(action="sample_capture_start", uid=self.uid)
        time.sleep(1)
        
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

    def _handle_red_signal(self):
        # (kept for completeness — red signal is normally sent inline)
        self._barrier(action="red_signal")
        time.sleep(2)
        self._goto(State.CLOSE_BARRIER)

    def _handle_close_barrier(self):
        msg = self._pop("plc_barrier/status")
        
        if msg and msg.get("status") == "red_sent":
            print("[MANAGER] Red signal confirmed.")
            logger.debug("Red signal confirmed.")

        time.sleep(10)
        self._barrier(action="close_barrier")
        time.sleep(2)

        # Privacy-safe print job: QR carries only dtstamp + secured PDF link.
        # resolve_pdf_url handles: existing PDF / vendor-mismatch (mail + reuse
        # old PDF) / brand-new vehicle (creates + uploads PDF on the spot).
        pdf_url = resolve_pdf_url(
            self.vehicle,
            self.uid,
            vehicle_img_path=self.paths.get("VEHICLE_IMG_PATH"),
        )
        dtstamp = self.uid.replace('_', '')
        self._printer(action="send_data", pdf_url=pdf_url, dtstamp=dtstamp)
        print(f"[MANAGER] Print job sent — dtstamp={dtstamp} url={'yes' if pdf_url else 'MISSING'}")
        logger.info(f"Print job sent — dtstamp={dtstamp} pdf_url={pdf_url}")
        
        self._goto(State.BARRIER_CLOSING)

    def _handle_barrier_closing(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        if status == "barrier_closed":
            print("[MANAGER] Barrier closed — moving auger to home ")
            self._sampler(action="move_home")
            self._last_home_cmd = time.time()
            self._goto(State.AUGER_HOME_POS)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_auger_home_pos(self):
        print("[MANAGER] Setting auger to home position ")

        msg = self._pop("plc_sampler/status")
        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            self._flush_topic("plc_sampler/status")  # drop stale queued messages
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        if msg and msg.get("status") == "auger_home":
            print("[MANAGER] Auger at home position.")
            logger.debug(f"Auger at home position after {self._elapsed():.1f}s")
            self._current_sample_index = 0
            self._successful_cycles = 0
            self._force_home_next = False   # Auger is at home — cycle 1 position can be absolute
            self._goto(State.CYCLE_POSITION)
            return

        if self._elapsed() > HOME_POSITION_TIMEOUT:
            print("[MANAGER] Home position timeout — Going back to vehicle placement")
            logger.debug("Home position timeout — Going back to vehicle placement")
            self._current_sample_index = 0
            self._successful_cycles = 0
            self._goto(State.VEHICLE_PLACEMENT)
            return

        # move_home was already commanded when the barrier closed.
        # Only RE-send it if 15 s pass with no reply (lost-message safety net) —
        # blind re-sends made the PLC re-run homing and queue duplicate
        # 'auger_home' replies, and the old sleep(2)+sleep(10) per pass turned
        # a ~3 s homing confirmation into ~14 s.
        if time.time() - self._last_home_cmd > 15:
            print("[MANAGER] No home confirmation yet — re-sending move_home")
            logger.debug("No home confirmation yet — re-sending move_home")
            self._sampler(action="move_home")
            self._last_home_cmd = time.time()

        time.sleep(1)

    def _handle_cycle_position(self):
        if self._successful_cycles >= TOTAL_CYCLES:
            self._barrier(action="green_signal")
            print(f"[MANAGER] All {TOTAL_CYCLES} cycles completed.")
            logger.debug(f"All {TOTAL_CYCLES} cycles completed.")
            self._goto(State.SAMPLE_COLLECTION)
            return

        # NEW: hard-material retry — reuse the pre-computed NEARBY point
        # (≤10% shift on X/Y from the failed point) instead of generating a
        # brand-new area position.
        is_hard_retry = self._hard_retry_pos is not None
        if is_hard_retry:
            self.positions.append(self._hard_retry_pos)
            self._hard_retry_pos = None
            print(f"[MANAGER] HARD-MATERIAL RETRY — using nearby point {self.positions[-1]}")
            logger.debug(f"Hard-material retry — using nearby point {self.positions[-1]}")
        else:
            # Get sample positions (original behaviour)
            areas = [p["area"] for p in self.positions]
            target_area = get_sample_positions(used_areas=areas, prev_points=self.positions)
            if target_area is not None: self.positions.append(target_area)
            else:
                target_area = get_sample_positions([], [])
                self.positions.append(target_area)

        pos = self.positions[self._current_sample_index]

        # NEW PROCESS: cycles 2 & 3 travel DIRECTLY from the previous position
        # (no homing in between). Cycle 1 — or any retry after an error /
        # timeout — goes via home so the auger starts from a known reference.
        # NEW: a hard-material retry also goes DIRECT (even on cycle 1),
        # because the Z cycle finished normally so the tracked position is
        # still trusted — the auger just slides ≤10% over.
        direct = ((self._successful_cycles > 0) or is_hard_retry) and (not self._force_home_next)
        self._force_home_next = False

        self._sampler(action="sample_cycle", x=pos["x"], y=pos["y"], direct=direct)
        print(f"[MANAGER] Sampling positions: {self.positions}")
        print(f"[MANAGER] Moving to sampling position: {pos} (direct={direct})")
        logger.debug(f"Moving to sampling position: {pos} (direct={direct})")
        self._goto(State.CYCLE_CONFIRM)

    def _handle_cycle_confirm(self):
        msg = self._pop("plc_sampler/status")
        print(f"[MANAGER] Cycle Waiting for position confirmation")
        time.sleep(1)  # faster polling — position_set is detected ~1s sooner

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            self._flush_topic("plc_sampler/status")  # drop stale queued messages
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
            time.sleep(1)  # settle time after cycle start (was 10s)
            self._goto(State.CYCLE_CAPTURE)
            return
        
        if self._elapsed() > POSITION_CONFIRMATION_TIMEOUT:
            print("[MANAGER] Position confirmation timeout — starting cycle")
            logger.debug("Position confirmation timeout — starting cycle")
            print("[MANAGER] Retrying with different position ")
            self.positions.pop()
            self._force_home_next = True   # Recover via home — position not trusted
            time.sleep(1)
            self._goto(State.CYCLE_POSITION)

    def _handle_cycle_capture(self):
        msg = self._pop("plc_sampler/status")
        time.sleep(1)

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            self._flush_topic("plc_sampler/status")  # drop stale queued messages
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        if msg and msg.get("status") == "cycle_start_given":
            print("[MANAGER] Cycle start given and recieved !!")
            self._sampler(action="check_sample_cycle_complete")
            time.sleep(1)

        self._cam(action="cam3_single", path=self.paths[f"SAMPLE_{self.cycle}_IMG_PATH"])
        self._goto(State.CYCLE_DONE)

    def _handle_cycle_done(self):
        time.sleep(1)  # PLC now PUSHES sample_cycle_complete / hard_material_detected — poll fast to catch it

        msg = self._pop("plc_sampler/status")
        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            self._flush_topic("plc_sampler/status")  # drop stale queued messages
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        if msg and msg.get("status", "") == "sample_cycle_complete":

            # Completion is detected on the sampler side via Z_UP FB 1 -> 0 -> 1
            print(f"[MANAGER] Cycle {self.cycle} completed (Z_UP FB 1->0->1)")
            logger.debug(f"Cycle {self.cycle} completed (Z_UP FB 1->0->1)")
            self._successful_cycles += 1
            self._current_sample_index += 1
            self._hard_retries = 0          # NEW: good sample — clear hard-retry counter

            if self._successful_cycles >= TOTAL_CYCLES:
                self._barrier(action="green_signal")
                time.sleep(1)
                print(f"[MANAGER] All {TOTAL_CYCLES} successful cycles completed.")
                logger.debug(f"All {TOTAL_CYCLES} successful cycles completed.")
                # NEW PROCESS: the auger stays at the last position — go straight
                # to SAMPLE_COLLECTION, which waits 1 minute then writes cycle stop.
                self._goto(State.SAMPLE_COLLECTION)
            else:
                print(f"[MANAGER] Moving DIRECTLY to next sampling position (no homing) ")
                self._goto(State.CYCLE_POSITION)

        # ── NEW: HARD MATERIAL — Z came back up (z_up=1) WITH hard material=1 ──
        elif msg and msg.get("status", "") == "hard_material_detected":

            self._hard_retries += 1
            bad = self.positions.pop()   # discard the failed point — cycle NOT counted,
                                         # _successful_cycles / _current_sample_index untouched
            print(f"[MANAGER] HARD MATERIAL at {bad} — cycle {self.cycle} NOT counted "
                  f"(retry {self._hard_retries}/{HARD_MATERIAL_MAX_RETRIES})")
            logger.warning(f"Hard material at {bad} — cycle not counted (retry {self._hard_retries}/{HARD_MATERIAL_MAX_RETRIES})")

            if self._hard_retries <= HARD_MATERIAL_MAX_RETRIES:
                # Get another X,Y NEARBY — not more than 10% shift on X and Y.
                # Z is back up and the move completed normally, so the tracked
                # position is still valid — the retry travels DIRECT (no homing).
                self._hard_retry_pos = get_nearby_position(bad, prev_points=self.positions)
                print(f"[MANAGER] Retrying at nearby point {self._hard_retry_pos}")
                logger.debug(f"Retrying at nearby point {self._hard_retry_pos}")
            else:
                # Whole neighbourhood seems hard — abandon it, pick a fresh
                # area through the normal generator, and recover via home.
                print("[MANAGER] Too many hard-material hits — picking a FRESH area via home")
                logger.warning("Too many hard-material hits — picking a fresh area via home")
                self._hard_retries   = 0
                self._hard_retry_pos = None
                self._force_home_next = True

            self._goto(State.CYCLE_POSITION)
            return

        elif msg and msg.get("status", "") == "cycle_error":
            print(f"[MANAGER] Cycle error: {msg.get('msg')}")
            print("[MANAGER] Retrying with different position ")
            self.positions.pop()
            self._force_home_next = True   # Recover via home — position not trusted
            self._goto(State.CYCLE_POSITION)

        if self._elapsed() > SAMPLE_CYCLE_TIMEOUT:
            print("[MANAGER] Cycle timeout — moving to next position")
            self.positions.pop()
            self._force_home_next = True   # Recover via home — position not trusted
            self._goto(State.CYCLE_POSITION)
        else:
            print("[MANAGER] Cycle in progress — waiting ")
            # Polls the sampler — the reply continues with ONE of:
            #   sample_cycle_complete | hard_material_detected | sample_cycle_not_complete
            self._sampler(action="check_sample_cycle_complete")
            time.sleep(2)
            
    def _handle_all_samples_collection(self):
        # ── Barrier chatter ───────────────────────────────────────────────────
        msg = self._pop("plc_barrier/status")
        if msg and msg.get("status") == "red_sent":
            print("[MANAGER] Red signal confirmed.")
            logger.debug("Red signal confirmed.")

        msg = self._pop("plc_sampler/status")

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            self._flush_topic("plc_sampler/status")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        # ── STEP 1: PASSIVELY wait for the auger to reach home ────────────────
        # The PLC drives it home on its own after the 3rd cycle — no move_home
        # is sent from here. Home is confirmed by the FBs, and CYCLE_COMPLETE
        # (DB24.18 = 1) confirms the PLC considers all 3 cycles finished.
        if not self._final_home_confirmed:

            if msg and msg.get("status") == "auger_at_home":
                if int(msg.get("cycle_complete", 0)) == 1:
                    self._final_home_confirmed = True
                    self._final_home_at        = time.time()
                    print(f"[MANAGER] Auger HOME + CYCLE_COMPLETE=1 — Cycle Stop in {CLOSE_CYCLE_WAIT_TIME}s.")
                    logger.debug(f"Auger home + CYCLE_COMPLETE=1 after {self._elapsed():.1f}s — "
                                 f"waiting {CLOSE_CYCLE_WAIT_TIME}s before cycle stop.")
                    return
                else:
                    print("[MANAGER] Auger at home — waiting for CYCLE_COMPLETE (DB24.20) = 1")
                    logger.debug("Auger at home, CYCLE_COMPLETE still 0 — polling")

            elif msg and msg.get("status") == "auger_not_home":
                print(f"[MANAGER] Auger returning home on its own ({self._elapsed():.0f}s)")

            if self._elapsed() > FINAL_HOME_TIMEOUT:
                # Never strand the vehicle — write Cycle Stop even without
                # home / CYCLE_COMPLETE confirmation.
                print("[MANAGER] Home / CYCLE_COMPLETE timeout — proceeding to Cycle Stop anyway.")
                logger.warning("Home or CYCLE_COMPLETE not confirmed within timeout — proceeding to cycle stop.")
                self._final_home_confirmed = True
                self._final_home_at        = time.time()
                return

            if time.time() - self._last_home_poll > HOME_POLL_SEC:
                self._sampler(action="check_home_status")
                self._last_home_poll = time.time()

            time.sleep(0.5)
            return

        # ── STEP 2: home reached — wait 1 minute, then write CYCLE_STOP ────────
        waited = time.time() - self._final_home_at
        if waited < CLOSE_CYCLE_WAIT_TIME:
            print(f"[MANAGER] Cycle Stop in {CLOSE_CYCLE_WAIT_TIME - waited:.0f}s")
            time.sleep(5)
            return

        print("[MANAGER] Wait elapsed — writing Cycle Stop.")
        logger.debug("Auger home + 1 min elapsed — writing cycle stop.")
        self._sampler(action="sample_cycle_stop")
        self._goto(State.COMPLETE_FINAL)

    def _handle_cycle_emergency_wait(self):
        print("[MANAGER] Waiting for emergency stop clearance on Sampler PLC ")
        logger.warning("Waiting for emergency stop clearance on Sampler PLC")

        # ACTIVELY query the LIVE emergency bit on the PLC instead of passively
        # waiting for a pushed 'emergency_cleared'. This prevents the deadlock
        # where a STALE queued 'emergency_stop' message put us in this state
        # AFTER the emergency was already cleared (the clearance push was
        # already consumed/lost, so it would never arrive again).
        self._sampler(action="check_emergency")
        time.sleep(3)  # Poll every 3 seconds
        
        msg = self._pop("plc_sampler/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        if status == "emergency_stop":
            print("[MANAGER] Emergency still active — waiting ")
            logger.warning("Emergency still active — waiting")
            return

        if status == "emergency_cleared":
            db_update_plc_comm(self.uid, "VEHICLE_PLACEMENT")
            self._barrier(action="check_truck")
            time.sleep(2)
            self._flush_topic("plc_sampler/status")
            time.sleep(2)
            print("[MANAGER] Emergency stop cleared. Resuming operation ")
            logger.warning("Emergency stop cleared — resuming operation from VEHICLE_PLACEMENT")
            
            # Reset successful cycles and return to CYCLE_CONFIRM
            self._successful_cycles = 0
            self._current_sample_index = 0
            self.positions = []
            self._force_home_next = True   # After emergency, always re-home first
            self._emergency_return_state = None
            self._goto(State.VEHICLE_PLACEMENT)
            return

    def _handle_complete_final(self):
        msg = self._pop("plc_sampler/status")
        # self._cam(action="sample_capture_stop", uid=self.uid)

        if msg and msg.get("status") == "sample_cycle_stop_comp": 
            print(f"[MANAGER] Sampling complete — generating QR code ")
            self._goto(State.COMPLETE)
        else:
            print(f"[MANAGER] Waiting for sample cycle stop confirmation ")
            return

    def _handle_green_signal(self):
        # (kept for completeness — green signal is normally sent inline)
        self._barrier(action="green_signal")
        time.sleep(2)
        self._goto(State.COMPLETE)

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
        self._force_home_next = True
        self._new_vehicle_mail_sent = False
        self._final_home_confirmed = False   # auger home + CYCLE_COMPLETE=1 seen
        self._final_home_at        = 0.0     # timestamp that happened
        self._last_home_poll       = 0.0     # last check_home_status poll
        self._goto(State.IDLE)
        print("[MANAGER] Ready for next vehicle")

    def _manual_watchdog_loop(self):
        """
        Background daemon thread.
        Every 5 seconds: if the main state machine is IDLE (no cycle in progress)
        and any PLC_COMM row has auto_manual='ACTIVE', deactivate it so the
        sampler PLC is never left in manual mode between cycles.
        """
        WATCHDOG_INTERVAL = 5  # seconds

        while not self._watchdog_stop.is_set():
            try:
                if self.state == State.IDLE:
                    affected_uids = db_deactivate_manuals()
                    if affected_uids:
                        print(f"[WATCHDOG] Idle & manual active on UIDs {affected_uids} — deactivating.")
                        logger.info(f"Manual watchdog deactivated auto_manual for UIDs: {affected_uids}")
                        # Tell the sampler PLC to exit manual mode
                        self._watchdog_mqtt.publish(
                            "manager/plc_sampler",
                            {"action": "deactivate_manual"}
                        )
            except Exception as e:
                print(f"[WATCHDOG] Error in watchdog loop: {e}")
                logger.error(f"[WATCHDOG] {traceback.format_exc()}")

            self._watchdog_stop.wait(WATCHDOG_INTERVAL)

    def run(self):
        print("[MANAGER] Coal Sampling Manager started.")

        try:
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
        finally:
            self._watchdog_stop.set()
            print("[MANAGER] Watchdog thread stopped.")

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