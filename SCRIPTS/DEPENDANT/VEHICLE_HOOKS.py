"""
SCRIPTS/DEPENDANT/VEHICLE_HOOKS.py

All alert + secured-PDF logic for MAIN_MANAGER. No WEB_APP changes needed.

Two entry points:

  on_new_vehicle(uid, rfids)
      Call when DB_CHECK fails (RFID not in VEHICLE_MASTER). Sends the
      new-vehicle mail in a background thread. Deduped per UID, so calling
      it from a polling loop is safe.

  resolve_pdf_url(vehicle, uid, vehicle_img_path=None)
      Call ONCE per session, just before sending the print job. Returns the
      secured PDF URL to encode in the QR. Handles all three cases:

        a) Vehicle already has PDF_URL            -> return it
        b) No PDF_URL, but SAME vehicle number exists under a DIFFERENT
           vendor with a PDF (vendor-mismatch case) -> mail alert with the
           vehicle image, reuse + inherit the OLD PDF URL (per requirement)
        c) No PDF_URL anywhere (genuinely new)     -> create + encrypt +
           upload the PDF now, save URL to DB, return it

      Never raises - on any failure it returns "" so the manager can print
      a dtstamp-only label instead of crashing the cycle.
"""

import threading
import traceback
import logging

import pymysql

from DEPENDANT.EMAIL_ALERTS import send_new_vehicle_alert, send_vendor_mismatch_alert
from DEPENDANT.PDF_UTILS import create_and_upload_vehicle_pdf, _db_connect

logger = logging.getLogger(__name__)

_new_vehicle_alerted_uids = set()   # dedupe: one mail per session UID
_ALERT_LOCK = threading.Lock()


# =====================================================================
# NEW VEHICLE ALERT
# =====================================================================

def on_new_vehicle(uid: str, vn: str, rfids: list[str]) -> None:
    """Fire-and-forget mail. Safe to call repeatedly (polling loop)."""
    with _ALERT_LOCK:
        if uid in _new_vehicle_alerted_uids:
            return
        _new_vehicle_alerted_uids.add(uid)
        # keep the set from growing forever
        if len(_new_vehicle_alerted_uids) > 500:
            _new_vehicle_alerted_uids.clear()
            _new_vehicle_alerted_uids.add(uid)

    threading.Thread(
        target=send_new_vehicle_alert, args=(uid, vn, rfids), daemon=True
    ).start()
    logger.info(f"[HOOKS] New-vehicle alert queued for UID {uid}")


# =====================================================================
# PDF URL RESOLUTION (mismatch + auto-create)
# =====================================================================

def _find_same_number_other_vendor(vehicle_number: str, vendor_code: str) -> dict | None:
    """Oldest row with the same vehicle number but a different vendor."""
    db = _db_connect()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT vm.ID, vm.RFID, vm.VEHICLE_NUMBER, vm.VENDOR_CODE,
                   vm.PDF_URL, vr.VENDER_NAME
            FROM VEHICLE_MASTER vm
            LEFT JOIN VENDOR_MASTER vr ON vr.VENDOR_CODE = vm.VENDOR_CODE
            WHERE vm.VEHICLE_NUMBER = %s
              AND (vm.VENDOR_CODE IS NULL OR vm.VENDOR_CODE <> %s)
            ORDER BY vm.ID ASC
            LIMIT 1
            """,
            (vehicle_number, vendor_code),
        )
        return cur.fetchone()
    finally:
        db.close()


def _save_pdf_url(rfid: str, url: str) -> None:
    db = _db_connect()
    try:
        cur = db.cursor()
        cur.execute("UPDATE VEHICLE_MASTER SET PDF_URL=%s WHERE RFID=%s", (url, rfid))
        db.commit()
    finally:
        db.close()


def resolve_pdf_url(vehicle: dict, uid: str, vehicle_img_path: str = None) -> str:
    """
    vehicle dict = row from db_find_vehicle / _resolve_rfid:
        RFID, VEHICLE_NUMBER, VENDOR_CODE, VENDER_NAME, PDF_URL (may be absent)
    Returns the secured PDF URL for the QR code, or "" on failure.
    """
    try:
        vehicle_number = vehicle.get("VEHICLE_NUMBER")
        vendor_code    = vehicle.get("VENDOR_CODE")
        vendor_name    = vehicle.get("VENDER_NAME") or vendor_code or "-"
        rfid           = vehicle.get("RFID")

        # (a) Already has a PDF -> nothing to do
        existing_url = vehicle.get("PDF_URL")
        if existing_url:
            return existing_url

        # (b) Vendor mismatch: same number, different vendor, PDF exists there
        other = _find_same_number_other_vendor(vehicle_number, vendor_code)
        if other and other.get("PDF_URL"):
            logger.warning(
                f"[HOOKS] Vendor mismatch: {vehicle_number} on record with "
                f"'{other.get('VENDER_NAME')}', arrived as '{vendor_name}'. "
                f"Reusing existing PDF."
            )
            images = [vehicle_img_path] if vehicle_img_path else None
            threading.Thread(
                target=send_vendor_mismatch_alert,
                args=(vehicle_number,
                      other.get("VENDER_NAME") or other.get("VENDOR_CODE") or "-",
                      vendor_name,
                      uid),
                kwargs={"image_paths": images},
                daemon=True,
            ).start()

            old_url = other["PDF_URL"]
            if rfid:
                _save_pdf_url(rfid, old_url)   # inherit so future arrivals hit case (a)
            return old_url

        # (c) Genuinely new vehicle -> create + upload PDF now (a few seconds)
        logger.info(f"[HOOKS] No PDF for {vehicle_number} - creating and uploading now")
        url = create_and_upload_vehicle_pdf({
            "VEHICLE_NUMBER": vehicle_number,
            "VENDOR_CODE":    vendor_code,
            "VENDER_NAME":    vehicle.get("VENDER_NAME") or "",
            "RFID":           rfid,
        })
        if rfid:
            _save_pdf_url(rfid, url)
        return url

    except Exception:
        logger.error(f"[HOOKS] resolve_pdf_url failed:\n{traceback.format_exc()}")
        return ""   # manager prints dtstamp-only label; never plaintext fallback