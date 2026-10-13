"""
SCRIPTS/DEPENDANT/EMAIL_ALERTS.py
Alert mails via Microsoft Graph (AZURE_SERVICE.azure_send_mail).

Env:
  ALERT_RECIPIENTS  = "person1@company.com,person2@company.com"
  CLIENT_RECIPIENTS = "client1@client.com,client2@client.com"   <- NEW

Recipient resolution and the per-mail on/off switches now live in
DEPENDANT/MAIL_RECIPIENTS.py. Every mail sent from here goes to
ALERT_RECIPIENTS + CLIENT_RECIPIENTS.

What changed (nothing existing was removed):
  * send_new_vehicle_alert  - kept, but now gated OFF by default
                              (MAIL_NEW_VEHICLE_ENABLED, default 0)
  * send_vendor_mismatch_alert - unchanged behaviour, client list added
  * send_inspection_complete_mail - NEW. Sampling report PDF + vehicle info,
                              sent as soon as an inspection finishes cleanly.
                              Attaches ONLY the compressed report that
                              compress_pdf() leaves in the RESULT folder. The
                              secured blob PDF we upload for the QR is NOT
                              attached and its link is NOT in the body.
  * send_cycle_blocked_alert   - NEW. AI-obstruction / hard-material alert
                              with the frozen camera images of the failed
                              cycles attached
"""

import os
import logging
import traceback
from datetime import datetime

from DEPENDANT.AZURE_SERVICE import azure_send_mail, azure_upload_file
from DEPENDANT.MAIL_RECIPIENTS import (
    recipients_for, is_enabled,
    KIND_NEW_VEHICLE, KIND_VENDOR_MISMATCH, KIND_INSPECTION_DONE,
    KIND_CYCLE_BLOCKED, KIND_CYCLE_COMPLETE, KIND_GENERIC,
)

logger = logging.getLogger(__name__)

# Kept for backwards compatibility - anything already importing RECIPIENTS from
# this module keeps working. New code should call recipients_for(kind).
RECIPIENTS = [r.strip() for r in os.getenv("ALERT_RECIPIENTS", "").split(",") if r.strip()]
MAIL_ATTACH_LIMIT_MB = 3.0   # Graph inline attachment ceiling

# Graph rejects an oversized message outright, so the whole attachment set is
# budgeted, not just each file. Images are the *_REDUCED.jpg copies, so a
# handful fits comfortably.
MAIL_TOTAL_ATTACH_MB  = float(os.getenv("MAIL_TOTAL_ATTACH_MB", "12"))
MAIL_MAX_FAILED_IMAGES = int(os.getenv("MAIL_MAX_FAILED_IMAGES", "6"))

SITE_TAG = os.getenv("MAIL_SITE_TAG", "COAL SAMPLING DHAR")


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
        f"[{SITE_TAG}] Cycle complete - {vehicle_no} (UID {uid})",
        body,
        attachments=attachments or None,
        kind=KIND_CYCLE_COMPLETE,
    )
    return sent, uploaded

def _safe_send(subject: str, body: str, attachments: list[str] | None = None,
               kind: str = KIND_GENERIC) -> bool:
    """
    Never let a mail failure crash the state machine.

    `kind` selects the recipient list and the on/off switch. Callers that pass
    no kind get the generic list (ALERT + CLIENT), which is exactly what the
    old code did plus the client list.
    """
    if not is_enabled(kind):
        logger.info(f"[ALERT] Mail kind '{kind}' is switched off - not sending: {subject}")
        return False

    recipients = recipients_for(kind)
    if not recipients:
        logger.error(f"[ALERT] No recipients for kind '{kind}' "
                     f"(ALERT_RECIPIENTS / CLIENT_RECIPIENTS empty) - mail not sent")
        return False
    try:
        return azure_send_mail(subject, body, recipients, attachments=attachments)
    except Exception:
        logger.error(f"[ALERT] Mail failed:\n{traceback.format_exc()}")
        return False


