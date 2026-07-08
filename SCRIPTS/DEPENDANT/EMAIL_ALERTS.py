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

from DEPENDANT.AZURE_SERVICE import azure_send_mail

logger = logging.getLogger(__name__)

RECIPIENTS = [r.strip() for r in os.getenv("ALERT_RECIPIENTS", "").split(",") if r.strip()]


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