import os
import time
import base64
import pymysql
import requests

from datetime import datetime

# ================= CONFIG =================
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "root",
    "database": "your_db"
}

DATA_API_URL = "http://192.168.1.58:8080/UltratechStagingV1_21_5_3_(1)/DharCoalAjaxReqController?apiName=syncDharCoalDataTable"
UPTIME_API_URL = "http://192.168.1.58:8080/UltratechStagingV1_21_5_3_(1)/DharCoalAjaxReqController?apiName=syncUptimeHealthStatus"
BASE_FILE_PATH = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/"

SYNC_INTERVAL = 600  # 10 minutes
BATCH_SIZE = 5
# ==========================================


# -------- DB CONNECTION --------
def get_db():
    return pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)


# -------- FETCH DATA (MATCHES DJANGO LOGIC) --------
def fetch_unsynced_rows(limit=BATCH_SIZE):
    rows = [{
        "uid": "uid1223",
        "rfids": "rfids3434",
        "vehicle_number": "vehicle_number32342",
        "create_time": "create_time234234",
        "vendor_name": "vendor_name234234",
        "vendor_code": "vendor_code234234",

        "vehicle_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
        "sample_1_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
        "sample_2_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
        "sample_3_img_path": "/home/omkar/Documents/logout_3_20250702_103147.jpg",
        "report_path": "/home/omkar/Downloads/Project_Timeline_MOM.pdf",
    }]
    # db = get_db()
    # cur = db.cursor()

    # query = f"""
    # SELECT vl.*,
    #        vm.vehicle_number,
    #        vm.vendor_code,
    #        vdm.vendor_name
    # FROM VEHICLE_LOGS vl
    # LEFT JOIN VEHICLE_MASTER vm 
    #     ON FIND_IN_SET(vm.rfid, REPLACE(vl.rfids, '|', ',')) > 0
    # LEFT JOIN VENDOR_MASTER vdm 
    #     ON vm.vendor_code = vdm.vendor_code
    # WHERE vl.status = 'COMPLETED'
    #   AND (vl.is_synced = 0 OR vl.is_synced IS NULL)
    # ORDER BY vl.create_time ASC
    # LIMIT {limit}
    # """

    # cur.execute(query)
    # rows = cur.fetchall()

    # cur.close()
    # db.close()
    return rows


# -------- UPDATE STATUS --------
def mark_synced(record_id):
    db = get_db()
    cur = db.cursor()

    cur.execute(
        "UPDATE VEHICLE_LOGS SET is_synced = 1 WHERE id = %s",
        (record_id,)
    )

    db.commit()
    cur.close()
    db.close()


# -------- FILE ENCODING --------
def encode_file(relative_path):
    if not relative_path:
        return None

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


# -------- BUILD PAYLOAD --------
def build_payload(row):
    return {
        "data": {
            "UID": row["uid"],
            "RFIDS": row["rfids"],
            "VEHICLE_NUM": row.get("vehicle_number"),
            "DT_STAMP": str(datetime.now()),
            "VENDOR_NAME": row.get("vendor_name"),
            "VENDOR_CODE": row.get("vendor_code"),

            # "VEHICLE_IMG": encode_file(row.get("vehicle_img_path")),
            # "SAMPLE_IMG_1": encode_file(row.get("sample_1_img_path")),
            # "SAMPLE_IMG_2": encode_file(row.get("sample_2_img_path")),
            # "SAMPLE_IMG_3": encode_file(row.get("sample_3_img_path")),
            "REPORT_PATH": encode_file(row.get("report_path"))
        }
    }

def build_payload_uptime():
    return {
        "conveyorList": [{
            "ID": 1245,
            "CAMERA1_UPTIME": 1440,
            "PLC_UPTIME": 1440,
            "SERVER_UPTIME": 1440,
            "INTERNET_UPTIME": 1440,
            "CURRENT_DATETTIME": str(datetime.now()),
        }]
    }

# -------- API CALL --------
def send_to_server(payload):
    try:
        res = requests.post(UPTIME_API_URL, json=payload)

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
                        mark_synced(row["id"])
                        print(f"[SYNC] Success UID: {row['uid']}")
                    else:
                        print(f"[SYNC] Failed UID: {row['uid']}")

        except Exception as e:
            print(f"[SYNC] Loop error: {e}")

        time.sleep(SYNC_INTERVAL)

# -------- MAIN LOOP --------
def run_uptime_sync():
    print("[SYNC] Started background sync service...")

    while True:
        try:

            print(f"[SYNC] Processing uptime sync")

            payload = build_payload_uptime()

            success = send_to_server(payload)

            if success:
                print(f"[SYNC] Success !!")
            else:
                print(f"[SYNC] Failed !!")

        except Exception as e:
            print(f"[SYNC] Loop error: {e}")

        time.sleep(SYNC_INTERVAL)

if __name__ == "__main__":
    run_uptime_sync()