def send_new_vehicle_alert(uid: str, vn: str, rfids: list[str]) -> bool:
    """
    RFID not found in VEHICLE_MASTER - new vehicle arrived.

    DISABLED BY DEFAULT. The body and the call path are kept intact so it can
    be switched back on with MAIL_NEW_VEHICLE_ENABLED=1 without a code change.
    """
    if not is_enabled(KIND_NEW_VEHICLE):
        logger.info(f"[ALERT] New-vehicle mail is disabled - skipping UID {uid} ({vn})")
        return False

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
    return _safe_send(f"[{SITE_TAG}] New vehicle arrived - UID {uid}", body,
                      kind=KIND_NEW_VEHICLE)


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
    kept, skipped = _fit_attachments(image_paths)
    if skipped:
        body += ("\nSome images were too large to attach and stayed on the sampling PC:\n"
                 + "\n".join(f"  {p}" for p in skipped) + "\n")

    return _safe_send(
        f"[{SITE_TAG}] Vendor mismatch - {vehicle_number}",
        body,
        attachments=kept or None,
        kind=KIND_VENDOR_MISMATCH,
    )


# =====================================================================
# ATTACHMENT BUDGETING
# =====================================================================

def _fit_attachments(paths: list[str] | None,
                     per_file_mb: float = MAIL_ATTACH_LIMIT_MB,
                     total_mb: float = MAIL_TOTAL_ATTACH_MB,
                     max_files: int | None = None) -> tuple[list[str], list[str]]:
    """
    Pick the attachments that will actually fit in one Graph message.

    Returns (kept, skipped). Missing files are silently dropped - a snapshot
    that never got written must not stop the mail going out.
    """
    kept: list[str] = []
    skipped: list[str] = []
    used_mb = 0.0

    for p in paths or []:
        if not p:
            continue
        if not os.path.exists(p):
            logger.warning(f"[ALERT] Attachment missing, skipping: {p}")
            continue
        if max_files is not None and len(kept) >= max_files:
            skipped.append(p)
            continue
        try:
            size_mb = os.path.getsize(p) / (1024 * 1024)
        except OSError:
            continue
        if size_mb > per_file_mb or (used_mb + size_mb) > total_mb:
            skipped.append(p)
            continue
        kept.append(p)
        used_mb += size_mb

    return kept, skipped


# =====================================================================
# SAMPLE-INFO HELPERS (shared by the two new mails)
# =====================================================================

def _attempts(sample_info: dict | None) -> list[dict]:
    if not isinstance(sample_info, dict):
        return []
    att = sample_info.get("attempts")
    return att if isinstance(att, list) else []


def failed_attempt_images(sample_info: dict | None,
                          limit: int = MAIL_MAX_FAILED_IMAGES) -> list[str]:
    """
    Frozen camera frames for the FAILED attempts of a session, newest last.
    _record_attempt() stores these under sample_info['attempts'][n]['image'].
    """
    images: list[str] = []
    for a in _attempts(sample_info):
        if a.get("status") == "SUCCESS":
            continue
        img = a.get("image")
        if img and img not in images:
            images.append(img)
    if limit and len(images) > limit:
        images = images[-limit:]          # keep the most recent failures
    return images


def attempts_table(sample_info: dict | None) -> str:
    """Plain-text attempt log for the mail body."""
    rows = _attempts(sample_info)
    if not rows:
        return "  (no attempts recorded)"

    lines = ["  #   CYCLE  AREA   X     Y     RESULT    REASON             TIME"]
    for a in rows:
        lines.append(
            "  {seq:<3} {cycle:<6} {area:<6} {x:<5} {y:<5} {status:<9} {reason:<18} {time}".format(
                seq=a.get("seq", "-"),
                cycle=a.get("cycle", "-"),
                area=a.get("area", "-"),
                x=a.get("x", "-"),
                y=a.get("y", "-"),
                status=a.get("status", "-"),
                reason=(a.get("reason") or "-"),
                time=(a.get("time") or "-"),
            )
        )
    return "\n".join(lines)


def _vehicle_block(uid, vehicle_no, vendor, vendor_code=None, rfids=None,
                   bucket_no=None) -> str:
    rfid_str = rfids if isinstance(rfids, str) else " | ".join(rfids or [])
    lines = [
        f"Vehicle Number : {vehicle_no or '-'}",
        f"Vendor         : {vendor or '-'}",
    ]
    if vendor_code:
        lines.append(f"Vendor Code    : {vendor_code}")
    lines += [
        f"RFID(s)        : {rfid_str or '-'}",
        f"Session UID    : {uid or '-'}",
    ]
    if bucket_no is not None:
        lines.append(f"Bucket No      : {bucket_no}")
    lines.append(f"Time           : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}")
    return "\n".join(lines)


# =====================================================================
# NEW: INSPECTION COMPLETE  (report PDF + vehicle info)
# =====================================================================

