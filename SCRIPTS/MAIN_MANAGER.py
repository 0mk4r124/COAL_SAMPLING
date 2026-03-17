import time
import random
import threading
import pymysql
from datetime import datetime
from enum import Enum, auto

from DEPENDANT.MQTT import MQTT

# ── Database ──────────────────────────────────────────────────────────────────
DB_HOST = "127.0.0.1"
DB_USER = "root"
DB_PASS = "insightzz@123"
DB_NAME = "COAL_SAMPLING_DHAR"

# ── Tuning ────────────────────────────────────────────────────────────────────
DB_POLL_SEC     = 10
DB_WAIT_TIMEOUT = 900
TOTAL_CYCLES    = 3
SAVE_PATH = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/"

SAMPLE_POSITIONS = [
    {"x": 100, "y": 150},
    {"x": 300, "y": 150},
    {"x": 500, "y": 150},
]

def get_sample_positions():
    positions = SAMPLE_POSITIONS.copy()
    random.shuffle(positions)
    return positions

# ── State machine ─────────────────────────────────────────────────────────────
class State(Enum):
    IDLE              = auto()
    DB_CHECK          = auto()
    WAITING_FOR_DB    = auto()
    OPEN_BARRIER      = auto()
    BARRIER_OPENING   = auto()
    SET_BUCKET        = auto()
    VEHICLE_PLACEMENT = auto()
    CLOSE_BARRIER     = auto()
    BARRIER_CLOSING   = auto()
    CYCLE_POSITION    = auto()
    CYCLE_CAPTURE     = auto()
    CYCLE_DONE        = auto()
    RED_SIGNAL        = auto()
    GREEN_SIGNAL      = auto()
    COMPLETE          = auto()
    ERROR             = auto()

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

def db_create_log(uid: str, rfids: list) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        now = datetime.now()
        cur.execute(
            """
            INSERT INTO VEHICLE_LOGS
                (UID, RFIDS, STATUS, CREATE_TIME, UPDATE_TIME)
            VALUES
                (%s, %s, %s, %s, %s)
            """,
            (uid, "|".join(rfids), "IN_PROGRESS", now, now)
        )
        db.commit()
        print(f"[DB] Log created  uid={uid}  rfids={rfids}")
        return True
    except Exception as e:
        print(f"[DB] db_create_log error: {e}")
        return False
    finally:
        if db: db.close()

