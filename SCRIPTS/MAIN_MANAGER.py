import os
import time
import random
import threading
import pymysql
from datetime import datetime
from enum import Enum, auto

from DEPENDANT.MQTT import MQTT
from LOGIC import (
    initialize_ai_model,
    check_vehicle_front_present,
    confirm_barrier_opening,
    confirm_barrier_closing,
    confirm_auger_position,
    check_sampling_cycle_completion,
    generate_qr_code
)

# ── Database ──────────────────────────────────────────────────────────────────
DB_HOST = "127.0.0.1"
DB_USER = "root"
DB_PASS = "insightzz@123"
DB_NAME = "COAL_SAMPLING_DHAR"

# ── Tuning ────────────────────────────────────────────────────────────────────
DB_POLL_SEC     = 10
DB_WAIT_TIMEOUT = 900
TOTAL_CYCLES    = 3
HOME_POSITION_TIMEOUT = 120
SAMPLE_CYCLE_TIMEOUT = 240
POSITION_CONFIRMATION_TIMEOUT = 240
CLOSE_CYCLE_WAIT_TIME = 180

TEMP_IMG_PATH = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/"
RESULT_IMG_PATH = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/RESULT/"
INF_IMG = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/INF/"

def get_sample_positions(used_areas=None):
    if used_areas is None:
        used_areas = []

    # Define all possible areas
    all_areas = set(range(1, 7))
    available_areas = list(all_areas - set(used_areas))

    if not available_areas:
        raise ValueError("All areas are already used")

    # Pick random available area
    area = random.choice(available_areas)

    # Global bounds
    x_min, x_max = 15, 85
    y_min, y_max = 30, 70

    # Grid split
    x_splits = 3
    y_splits = 2

    x_step = (x_max - x_min) / x_splits
    y_step = (y_max - y_min) / y_splits

    # Map area → grid index
    row = (area - 1) // x_splits
    col = (area - 1) % x_splits

    # Cell bounds
    x_start = x_min + col * x_step
    x_end = x_start + x_step

    y_start = y_min + row * y_step
    y_end = y_start + y_step

    # Uniform integer sampling
    x = random.randint(int(x_start), int(x_end) - 1)
    y = random.randint(int(y_start), int(y_end) - 1)

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

