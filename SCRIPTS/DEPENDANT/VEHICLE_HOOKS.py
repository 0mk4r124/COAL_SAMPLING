"""
SCRIPTS/DEPENDANT/VEHICLE_HOOKS.py

All alert + secured-PDF logic for MAIN_MANAGER. No WEB_APP changes needed.

Entry points:

  on_new_vehicle(uid, vn, rfids)
      Call when DB_CHECK fails (RFID not in VEHICLE_MASTER).
      NOTE: the new-vehicle MAIL IS NOW OFF (MAIL_NEW_VEHICLE_ENABLED, default
      0). The hook itself is kept - signature, dedupe and call site unchanged -
      so nothing in MAIN_MANAGER had to move and the mail can be switched back
      on from .env alone.

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

  on_inspection_complete(...)                                        NEW
      Call once the sampling report PDF has been generated and the session is
      being closed. Mails the report PDF + vehicle info to the alert and
      client lists, in a background thread.

  on_cycle_blocked(...)                                              NEW
      Call when a cycle is rejected by the AI check or by hard material.
      Mails the details plus the frozen camera images of the failed cycles.

Both new hooks are fire-and-forget and swallow every error: a mail problem
must never touch the state machine.
"""

import threading
import traceback
import logging

import pymysql

from DEPENDANT.EMAIL_ALERTS import (
    send_new_vehicle_alert, send_vendor_mismatch_alert,
    send_inspection_complete_mail, send_cycle_blocked_alert,
)
from DEPENDANT.PDF_UTILS import create_and_upload_vehicle_pdf, _db_connect
from DEPENDANT.BLOB_NAMING import blob_url_for
from DEPENDANT.PDF_UPLOAD_WORKER import enqueue_upload

logger = logging.getLogger(__name__)

_new_vehicle_alerted_uids = set()   # dedupe: one mail per session UID
_ALERT_LOCK = threading.Lock()

# NEW: dedupe for the inspection-complete mail - one per session UID, so a
# retried COMPLETE state can never mail the same report twice.
_inspection_mailed_uids = set()
_INSPECTION_LOCK = threading.Lock()

# NEW: how many block alerts one session may send before it goes quiet. A
# session that keeps hitting hard material should not turn into 20 mails.
MAX_BLOCK_ALERTS_PER_SESSION = 4
_block_alert_counts: dict[str, int] = {}
_BLOCK_LOCK = threading.Lock()


# =====================================================================
# NEW VEHICLE ALERT   (mail disabled - hook kept intact)
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

    # send_new_vehicle_alert() is gated on MAIL_NEW_VEHICLE_ENABLED and
    # returns immediately when the mail is off, so the thread is a no-op in
    # normal operation.
    threading.Thread(
        target=send_new_vehicle_alert, args=(uid, vn, rfids), daemon=True
    ).start()
    logger.debug(f"[HOOKS] New-vehicle hook fired for UID {uid} (mail off by default)")


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

        # (c) Genuinely new vehicle:
        #     Compute the FINAL url instantly (deterministic) and hand it to the
        #     printer now. The actual encrypt+upload runs in the background and
        #     retries for up to 1 day; if it never succeeds the vehicle is
        #     marked unsynced. No 30-40s wait, no cycle blocking.
        url = blob_url_for(vehicle_number)
        logger.info(f"[HOOKS] No PDF for {vehicle_number} - url ready instantly, "
                    f"upload queued in background -> {url}")

        # Save url now so future arrivals hit case (a). IS_PDF_SYNCED stays 0
        # until the worker confirms the blob is actually up.
        if rfid:
            _save_pdf_url(rfid, url)

        enqueue_upload({
            "VEHICLE_NUMBER": vehicle_number,
            "VENDOR_CODE":    vendor_code,
            "VENDER_NAME":    vehicle.get("VENDER_NAME") or "",
            "RFID":           rfid,
        })
        return url

    except Exception:
        logger.error(f"[HOOKS] resolve_pdf_url failed:\n{traceback.format_exc()}")
        return ""   # manager prints dtstamp-only label; never plaintext fallback


# =====================================================================
# NEW: INSPECTION COMPLETE  (report PDF + vehicle info by mail)
# =====================================================================

