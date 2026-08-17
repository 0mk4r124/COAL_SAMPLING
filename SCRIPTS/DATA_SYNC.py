import csv
import os
import time
import base64
import pymysql
import requests
import threading

from datetime import datetime, timedelta

from DEPENDANT.TRAINING_SYNC import run_training_sync

# ================= CONFIG =================
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "insightzz@123",
    "database": "COAL_SAMPLING_DHAR"
}

DATA_API_URL = "https://www.insightzz-analytics.com/Ultratech//DharCoalAjaxReqController?apiName=syncDharCoalDataTable"
UPTIME_API_URL = "https://www.insightzz-analytics.com/Ultratech//DharCoalAjaxReqController?apiName=syncUptimeHealthStatus"
BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')

SYNC_INTERVAL = 600  # 10 minutes
BATCH_SIZE = 5
SYNC_TRAINING_DATA = True
# ==========================================


# -------- DB CONNECTION --------
def get_db():
    return pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)


# -------- FETCH DATA (MATCHES DJANGO LOGIC) --------
def fetch_unsynced_rows(limit=BATCH_SIZE):
    # rows = [{
    #     "uid": "uid1223",
    #     "rfids": "rfids3434",
    #     "vehicle_number": "vehicle_number32342",
    #     "create_time": "create_time234234",
    #     "vendor_name": "vendor_name234234",
    #     "vendor_code": "vendor_code234234",

    #     "vehicle_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
    #     "sample_1_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
    #     "sample_2_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
    #     "sample_3_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
    #     "report_path": "/home/omkar/Downloads/Project_Timeline_MOM.pdf",
    # }]
    db = get_db()
    cur = db.cursor()

    query = f"""
    SELECT vl.*,
           vm.vehicle_number,
           vm.vendor_code,
           vdm.vender_name
    FROM VEHICLE_LOGS vl
    LEFT JOIN VEHICLE_MASTER vm 
        ON vm.rfid = vl.rfids
    LEFT JOIN VENDOR_MASTER vdm 
        ON vm.vendor_code = vdm.vendor_code
    WHERE vl.status = 'COMPLETED'
      AND (vl.is_synced = 0 OR vl.is_synced IS NULL)
    ORDER BY vl.create_time ASC
    LIMIT {limit}
    """

    cur.execute(query)
    rows = cur.fetchall()

    cur.close()
    db.close()

    return rows

# -------- UPDATE STATUS --------
def mark_synced(record_id):
    db = get_db()
    cur = db.cursor()

    cur.execute(
        "UPDATE VEHICLE_LOGS SET is_synced = 1 WHERE UID = %s",
        (record_id,)
    )

    db.commit()
    cur.close()
    db.close()


# -------- FILE ENCODING --------
def encode_file(relative_path):
    if not relative_path:
        return None

    # if "pdf" in relative_path: relative_path = relative_path.split('.pdf')[0] + "_compressed.pdf"
    abs_path = os.path.join(relative_path)

    if not os.path.exists(abs_path):
        print(f"[WARN] Missing file: {abs_path}")
        return None

    try:
        with open(abs_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"[ERROR] Failed reading: {abs_path}, {e}")
        return None

def to_minutes(s):
    if s["total"] == 0:
        return 0
    return round((s["online"] / s["total"]) * 1440)

# -------- BUILD PAYLOAD --------
def build_payload(row):
    return {
        "data": {
            "UID": row["UID"],
            "RFIDS": row["RFIDS"],
            "VEHICLE_NUM": row.get("vehicle_number"),
            "DT_STAMP": str(row.get("CREATE_TIME")),
            "VENDOR_NAME": row.get("vender_name"),
            "VENDOR_CODE": row.get("vendor_code"),

            "REPORT_PATH": encode_file(row.get("REPORT_PATH"))
        }
    }

def build_payload_uptime():
    # previous day
    target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    file_path = os.path.join(BASE_FILE_PATH, "HEALTH_LOGS", f"{target_date}.csv")

    if not os.path.exists(file_path):
        print(f"[UPTIME] No CSV found for {target_date}")
        return None

    stats = {}

    # aggregate per device_type
    with open(file_path, "r") as f:
        reader = csv.DictReader(f)

        for row in reader:
            dtype = (row.get("device_type") or "").upper()
            status = row.get("status")

            if dtype not in stats:
                stats[dtype] = {"total": 0, "online": 0}

            stats[dtype]["total"] += 1
            if status == "ONLINE":
                stats[dtype]["online"] += 1

    payload = {
        "conveyorList": [{
            "ID": int(time.time()),  # epoch
            "CAMERA1_UPTIME": to_minutes(stats.get("CAMERA", {"total": 0, "online": 0})),
            "PLC_UPTIME": to_minutes(stats.get("PLC", {"total": 0, "online": 0})),
            "SERVER_UPTIME": to_minutes(stats.get("SERVER", {"total": 0, "online": 0})),
            "INTERNET_UPTIME": to_minutes(stats.get("INTERNET", {"total": 0, "online": 0})),
            "CURRENT_DATETTIME": (datetime.now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
        }]
    }

    return payload

# -------- API CALL --------
def send_to_server(payload, api="data"):
    try:
        if api == "data": res = requests.post(DATA_API_URL, json=payload)
        else: res = requests.post(UPTIME_API_URL, json=payload)

        if res.status_code != 200:
            print(f"[SYNC] HTTP Error: {res.status_code}")
            return False

        data = res.json()

        return data.get("status") == "success" or data.get("synced") is True

    except Exception as e:
        print(f"[SYNC] Exception: {e}")
        return False


# -------- MAIN LOOP --------
def run_sync():
    print("[SYNC] Started background sync service...")

    while True:
        try:
            rows = fetch_unsynced_rows()

            if not rows:
                print("[SYNC] No pending records")
            else:
                print(f"[SYNC] Processing {len(rows)} records")

                for row in rows:
                    payload = build_payload(row)

                    success = send_to_server(payload)

                    if success:
                        mark_synced(row["UID"])
                        print(f"[SYNC] Success UID: {row['UID']}")
                    else:
                        print(f"[SYNC] Failed UID: {row['UID']}")

        except Exception as e:
            print(f"[SYNC] Loop error: {e}")

        time.sleep(SYNC_INTERVAL)

# -------- MAIN LOOP --------
def run_uptime_sync():
    print("[SYNC] Uptime scheduler started...")

    last_run_date = None

    while True:
        now = datetime.now()

        if now.hour == 0 and now.minute == 20:
            if last_run_date != now.date():
                print("[SYNC] Running uptime sync...")

                payload = build_payload_uptime()

                if payload:
                    success = send_to_server(payload, api="uptime")

                    if success:
                        print("[SYNC] Uptime sent successfully")
                    else:
                        print("[SYNC] Uptime send failed")

                last_run_date = now.date()

        time.sleep(30)

if __name__ == "__main__":
    threads = []

    # t1 = threading.Thread(target=run_uptime_sync, name="UptimeSyncThread", daemon=True)
    # t2 = threading.Thread(target=run_sync, name="SyncThread", daemon=True)
    # threads += [t1, t2]

    # Training data sync — only when the flag above is on
    if SYNC_TRAINING_DATA:
        t3 = threading.Thread(target=run_training_sync, name="TrainingSyncThread", daemon=True)
        threads.append(t3)
        print("[SYNC] Training data sync ENABLED")
    else:
        print("[SYNC] Training data sync DISABLED (SYNC_TRAINING_DATA = False)")

    for t in threads:
        t.start()

    # Keep the main thread alive
    for t in threads:
        t.join()