# ═══════════════════════════════════════════════════════════════════════════════
# MAIN_MANAGER.py
#
# Coal sampling state machine.
#
# Sampling geometry (see the "Sampling geometry" block below):
#   - X is drawn with the order statistics of three uniforms, so all three
#     points are uniform over EVERY layout that satisfies MIN_X_GAP and each
#     area gets the same amount of room to move. The old greedy draw spent the
#     slack front-to-back, which pinned area 3 against the head end.
#   - Y is rejection-sampled against MIN_Y_GAP, so two points can never land on
#     the same line across the bed.
#   - Every AI-obstruction / hard-material failure is remembered in
#     self._avoid_points, and later points give that ground a wide berth.
#   - A blocked point is retried in the OPPOSITE direction (Y flips to the far
#     half of the bed, X moves to the far side of the bad column) instead of
#     being nudged a few percent back into the same rock.
# ═══════════════════════════════════════════════════════════════════════════════

import os
import time
import random
import pymysql
import subprocess
import traceback
import threading
import json
import shutil

from datetime import datetime
from enum import Enum, auto
from fpdf import FPDF

from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger
# from DEPENDANT.EMAIL_ALERTS import send_new_vehicle_alert
from DEPENDANT.VEHICLE_HOOKS import (
    on_new_vehicle, resolve_pdf_url,
    on_inspection_complete, on_cycle_blocked,
    on_session_closed,
)

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

# ── AI auger position confirmation (before cycle start) ───────────────────────
AI_CONFIRM_ENABLED  = True   # False -> skip the check, start the cycle as before
AI_CONFIRM_RETRIES  = 3      # attempts at the SAME position (fresh capture each time)
AI_CONFIRM_DELAY    = 2      # seconds between attempts
TEMP_IMG_MAX_AGE = 2   # seconds — the background loop rewrites every ~1s,
                        # so anything older means that camera thread is dead
CAM_CMD_GAP      = 0.4  # gap between the two cam commands (see note below)

# How long to wait for the operator's choice on the AI-blocked popup before
# giving up on them and continuing automatically with a new position.
AI_DECISION_TIMEOUT = 600    # 10 minutes
# Two hard-material hits back to back mean the load is hard where we are
# probing — retrying more just hammers the auger into rock.
HARD_MATERIAL_CONSECUTIVE_LIMIT = 2

# If nobody answers the popup, default to MANUAL (see the note in the handler)
HARD_DECISION_TIMEOUT = 600     # 10 minutes

# ── Sample image collection (training data) ───────────────────────────────────
SAMPLE_CAPTURE_ENABLED  = False
SAMPLE_CAPTURE_CAMS     = ("CAM1", "CAM2", "CAM3")
SAMPLE_CAPTURE_INTERVAL = 1.0    # seconds between saves; the background camera
                                 # threads refresh TEMP_IMG about once a second
SAMPLE_CAPTURE_MAX_SECS = 120    # hard cap — a cycle runs ~60 s, so this can
                                 # never outlive the cycle even if a stop is missed

# ── Sampling geometry ─────────────────────────────────────────────────────────
X_MIN, X_MAX = 40, 100   # X travel bounds, % of full travel
Y_MIN, Y_MAX = 25, 85    # Y travel bounds, % of full travel
MIN_X_GAP    = 20        # Minimum X separation between the 3 sample points (%)

# Anti-clustering. Three independent uniform draws sit in a near-straight line
# often enough that the map stops looking random. These push the points apart
# without making them predictable.
MIN_Y_GAP       = 15     # Minimum Y separation between sample points (%)
PICK_CANDIDATES = 60     # Rejection-sampling tries before best-effort fallback
X_JITTER        = 4      # How far X may slide off its draw to dodge bad ground

# Blocked-ground avoidance (AI obstruction / hard material)
AVOID_RADIUS  = 14   # New points stay at least this far from any blocked point
ALT_MIN_SHIFT = 12   # Minimum move away from the blocked point on a retry

BLOCKING_REASONS = ("AI_OBSTRUCTION", "HARD_MATERIAL")

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
TEMP_IMG_PATH = os.path.join(BASE_FILE_PATH, "TEMP_IMG")
RAW_IMG_PATH = os.path.join(BASE_FILE_PATH, "RAW_IMG")
RESULT_IMG_PATH = os.path.join(BASE_FILE_PATH, "RESULT")
INF_IMG = os.path.join(BASE_FILE_PATH, "INF")
LOGS_PATH = os.path.join(BASE_FILE_PATH, "LOGS")

TOPIC_DECISION = "manager/decision"
IN_TOPICS = (
    "camera/status",
    # TOPIC_DECISION,
    "plc_barrier/status",
    "plc_sampler/status",
    "rfid/status",
)

# Initialize logger
logger = initializeLogger("MAIN_MANAGER", LOGS_PATH=LOGS_PATH)

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

def _dist(ax, ay, bx, by):
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5

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

# ── Sample point selection ────────────────────────────────────────────────────

def _x_slot(area, prev_points=None):
    """
    Feasible X window for `area`, i.e. the range that still honours MIN_X_GAP
    against every point already placed AND reserves MIN_X_GAP for each area
    still to come, so the last area can never be squeezed out of range.
    """
    x_lo = X_MIN
    x_hi = X_MAX - (TOTAL_CYCLES - area) * MIN_X_GAP

    for p in (prev_points or []):
        pa = p.get("area")
        if pa is None or pa == area:
            continue
        if pa < area:
            x_lo = max(x_lo, p["x"] + MIN_X_GAP)
        else:
            x_hi = min(x_hi, p["x"] - MIN_X_GAP)

    if x_lo > x_hi:
        print(f"[MANAGER] X range collapsed for area {area} (lo={x_lo}, hi={x_hi}) — clamping")
        logger.warning(f"X range collapsed for area {area} (lo={x_lo}, hi={x_hi})")
        x_lo = x_hi = _clamp(x_lo, X_MIN, X_MAX)

    return x_lo, x_hi

def _x_draw(area, prev_points=None):
    """
    Random X for `area`, drawn so the three points are UNIFORM over every
    layout that satisfies MIN_X_GAP — not just uniform over whatever range is
    left after the previous point was placed.

    Squeeze MIN_X_GAP out of the travel and the leftover slack is
    X_MAX - X_MIN - 2*MIN_X_GAP. Splitting that slack with the order statistics
    of three uniforms gives every area the same room to move. The old greedy
    draw spent the slack front-to-back, which pinned area 3 up against the head
    end and made the last point look fixed.
    """
    slack = X_MAX - X_MIN - MIN_X_GAP * (TOTAL_CYCLES - 1)
    if slack <= 0:
        return X_MIN + MIN_X_GAP * (area - 1)

    # How much slack the areas before this one have already consumed
    used = 0.0
    for p in (prev_points or []):
        pa = p.get("area")
        if pa is None or pa >= area:
            continue
        used = max(used, p["x"] - X_MIN - MIN_X_GAP * (pa - 1))
    used = _clamp(used, 0.0, float(slack))

    remaining = max(1, TOTAL_CYCLES - area + 1)
    u = used + (slack - used) * (1.0 - random.random() ** (1.0 / remaining))
    return X_MIN + MIN_X_GAP * (area - 1) + u

