"""
SCRIPTS/DEPENDANT/EMAIL_ALERTS.py
Alert mails via Microsoft Graph (AZURE_SERVICE.azure_send_mail).

Env:
  ALERT_RECIPIENTS = "person1@company.com,person2@company.com"
"""

import os
import logging
import traceback
from datetime import datetime

from DEPENDANT.AZURE_SERVICE import azure_send_mail, azure_upload_file

logger = logging.getLogger(__name__)

RECIPIENTS = [r.strip() for r in os.getenv("ALERT_RECIPIENTS", "").split(",") if r.strip()]
MAIL_ATTACH_LIMIT_MB = 3.0   # Graph inline attachment ceiling


def send_cycle_complete_mail(uid: str, vehicle_no: str, vendor: str,
                             raw_zip_parts: list[str] | None, raw_img_count: int,
                             raw_total_mb: float = 0.0,
                             report_pdf_path: str | None = None) -> tuple[bool, list[str]]:
    """
    End-of-cycle mail: sampling done for this vehicle, with download links to
    the UNMODIFIED full-resolution raw training images.

    raw_zip_parts : zip paths from RAW_IMAGE_UTILS.archive_raw_images()
    raw_img_count : number of images inside the archive
    raw_total_mb  : total archive size, for the mail body
    report_pdf_path: optional sampling-report PDF, attached inline if it fits

    Returns (mail_sent, uploaded_parts) — uploaded_parts are the zips that
    reached OneDrive and can now be deleted locally by the caller.
    """
    attachments: list[str] = []
    uploaded: list[str] = []
    links: list[str] = []
    failed: list[str] = []

    # ── Upload each zip part to OneDrive and collect the links ──────────────
    for part in raw_zip_parts or []:
        if not part or not os.path.exists(part):
            continue
        try:
            link = azure_upload_file(part)
        except Exception:
            logger.error(f"[ALERT] Raw archive upload failed for {part}:\n{traceback.format_exc()}")
            link = None

        if link:
            links.append(f"  {os.path.basename(part)}\n    {link}")
            uploaded.append(part)
        else:
            failed.append(part)

    # ── Build the raw-images section of the body ────────────────────────────
    if not raw_zip_parts:
        raw_note = "Raw training images: none were collected for this session."
    elif links and not failed:
        part_word = "part" if len(links) == 1 else "parts"
        raw_note = (
            f"Raw training images: {raw_img_count} full-resolution frames "
            f"(3840x2160, unmodified), {raw_total_mb} MB in {len(links)} zip {part_word}.\n"
            f"Download link(s):\n" + "\n".join(links)
        )
    elif links and failed:
        raw_note = (
            f"Raw training images: {raw_img_count} frames, {raw_total_mb} MB — "
            f"PARTIAL UPLOAD.\nUploaded:\n" + "\n".join(links) +
            "\n\nThese parts failed to upload and are still on the sampling PC:\n" +
            "\n".join(f"  {p}" for p in failed)
        )
    else:
        raw_note = (
            f"Raw training images: {raw_img_count} frames, {raw_total_mb} MB collected, "
            f"but the upload FAILED.\nThe archive is kept on the sampling PC at:\n" +
            "\n".join(f"  {p}" for p in failed)
        )

    # ── Report PDF is small enough to attach inline ─────────────────────────
    if report_pdf_path and os.path.exists(report_pdf_path):
        if (os.path.getsize(report_pdf_path) / (1024 * 1024)) <= MAIL_ATTACH_LIMIT_MB:
            attachments.append(report_pdf_path)

    body = (
        "SAMPLING CYCLE COMPLETED at the coal sampling station.\n\n"
        f"Session UID    : {uid}\n"
        f"Vehicle Number : {vehicle_no}\n"
        f"Vendor         : {vendor}\n"
        f"Time           : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        f"{raw_note}\n"
    )

    sent = _safe_send(
        f"[COAL SAMPLING DHAR] Cycle complete - {vehicle_no} (UID {uid})",
        body,
        attachments=attachments or None,
    )
    return sent, uploaded

def _safe_send(subject: str, body: str, attachments: list[str] | None = None) -> bool:
    """Never let a mail failure crash the state machine."""
    if not RECIPIENTS:
        logger.error("[ALERT] ALERT_RECIPIENTS env var is empty - mail not sent")
        return False
    try:
        return azure_send_mail(subject, body, RECIPIENTS, attachments=attachments)
    except Exception:
        logger.error(f"[ALERT] Mail failed:\n{traceback.format_exc()}")
        return False


def send_new_vehicle_alert(uid: str, vn: str, rfids: list[str]) -> bool:
    """RFID not found in VEHICLE_MASTER - new vehicle arrived."""
    rfid_str = rfids if isinstance(rfids, str) else " | ".join(rfids or [])
    body = (
        "A NEW VEHICLE has arrived at the coal sampling station.\n\n"
        f"Session UID : {uid}\n"
        f"Vehicle Number : {vn}\n"
        f"RFID(s)     : {rfid_str}\n"
        f"Time        : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        "The RFID was not found in VEHICLE_MASTER. The system is waiting for the\n"
        "vehicle to be added via the dashboard (standard procedure). Its secured\n"
        "PDF will be created on the next weekly sync, or run PDF sync manually:\n"
    )
    return _safe_send(f"[COAL SAMPLING DHAR] New vehicle arrived - UID {uid}", body)


def send_vendor_mismatch_alert(vehicle_number: str, existing_vendor: str,new_vendor: str, uid: str, image_paths: list[str] | None = None) -> bool:
    """Same vehicle number, different vendor. Existing PDF/QR is reused."""
    body = (
        "VENDOR MISMATCH detected at the coal sampling station.\n\n"
        f"Vehicle Number    : {vehicle_number}\n"
        f"Vendor on record  : {existing_vendor}\n"
        f"Vendor now claimed: {new_vendor}\n"
        f"Session UID       : {uid}\n"
        f"Time              : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        "The QR code / secured PDF of the EXISTING record was used.\n"
        "Vehicle images attached for verification. Please review and correct\n"
        "VEHICLE_MASTER if required.\n"
    )
    return _safe_send(
        f"[COAL SAMPLING DHAR] Vendor mismatch - {vehicle_number}",
        body,
        attachments=image_paths,
    )