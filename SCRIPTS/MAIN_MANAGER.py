import time
import random
import threading
import pymysql
from datetime import datetime
from enum import Enum, auto

from DEPENDANT.MQTT import MQTT

DB_HOST = "127.0.0.1"
DB_USER = "root"
DB_PASS = "insightzz@123"
DB_NAME = "COAL_SAMPLING_DHAR"

DB_POLL_SEC     = 10    # re-check interval when RFID not yet in master table
DB_WAIT_TIMEOUT = 300   # give up after this many seconds
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

class State(Enum):
    IDLE              = auto()
    DB_CHECK          = auto()
    WAITING_FOR_DB    = auto()
    OPEN_BARRIER      = auto()
    BARRIER_OPENING   = auto()
    SET_BUCKET        = auto()
    VEHICLE_PLACEMENT = auto()
    CYCLE_POSITION    = auto()
    CYCLE_CAPTURE     = auto()
    CYCLE_DONE        = auto()
    GREEN_SIGNAL      = auto()
    COMPLETE          = auto()
    ERROR             = auto()

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
            "SELECT * FROM VEHICLE_MASTER WHERE RFID = %s LIMIT 1", (rfid,)
        )
        return cur.fetchone()
    except Exception as e:
        print(f"[DB] db_find_vehicle error: {e}")
        return None
    finally:
        if db: db.close()

def db_vehicle_already_in_front(vehicle_no: str) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        cur.execute(
            """SELECT COUNT(*) FROM VEHICLE_LOGS
               WHERE VEHICLE_NO = %s AND STATUS = 'IN_PROGRESS'""",
            (vehicle_no,)
        )
        row = cur.fetchone()
        return (row[0] > 0) if row else False
    except Exception as e:
        print(f"[DB] db_vehicle_already_in_front error: {e}")
        return False
    finally:
        if db: db.close()