def db_error_log(uid: str) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        cur.execute(
            """
            UPDATE VEHICLE_LOGS
               SET STATUS = 'ERROR', UPDATE_TIME = %s
             WHERE UID = %s
            """,
            (datetime.now(), uid)
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
    except Exception as e:
        print(f"[DB] db_complete_log error: {e}")
    finally:
        if db: db.close()


# ══════════════════════════════════════════════════════════════════════════════
class Manager:

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

        self._state_entered_at: float = 0.0
        self._db_last_polled  : float = 0.0

    def _pop(self, topic: str) -> dict | None:
        return self.mqtt.pop(topic)

    def _cam(self, **kw):     self.mqtt.publish("manager/camera",      kw)
    def _barrier(self, **kw): self.mqtt.publish("manager/plc_barrier", kw)
    def _sampler(self, **kw): self.mqtt.publish("manager/plc_sampler", kw)

    def _goto(self, new_state: State):
        print(f"[MANAGER] State: {self.state.name}  {new_state.name}")
        self.state             = new_state
        self._state_entered_at = time.time()

    def _elapsed(self) -> float:
        return time.time() - self._state_entered_at

    def _resolve_rfid(self, rfids: list) -> tuple[str | None, dict | None]:
        for rfid in rfids:
            row = db_find_vehicle(rfid)
            if row:
                return rfid, row
        return None, None

    # ── State handlers ────────────────────────────────────────────────────────

    def _handle_idle(self):
        # BUG 2 FIX: removed print(self._inbox) — _inbox doesn't exist on Manager
        msg = self._pop("manager/rfid")
        if not msg:
            return
        self.uid   = msg["uid"]
        self.rfids = msg.get("rfids", [])
        print(f"[MANAGER] RFID event uid={self.uid}  tags={self.rfids}")
        self._cam(action="cam2_single", uid=self.uid, cycle=0)
        self._goto(State.DB_CHECK)

    def _handle_db_check(self):
        valid_rfid, vehicle = self._resolve_rfid(self.rfids)
        db_create_log(self.uid, self.rfids)
        print("[MANAGER] RFID not in DB — will poll …")
        self._db_last_polled = time.time()
        self._goto(State.WAITING_FOR_DB)

    def _handle_waiting_for_db(self):
        if self._elapsed() > DB_WAIT_TIMEOUT:
            print("[MANAGER] DB wait timed out — resetting.")
            self._reset()
            return

        if time.time() - self._db_last_polled < DB_POLL_SEC:
            return

        self._db_last_polled = time.time()
        print("[MANAGER] Polling DB for RFID …")
        valid_rfid, vehicle = self._resolve_rfid(self.rfids)

        if vehicle:
            print(f"[MANAGER] RFID now in DB: {vehicle}")
            self.vehicle = vehicle

            if db_vehicle_already_in_front("|".join(self.rfids)):
                print("[MANAGER] Vehicle already in front — aborting.")
                self._reset()
                return

            # Log already created in DB_CHECK — no second insert needed
            self._goto(State.OPEN_BARRIER)

    def _handle_open_barrier(self):
        self._barrier(action="open_barrier")
        self._goto(State.BARRIER_OPENING)

    def _handle_barrier_opening(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return
        status = msg.get("status", "")
        if status == "barrier_opened":
            self._cam(action="cam13_start", uid=self.uid, cycle=self.cycle)
            self._goto(State.SET_BUCKET)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_set_bucket(self):
        bucket_no = int(self.vehicle.get("BUCKET_NO", 1))
        self._barrier(action="set_bucket", bucket_no=bucket_no)
        deadline = time.time() + 150
        while time.time() < deadline:
            msg = self._pop("plc_barrier/status")
            if msg and msg.get("status") == "bucket_set":
                print(f"[MANAGER] Bucket {bucket_no} confirmed.")
                break
            time.sleep(0.1)
        self._goto(State.VEHICLE_PLACEMENT)

    def _handle_vehicle_placement(self):
        msg = self._pop("plc_barrier/status")
        self._barrier(action="check_truck")
        if not msg:
            return
        status = msg.get("status", "")
        if status == "truck":
            present = msg.get("present", False)
            if present:
                print("[MANAGER] Truck placement confirmed.")
                time.sleep(10)
                self._goto(State.RED_SIGNAL)
            else:
                print("[MANAGER] Truck removed — resetting.")
                
    def _handle_close_barrier(self):
        self._barrier(action="close_barrier")
        self._goto(State.BARRIER_CLOSING)

    def _handle_barrier_closing(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return
        status = msg.get("status", "")
        if status == "barrier_closed":
            time.sleep(10)
            self._goto(State.CYCLE_CAPTURE)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_cycle_capture(self):
        msg = self._pop("plc_sampler/status")
        if not msg:
            if self.cycle == 0:
                print("[MANAGER] Starting sampling cycle 1")
                self._sampler(action="sample_cycle_1")
                self.cycle = -1   # prevent retrigger
            return

        status = msg.get("status", "")
        if status == "sampler_error":
            print(f"[MANAGER] Sampler error: {msg.get('msg')}")
            self._goto(State.ERROR)
            return

        if status == "sample_cycle_1_comp":
            print("[MANAGER] Sample cycle 1 completed")
            self._sampler(action="sample_cycle_2")
            print("[MANAGER] Starting sampling cycle 2")
        elif status == "sample_cycle_2_comp":
            print("[MANAGER] Sample cycle 2 completed")
            self._sampler(action="sample_cycle_3")
            print("[MANAGER] Starting sampling cycle 3")
        elif status == "sample_cycle_3_comp":
            print("[MANAGER] Sample cycle 3 completed")
            self._sampler(action="sample_cycle_stop")
            print("[MANAGER] Stopping sampler")
        elif status == "sample_cycle_stop_comp":
            print("[MANAGER] Sampler stopped")
            self._goto(State.GREEN_SIGNAL)

    def _handle_red_signal(self):
        self._barrier(action="red_signal")
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = self._pop("plc_barrier/status")
            if msg and msg.get("status") == "red_sent":
                break
            time.sleep(0.1)
        self._goto(State.CLOSE_BARRIER)

    def _handle_green_signal(self):
        self._barrier(action="green_signal")
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = self._pop("plc_barrier/status")
            if msg and msg.get("status") == "green_sent":
                break
            time.sleep(0.1)
        self._goto(State.COMPLETE)

    def _handle_complete(self):
        print(f"[MANAGER] Session {self.uid} COMPLETE.")
        db_complete_log(self.uid)
        time.sleep(10)
        self._cam(action="cam13_stop")
        self._sampler(action="reset")
        self._reset()

    def _handle_error(self):
        print("[MANAGER] Error state — resetting system.")
        self._cam(action="reset")
        self._barrier(action="close_barrier")
        self._sampler(action="reset")
        db_error_log(self.uid)
        self._reset()

    def _reset(self):
        db_error_log(self.uid)
        self.uid       = None
        self.rfids     = []
        self.vehicle   = None
        self.positions = []
        self.cycle     = 0
        self._goto(State.IDLE)
        print("[MANAGER] Ready for next vehicle")

    HANDLERS = {
        State.IDLE             : "_handle_idle",
        State.DB_CHECK         : "_handle_db_check",
        State.WAITING_FOR_DB   : "_handle_waiting_for_db",
        State.OPEN_BARRIER     : "_handle_open_barrier",
        State.BARRIER_OPENING  : "_handle_barrier_opening",
        State.SET_BUCKET       : "_handle_set_bucket",
        State.VEHICLE_PLACEMENT: "_handle_vehicle_placement",
        State.CLOSE_BARRIER    : "_handle_close_barrier",
        State.BARRIER_CLOSING  : "_handle_barrier_closing",
        State.RED_SIGNAL       : "_handle_red_signal",
        State.CYCLE_CAPTURE    : "_handle_cycle_capture",
        State.GREEN_SIGNAL     : "_handle_green_signal",
        State.COMPLETE         : "_handle_complete",
        State.ERROR            : "_handle_error",
    }

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