def _pick_point(x_lo, x_hi, y_lo, y_hi, area, prev_points=None, avoid=None,
                min_y_gap=MIN_Y_GAP, avoid_radius=AVOID_RADIUS,
                anchor=None, min_shift=0):
    """
    Draw a random (x, y) inside the window, rejecting candidates that cluster
    with an existing sample point, sit on blocked ground, or fail to move far
    enough away from `anchor`.

    Every constraint is a PREFERENCE, not a hard rule: after PICK_CANDIDATES
    tries the best-scoring candidate is returned. This can never fail to
    produce a point and can never spin.
    """
    x_lo, x_hi = int(round(x_lo)), int(round(x_hi))
    y_lo, y_hi = int(round(y_lo)), int(round(y_hi))
    if x_lo > x_hi:
        x_lo = x_hi = int(_clamp(x_lo, X_MIN, X_MAX))
    if y_lo > y_hi:
        y_lo = y_hi = int(_clamp(y_lo, Y_MIN, Y_MAX))

    prev_ys = [p["y"] for p in (prev_points or [])
               if p.get("area") != area and p.get("y") is not None]
    avoid   = [a for a in (avoid or []) if a.get("x") is not None
                                       and a.get("y") is not None]

    best, best_score = None, None
    for _ in range(PICK_CANDIDATES):
        x = random.randint(x_lo, x_hi)
        y = random.randint(y_lo, y_hi)

        y_gap  = min((abs(y - py) for py in prev_ys), default=999.0)
        a_dist = min((_dist(x, y, a["x"], a["y"]) for a in avoid), default=999.0)
        shift  = _dist(x, y, anchor["x"], anchor["y"]) if anchor else 999.0

        if y_gap >= min_y_gap and a_dist >= avoid_radius and shift >= min_shift:
            return {"x": x, "y": y, "area": area}

        # Blocked ground matters most, then getting clear of the bad point,
        # then spreading out on Y.
        score = (min(a_dist, avoid_radius),
                 min(shift, min_shift),
                 min(y_gap, min_y_gap))
        if best_score is None or score > best_score:
            best, best_score = {"x": x, "y": y, "area": area}, score

    return best

def get_sample_positions(used_areas=None, prev_points=None, avoid=None):
    """
    Return the next sampling point.

    Areas are taken IN ORDER: 1, then 2, then 3 (no random choice).
    X comes from _x_draw(), so all three points are uniform over every layout
    that satisfies MIN_X_GAP and each area gets the same room to move.
    Y is a free random draw that is rejected if it lands within MIN_Y_GAP of
    another point's Y, so the three dots never line up across the bed.
    Points that failed on AI obstruction or hard material are passed in
    `avoid` and are given a wide berth.
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

    x_lo, x_hi = _x_slot(area, prev_points)

    # Narrow the feasible slot down to a window around the order-statistic
    # draw, so X is random over the whole layout but still gets a few percent
    # of wiggle while it hunts for a spot clear of blocked ground.
    x = _x_draw(area, prev_points)
    w_lo = _clamp(x - X_JITTER, x_lo, x_hi)
    w_hi = _clamp(x + X_JITTER, x_lo, x_hi)
    if w_lo <= w_hi:
        x_lo, x_hi = w_lo, w_hi

    return _pick_point(x_lo, x_hi, Y_MIN, Y_MAX, area, prev_points, avoid)

def get_alternate_position(pos, prev_points=None, avoid=None,
                           min_shift=ALT_MIN_SHIFT):
    """
    The point at `pos` was blocked — AI obstruction or hard material.

    Return a new point in the SAME area (three areas still have to be covered)
    but in a DIFFERENT DIRECTION: Y flips to the opposite half of the bed and X
    moves to the far side of the bad column inside the area's slot. Nudging a
    few percent, as the old nearby-retry did, just drives the auger back into
    the same rock or the same obstruction.
    """
    prev_points = prev_points or []
    avoid = list(avoid or [])
    if not any(a.get("x") == pos["x"] and a.get("y") == pos["y"] for a in avoid):
        avoid.append({"x": pos["x"], "y": pos["y"], "area": pos.get("area")})

    area = pos["area"]
    x_lo, x_hi = _x_slot(area, prev_points)

    # ── Y: jump to the opposite half of the bed ───────────────────────────────
    y_mid = (Y_MIN + Y_MAX) / 2.0
    if pos["y"] >= y_mid:
        y_lo, y_hi = Y_MIN, max(Y_MIN, y_mid - 1)        # was high -> go low
    else:
        y_lo, y_hi = min(Y_MAX, y_mid + 1), Y_MAX        # was low  -> go high

    # ── X: stay in the area's slot, but prefer the far side of the bad column ─
    if (pos["x"] - x_lo) >= (x_hi - pos["x"]):
        xa_lo, xa_hi = x_lo, pos["x"] - min_shift        # more room to the left
    else:
        xa_lo, xa_hi = pos["x"] + min_shift, x_hi        # more room to the right
    if xa_lo > xa_hi:                 # slot too tight to move X — Y does the work
        xa_lo, xa_hi = x_lo, x_hi

    new = _pick_point(xa_lo, xa_hi, y_lo, y_hi, area, prev_points, avoid,
                      anchor=pos, min_shift=min_shift)

    # Last resort: the window collapsed onto the bad point itself. Force the
    # Y flip so the retry is at least on the other side of the bed.
    if new is None or (new["x"] == pos["x"] and new["y"] == pos["y"]):
        new = {"x": int(_clamp(pos["x"], x_lo, x_hi)),
               "y": int(_clamp(Y_MAX if pos["y"] < y_mid else Y_MIN, Y_MIN, Y_MAX)),
               "area": area}

    return new

# Old name kept so any external caller keeps working. The `max_shift` keyword
# is gone — the point of a retry is now to get AWAY from the blocked spot,
# not to stay within 10% of it.
get_nearby_position = get_alternate_position


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
    AI_BLOCKED_WAIT          = auto()
    HARD_BLOCKED_WAIT        = auto()

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

def db_update_sample_info(uid: str, info: dict) -> bool:
    """Persist the sample map JSON onto VEHICLE_LOGS.SAMPLE_INFO."""
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()
        cur.execute(
            "UPDATE VEHICLE_LOGS SET SAMPLE_INFO = %s, UPDATE_TIME = %s WHERE UID = %s",
            (json.dumps(info), datetime.now(), uid)
        )
        db.commit()
        return True
    except Exception as e:
        print(f"[DB] db_update_sample_info error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()
    return False

def db_read_decision(uid: str) -> str:
    """Read the operator's decision from PLC_COMM (written by the web app)."""
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()
        cur.execute("SELECT AI_DECISION FROM PLC_COMM WHERE UID = %s", (uid,))
        row = cur.fetchone()
        if not row:
            return ""
        val = row["AI_DECISION"] if isinstance(row, dict) else row[0]
        return (val or "").strip().upper()
    except Exception as e:
        print(f"[DB] db_read_decision error: {e}")
        logger.error(f"{traceback.format_exc()}")
    finally:
        if db: db.close()
    return ""