def on_inspection_complete(uid: str,
                           vehicle: dict | None,
                           rfids: list[str] | None = None,
                           bucket_no: int | None = None,
                           report_pdf_path: str | None = None,
                           pdf_url: str | None = None,
                           sample_info: dict | None = None) -> None:
    """
    Fire-and-forget. Call from MAIN_MANAGER._handle_complete AFTER
    generate_sampling_report()/compress_pdf(), before the session is reset.

    Deduped per UID, so a re-entered COMPLETE state cannot double-mail.
    `vehicle` is the row MAIN_MANAGER already holds in self.vehicle.

    What gets attached is the COMPRESSED sampling report from the RESULT
    folder (report_pdf_path = self.paths["REPORT_PATH"], which compress_pdf
    has already compressed in place). The secured PDF uploaded to blob storage
    for the QR is NOT sent. `pdf_url` is still accepted so the existing
    MAIN_MANAGER call keeps working, but it is ignored downstream.
    """
    try:
        if not uid:
            return

        with _INSPECTION_LOCK:
            if uid in _inspection_mailed_uids:
                logger.debug(f"[HOOKS] Inspection mail already sent for {uid} - skipping")
                return
            _inspection_mailed_uids.add(uid)
            if len(_inspection_mailed_uids) > 500:
                _inspection_mailed_uids.clear()
                _inspection_mailed_uids.add(uid)

        v = vehicle or {}
        kwargs = {
            "uid":             uid,
            "vehicle_no":      v.get("VEHICLE_NUMBER") or "-",
            "vendor":          v.get("VENDER_NAME") or v.get("VENDOR_CODE") or "-",
            "vendor_code":     v.get("VENDOR_CODE"),
            "rfids":           rfids,
            "bucket_no":       bucket_no,
            "report_pdf_path": report_pdf_path,
            "pdf_url":         None,   # uploaded/secured PDF is deliberately not mailed
            "sample_info":     sample_info,
        }

        def _run():
            try:
                ok = send_inspection_complete_mail(**kwargs)
                logger.info(f"[HOOKS] Inspection-complete mail for {uid}: "
                            f"{'sent' if ok else 'NOT sent'}")
            except Exception:
                logger.error(f"[HOOKS] Inspection-complete mail failed:\n"
                             f"{traceback.format_exc()}")

        threading.Thread(target=_run, name=f"InspectionMail-{uid}", daemon=True).start()
        logger.info(f"[HOOKS] Inspection-complete mail queued for UID {uid}")

    except Exception:
        # Bookkeeping must never break the close-out path
        logger.error(f"[HOOKS] on_inspection_complete failed:\n{traceback.format_exc()}")


# =====================================================================
# NEW: CYCLE BLOCKED  (AI obstruction / hard material, with images)
# =====================================================================

def on_cycle_blocked(uid: str,
                     vehicle: dict | None,
                     reason: str,
                     position: dict | None = None,
                     attempt_no: int | None = None,
                     consecutive: int | None = None,
                     waiting_for_operator: bool = False,
                     image_paths: list[str] | None = None,
                     sample_info: dict | None = None,
                     rfids: list[str] | None = None,
                     bucket_no: int | None = None) -> None:
    """
    Fire-and-forget. Call the moment a cycle is rejected by the AI check or by
    hard material - MAIN_MANAGER already has the frozen camera frames recorded
    in sample_info['attempts'][n]['image'] by then.

    Capped at MAX_BLOCK_ALERTS_PER_SESSION so a bad load cannot flood the
    mailbox; the inspection-complete mail still carries the full attempt log.
    """
    try:
        if not uid:
            return

        with _BLOCK_LOCK:
            count = _block_alert_counts.get(uid, 0)
            if count >= MAX_BLOCK_ALERTS_PER_SESSION:
                logger.info(f"[HOOKS] Block-alert cap reached for {uid} "
                            f"({MAX_BLOCK_ALERTS_PER_SESSION}) - not mailing again")
                return
            _block_alert_counts[uid] = count + 1
            if len(_block_alert_counts) > 500:
                _block_alert_counts.clear()
                _block_alert_counts[uid] = count + 1

        v = vehicle or {}
        kwargs = {
            "uid":                  uid,
            "vehicle_no":           v.get("VEHICLE_NUMBER") or "-",
            "vendor":               v.get("VENDER_NAME") or v.get("VENDOR_CODE") or "-",
            "vendor_code":          v.get("VENDOR_CODE"),
            "reason":               reason,
            "position":             position,
            "attempt_no":           attempt_no,
            "consecutive":          consecutive,
            "waiting_for_operator": waiting_for_operator,
            "image_paths":          image_paths,
            "sample_info":          sample_info,
            "rfids":                rfids,
            "bucket_no":            bucket_no,
        }

        def _run():
            try:
                ok = send_cycle_blocked_alert(**kwargs)
                logger.info(f"[HOOKS] Cycle-blocked ({reason}) mail for {uid}: "
                            f"{'sent' if ok else 'NOT sent'}")
            except Exception:
                logger.error(f"[HOOKS] Cycle-blocked mail failed:\n"
                             f"{traceback.format_exc()}")

        threading.Thread(target=_run, name=f"BlockMail-{uid}", daemon=True).start()
        logger.info(f"[HOOKS] Cycle-blocked ({reason}) mail queued for UID {uid}")

    except Exception:
        logger.error(f"[HOOKS] on_cycle_blocked failed:\n{traceback.format_exc()}")