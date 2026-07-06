"""
SCRIPTS/DEPENDANT/PDF_UTILS.py
Build + encrypt vehicle-info PDFs, and sync missing PDFs to the blob container.

Env:
  VEHICLE_PDF_PASSWORD  - shared password (AES-256). Pick a fresh one; the old
                          one was exposed in chat.
  DB_HOST / DB_USER / DB_PASSWORD / DB_NAME
"""

import os
import tempfile
import traceback
import logging
from datetime import datetime

import pymysql
import pikepdf
from fpdf import FPDF

from DEPENDANT.AZURE_SERVICE import upload_pdf_to_container

logger = logging.getLogger(__name__)

PDF_PASSWORD = os.getenv("VEHICLE_PDF_PASSWORD")

DB_CONFIG = {
    "host":     os.getenv("DB_HOST", "127.0.0.1"),
    "user":     os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "coal_sampling"),
    "charset":  "utf8mb4",
}


def _db_connect():
    return pymysql.connect(**DB_CONFIG)


def build_vehicle_pdf(vehicle: dict, out_path: str):
    """A5 industrial-style vehicle info sheet."""
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
    label_w = 45
    for label, value in rows:
        pdf.set_font("Arial", "B", 11)
        pdf.cell(label_w, 9, label, border=1)
        pdf.set_font("Arial", "", 11)
        pdf.cell(0, 9, str(value), border=1, ln=1)

    pdf.output(out_path)


def encrypt_pdf(in_path: str, out_path: str, password: str = None):
    """AES-256; opening requires the password."""
    password = password or PDF_PASSWORD
    if not password:
        raise RuntimeError("VEHICLE_PDF_PASSWORD env var is not set")
    with pikepdf.open(in_path) as pdf:
        pdf.save(out_path, encryption=pikepdf.Encryption(user=password, owner=password, R=6))


def create_and_upload_vehicle_pdf(vehicle: dict) -> str:
    """
    vehicle dict needs: VEHICLE_NUMBER, VENDOR_CODE, VENDER_NAME, RFID.
    Returns the blob URL of the encrypted PDF.
    """
    with tempfile.TemporaryDirectory() as tmp:
        raw = os.path.join(tmp, "raw.pdf")
        enc = os.path.join(tmp, "enc.pdf")
        build_vehicle_pdf(vehicle, raw)
        encrypt_pdf(raw, enc)
        return upload_pdf_to_container(enc, vehicle["VEHICLE_NUMBER"])


def sync_missing_pdfs(force: bool = False) -> dict:
    """
    For every VEHICLE_MASTER row without PDF_URL (or ALL rows if force=True):
    build -> encrypt -> upload -> save URL back to local DB.
    Returns summary dict {"total", "success", "failed"}.
    """
    db = _db_connect()
    try:
        cur   = db.cursor(pymysql.cursors.DictCursor)
        where = "" if force else "WHERE vm.PDF_URL IS NULL OR vm.PDF_URL = ''"
        cur.execute(
            f"""
            SELECT vm.ID, vm.RFID, vm.VEHICLE_NUMBER, vm.VENDOR_CODE, vr.VENDER_NAME
            FROM VEHICLE_MASTER vm
            LEFT JOIN VENDOR_MASTER vr ON vr.VENDOR_CODE = vm.VENDOR_CODE
            {where}
            """
        )
        vehicles = cur.fetchall()
    finally:
        db.close()

    summary = {"total": len(vehicles), "success": 0, "failed": 0, "failed_vehicles": []}
    logger.info(f"[PDF SYNC] {summary['total']} vehicle(s) to process (force={force})")

    for v in vehicles:
        vehicle_no = v.get("VEHICLE_NUMBER")
        if not vehicle_no:
            summary["failed"] += 1
            summary["failed_vehicles"].append(f"ID={v['ID']} (no vehicle number)")
            continue
        try:
            url = create_and_upload_vehicle_pdf(v)

            db = _db_connect()
            try:
                cur = db.cursor()
                cur.execute("UPDATE VEHICLE_MASTER SET PDF_URL=%s WHERE ID=%s", (url, v["ID"]))
                db.commit()
            finally:
                db.close()

            logger.info(f"[PDF SYNC] OK {vehicle_no} -> {url}")
            summary["success"] += 1
        except Exception:
            logger.error(f"[PDF SYNC] FAIL {vehicle_no}:\n{traceback.format_exc()}")
            summary["failed"] += 1
            summary["failed_vehicles"].append(vehicle_no)

    logger.info(f"[PDF SYNC] Done: {summary}")
    return summary