def db_clear_decision(uid: str) -> bool:
    """
    Clear any stale decision. Called on ENTRY to a wait state so a choice left
    over from an earlier block can't be consumed by the next one.
    """
    db = None
    try:
        db = _db_connect()
        cur = db.cursor()
        cur.execute(
            "UPDATE PLC_COMM SET AI_DECISION = NULL, AI_DECISION_AT = NULL WHERE UID = %s",
            (uid,)
        )
        db.commit()
        return True
    except Exception as e:
        print(f"[DB] db_clear_decision error: {e}")
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
        State.AI_BLOCKED_WAIT      : "_handle_ai_blocked_wait",
        State.HARD_BLOCKED_WAIT    : "_handle_hard_blocked_wait",
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
         
        self._sample_info      = self._new_sample_info()
        self._ai_blocked_pos   = None    # position the AI rejected (for logging)
        self._ai_block_count   = 0       # how many times the AI has blocked this session
        self._session_pdf_url  = ""      # secured PDF/QR url resolved this session (for mail)
        # self._stop_sample_capture()
        self._capture_stop   = None
        self._capture_thread = None

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
        self._hard_retry_pos = None   # pre-computed point for the next CYCLE_POSITION pass (blocked-point retry)
        self._hard_retries   = 0      # consecutive hard-material hits for the CURRENT cycle number
        self._consecutive_hard = 0    # hard-material hits in a row; any SUCCESS clears it
        self._avoid_points     = []   # every AI-blocked / hard-material spot this
                                      # session, so later points steer clear of it

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
    
    def _start_sample_capture(self, cycle: int):
        """
        Start collecting training frames for one sampling cycle.

        Runs in a daemon thread that only COPIES the TEMP_IMG frames the
        camera service already refreshes every second. It sends no MQTT
        commands, so it cannot collide with the manager's own camera traffic,
        and it touches no manager state beyond its own stop flag.

        Safe to call repeatedly: an already-running capture is stopped first,
        so cycles can never stack up into multiple loops.
        """
        if not SAMPLE_CAPTURE_ENABLED:
            return

        self._stop_sample_capture()          # never run two at once

        uid   = self.uid
        stop  = threading.Event()
        self._capture_stop = stop

        def _worker():
            started   = time.time()
            saved     = 0
            last_mtime = {}                  # skip frames the camera hasn't refreshed

            try:
                while not stop.is_set():
                    if (time.time() - started) > SAMPLE_CAPTURE_MAX_SECS:
                        logger.warning(f"Sample capture hit the {SAMPLE_CAPTURE_MAX_SECS}s "
                                       f"cap for cycle {cycle} — stopping")
                        break

                    for cam in SAMPLE_CAPTURE_CAMS:
                        src = os.path.join(TEMP_IMG_PATH, f"{cam}_REDUCED.jpg")
                        try:
                            if not os.path.exists(src):
                                continue

                            mtime = os.path.getmtime(src)
                            if last_mtime.get(cam) == mtime:
                                continue     # same frame as last pass, don't duplicate
                            last_mtime[cam] = mtime

                            dest_dir = os.path.join(RAW_IMG_PATH, uid, cam)
                            os.makedirs(dest_dir, exist_ok=True)
                            stamp = datetime.now().strftime("%H%M%S_%f")[:-3]
                            dest  = os.path.join(dest_dir, f"{uid}_c{cycle}_{stamp}.jpg")

                            shutil.copy2(src, dest)
                            saved += 1
                        except Exception:
                            # One bad frame must never kill the collection thread
                            logger.error(f"Sample capture error for {cam}:\n{traceback.format_exc()}")

                    stop.wait(SAMPLE_CAPTURE_INTERVAL)   # interruptible sleep

            finally:
                print(f"[MANAGER] Sample capture stopped for cycle {cycle} ({saved} frames)")
                logger.debug(f"Sample capture stopped for cycle {cycle} ({saved} frames)")

        self._capture_thread = threading.Thread(
            target=_worker, name=f"SampleCapture-c{cycle}", daemon=True
        )
        self._capture_thread.start()
        print(f"[MANAGER] Sample capture started for cycle {cycle}")
        logger.debug(f"Sample capture started for cycle {cycle}")

    def _stop_sample_capture(self):
        """Signal the capture thread to finish. Never blocks the state machine."""
        if self._capture_stop:
            self._capture_stop.set()
        self._capture_stop   = None
        self._capture_thread = None

    def _capture_images_for_confirmation(self, loop_num: int, movement_type: str = "") -> tuple[str, str] | None:
        """
        Get a CAM1 + CAM3 pair for the AI check from TEMP_IMG.

        No fresh single-capture request is made. CAM_CAPTURE's background
        threads already rewrite TEMP_IMG/CAM*_REDUCED.jpg about once a second,
        so those frames are as current as anything we could ask for — and the
        request path cost a 10 s timeout on every attempt because CAM1's
        command was being dropped (CAM_CAPTURE keeps one MQTT slot and the
        cam3 publish overwrote the cam1 publish before its 50 ms poll).

        The frames are copied into the session folder so the AI's _vis.jpg and
        _masked.jpg outputs are kept per session instead of overwriting
        TEMP_IMG, which the background threads would clobber a second later.
        """
        try:
            inf_dir = os.path.join(INF_IMG, self.date_file, self.uid)
            os.makedirs(inf_dir, exist_ok=True)

            suffix = f"_loop{loop_num}_{movement_type}" if movement_type else f"_loop{loop_num}"
            cam1_dest = os.path.join(inf_dir, f"CAM1{suffix}.jpg")
            cam3_dest = os.path.join(inf_dir, f"CAM3{suffix}.jpg")

            pairs = [("CAM1", os.path.join(TEMP_IMG_PATH, "CAM1_REDUCED.jpg"), cam1_dest),
                     ("CAM3", os.path.join(TEMP_IMG_PATH, "CAM3_REDUCED.jpg"), cam3_dest)]

            # A frame older than TEMP_IMG_MAX_AGE means that camera's
            # background thread has died. Running the AI on a minutes-old
            # picture would be worse than reporting a failure.
            for name, src, _dest in pairs:
                if not os.path.exists(src):
                    print(f"[MANAGER] TEMP image missing for {name}: {src}")
                    logger.warning(f"TEMP image missing for {name}: {src}")
                    return None

                age = time.time() - os.path.getmtime(src)
                if age > TEMP_IMG_MAX_AGE:
                    print(f"[MANAGER] TEMP image for {name} is stale ({age:.0f}s old)")
                    logger.warning(f"TEMP image for {name} is stale ({age:.0f}s old)")
                    return None

            for name, src, dest in pairs:
                shutil.copy2(src, dest)

            logger.debug(f"Using TEMP frames: {cam1_dest}, {cam3_dest}")
            return (cam1_dest, cam3_dest)

        except Exception as e:
            print(f"[MANAGER] Error preparing confirmation images: {e}")
            logger.error(f"{traceback.format_exc()}")
            return None

    def _temp_image_pair(self, cam1_dest: str, cam3_dest: str) -> tuple[str, str] | None:
        """
        Copy the latest TEMP_IMG frames into the session folder.

        Returns None if either frame is missing or stale — a frame older than
        TEMP_IMG_MAX_AGE means that camera's background thread has stopped, and
        running the AI on a minutes-old picture of a different truck would be
        worse than reporting a failure.
        """
        pairs = [("CAM1", os.path.join(TEMP_IMG_PATH, "CAM1_REDUCED.jpg"), cam1_dest),
                 ("CAM3", os.path.join(TEMP_IMG_PATH, "CAM3_REDUCED.jpg"), cam3_dest)]

        for name, src, _dest in pairs:
            if not os.path.exists(src):
                print(f"[MANAGER] TEMP image missing for {name}: {src}")
                logger.warning(f"TEMP image missing for {name}: {src}")
                return None

            age = time.time() - os.path.getmtime(src)
            if age > TEMP_IMG_MAX_AGE:
                print(f"[MANAGER] TEMP image for {name} is stale ({age:.0f}s old) — not using it")
                logger.warning(f"TEMP image for {name} is stale ({age:.0f}s old)")
                return None

        try:
            for name, src, dest in pairs:
                shutil.copy2(src, dest)
                logger.debug(f"Using TEMP frame for {name}: {src} -> {dest}")
            print(f"[MANAGER] Using TEMP frames: {cam1_dest}, {cam3_dest}")
            return (cam1_dest, cam3_dest)
        except Exception as e:
            print(f"[MANAGER] Could not copy TEMP frames: {e}")
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

    # ─────────────────────────────────────────────────────────────────────────────
    # Sample-info tracking
    # ─────────────────────────────────────────────────────────────────────────────
 
    def _new_sample_info(self) -> dict:
        return {
            "uid": self.uid,
            "total_cycles": TOTAL_CYCLES,
            "successful": 0,
            "failed": 0,
            "collection_mode": "AUTO",
            "bounds": {"x_min": X_MIN, "x_max": X_MAX, "y_min": Y_MIN, "y_max": Y_MAX},
            "attempts": [],
            "current": None,          # point being sampled RIGHT NOW (live dashboard dot)
            "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "cycle_started": None,    # barrier opens — the operator's clock starts here
            "cycle_ended": None,      # sampling stops, vehicle released
            "updated": None,
        }

    # Which cameras to freeze when an attempt fails. CAM1 is the ROI/coal view
    # the AI judges on, CAM3 is the wide auger view — between them an operator
    # can tell an AI misread from genuinely hard material.
    ATTEMPT_SNAPSHOT_CAMS = ("CAM3", ) # "CAM1",

    def _snapshot_attempt(self, seq: int, reason: str | None) -> str | None:
        """
        Freeze the current camera view for a FAILED attempt.

        Copies the newest TEMP_IMG frames — the background camera threads
        rewrite those roughly once a second, so they are as current as a fresh
        capture would be, and copying costs no MQTT round trip (a single-capture
        request here would reintroduce the CAM1 command-drop stall).

        Returns the CAM1 path, which becomes the thumbnail behind that dot in
        the History sample map. Returns None rather than raising — a missing
        picture must never abort a sampling cycle.
        """
        try:
            out_dir = os.path.join(
                RESULT_IMG_PATH, self.date_file, str(self.uid), "ATTEMPTS"
            )
            os.makedirs(out_dir, exist_ok=True)

            stamp = datetime.now().strftime("%H%M%S")
            tag   = (reason or "FAILED").upper()
            first = None

            for cam in self.ATTEMPT_SNAPSHOT_CAMS:
                src = os.path.join(TEMP_IMG_PATH, f"{cam}_REDUCED.jpg")
                if not os.path.exists(src):
                    logger.warning(f"Attempt snapshot: {cam} frame missing ({src})")
                    continue

                dest = os.path.join(
                    out_dir, f"{self.uid}_a{seq}_{tag}_{cam}_{stamp}.jpg"
                )
                shutil.copy2(src, dest)
                if first is None:
                    first = normalize_path(dest)

            if first:
                logger.debug(f"Attempt #{seq} ({tag}) snapshot saved: {first}")
            else:
                logger.warning(f"Attempt #{seq} ({tag}): no camera frame available")
            return first

        except Exception:
            logger.error(f"{traceback.format_exc()}")
            return None

    def _freeze_ai_images(self) -> list:
        """
        Keep the CAM1 frames of the most recent AI check for the attempt that
        just failed (AI_OBSTRUCTION or HARD_MATERIAL) and record them as
        sample_info['attempts'][-1]['ai_images'] = [{check, raw, vis}].

          raw : CAM1_loopN_precycle.jpg      - the frame the model ran on
          vis : CAM1_loopN_precycle_vis.jpg  - same frame with the yellow ROI,
                                               detections and verdict drawn

        AI block      -> the LAST failed check (the one that decided the block)
        Hard material -> the check that passed before this cycle started, so
                         the mail shows what the model accepted as coal at the
                         spot where the auger then hit hard material

        Only one raw+vis pair is kept: with the CAM3 snapshot that makes the
        3 pictures per block that go into the alert and summary mails.

        Files are only taken if written by the current check (mtime after the
        check started, minus the TEMP frame age allowance), so a capture
        failure can't pass off older pictures as this one's.
        The next AI check overwrites INF/<date>/<uid>/CAM1_loopN_precycle*.jpg,
        which is why they are copied out here. Never raises.
        """
        try:
            attempts = (self._sample_info or {}).get("attempts") or []
            if not attempts:
                return []
            attempt = attempts[-1]
            reason  = attempt.get("reason")
            if reason not in ("AI_OBSTRUCTION", "HARD_MATERIAL"):
                return []
            seq = attempt.get("seq")

            inf_dir = os.path.join(INF_IMG, self.date_file, self.uid)
            out_dir = os.path.join(RESULT_IMG_PATH, self.date_file, str(self.uid), "ATTEMPTS")
            os.makedirs(out_dir, exist_ok=True)

            since = getattr(self, "_ai_check_started", 0) - (TEMP_IMG_MAX_AGE + 3)

            # Newest check first - the last one written is the one that decided
            chosen = None
            for n in range(AI_CONFIRM_RETRIES, 0, -1):
                raw = os.path.join(inf_dir, f"CAM1_loop{n}_precycle.jpg")
                vis = os.path.join(inf_dir, f"CAM1_loop{n}_precycle_vis.jpg")
                fresh = [p for p in (raw, vis)
                         if os.path.exists(p) and os.path.getmtime(p) >= since]
                if fresh:
                    chosen = (n, raw if raw in fresh else None, vis if vis in fresh else None)
                    break

            if not chosen:
                print(f"[MANAGER] {reason} #{seq}: no CAM1 check frames found to keep")
                logger.warning(f"{reason} #{seq}: no CAM1 check frames found in {inf_dir}")
                return []

            n, raw, vis = chosen
            entry = {"check": n}
            for kind, src in (("raw", raw), ("vis", vis)):
                if not src:
                    continue
                suffix = "_vis" if kind == "vis" else ""
                dest = os.path.join(out_dir, f"{self.uid}_a{seq}_{reason}_CAM1{suffix}.jpg")
                shutil.copy2(src, dest)
                entry[kind] = normalize_path(dest)

            attempt["ai_images"] = [entry]
            if self.uid:
                db_update_sample_info(self.uid, self._sample_info)
            print(f"[MANAGER] {reason} #{seq}: kept CAM1 frames of AI check {n}")
            logger.debug(f"{reason} #{seq} CAM1 frames: {entry}")
            return [entry]

        except Exception:
            logger.error(f"{traceback.format_exc()}")
            return []

    def _set_live_target(self, pos: dict | None):
        """
        Publish the point the auger is heading to, so the dashboard can blink
        it yellow before the outcome is known. Cleared by _record_attempt().
        """
        try:
            if self._sample_info is None:
                self._sample_info = self._new_sample_info()
            self._sample_info["current"] = dict(pos) if pos else None
            self._sample_info["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if self.uid:
                db_update_sample_info(self.uid, self._sample_info)
        except Exception:
            # Purely cosmetic bookkeeping — never let it break the cycle
            logger.error(f"{traceback.format_exc()}")

    def _mark_cycle_start(self):
        """
        Stamp the moment the barrier opens.

        Idempotent on purpose: safe to call on every tick of the OPEN_BARRIER
        state, and safe if the state is re-entered. First call wins, so a
        barrier that reopens doesn't restart the operator's clock.
        """
        try:
            if self._sample_info is None:
                self._sample_info = self._new_sample_info()
            if self._sample_info.get("cycle_started"):
                return                                  # already running
            self._sample_info["cycle_started"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._sample_info["updated"] = self._sample_info["cycle_started"]
            if self.uid:
                db_update_sample_info(self.uid, self._sample_info)
            logger.debug(f"Cycle clock started: {self._sample_info['cycle_started']}")
        except Exception:
            logger.error(f"{traceback.format_exc()}")

    def _mark_cycle_end(self):
        """
        Stamp the moment sampling stops. Idempotent — first call wins, so the
        recorded duration is when work actually finished, not when the last
        piece of cleanup happened to run.
        """
        try:
            if self._sample_info is None:
                self._sample_info = self._new_sample_info()
            if self._sample_info.get("cycle_ended"):
                return
            self._sample_info["cycle_ended"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._sample_info["updated"] = self._sample_info["cycle_ended"]
            self._sample_info["current"] = None          # nothing is being sampled now
            if self.uid:
                db_update_sample_info(self.uid, self._sample_info)
            logger.debug(f"Cycle clock stopped: {self._sample_info['cycle_ended']}")
        except Exception:
            logger.error(f"{traceback.format_exc()}")

    def _record_attempt(self, pos: dict | None, status: str, reason: str | None = None):
        """
        Log one sampling attempt — successful or failed — and persist it.

        Written immediately rather than at the end of the session so the data
        survives a crash, a reset, or an ERROR state mid-cycle.

        A failure caused by AI obstruction or hard material also registers the
        point on the avoid list, so every later draw — fresh point or retry —
        steers clear of ground that has already proved bad. Doing it here means
        both the AI path and the hard-material path get it without either
        having to remember to.
        """
        try:
            if self._sample_info is None:
                self._sample_info = self._new_sample_info()
            self._sample_info["uid"] = self.uid

            seq = len(self._sample_info["attempts"]) + 1

            # Failures (AI block, hard material, cycle error/timeout) get a
            # frozen camera view. Successes already have SAMPLE_n_IMG_PATH.
            image = None
            if status != "SUCCESS":
                image = self._snapshot_attempt(seq, reason)

            self._sample_info["attempts"].append({
                "seq":    seq,
                "cycle":  self.cycle,
                "area":   (pos or {}).get("area"),
                "x":      (pos or {}).get("x"),
                "y":      (pos or {}).get("y"),
                "status": status,                       # SUCCESS | FAILED
                "reason": reason,
                "image":  image,
                "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })

            if status == "SUCCESS": self._sample_info["successful"] += 1
            else:                   self._sample_info["failed"] += 1

            # The outcome is known, so the live blinking dot is retired here.
            self._sample_info["current"] = None
            self._sample_info["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if self.uid:
                db_update_sample_info(self.uid, self._sample_info)

            print(f"[MANAGER] Sample attempt logged: {status} "
                  f"{reason or ''} at {pos}")
            logger.debug(f"Sample attempt logged: {status} {reason or ''} at {pos}"
                         f"{' img=' + image if image else ''}")
        except Exception:
            # Never let bookkeeping break the cycle
            logger.error(f"{traceback.format_exc()}")

        # Remember blocked ground so later points give it a wide berth.
        # Kept outside the try above so a bookkeeping failure can't silently
        # lose the avoidance, and vice versa.
        try:
            if status == "FAILED" and reason in BLOCKING_REASONS and pos:
                if not hasattr(self, "_avoid_points") or self._avoid_points is None:
                    self._avoid_points = []
                self._avoid_points.append({
                    "x":      pos.get("x"),
                    "y":      pos.get("y"),
                    "area":   pos.get("area"),
                    "reason": reason,
                })
                print(f"[MANAGER] Avoid list now has {len(self._avoid_points)} blocked point(s)")
                logger.debug(f"Avoid list: {self._avoid_points}")
        except Exception:
            logger.error(f"{traceback.format_exc()}")

    def _avoid(self) -> list:
        """Blocked points for this session, safe even before __init__ ran."""
        pts = getattr(self, "_avoid_points", None)
        if pts is None:
            pts = self._avoid_points = []
        return pts
 
 
# ─────────────────────────────────────────────────────────────────────────────
# AI CONFIRMATION
# ─────────────────────────────────────────────────────────────────────────────
 
    def _confirm_position_before_start(self, target_area: int) -> bool:
        """
        Run the AI auger-position check before giving cycle start.
 
        Retries AI_CONFIRM_RETRIES times at the SAME X/Y, capturing FRESH
        frames each time — re-running the model on the identical image file
        returns the identical answer, so a retry is only meaningful with a new
        capture (it also rides out motion blur or a passing shadow).
        """
        for attempt in range(1, AI_CONFIRM_RETRIES + 1):
            print(f"[MANAGER] AI position check {attempt}/{AI_CONFIRM_RETRIES} (area {target_area})")
            logger.debug(f"AI position check {attempt}/{AI_CONFIRM_RETRIES} (area {target_area})")
 
            images = self._capture_images_for_confirmation(attempt, "precycle")
            if not images:
                print(f"[MANAGER] AI check {attempt}: image capture failed")
                logger.warning(f"AI check {attempt}: image capture failed")
            else:
                try:
                    result = confirm_auger_position(images[0], images[1], target_area)
                except Exception as e:
                    print(f"[MANAGER] AI check {attempt} error: {e}")
                    logger.error(f"{traceback.format_exc()}")
                    result = False
 
                if result:
                    print(f"[MANAGER] AI POSITION CONFIRMED on attempt {attempt}")
                    logger.debug(f"AI position confirmed on attempt {attempt}")
                    return True
 
                print(f"[MANAGER] AI check {attempt} FAILED")
                logger.warning(f"AI check {attempt} failed (area {target_area})")
 
            if attempt < AI_CONFIRM_RETRIES:
                time.sleep(AI_CONFIRM_DELAY)
 
        print(f"[MANAGER] AI position NOT confirmed after {AI_CONFIRM_RETRIES} attempts")
        logger.warning(f"AI position not confirmed after {AI_CONFIRM_RETRIES} attempts")
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
        self._mark_cycle_start()

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
        self._session_pdf_url = pdf_url or ""   # reused by the inspection-complete mail
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

        # A blocked point (AI obstruction / hard material) has already had its
        # replacement computed by get_alternate_position() — that point sits in
        # the opposite half of the bed, clear of every spot known to be bad.
        is_hard_retry = self._hard_retry_pos is not None
        if is_hard_retry:
            self.positions.append(self._hard_retry_pos)
            self._hard_retry_pos = None
            print(f"[MANAGER] BLOCKED-POINT RETRY — using {self.positions[-1]}")
            logger.debug(f"Blocked-point retry — using {self.positions[-1]}")
        else:
            # Fresh point for the next area, kept clear of anything blocked
            areas = [p["area"] for p in self.positions]
            avoid = self._avoid()
            target_area = get_sample_positions(used_areas=areas,
                                               prev_points=self.positions,
                                               avoid=avoid)
            if target_area is not None:
                self.positions.append(target_area)
            else:
                self.positions.append(get_sample_positions([], [], avoid))

        pos = self.positions[self._current_sample_index]

        # NEW PROCESS: cycles 2 & 3 travel DIRECTLY from the previous position
        # (no homing in between). Cycle 1 — or any retry after an error /
        # timeout — goes via home so the auger starts from a known reference.
        # A hard-material retry also goes DIRECT (even on cycle 1), because the
        # Z cycle finished normally so the tracked position is still trusted.
        # An AI-blocked retry sets _force_home_next, so it homes first.
        direct = ((self._successful_cycles > 0) or is_hard_retry) and (not self._force_home_next)
        self._force_home_next = False

        self._set_live_target(self.positions[-1] if self.positions else None)
        self._sampler(action="sample_cycle", x=pos["x"], y=pos["y"], direct=direct)
        print(f"[MANAGER] Sampling positions: {self.positions}")
        print(f"[MANAGER] Moving to sampling position: {pos} (direct={direct})")
        logger.debug(f"Moving to sampling position: {pos} (direct={direct})")
        self._goto(State.CYCLE_CONFIRM)

    def _handle_ai_blocked_wait(self):
        """
        The AI could not confirm the auger position. The UI shows a popup with
        two choices; the web app writes the answer into PLC_COMM.AI_DECISION.

          CONTINUE -> move to a different spot in this area and carry on
          MANUAL   -> operator collects the sample by hand; the session is
                      closed out and marked COMPLETED

        On CONTINUE the replacement point is computed HERE rather than being
        left to the fresh-point path, which would regenerate inside the same
        area and could land a few percent from the spot the AI just rejected.

        Emergency still takes priority, and an unanswered popup falls through
        to CONTINUE after AI_DECISION_TIMEOUT so a truck is never stranded.
        """
        # Emergency must still win while we are waiting
        emg = self._pop("plc_sampler/status")
        if emg and emg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop while waiting for AI decision")
            logger.warning("Emergency stop while waiting for AI decision")
            self._flush_topic("plc_sampler/status")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        # Operator decision comes from the DB, not MQTT
        decision = db_read_decision(self.uid)
        if decision:
            print(f"[MANAGER] Operator decision received: {decision}")
            logger.debug(f"Operator decision received: {decision}")
            db_clear_decision(self.uid)          # consume it exactly once

        if decision == "MANUAL":
            print("[MANAGER] Operator chose MANUAL collection — closing session as COMPLETED")
            logger.warning("Operator chose MANUAL collection — closing session as COMPLETED")

            self._sample_info["collection_mode"] = "MANUAL"
            self._sample_info["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db_update_sample_info(self.uid, self._sample_info)

            db_update_plc_comm(self.uid, State.COMPLETE_FINAL.name)
            self._barrier(action="green_signal")
            time.sleep(1)
            self._sampler(action="sample_cycle_stop")
            self._goto(State.COMPLETE_FINAL)
            return

        if decision == "CONTINUE" or self._elapsed() > AI_DECISION_TIMEOUT:
            if decision == "CONTINUE":
                print("[MANAGER] Operator chose CONTINUE — moving away from the blocked spot")
                logger.warning("Operator chose CONTINUE — moving away from the blocked spot")
            else:
                print("[MANAGER] No operator decision — continuing away from the blocked spot")
                logger.warning("No operator decision within timeout — continuing automatically")

            if self._ai_blocked_pos:
                self._hard_retry_pos = get_alternate_position(
                    self._ai_blocked_pos,
                    prev_points=self.positions,
                    avoid=self._avoid(),
                )
                print(f"[MANAGER] New point clear of the obstruction: {self._hard_retry_pos}")
                logger.debug(f"New point clear of the obstruction: {self._hard_retry_pos}")

            self._ai_blocked_pos = None
            self._force_home_next = True      # recover from a known reference
            db_update_plc_comm(self.uid, State.CYCLE_POSITION.name)
            self._goto(State.CYCLE_POSITION)
            return

        print("[MANAGER] Waiting for operator decision on AI block ")
        time.sleep(2)

    def _handle_cycle_confirm(self):
        msg = self._pop("plc_sampler/status")
        print(f"[MANAGER] Cycle Waiting for position confirmation")
        time.sleep(1)
 
        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            self._flush_topic("plc_sampler/status")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
 
        if msg and msg.get("status") == "position_set":
            print("[MANAGER] Auger positioned — running AI confirmation ")
 
            pos = self.positions[self._current_sample_index]
 
            confirmed = True
            if AI_CONFIRM_ENABLED and self.ai_model:
                # Lets _freeze_ai_images() tell THIS check's CAM1 frames from
                # the previous check's files of the same name.
                self._ai_check_started = time.time()
                confirmed = self._confirm_position_before_start(pos["area"])
 
            if not confirmed:
                # ── AI could not see the auger / coal clearly ────────────────
                self._record_attempt(pos, "FAILED", "AI_OBSTRUCTION")
                # Keep the CAM1 frames the model judged - the next AI check
                # overwrites INF/<date>/<uid>/CAM1_loopN_precycle*.jpg. They
                # go into the end-of-session summary mail with the CAM3 snapshot.
                self._freeze_ai_images()
                self._ai_blocked_pos = self.positions.pop()   # discard this point
                self._ai_block_count += 1

                # Mail the block with the frozen camera frames of the failed
                # cycles. Fire-and-forget on its own thread — never blocks.
                try:
                    on_cycle_blocked(
                        uid=self.uid,
                        vehicle=self.vehicle,
                        reason="AI_OBSTRUCTION",
                        position=self._ai_blocked_pos,
                        attempt_no=self._ai_block_count,
                        waiting_for_operator=True,
                        sample_info=self._sample_info,
                        rfids=self.rfids,
                        bucket_no=self.bucket_no,
                    )
                except Exception:
                    logger.error(f"{traceback.format_exc()}")
 
                print("[MANAGER] AI BLOCKED — waiting for operator decision")
                logger.warning(f"AI blocked at {self._ai_blocked_pos} — waiting for operator decision")
 
                # self._flush_topic(TOPIC_DECISION)             # ignore stale choices
                db_clear_decision(self.uid)                   # ignore stale choices
                db_update_plc_comm(self.uid, State.AI_BLOCKED_WAIT.name)
                self._goto(State.AI_BLOCKED_WAIT)
                return
 
            # ── Cycle start is only given past this point ────────────────────
            cycle_num = self._successful_cycles + 1
            self.cycle = cycle_num
            self._sampler(action="start_cycle", cycle=cycle_num)
            self._start_sample_capture(cycle_num)
            print("[MANAGER] CYCLE START GIVEN !!!")
            logger.debug("Position set, cycle start given")
            time.sleep(1)
            self._goto(State.CYCLE_CAPTURE)
            return
 
        if self._elapsed() > POSITION_CONFIRMATION_TIMEOUT:
            print("[MANAGER] Position confirmation timeout — retrying with different position")
            logger.debug("Position confirmation timeout")
            bad = self.positions.pop()
            self._record_attempt(bad, "FAILED", "POSITION_TIMEOUT")
            self._force_home_next = True
            time.sleep(1)
            self._goto(State.CYCLE_POSITION)

    def _handle_cycle_capture(self):
        msg = self._pop("plc_sampler/status")
        time.sleep(1)

        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            db_clear_decision(self.uid)  # drop stale queued messages
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

    def _handle_hard_blocked_wait(self):
        """
        Hard material was hit twice in a row. The UI shows a popup telling the
        operator to collect manually; the web app writes the acknowledgement
        into PLC_COMM.AI_DECISION.

        On MANUAL (the only offered choice) the session is closed out and
        marked COMPLETED, exactly like the AI-blocked manual path.

        If nobody answers within HARD_DECISION_TIMEOUT the manager defaults to
        MANUAL rather than resuming: continuing would drive the auger back into
        material the PLC has already reported as hard, twice.
        """
        # Emergency still wins while waiting
        emg = self._pop("plc_sampler/status")
        if emg and emg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop while waiting for hard-material decision")
            logger.warning("Emergency stop while waiting for hard-material decision")
            self._flush_topic("plc_sampler/status")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return

        # Operator decision comes from the DB, not MQTT
        decision = db_read_decision(self.uid)
        if decision:
            print(f"[MANAGER] Hard-material decision received: {decision}")
            logger.debug(f"Hard-material decision received: {decision}")
            db_clear_decision(self.uid)          # consume it exactly once

        timed_out = self._elapsed() > HARD_DECISION_TIMEOUT

        if decision == "CONTINUE":
            # The popup does not offer this, but an operator with dashboard
            # access could still send it deliberately. The avoid list is kept
            # intact so the fresh point still steers clear of the hard spots.
            print("[MANAGER] Operator chose CONTINUE after hard material — new position")
            logger.warning("Operator chose CONTINUE after hard material — new position")
            self._consecutive_hard = 0
            self._hard_retries     = 0
            self._hard_retry_pos   = None
            self._force_home_next  = True
            db_update_plc_comm(self.uid, State.CYCLE_POSITION.name)
            self._goto(State.CYCLE_POSITION)
            return

        if decision == "MANUAL" or timed_out:
            if timed_out:
                print("[MANAGER] No operator response — defaulting to MANUAL collection")
                logger.warning("No operator response to hard-material popup — defaulting to MANUAL")
            else:
                print("[MANAGER] Operator acknowledged — MANUAL collection, marking COMPLETED")
                logger.warning("Operator acknowledged hard material — MANUAL collection")

            self._sample_info["collection_mode"] = "MANUAL"
            self._sample_info["manual_reason"]   = "HARD_MATERIAL"
            self._sample_info["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db_update_sample_info(self.uid, self._sample_info)

            db_update_plc_comm(self.uid, State.COMPLETE_FINAL.name)
            self._barrier(action="green_signal")
            time.sleep(1)
            self._sampler(action="sample_cycle_stop")
            self._goto(State.COMPLETE_FINAL)
            return

        print("[MANAGER] Waiting for operator acknowledgement (hard material) ")
        time.sleep(2)

    def _handle_cycle_done(self):
        time.sleep(1)
 
        msg = self._pop("plc_sampler/status")
        if msg and msg.get("status") == "emergency_stop":
            print("[MANAGER] Emergency stop detected waiting until reset !")
            logger.warning(f"Emergency stop detected in {self.state.name} — waiting until reset")
            self._flush_topic("plc_sampler/status")
            self._emergency_return_state = State.AUGER_HOME_POS
            db_update_plc_comm(self.uid, self.state.name, emergency="ACTIVE")
            self._goto(State.CYCLE_EMERGENCY_WAIT)
            return
 
        if msg and msg.get("status", "") == "sample_cycle_complete":
            print(f"[MANAGER] Cycle {self.cycle} completed (Z_UP FB 1->0->1)")
            logger.debug(f"Cycle {self.cycle} completed (Z_UP FB 1->0->1)")
 
            self._record_attempt(self.positions[self._current_sample_index], "SUCCESS")
 
            self._successful_cycles += 1
            self._current_sample_index += 1
            self._hard_retries     = 0
            self._consecutive_hard = 0     # a good sample breaks the streak
 
            if self._successful_cycles >= TOTAL_CYCLES:
                self._barrier(action="green_signal")
                time.sleep(1)
                print(f"[MANAGER] All {TOTAL_CYCLES} successful cycles completed.")
                logger.debug(f"All {TOTAL_CYCLES} successful cycles completed.")
                self._goto(State.SAMPLE_COLLECTION)
            else:
                print(f"[MANAGER] Moving DIRECTLY to next sampling position (no homing) ")
                self._goto(State.CYCLE_POSITION)
            return
        
        elif msg and msg.get("status", "") == "hard_material_detected":
            self._hard_retries     += 1
            self._consecutive_hard += 1
 
            bad = self.positions.pop()
            self._record_attempt(bad, "FAILED", "HARD_MATERIAL")
            # CAM1 frames of the AI check that cleared this point - mailed
            # with the hard-material alert next to the CAM3 snapshot.
            self._freeze_ai_images()
 
            print(f"[MANAGER] HARD MATERIAL at {bad} — cycle {self.cycle} NOT counted "
                  f"(consecutive {self._consecutive_hard}/{HARD_MATERIAL_CONSECUTIVE_LIMIT})")
            logger.warning(f"Hard material at {bad} — consecutive "
                           f"{self._consecutive_hard}/{HARD_MATERIAL_CONSECUTIVE_LIMIT}")

            # Mail the hard-material hit with the frozen camera frames.
            try:
                on_cycle_blocked(
                    uid=self.uid,
                    vehicle=self.vehicle,
                    reason="HARD_MATERIAL",
                    position=bad,
                    attempt_no=self._hard_retries,
                    consecutive=self._consecutive_hard,
                    waiting_for_operator=(
                        self._consecutive_hard >= HARD_MATERIAL_CONSECUTIVE_LIMIT
                    ),
                    sample_info=self._sample_info,
                    rfids=self.rfids,
                    bucket_no=self.bucket_no,
                )
            except Exception:
                logger.error(f"{traceback.format_exc()}")
 
            # ── Two in a row: stop probing, hand over to the operator ────────
            if self._consecutive_hard >= HARD_MATERIAL_CONSECUTIVE_LIMIT:
                print("[MANAGER] HARD MATERIAL TWICE IN A ROW — waiting for operator")
                logger.warning("Hard material twice in a row — waiting for operator decision")
 
                db_clear_decision(self.uid)            # ignore stale choices
                db_update_plc_comm(self.uid, State.HARD_BLOCKED_WAIT.name)
                self._goto(State.HARD_BLOCKED_WAIT)
                return
 
            # ── First hit: retry in a DIFFERENT direction, not next door ─────
            # `bad` is already on the avoid list (_record_attempt put it there),
            # so the new point clears it and every earlier hard/AI spot.
            self._hard_retry_pos = get_alternate_position(
                bad,
                prev_points=self.positions,
                avoid=self._avoid(),
            )
            print(f"[MANAGER] Retrying away from the hard spot at {self._hard_retry_pos}")
            logger.debug(f"Retrying away from the hard spot at {self._hard_retry_pos}")
 
            self._goto(State.CYCLE_POSITION)
            return
 
        elif msg and msg.get("status", "") == "cycle_error":
            print(f"[MANAGER] Cycle error: {msg.get('msg')}")
            print("[MANAGER] Retrying with different position ")
            bad = self.positions.pop()
            self._record_attempt(bad, "FAILED", "CYCLE_ERROR")
            self._force_home_next = True
            self._goto(State.CYCLE_POSITION)
            return
 
        if self._elapsed() > SAMPLE_CYCLE_TIMEOUT:
            print("[MANAGER] Cycle timeout — moving to next position")
            bad = self.positions.pop()
            self._record_attempt(bad, "FAILED", "CYCLE_TIMEOUT")
            self._force_home_next = True
            self._goto(State.CYCLE_POSITION)
        else:
            print("[MANAGER] Cycle in progress — waiting ")
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
            self._hard_retry_pos = None
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

        # ── Inspection finished: mail the report PDF + vehicle info ──────────
        # Threaded and deduped per UID inside the hook; a mail failure here
        # cannot stop the session being closed out below.
        try:
            self._mark_cycle_end()          # so the mail carries the real end time
            on_inspection_complete(
                uid=self.uid,
                vehicle=self.vehicle,
                rfids=self.rfids,
                bucket_no=self.bucket_no,
                report_pdf_path=self.paths.get("REPORT_PATH"),
                pdf_url=getattr(self, "_session_pdf_url", "") or None,
                sample_info=self._sample_info,
            )
        except Exception:
            logger.error(f"{traceback.format_exc()}")

        db_complete_log(self.uid)
 
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
        self._mark_cycle_end()

        # A session that ends on an ERROR still gets its one summary mail if
        # it had a vendor mismatch / AI block / hard material. Completed
        # sessions were already mailed from _handle_complete (deduped per UID).
        if msg != "":
            try:
                on_session_closed(
                    uid=self.uid,
                    vehicle=self.vehicle,
                    rfids=self.rfids,
                    bucket_no=self.bucket_no,
                    sample_info=self._sample_info,
                    error_message=msg,
                )
            except Exception:
                logger.error(f"{traceback.format_exc()}")
        
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
        self._sample_info    = self._new_sample_info()
        self._ai_blocked_pos = None
        self._ai_block_count = 0
        self._hard_retry_pos   = None
        self._hard_retries     = 0
        self._consecutive_hard = 0     # a good sample breaks the streak
        self._avoid_points     = []    # blocked ground is per-vehicle, not global
        self._stop_sample_capture()
        self._capture_stop   = None
        self._capture_thread = None
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