def resolve_report_pdf(report_pdf_path: str | None) -> str | None:
    """
    Pick the COMPRESSED sampling report sitting in the RESULT folder.

    LOGIC.compress_pdf() compresses in place: it writes
    <report>_compressed_tmp.pdf and then os.replace()s it over REPORT_PATH, so
    the file at REPORT_PATH is normally already the compressed one.

    The one case where it isn't: if that atomic replace fails (file locked by a
    viewer, cross-drive move) compress_pdf keeps the compressed copy under the
    _compressed_tmp.pdf name and returns THAT path instead. So the temp name is
    checked first - if it exists it is the smaller, newer file and is the one
    worth mailing.

    Returns a path that exists, or None.
    """
    if not report_pdf_path:
        return None

    candidates = []
    if report_pdf_path.lower().endswith(".pdf"):
        candidates.append(report_pdf_path[:-4] + "_compressed_tmp.pdf")
    candidates.append(report_pdf_path)

    for path in candidates:
        try:
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                return path
        except OSError:
            continue
    return None


def send_inspection_complete_mail(uid: str,
                                  vehicle_no: str,
                                  vendor: str,
                                  vendor_code: str | None = None,
                                  rfids: list[str] | None = None,
                                  bucket_no: int | None = None,
                                  report_pdf_path: str | None = None,
                                  pdf_url: str | None = None,
                                  sample_info: dict | None = None) -> bool:
    """
    Sent as soon as a vehicle inspection finishes successfully.

    Attaches the COMPRESSED sampling report from the RESULT folder, plus the
    vehicle details and a per-attempt summary.

    The secured vehicle PDF we upload to blob storage for the QR code is
    deliberately NOT sent - not as an attachment and not as a link in the body.
    `pdf_url` is accepted so existing callers keep working, but it is ignored.

    Nothing is uploaded anywhere by this mail. If the compressed report is
    still too large for Graph, the mail goes out without it and names the local
    path instead.
    """
    info = sample_info if isinstance(sample_info, dict) else {}
    successful = info.get("successful", "-")
    failed_n   = info.get("failed", "-")
    total      = info.get("total_cycles", "-")
    mode       = info.get("collection_mode", "AUTO")
    started    = info.get("cycle_started") or info.get("started") or "-"
    ended      = info.get("cycle_ended") or "-"

    if pdf_url:
        # Explicit, so a future reader doesn't think this was an oversight.
        logger.debug("[ALERT] Ignoring secured/uploaded PDF url for the "
                     "inspection mail - compressed report only")

    attachments: list[str] = []
    report_path = resolve_report_pdf(report_pdf_path)

    if not report_path:
        pdf_note = ("Sampling report: not available for this session "
                    f"(expected at {report_pdf_path or '-'}).")
    else:
        size_mb = os.path.getsize(report_path) / (1024 * 1024)
        kept, _skipped = _fit_attachments([report_path])
        if kept:
            attachments += kept
            pdf_note = (f"Sampling report attached: {os.path.basename(report_path)} "
                        f"({size_mb:.1f} MB)")
        else:
            pdf_note = (f"Sampling report is {size_mb:.1f} MB, over the "
                        f"{MAIL_ATTACH_LIMIT_MB:.0f} MB mail limit, so it is not "
                        f"attached.\nIt is on the sampling PC at:\n  {report_path}")
            logger.warning(f"[ALERT] Report too large to attach ({size_mb:.1f} MB): "
                           f"{report_path}")

    body = (
        "VEHICLE INSPECTION COMPLETED SUCCESSFULLY at the coal sampling station.\n\n"
        + _vehicle_block(uid, vehicle_no, vendor, vendor_code, rfids, bucket_no) + "\n\n"
        "Sampling summary\n"
        f"  Collection mode   : {mode}\n"
        f"  Successful cycles : {successful} / {total}\n"
        f"  Failed attempts   : {failed_n}\n"
        f"  Started           : {started}\n"
        f"  Ended             : {ended}\n\n"
        # "Attempt log\n"
        # f"{attempts_table(info)}\n\n"
        # f"{pdf_note}\n"
    )

    return _safe_send(
        f"[{SITE_TAG}] Inspection complete - {vehicle_no} (UID {uid})",
        body,
        attachments=attachments or None,
        kind=KIND_INSPECTION_DONE,
    )


# =====================================================================
# NEW: AI-BLOCKED / HARD-MATERIAL  (with images of the failed cycles)
# =====================================================================