def db_find_vehicle(rfid: str) -> dict | None:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT
                vm.RFID,
                vm.VEHICLE_NUMBER,
                vm.VENDOR_CODE,
                vr.VENDER_NAME,
                vr.BUCKET_NO
            FROM VEHICLE_MASTER vm
            LEFT JOIN VENDOR_MASTER vr ON vr.VENDOR_CODE = vm.VENDOR_CODE
            WHERE vm.RFID = %s
            LIMIT 1
            """,
            (rfid,)
        )
        return cur.fetchone()
    except Exception as e:
        print(f"[DB] db_find_vehicle error: {e}")
        return None
    finally:
        if db: db.close()

def db_vehicle_already_in_front(rfids_str: str) -> bool:
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()
        rfid_list = [r.strip() for r in rfids_str.split("|") if r.strip()]
        if not rfid_list:
            return False

        placeholders = " OR ".join(
            ["FIND_IN_SET(%s, REPLACE(RFIDS, '|', ',')) > 0"] * len(rfid_list)
        )
        query = f"""
            SELECT COUNT(*) FROM VEHICLE_LOGS
            WHERE STATUS = 'IN_PROGRESS'
              AND ({placeholders})
        """
        cur.execute(query, rfid_list)
        row = cur.fetchone()
        return (row[0] > 1) if row else False
    except Exception as e:
        print(f"[DB] db_vehicle_already_in_front error: {e}")
        return False
    finally:
        if db: db.close()

def db_create_log(uid: str, rfids: list, paths: dict) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        now = datetime.now()
        cur.execute(
            """
            INSERT INTO VEHICLE_LOGS
                (UID, RFIDS, STATUS, CREATE_TIME, 
                VEHICLE_IMG_PATH, 
                SAMPLE_1_IMG_PATH, 
                SAMPLE_2_IMG_PATH, 
                SAMPLE_3_IMG_PATH, 
                QR_CODE_PATH)
            VALUES
                (
                %s, %s, %s, %s, 
                %s, 
                %s, 
                %s,
                %s,
                %s
                )
            """,
            (uid, "|".join(rfids), "IN_PROGRESS", now, 
                paths.get("VEHICLE_IMG_PATH"), 
                paths.get("SAMPLE_1_IMG_PATH"), 
                paths.get("SAMPLE_2_IMG_PATH"), 
                paths.get("SAMPLE_3_IMG_PATH"), 
                paths.get("QR_CODE_PATH")
            )
        )
        db.commit()
        print(f"[DB] Log created  uid={uid}  rfids={rfids}")
        return True
    except Exception as e:
        print(f"[DB] db_create_log error: {e}")
        return False
    finally:
        if db: db.close()

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
        print("DB complete Added !!!!!!")
    except Exception as e:
        print(f"[DB] db_complete_log error: {e}")
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
        return False
    finally:
        if db: db.close()

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
        return False
    finally:
        if db: db.close()

# ══════════════════════════════════════════════════════════════════════════════
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

        for topic in (
            "manager/rfid",
            "camera/status",
            "plc_barrier/status",
            "plc_sampler/status",
        ):
            self.mqtt.subscribe(topic)

        self.state     = State.IDLE
        self.uid       : str | None  = None
        self.rfids     : list        = []
        self.vehicle   : dict | None = None
        self.positions : list        = []
        self.cycle     : int         = 0
        self.date_file : str         = ""
        self.ai_model  : bool        = False
        self._emergency_return_state: State | None = State.CYCLE_CONFIRM  # Track which state to resume to after emergency

        self._state_entered_at: float = 0.0
        self._db_last_polled  : float = 0.0
        
        # AI Model initialization
        if initialize_ai_model():
            self.ai_model = True
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

    def _goto(self, new_state: State):
        print(f"[MANAGER] State: {self.state.name}  {new_state.name}")
        self.state             = new_state
        self._state_entered_at = time.time()
        
        # Update database with new state
        if self.uid:
            db_update_plc_comm(self.uid, new_state.name)

    def _elapsed(self) -> float:
        return time.time() - self._state_entered_at

    def _resolve_rfid(self, rfids: list) -> tuple[str | None, dict | None]:
        for rfid in rfids:
            row = db_find_vehicle(rfid)
            if row:
                return rfid, row
        return None, None
    
    def _wait_for_file(self, check_path_1: str, check_path_3: str, timeout: float = 10.0) -> bool:
        start = time.time()

        while time.time() - start < timeout:
            msg = self._pop("camera/status")
            if msg and msg.get("status") == "cam1_done":
                cam1_path = msg.get("path")
                if os.path.exists(cam1_path) and (check_path_1 == cam1_path): cam1_done = True
                time.sleep(0.05)
            if msg and msg.get("status") == "cam3_done":
                cam3_path = msg.get("path")
                if os.path.exists(cam3_path) and (check_path_3 == cam3_path): cam3_done = True
                time.sleep(0.05)

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
                return (cam1_path, cam3_path)

            return None

        except Exception as e:
            print(f"[MANAGER] Error capturing images: {e}")
            return None

    def _confirm_auger_position_with_movement_loop(self, target_area: int) -> bool:
        MOVEMENT_DURATION = 2  # seconds
        confirmations = []
        
        try:
            print(f"[MANAGER] Starting 5-loop auger position confirmation (Target Area: {target_area})")
            
            # ─── Loop 1: Original Position ─────────────────────────────────────────
            print("\n[MANAGER] ═══ Loop 1/5: Original Position ═══")
            images = self._capture_images_for_confirmation(1, "original")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 1 Result: {'PASS' if result else 'FAIL'}")
            else:
                print("[MANAGER] Loop 1: Image capture failed")
                confirmations.append(False)
            
            # ─── Loop 2: Move Right ────────────────────────────────────────────────
            print("\n[MANAGER] ═══ Loop 2/5: Move Right ═══")
            print(f"[MANAGER] Moving Y-axis right for {MOVEMENT_DURATION}s…")
            self._sampler(action="move_y_right", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)  # Wait for movement to complete
            
            images = self._capture_images_for_confirmation(2, "right")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 2 Result: {'PASS' if result else 'FAIL'}")
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving Y-axis left for {MOVEMENT_DURATION}s (returning to original)…")
            self._sampler(action="move_y_left", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            # ─── Loop 3: Move Left ─────────────────────────────────────────────────
            print("\n[MANAGER] ═══ Loop 3/5: Move Left ═══")
            print(f"[MANAGER] Moving Y-axis left for {MOVEMENT_DURATION}s…")
            self._sampler(action="move_y_left", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            images = self._capture_images_for_confirmation(3, "left")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 3 Result: {'PASS' if result else 'FAIL'}")
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving Y-axis right for {MOVEMENT_DURATION}s (returning to original)…")
            self._sampler(action="move_y_right", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            # ─── Loop 4: Move Forward ──────────────────────────────────────────────
            print("\n[MANAGER] ═══ Loop 4/5: Move Forward ═══")
            print(f"[MANAGER] Moving X-axis forward for {MOVEMENT_DURATION}s…")
            self._sampler(action="move_x_forward", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 3)
            
            images = self._capture_images_for_confirmation(4, "forward")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 4 Result: {'PASS' if result else 'FAIL'}")
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving X-axis reverse for {MOVEMENT_DURATION}s (returning to original)…")
            self._sampler(action="move_x_reverse", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 1)
            
            # ─── Loop 5: Move Reverse ──────────────────────────────────────────────
            print("\n[MANAGER] ═══ Loop 5/5: Move Reverse ═══")
            print(f"[MANAGER] Moving X-axis reverse for {MOVEMENT_DURATION}s…")
            self._sampler(action="move_x_reverse", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 1)
            
            images = self._capture_images_for_confirmation(5, "reverse")
            if images:
                result = confirm_auger_position(images[0], images[1], target_area)
                confirmations.append(result)
                print(f"[MANAGER] Loop 5 Result: {'PASS' if result else 'FAIL'}")
            else:
                confirmations.append(False)
            
            print(f"[MANAGER] Moving X-axis forward for {MOVEMENT_DURATION}s (returning to original)…")
            self._sampler(action="move_x_forward", duration=MOVEMENT_DURATION)
            time.sleep(MOVEMENT_DURATION + 1)
            
            # ─── Summary ────────────────────────────────────────────────────────────
            self._confirmation_results = confirmations
            passed = sum(confirmations)
            total = len(confirmations)
            
            print(f"\n[MANAGER] ╔════ AUGER POSITION CONFIRMATION SUMMARY ════╗")
            print(f"[MANAGER] ║ Loop 1 (Original): {'PASS' if confirmations[0] else 'FAIL':<28} ║")
            print(f"[MANAGER] ║ Loop 2 (Right):    {'PASS' if confirmations[1] else 'FAIL':<28} ║")
            print(f"[MANAGER] ║ Loop 3 (Left):     {'PASS' if confirmations[2] else 'FAIL':<28} ║")
            print(f"[MANAGER] ║ Loop 4 (Forward):  {'PASS' if confirmations[3] else 'FAIL':<28} ║")
            print(f"[MANAGER] ║ Loop 5 (Reverse):  {'PASS' if confirmations[4] else 'FAIL':<28} ║")
            print(f"[MANAGER] ║ Total: {passed}/{total} confirmations passed                ║")
            print(f"[MANAGER] ╚═════════════════════════════════════════════╝")
            
            # Return True only if ALL 5 loops pass
            final_result = all(confirmations)
            if final_result:
                print(f"[MANAGER] AUGER POSITIONING CONFIRMED - All 5 loops passed!")
            else:
                print(f"[MANAGER] AUGER POSITIONING FAILED - Some loops did not pass")
            
            return final_result
        
        except Exception as e:
            print(f"[MANAGER] Error in auger position confirmation loop: {e}")
            import traceback
            traceback.print_exc()
            return False

    # ── State handlers ────────────────────────────────────────────────────────

    def _handle_idle(self):
        msg = self._pop("manager/rfid")
        if not msg:
            return
        
        self.uid   = msg["uid"]
        self.date_file = self.uid[:8]  # Assuming UID starts with YYYYMMDD
        self.rfids = msg.get("rfids", [])
        self.paths = {
            "VEHICLE_IMG_PATH": os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "VEHICLE_IMG.jpg"),
            "SAMPLE_1_IMG_PATH": os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "SAMPLE_1_IMG.jpg"),
            "SAMPLE_2_IMG_PATH": os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "SAMPLE_2_IMG.jpg"),
            "SAMPLE_3_IMG_PATH": os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "SAMPLE_3_IMG.jpg"),
            "QR_CODE_PATH": os.path.join(RESULT_IMG_PATH, self.date_file, self.uid, "QR_CODE.jpg"),
        }
        print(f"[MANAGER] RFID event uid={self.uid}  tags={self.rfids}")
        self._cam(action="cam2_single", path=self.paths["VEHICLE_IMG_PATH"])
        self._goto(State.DB_CHECK)

    def _handle_db_check(self):
        msg = self._pop("camera/status")
        if msg and msg.get("action") == "cam2_done":
            print("[MANAGER] CAM2 captured — checking for vehicle front …")
            # TODO: Get image and run AI check
            # For now, proceed to DB check
        
        valid_rfid, vehicle = self._resolve_rfid(self.rfids)
        
        if vehicle:
            print(f"[MANAGER] Vehicle found in DB: {vehicle}")
            self.vehicle = vehicle
            
            if db_vehicle_already_in_front("|".join(self.rfids)):
                print("[MANAGER] Vehicle already in front — aborting.")
                self._reset()
                return
            
            # Create log entry
            db_create_log(self.uid, self.rfids, self.paths)
            db_add_plc_comm(self.uid, self.state.name)
            self._goto(State.OPEN_BARRIER)
        else:
            print("[MANAGER] RFID not in DB — will poll …")
            db_create_log(self.uid, self.rfids, self.paths)
            self._db_last_polled = time.time()
            self._goto(State.WAITING_FOR_DB)

    def _handle_waiting_for_db(self):
        if self._elapsed() > DB_WAIT_TIMEOUT:
            print("[MANAGER] DB wait timed out — resetting.")
            self._reset("DB wait timed out — resetting")
            return

        if time.time() - self._db_last_polled < DB_POLL_SEC:
            return

        self._db_last_polled = time.time()
        print("[MANAGER] Polling DB for vehicle …")
        valid_rfid, vehicle = self._resolve_rfid(self.rfids)

        if vehicle:
            print(f"[MANAGER] Vehicle now in DB: {vehicle}")
            self.vehicle = vehicle
            self._goto(State.OPEN_BARRIER)

    def _handle_open_barrier(self):
        print("[MANAGER] Sending open barrier command …")
        self._barrier(action="open_barrier")
        self._cam(action="sample_capture_start", uid=self.uid)
        self._goto(State.BARRIER_OPENING)

    def _handle_barrier_opening(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        if status == "barrier_opened":
            print("[MANAGER] Barrier opened — waiting for AI confirmation …")
            self._goto(State.SET_BUCKET)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_set_bucket(self):
        bucket_no = int(self.vehicle.get("BUCKET_NO", 1))
        print(f"[MANAGER] Setting bucket to {bucket_no} …")
        self._barrier(action="set_bucket", bucket_no=bucket_no)
        
        # Wait for bucket confirmation
        deadline = time.time() + 60
        while time.time() < deadline:
            msg = self._pop("plc_barrier/status")
            if msg and msg.get("status") == "bucket_set":
                print(f"[MANAGER] Bucket {bucket_no} confirmed.")
                self._goto(State.VEHICLE_PLACEMENT)
                return
            time.sleep(0.1)
        
        print("[MANAGER] Bucket set timeout — continuing anyway")
        self._goto(State.VEHICLE_PLACEMENT)

    def _handle_vehicle_placement(self):
        msg = self._pop("plc_barrier/status")
        
        # Check if truck is present at both positions
        if not msg or msg.get("status") == "truck_not_present":
            print("[MANAGER] Waiting for truck presence …")
            self._barrier(action="check_truck")
            time.sleep(1)
            return
        
        if msg.get("status") == "truck_present":
            print("[MANAGER] Truck placement confirmed.")
            # Check for AUTO/MANUAL signal
            self._sampler(action="auto_manual")
            time.sleep(2)  # Give PLC time to update status
            msg = self._pop("plc_sampler/status")
            auto_manual_status = msg.get("status", True)
            if auto_manual_status == "auto_manual_off":
                print("[MANAGER] Manual mode — waiting for user confirmation …")
                # Update database to trigger popup
                db_update_plc_comm(self.uid, self.state.name, auto_manual="MANUAL")
                self._barrier(action="check_truck")
                time.sleep(1)
                # Web app will handle this - popup will appear
                return
            
            # Set red signal
            self._barrier(action="red_signal")
            self._goto(State.RED_SIGNAL)
        else:
            print("[MANAGER] Waiting for vehicle to be placed …")

    def _handle_red_signal(self):
        msg = self._pop("plc_barrier/status")
        
        if msg and msg.get("status") == "red_sent":
            print("[MANAGER] Red signal confirmed.")
            self._goto(State.CLOSE_BARRIER)
            return
        
        if self._elapsed() > 30:
            print("[MANAGER] Red signal timeout — proceeding to close barrier")
            self._goto(State.CLOSE_BARRIER)

    def _handle_close_barrier(self):
        print("[MANAGER] Sending close barrier command …")
        self._barrier(action="close_barrier")
        self._goto(State.BARRIER_CLOSING)

    def _handle_barrier_closing(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        if status == "barrier_closed":
            print("[MANAGER] Barrier closed — moving auger to home …")
            self._sampler(action="move_home")
            self._goto(State.AUGER_HOME_POS)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_auger_home_pos(self):
        print("[MANAGER] Setting auger to home position …")
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
            print("[MANAGER] Home position timeout — proceeding with sampling")
            self._current_sample_index = 0
            self._successful_cycles = 0
            self._goto(State.CYCLE_POSITION)

        self._sampler(action="move_home")
        time.sleep(10)

    def _handle_cycle_position(self):
        if self._successful_cycles >= TOTAL_CYCLES:
            print(f"[MANAGER] All {TOTAL_CYCLES} cycles completed.")
            self._goto(State.COMPLETE_FINAL)
        
        # Get sample positions
        areas = [p["area"] for p in self.positions]
        self.positions.append(get_sample_positions(areas))
        print(f"[MANAGER] Sampling positions: {self.positions}")
        
        pos = self.positions[self._current_sample_index]
        print(f"[MANAGER] Moving to sampling position: {pos}")
        
        self._sampler(action="sample_cycle", x=pos["x"], y=pos["y"])
        self._goto(State.CYCLE_CONFIRM)

    def _handle_cycle_confirm(self):
        msg = self._pop("plc_sampler/status")

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
        
        if msg and msg.get("status") == "position_set":
            print("[MANAGER] Auger positioned — waiting for AI confirmation …")
            if self.ai_model:
                try: self._confirm_auger_position_with_movement_loop(self.positions[self._current_sample_index]["area"])
                except: pass

            cycle_num = self._successful_cycles + 1
            self.cycle = cycle_num
            self._sampler(action="start_cycle", cycle=cycle_num)
            self._goto(State.CYCLE_CAPTURE)
            return
        
        if self._elapsed() > POSITION_CONFIRMATION_TIMEOUT:
            print("[MANAGER] Position confirmation timeout — starting cycle")
            self._goto(State.CYCLE_CAPTURE)

    def _handle_cycle_capture(self):
        """Start sampling cycle"""
        # Check for emergency stop before starting cycle
        msg = self._pop("plc_sampler/status")

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        self._sampler(action="check_sample_cycle_complete") 
        self._goto(State.CYCLE_DONE)

    def _handle_cycle_done(self):
        """Wait for sampling cycle completion"""
        msg = self._pop("plc_sampler/status")
        
        if not msg:
            return
        
        status = msg.get("status", "")
        
        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
        
        if status == "sample_cycle_complete":
            print(f"[MANAGER] Cycle {self.cycle} completed")
            # TODO: AI verification of successful sample
            self._successful_cycles += 1
            self._current_sample_index += 1
            
            if self._successful_cycles >= TOTAL_CYCLES:
                print(f"[MANAGER] All {TOTAL_CYCLES} successful cycles completed.")
                time.sleep(100)  # Ensure PLC has time to update status
                self._sampler(action="check_all_samples_status")
                self._goto(State.SAMPLE_COLLECTION)
            else:
                self._cam(action="cam3_single", path=self.paths[f"SAMPLE_{self._successful_cycles}_IMG_PATH"])
                print(f"[MANAGER] Moving to next sampling position …")
                self._goto(State.CYCLE_POSITION)

        elif status == "cycle_error":
            print(f"[MANAGER] Cycle error: {msg.get('msg')}")
            print("[MANAGER] Retrying with different position …")
            self.positions.pop()
            self._goto(State.CYCLE_POSITION)
        
        if self._elapsed() > SAMPLE_CYCLE_TIMEOUT:
            print("[MANAGER] Cycle timeout — moving to next position")
            self.positions.pop()
            self._goto(State.CYCLE_POSITION)
        else:
            print("[MANAGER] Cycle inrpogress — waiting …")
            self._sampler(action="check_sample_cycle_complete")
            time.sleep(5)
            
    def _handle_all_samples_collection(self):
        """Start sampling cycle"""
        # Check for emergency stop before starting cycle
        msg = self._pop("plc_sampler/status")

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
        
        elif msg and msg.get("status") == "all_samples_collected":
            now = time.time()
            while time.time() - now < CLOSE_CYCLE_WAIT_TIME:
                print("[MANAGER] Waiting for Cycle Stop.")
            self._sampler(action="sample_cycle_stop")
            self._goto(State.COMPLETE_FINAL)

        else:
            self._sampler(action="check_all_samples_status")
            time.sleep(1)

    def _handle_cycle_emergency_wait(self):
        print("[MANAGER] Waiting for emergency stop clearance on Sampler PLC …")
        
        msg = self._pop("plc_sampler/status")
        if not msg:
            return
        
        status = msg.get("status", "")
        # Emergency stop cleared - resume to CYCLE_CONFIRM and reset cycles
        if status == "emergency_cleared":
            print("[MANAGER] Emergency stop cleared. Resuming operation …")
            
            # Reset successful cycles and return to CYCLE_CONFIRM
            self._successful_cycles = 0
            self._current_sample_index = 0
            self.positions = []
            state = self._emergency_return_state
            self._emergency_return_state = None
            self._goto(state)

    def _handle_complete_final(self):
        """Complete sampling — generate QR code and save logs"""
        msg = self._pop("plc_sampler/status")

        if msg and msg.get("status") == "sample_cycle_stop_comp": 
            print(f"[MANAGER] Sampling complete — generating QR code …")
        
            vendor_name = self.vehicle.get("VENDER_NAME", "UNKNOWN")
            vehicle_number = self.vehicle.get("VEHICLE_NUMBER", "UNKNOWN")
            qr_path = generate_qr_code(vendor_name, vehicle_number, self.uid, self.paths["QR_CODE_PATH"])
            print(f"[MANAGER] QR code generated at {qr_path}")
            
            # Set green signal
            self._barrier(action="green_signal")
            self._goto(State.GREEN_SIGNAL)

        else:
            print(f"[MANAGER] Waiting for sample cycle stop confirmation …")
            return

    def _handle_green_signal(self):
        """Send green signal to PLC"""
        msg = self._pop("plc_barrier/status")
        
        if msg and msg.get("status") == "green_sent":
            print("[MANAGER] Green signal confirmed.")
            self._goto(State.COMPLETE)
            return
        
        if self._elapsed() > 10:
            print("[MANAGER] Green signal timeout — marking as complete")
            self._goto(State.COMPLETE)

    def _handle_complete(self):
        """Session complete — ready for next vehicle"""
        print(f"[MANAGER] Session {self.uid} complete.")
        db_complete_log(self.uid)
        time.sleep(5)
        self._cam(action="sample_capture_stop")
        self._sampler(action="reset")
        self._barrier(action="reset")
        self._reset("")
        self._cam(action="reset")

    def _handle_error(self):
        """Error state — reset system"""
        print("[MANAGER] Error state — resetting system.")
        self._cam(action="sample_capture_stop")
        self._cam(action="reset")
        self._barrier(action="close_barrier")
        self._sampler(action="reset")
        self._reset("Handled error state !!")

    def _reset(self, msg: str = ""):
        """Reset state for next vehicle"""
        if msg != "":
            db_error_log(self.uid, f"Session reset due to error : {msg}")
        self.uid       = None
        self.rfids     = []
        self.vehicle   = None
        self.positions = []
        self.cycle     = 0
        self._current_sample_index = 0
        self._successful_cycles = 0
        self._goto(State.IDLE)
        print("[MANAGER] Ready for next vehicle")

    def run(self):
        print("[MANAGER] Coal Sampling Manager started.")
        print("[MANAGER] Waiting for vehicles …\n")
        while True:
            try:
                handler_name = self.HANDLERS.get(self.state)
                if handler_name:
                    getattr(self, handler_name)()
            except Exception as e:
                import traceback
                print(f"[MANAGER] Unhandled exception in {self.state.name}: {e}")
                traceback.print_exc()
                self._goto(State.ERROR)
            time.sleep(0.05)

def main():
    mgr = Manager()
    mgr.run()

if __name__ == "__main__":
    main()