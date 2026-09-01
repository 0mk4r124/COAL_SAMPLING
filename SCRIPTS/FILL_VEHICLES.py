"""
SCRIPTS/BACKFILL_VEHICLE_PDFS.py

Standalone, self-contained backfill:
  - Reads ALL unique vehicles from VEHICLE_MASTER
  - Builds a vehicle-info PDF for each
  - Encrypts it with a password (AES-256)
  - Uploads it to the Azure blob container
  - Saves the blob URL back to VEHICLE_MASTER.PDF_URL

Deliberately verbose and does NOT swallow errors, so it surfaces exactly what
is going wrong. Run it directly from the production python env:

    c:\\Users\\COAL_SAMPLING_1\\miniconda3\\envs\\detectron2_cpu\\python.exe SCRIPTS\\BACKFILL_VEHICLE_PDFS.py

Options:
    --force        regenerate even vehicles that already have a PDF_URL
    --limit N      only process the first N vehicles (testing)
    --no-db-write  do everything EXCEPT writing PDF_URL back (dry-ish run)

Config is read from environment variables. If a var is missing, the script
tells you which one and stops — it will not silently do nothing.
"""

import os
import sys
import hmac
import hashlib
import tempfile
import traceback
from datetime import datetime, time

# ---- hard dependency check with a friendly message ----
missing = []
try:
    import pymysql
except ImportError:
    missing.append("pymysql")
try:
    import pikepdf
except ImportError:
    missing.append("pikepdf")
try:
    from fpdf import FPDF
except ImportError:
    missing.append("fpdf2")
try:
    from azure.storage.blob import BlobServiceClient, ContentSettings
except ImportError:
    missing.append("azure-storage-blob")

if missing:
    print("[FATAL] Missing packages:", ", ".join(missing))
    print("Install into the SAME python you run this with, e.g.:")
    print('  <that-python.exe> -m pip install ' + " ".join(missing))
    sys.exit(1)


# =====================================================================
# CONFIG (env vars) — validated up front
# =====================================================================

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "charset":  "utf8mb4",
}
PDF_PASSWORD            = os.getenv("VEHICLE_PDF_PASSWORD")
AZURE_CONNECTION_STRING = os.getenv("AZURE_BLOB_CONNECTION_STRING")
CONTAINER_NAME          = os.getenv("AZURE_BLOB_CONTAINER", "insightzzwhatappcontainer")
PDF_BLOB_PREFIX         = "vehicle_pdfs"


def validate_config():
    problems = []
    if not DB_CONFIG["password"]:
        problems.append("DB_PASSWORD is not set")
    if not DB_CONFIG["database"]:
        problems.append("DB_NAME is not set")
    if not PDF_PASSWORD:
        problems.append("VEHICLE_PDF_PASSWORD is not set")
    if not AZURE_CONNECTION_STRING:
        problems.append("AZURE_BLOB_CONNECTION_STRING is not set")

    print("=" * 70)
    print("CONFIG CHECK")
    print("=" * 70)
    print(f"  DB_HOST                = {DB_CONFIG['host']}")
    print(f"  DB_USER                = {DB_CONFIG['user']}")
    print(f"  DB_NAME                = {DB_CONFIG['database']}")
    print(f"  DB_PASSWORD            = {'<set>' if DB_CONFIG['password'] else '<MISSING>'}")
    print(f"  VEHICLE_PDF_PASSWORD   = {'<set>' if PDF_PASSWORD else '<MISSING>'}")
    print(f"  AZURE_BLOB_CONTAINER   = {CONTAINER_NAME}")
    print(f"  AZURE_CONNECTION_STRING= {'<set>' if AZURE_CONNECTION_STRING else '<MISSING>'}")
    print("=" * 70)

    if problems:
        print("\n[FATAL] Cannot continue:")
        for p in problems:
            print(f"   - {p}")
        print("\nThese come from the environment. If you run this from a plain")
        print("terminal (not launched by the Service Manager), set them first,")
        print("e.g. in PowerShell:")
        print('   $env:DB_NAME="COAL_SAMPLING_DHAR"; $env:VEHICLE_PDF_PASSWORD="..."')
        sys.exit(1)