_REASON_TITLES = {
    "AI_OBSTRUCTION":   "AI BLOCKED - auger position could not be confirmed",
    "HARD_MATERIAL":    "HARD MATERIAL - auger could not take the sample",
    "POSITION_TIMEOUT": "POSITION TIMEOUT - auger did not confirm position",
}

_REASON_NOTES = {
    "AI_OBSTRUCTION": (
        "The vision model could not confirm the auger was over coal, so the cycle\n"
        "was not started. The dashboard is showing the operator popup: CONTINUE\n"
        "moves to a different point, MANUAL closes the session as collected by hand.\n"
        "If nobody answers, the system continues automatically at a clear point."
    ),
    "HARD_MATERIAL": (
        "The PLC reported hard material, so the cycle was not counted. Automatic\n"
        "probing stops once it happens twice in a row, to protect the auger, and\n"
        "the dashboard asks the operator to collect the sample manually."
    ),
    "POSITION_TIMEOUT": (
        "The auger did not report position confirmation in time, so the point was\n"
        "discarded and a fresh one is being used."
    ),
}


def send_cycle_blocked_alert(uid: str,
                             vehicle_no: str,
                             vendor: str,
                             reason: str,
                             position: dict | None = None,
                             attempt_no: int | None = None,
                             consecutive: int | None = None,
                             waiting_for_operator: bool = False,
                             image_paths: list[str] | None = None,
                             sample_info: dict | None = None,
                             vendor_code: str | None = None,
                             rfids: list[str] | None = None,
                             bucket_no: int | None = None) -> bool:
    """
    A cycle was rejected by the AI check or by hard material.

    The frozen camera frames of the failed cycles are attached - explicit
    `image_paths` if the caller has them, otherwise every failed-attempt image
    recorded in sample_info (most recent MAIL_MAX_FAILED_IMAGES).
    """
    reason = (reason or "FAILED").upper()
    info   = sample_info if isinstance(sample_info, dict) else {}

    images = list(image_paths or [])
    if not images:
        images = failed_attempt_images(info)
    kept, skipped = _fit_attachments(images, max_files=MAIL_MAX_FAILED_IMAGES)

    pos_line = "-"
    if isinstance(position, dict):
        pos_line = (f"area {position.get('area', '-')} "
                    f"x={position.get('x', '-')} y={position.get('y', '-')}")

    detail_lines = [f"  Reason            : {reason}", f"  Blocked point     : {pos_line}"]
    if attempt_no is not None:
        detail_lines.append(f"  Attempt number    : {attempt_no}")
    if consecutive is not None:
        detail_lines.append(f"  Consecutive fails : {consecutive}")
    detail_lines.append(
        f"  Cycle halted      : {'YES - waiting for operator' if waiting_for_operator else 'no - retrying automatically'}"
    )
    detail_lines.append(f"  Successful cycles : {info.get('successful', '-')} / {info.get('total_cycles', '-')}")
    detail_lines.append(f"  Failed attempts   : {info.get('failed', '-')}")

    if kept:
        img_note = (f"{len(kept)} camera image(s) of the failed cycle(s) attached:\n"
                    + "\n".join(f"  {os.path.basename(p)}" for p in kept))
    else:
        img_note = "No camera image was available for the failed cycle(s)."

    if skipped:
        img_note += ("\n\nThese images did not fit in the mail and are on the sampling PC:\n"
                     + "\n".join(f"  {p}" for p in skipped))

    body = (
        f"{_REASON_TITLES.get(reason, 'SAMPLING CYCLE BLOCKED')} at the coal sampling station.\n\n"
        + _vehicle_block(uid, vehicle_no, vendor, vendor_code, rfids, bucket_no) + "\n\n"
        "Block details\n"
        + "\n".join(detail_lines) + "\n\n"
        + (_REASON_NOTES.get(reason, "") + "\n\n" if _REASON_NOTES.get(reason) else "")
        + "Attempt log\n"
        f"{attempts_table(info)}\n\n"
        f"{img_note}\n"
    )

    label = "AI blocked" if reason == "AI_OBSTRUCTION" else (
        "Hard material" if reason == "HARD_MATERIAL" else reason.replace("_", " ").title()
    )

    return _safe_send(
        f"[{SITE_TAG}] {label} - {vehicle_no} (UID {uid})",
        body,
        attachments=kept or None,
        kind=KIND_CYCLE_BLOCKED,
    )