def db_create_log(uid: str, rfids: list, vehicle: dict) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        cur.execute(
            """INSERT INTO VEHICLE_LOGS
               (UID, RFIDS, IMG_1_PATH, IMG_2_PATH, IMG_3_PATH,
                CREATE_TIME, STATUS)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                uid, "|".join(rfids),
                f"{SAVE_PATH}{uid}/CAM1/",
                f"{SAVE_PATH}{uid}/CAM2/",
                f"{SAVE_PATH}{uid}/CAM3/",
                datetime.now(), "IN_PROGRESS"
            )
        )
        db.commit()
        return True
    except Exception as e:
        print(f"[DB] db_create_log error: {e}")
        return False
    finally:
        if db: db.close()

def db_complete_log(uid: str) -> bool:
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor()
        cur.execute(
            "UPDATE VEHICLE_LOGS SET STATUS = 'COMPLETED' WHERE UID = %s",
            (uid,)
        )
        db.commit()
        return True
    except Exception as e:
        print(f"[DB] db_complete_log error: {e}")
        return False
    finally:
        if db: db.close()

# ══════════════════════════════════════════════════════════════════════════════
class Manager:

    def __init__(self):
        self.mqtt = MQTT("MAIN_MANAGER")

        # ── BUG 2 FIX: subscribe() now creates the queue BEFORE subscribing,
        #   so no retained message can arrive before its queue exists.
        #   No monkey-patch needed — MQTT.pop() replaces self._pop().
        for topic in (
            "manager/rfid",
            "camera/status",
            "plc_barrier/status",
            "plc_sampler/status",
        ):
            self.mqtt.subscribe(topic)

        self.state    = State.IDLE
        self.uid      : str | None  = None
        self.rfids    : list        = []
        self.vehicle  : dict | None = None
        self.positions: list        = []
        self.cycle    : int         = 0

        self._state_entered_at: float = 0.0
        self._db_last_polled  : float = 0.0

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _pop(self, topic: str) -> dict | None:
        """BUG 1 FIX: delegates to MQTT.pop() which uses a deque, not a dict
        slot — every message is preserved in arrival order."""
        return self.mqtt.pop(topic)

    def _cam(self, **kw):     self.mqtt.publish("manager/camera",       kw)
    def _barrier(self, **kw): self.mqtt.publish("manager/plc_barrier",  kw)
    def _sampler(self, **kw): self.mqtt.publish("manager/plc_sampler",  kw)

    def _goto(self, new_state: State):
        print(f"[MANAGER] State: {self.state.name} → {new_state.name}")
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

        if vehicle:
            print(f"[MANAGER] RFID found in DB: {vehicle}")
            self.vehicle = vehicle
            if db_vehicle_already_in_front(vehicle.get("VEHICLE_NO", "")):
                print("[MANAGER] Vehicle already in front — aborting.")
                self._reset()
                return
            db_create_log(self.uid, self.rfids, vehicle)
            self._goto(State.OPEN_BARRIER)
        else:
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
            if db_vehicle_already_in_front(vehicle.get("VEHICLE_NO", "")):
                print("[MANAGER] Vehicle already in front — aborting.")
                self._reset()
                return
            db_create_log(self.uid, self.rfids, vehicle)
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
            self._goto(State.VEHICLE_PLACEMENT)
        elif status == "barrier_error":
            print(f"[MANAGER] Barrier error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_set_bucket(self):
        bucket_no = int(self.vehicle.get("BUCKET_NO", 1))
        self._barrier(action="set_bucket", bucket_no=bucket_no)
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = self._pop("plc_barrier/status")
            if msg and msg.get("status") == "bucket_set":
                print(f"[MANAGER] Bucket {bucket_no} confirmed.")
                break
            time.sleep(0.1)
        self._goto(State.VEHICLE_PLACEMENT)

    def _handle_vehicle_placement(self):
        msg = self._pop("plc_barrier/status")
        if not msg:
            return

        status = msg.get("status", "")
        if status == "truck":
            present = msg.get("present", False)
            if present:
                print("[MANAGER] Truck placement confirmed.")
                time.sleep(60)
                self._barrier(action="close_barrier")
                self._goto(State.COMPLETE)
            else:
                print("[MANAGER] Truck removed — resetting.")
                self._reset()

    def _handle_cycle_position(self):
        self.cycle += 1
        if self.cycle > TOTAL_CYCLES:
            self._goto(State.GREEN_SIGNAL)
            return
        pos = self.positions[self.cycle - 1]
        print(f"[MANAGER] Cycle {self.cycle}/{TOTAL_CYCLES} → {pos}")
        self._sampler(action="set_position",
                      x=pos["x"], y=pos["y"], cycle=self.cycle)
        self._goto(State.CYCLE_CAPTURE)

    def _handle_cycle_capture(self):
        msg = self._pop("plc_sampler/status")
        if not msg:
            return
        status = msg.get("status", "")
        if status == "position_set":
            self._cam(action="cam13_start", uid=self.uid, cycle=self.cycle)
            self._sampler(action="start_cycle", cycle=self.cycle)
        elif status == "discharge_received":
            print(f"[MANAGER] Discharge received for cycle {msg.get('cycle')}.")
            self._cam(action="cam13_stop")
            self._goto(State.CYCLE_DONE)
        elif status == "error":
            print(f"[MANAGER] Sampler error: {msg.get('msg')}")
            self._goto(State.ERROR)

    def _handle_cycle_done(self):
        if self.cycle < TOTAL_CYCLES:
            time.sleep(1)
            self._goto(State.CYCLE_POSITION)
        else:
            self._goto(State.GREEN_SIGNAL)

    def _handle_green_signal(self):
        print("[MANAGER] All cycles complete → sending GREEN signal.")
        self._sampler(action="send_green")
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = self._pop("plc_sampler/status")
            if msg and msg.get("status") == "green_sent":
                break
            time.sleep(0.1)
        self._goto(State.COMPLETE)

    def _handle_complete(self):
        print(f"[MANAGER] Session {self.uid} COMPLETE.")
        db_complete_log(self.uid)
        self._sampler(action="reset")
        self._reset()

    def _handle_error(self):
        print("[MANAGER] Error state — resetting system.")
        self._cam(action="reset")
        self._barrier(action="close_barrier")
        self._sampler(action="reset")
        self._reset()

    def _reset(self):
        self.uid       = None
        self.rfids     = []
        self.vehicle   = None
        self.positions = []
        self.cycle     = 0
        self._goto(State.IDLE)
        print("[MANAGER] ── Ready for next vehicle ──")

    # ── Dispatch table ────────────────────────────────────────────────────────
    HANDLERS = {
        State.IDLE             : "_handle_idle",
        State.DB_CHECK         : "_handle_db_check",
        State.WAITING_FOR_DB   : "_handle_waiting_for_db",
        State.OPEN_BARRIER     : "_handle_open_barrier",
        State.BARRIER_OPENING  : "_handle_barrier_opening",
        # State.SET_BUCKET     : "_handle_set_bucket",   # uncomment when needed
        State.VEHICLE_PLACEMENT: "_handle_vehicle_placement",
        # State.CYCLE_POSITION : "_handle_cycle_position",
        # State.CYCLE_CAPTURE  : "_handle_cycle_capture",
        # State.CYCLE_DONE     : "_handle_cycle_done",
        # State.GREEN_SIGNAL   : "_handle_green_signal",
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