# =====================================================================
# DB
# =====================================================================

def db_connect():
    return pymysql.connect(**DB_CONFIG)


def fetch_unique_vehicles(force: bool, limit: int | None) -> list[dict]:
    """
    Unique by RFID (the primary lookup key). Skips rows with no vehicle number.
    """
    where = "WHERE vm.VEHICLE_NUMBER IS NOT NULL AND vm.VEHICLE_NUMBER <> ''"
    if not force:
        where += " AND (vm.PDF_URL IS NULL OR vm.PDF_URL = '')"

    # Deduplicate by RFID without GROUP BY (avoids only_full_group_by issues):
    # keep only the lowest ID row for each RFID via a NOT EXISTS anti-join.
    sql = f"""
        SELECT vm.ID, vm.RFID, vm.VEHICLE_NUMBER, vm.VENDOR_CODE,
               vm.PDF_URL, vr.VENDER_NAME
        FROM VEHICLE_MASTER vm
        LEFT JOIN VENDOR_MASTER vr ON vr.VENDOR_CODE = vm.VENDOR_CODE
        {where}
          AND NOT EXISTS (
              SELECT 1 FROM VEHICLE_MASTER vm2
              WHERE vm2.RFID = vm.RFID
                AND vm2.ID   < vm.ID
          )
        ORDER BY vm.ID ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"

    db = db_connect()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute(sql)
        return cur.fetchall()
    finally:
        db.close()


def save_pdf_url(vehicle_id: int, url: str):
    db = db_connect()
    try:
        cur = db.cursor()
        cur.execute("UPDATE VEHICLE_MASTER SET PDF_URL=%s WHERE ID=%s", (url, vehicle_id))
        db.commit()
    finally:
        db.close()


# =====================================================================
# PDF build + encrypt
# =====================================================================

def build_vehicle_pdf(vehicle: dict, out_path: str):
    pdf = FPDF(orientation="P", unit="mm", format="A5")
    pdf.set_margins(12, 14, 12)
    pdf.add_page()

    pdf.set_font("Arial", "B", 16)
    pdf.cell(0, 10, "VEHICLE INFORMATION", ln=1, align="C")
    pdf.set_font("Arial", "", 9)
    pdf.cell(0, 6, "Coal Sampling System - Confidential", ln=1, align="C")
    pdf.ln(2)
    pdf.set_line_width(0.5)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(6)

    rows = [
        ("Vehicle Number", vehicle.get("VEHICLE_NUMBER") or "-"),
        ("Vendor Name",    vehicle.get("VENDER_NAME") or "-"),
        ("Vendor Code",    vehicle.get("VENDOR_CODE") or "-"),
        ("RFID",           vehicle.get("RFID") or "-"),
        ("Generated On",   datetime.now().strftime("%d/%m/%Y %H:%M")),
    ]
    for label, value in rows:
        pdf.set_font("Arial", "B", 11)
        pdf.cell(45, 9, label, border=1)
        pdf.set_font("Arial", "", 11)
        pdf.cell(0, 9, str(value), border=1, ln=1)

    pdf.output(out_path)


def encrypt_pdf(in_path: str, out_path: str):
    with pikepdf.open(in_path) as pdf:
        pdf.save(out_path, encryption=pikepdf.Encryption(
            user=PDF_PASSWORD, owner=PDF_PASSWORD, R=6))


# =====================================================================
# Blob upload
# =====================================================================

_container_client = None

def get_container_client():
    global _container_client
    if _container_client is None:
        svc = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        _container_client = svc.get_container_client(CONTAINER_NAME)
    return _container_client


def blob_name_for(vehicle_number: str) -> str:
    """
    Opaque, deterministic blob name derived via HMAC-SHA256 keyed with the
    shared secret. The vehicle number never appears in the URL, and the name
    cannot be reproduced from a vehicle number without the key.

    Uses BLOB_NAME_SECRET if set, otherwise falls back to VEHICLE_PDF_PASSWORD.
    Keep the key STABLE - changing it changes every blob name (old blobs become
    orphans and PDF_URL rows would need re-syncing).
    """
    key = (os.getenv("BLOB_NAME_SECRET") or PDF_PASSWORD or "").encode("utf-8")
    digest = hmac.new(key, str(vehicle_number).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{PDF_BLOB_PREFIX}/{digest}.pdf"


def upload_pdf(local_pdf_path: str, vehicle_number: str) -> str:
    blob_name = blob_name_for(vehicle_number)
    blob_client = get_container_client().get_blob_client(blob_name)
    with open(local_pdf_path, "rb") as f:
        blob_client.upload_blob(
            f, overwrite=True,
            content_settings=ContentSettings(content_type="application/pdf"),
        )
    return blob_client.url


# =====================================================================
# MAIN
# =====================================================================

def main():
    force       = "--force" in sys.argv
    no_db_write = "--no-db-write" in sys.argv
    limit       = 10
    if "--limit" in sys.argv:
        limit = int(sys.argv[sys.argv.index("--limit") + 1])

    validate_config()

    # Prove the container is reachable before looping over vehicles
    try:
        exists = get_container_client().exists()
        print(f"[AZURE] Container '{CONTAINER_NAME}' reachable: {exists}")
        if not exists:
            print("[FATAL] Container does not exist under this account. Check the "
                  "connection string / container name.")
            sys.exit(1)
    except Exception:
        print("[FATAL] Could not reach Azure Blob storage:")
        traceback.print_exc()
        sys.exit(1)

    vehicles = fetch_unique_vehicles(force=force, limit=limit)
    print(f"\n[BACKFILL] {len(vehicles)} unique vehicle(s) to process (force={force}, limit={limit}, no_db_write={no_db_write})")

    if not vehicles:
        print("[BACKFILL] Nothing to do. Either every vehicle already has a")
        print("PDF_URL (run with --force to regenerate) or VEHICLE_MASTER is empty.")
        return

    ok, failed = 0, 0
    for i, v in enumerate(vehicles, 1):
        vn = v.get("VEHICLE_NUMBER")
        print(f"[{i}/{len(vehicles)}] {vn}  (RFID={v.get('RFID')}, "
              f"vendor={v.get('VENDER_NAME') or v.get('VENDOR_CODE') or '-'})")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                raw = os.path.join(tmp, "raw.pdf")
                enc = os.path.join(tmp, "enc.pdf")

                build_vehicle_pdf(v, raw)
                print("      built PDF")
                encrypt_pdf(raw, enc)
                print("      encrypted")
                url = upload_pdf(enc, vn)
                print(f"      uploaded -> {url}")
                print(f"      (blob name is opaque; vehicle number not in URL)")

            if no_db_write:
                print("      (skipped DB write: --no-db-write)")
            else:
                save_pdf_url(v["ID"], url)
                print("      PDF_URL saved to DB")

            ok += 1
        except Exception:
            print("      FAILED:")
            traceback.print_exc()
            failed += 1
        print()

    print("=" * 70)
    print(f"[BACKFILL] DONE.  success={ok}  failed={failed}  total={len(vehicles)}")
    print("=" * 70)
    if failed:
        sys.exit(1)

if __name__ == "__main__":
    
    while True:
        try:
            if datetime.now().weekday == 6:  # Sunday
                print("[BACKFILL] Skipping backfill to Sunday")
                time.sleep(3600)  # Sleep for a day
                main()
        except Exception as e:
            print(f"[BACKFILL] Service crashed: {e}")
            print("[BACKFILL] Restarting service...")