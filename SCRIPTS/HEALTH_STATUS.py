import os
import csv
import time
import pymysql
import subprocess
import platform
from datetime import datetime, timedelta

DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "insightzz@123",
    "database": "COAL_SAMPLING_DHAR",
    "cursorclass": pymysql.cursors.DictCursor
}

BASE_DIR = "health_logs"
PING_TIMEOUT = 3

def get_connection():
    return pymysql.connect(**DB_CONFIG)

def ping(ip):
    if not ip:
        return False

    param = "-n" if platform.system().lower() == "windows" else "-c"
    timeout_param = "-w" if platform.system().lower() == "windows" else "-W"

    try:
        result = subprocess.run(
            ["ping", param, "1", timeout_param, str(PING_TIMEOUT), ip],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except Exception:
        return False

def get_csv_path(now):
    return os.path.join(BASE_DIR, now.strftime("%Y-%m-%d") + ".csv")


def write_header_if_needed(file_path):
    if not os.path.exists(file_path):
        with open(file_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "ip", "location", "device_type", "status"])

def fetch_devices(conn):
    with conn.cursor() as cursor:
        cursor.execute("SELECT ID, IP, LOCATION, DEVICE_TYPE FROM HEALTH_STATUS")
        return cursor.fetchall()

def update_device(conn, device_id, status):
    with conn.cursor() as cursor:
        cursor.execute(
            "UPDATE HEALTH_STATUS SET STATUS=%s, LAST_PING=NOW() WHERE ID=%s",
            (status, device_id)
        )
    conn.commit()

def log_devices(conn, now):
    file_path = get_csv_path(now)
    write_header_if_needed(file_path)

    timestamp = now.strftime("%Y-%m-%d %H:%M:%S")

    devices = fetch_devices(conn)
    rows = []

    for d in devices:
        is_online = ping(d["IP"])
        status = "ONLINE" if is_online else "OFFLINE"

        update_device(conn, d["ID"], status)

        rows.append([
            timestamp,
            d["IP"],
            d["LOCATION"],
            d["DEVICE_TYPE"],
            status
        ])

    with open(file_path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

def cleanup_old_files(now):
    if not os.path.exists(BASE_DIR):
        return

    for file in os.listdir(BASE_DIR):
        if not file.endswith(".csv"):
            continue

        try:
            file_date = datetime.strptime(file.replace(".csv", ""), "%Y-%m-%d")
            if (now - file_date).days > 3:
                os.remove(os.path.join(BASE_DIR, file))
                print(f"[CLEANUP] Deleted {file}")
        except Exception:
            continue

def calculate_uptime(date_str):
    file_path = os.path.join(BASE_DIR, f"{date_str}.csv")

    if not os.path.exists(file_path):
        print(f"[WARN] No file for {date_str}")
        return []

    stats = {}

    with open(file_path, "r") as f:
        reader = csv.DictReader(f)

        for row in reader:
            ip = row["ip"]
            status = row["status"]

            if ip not in stats:
                stats[ip] = {"total": 0, "online": 0}

            stats[ip]["total"] += 1
            if status == "ONLINE":
                stats[ip]["online"] += 1

    report = []
    for ip, s in stats.items():
        total = s["total"]
        online = s["online"]

        uptime_min = (online / total * 1440) if total > 0 else 0

        report.append({
            "ip": ip,
            "uptime_minutes": round(uptime_min, 2),
            "uptime_percent": round((online / total * 100), 2) if total > 0 else 0
        })

    return report

def main():
    print("[START] Health monitor running...")
    os.makedirs(BASE_DIR, exist_ok=True)

    last_report_date = None

    while True:
        start = time.time()
        now = datetime.now()

        conn = get_connection()

        try:
            log_devices(conn, now)
            cleanup_old_files(now)

            # Run report at 00:15
            if now.hour == 0 and now.minute == 15:
                if last_report_date != now.date():
                    target_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                    print(f"[REPORT] {target_date}")

                    report = calculate_uptime(target_date)
                    for r in report:
                        print(r)

                    last_report_date = now.date()

        finally:
            conn.close()

        elapsed = time.time() - start
        time.sleep(max(0, 10 - elapsed))


if __name__ == "__main__":
    main()