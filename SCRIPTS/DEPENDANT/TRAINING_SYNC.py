"""
SCRIPTS/DEPENDANT/TRAINING_SYNC.py

Training-data sync: uploads the raw model-training images collected by
CAM_CAPTURE (RAW_IMG/<uid>/CAM1|CAM2|CAM3/*.jpg) and mails the links,
folder by folder.

Driven by DATA_SYNC.py as a third background thread (see SYNC_TRAINING_DATA
in that file). It reuses the project's Azure helpers for uploading and mail,
but has NO MQTT and no contact with MAIN_MANAGER / PLC_SAMPLER / CAM_CAPTURE:
it only reads folders off disk, so it can fail or be switched off without
touching the sampling flow.

Behaviour
  * Images are archived BYTE-FOR-BYTE (ZIP_STORED) - never resized or
    re-encoded, so they stay valid training data.
  * A folder is only picked up once nothing has been written to it for
    QUIET_MINUTES, so a live session is never touched.
  * Progress lives in RAW_SYNC_STATE.json at the project root. Parts that
    already uploaded are skipped on the next run, so a restart never
    re-sends what is already in OneDrive.
  * One mail per folder, with per-camera counts and the download links.

Manual use (does not need DATA_SYNC running):
    python -m DEPENDANT.TRAINING_SYNC --once
    python -m DEPENDANT.TRAINING_SYNC --status
    python -m DEPENDANT.TRAINING_SYNC --uid 20260812144728
    python -m DEPENDANT.TRAINING_SYNC --retry-failed
"""

import os
import json
import time
import zipfile
import logging
import argparse
import traceback
from datetime import datetime

from DEPENDANT.AZURE_SERVICE import azure_upload_file, azure_send_mail

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

BASE_FILE_PATH = os.environ.get(
    'BASE_FILE_PATH',
    'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/'
)

RAW_PATH   = os.path.join(BASE_FILE_PATH, "RAW_IMG")
WORK_DIR   = os.path.join(BASE_FILE_PATH, "RAW_SYNC_TMP")      # zip parts
STATE_FILE = os.path.join(BASE_FILE_PATH, "RAW_SYNC_STATE.json")

RECIPIENTS = [r.strip() for r in os.getenv("ALERT_RECIPIENTS", "").split(",") if r.strip()]

# Zip part size. Small parts matter on a weak link: a part that fails costs
# only that part, and each OneDrive upload session finishes well inside its
# expiry window.
PART_SIZE_MB = int(os.getenv("TRAINING_SYNC_PART_MB", "50"))

# A folder must be idle this long before it is considered finished.
QUIET_MINUTES = int(os.getenv("TRAINING_SYNC_QUIET_MIN", "15"))

# Minutes between scans when run as a thread from DATA_SYNC.
INTERVAL_MIN = int(os.getenv("TRAINING_SYNC_INTERVAL_MIN", "30"))

# Restrict uploads to off-hours, e.g. "22:00-06:00". Empty = any time.
UPLOAD_WINDOW = os.getenv("TRAINING_SYNC_WINDOW", "").strip()

MAX_FOLDER_ATTEMPTS = 5
IMG_EXTS = (".jpg", ".jpeg", ".png")


def log(msg: str):
    print(f"[TRAIN_SYNC] {msg}")
    logger.info(f"[TRAIN_SYNC] {msg}")


# ═══════════════════════════════════════════════════════════════════════════════
# STATE FILE (JSON at project root)
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"version": 1, "updated": None, "folders": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        state.setdefault("folders", {})
        return state
    except Exception:
        logger.error(f"[TRAIN_SYNC] State file unreadable:\n{traceback.format_exc()}")
        try:
            os.replace(STATE_FILE, STATE_FILE + ".corrupt")   # keep for inspection
        except OSError:
            pass
        return {"version": 1, "updated": None, "folders": {}}


def save_state(state: dict):
    """Atomic write - a power cut can never leave a half-written state file."""
    state["updated"] = datetime.now().isoformat(timespec="seconds")
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        logger.error(f"[TRAIN_SYNC] Could not save state:\n{traceback.format_exc()}")


def folder_record(state: dict, uid: str) -> dict:
    rec = state["folders"].get(uid)
    if rec is None:
        rec = {
            "status": "pending",      # pending | zipping | uploading | done | failed
            "images": 0,
            "size_mb": 0.0,
            "cameras": {},
            "parts": {},              # name -> {size, link}
            "attempts": 0,
            "first_seen": datetime.now().isoformat(timespec="seconds"),
            "completed": None,
            "mailed": False,
            "last_error": None,
        }
        state["folders"][uid] = rec
    return rec


# ═══════════════════════════════════════════════════════════════════════════════
# SCAN / ARCHIVE  (images copied byte-for-byte)
# ═══════════════════════════════════════════════════════════════════════════════

def scan_folder(session_dir: str):
    """Return (images, total_bytes, per_camera_counts, newest_mtime)."""
    images, total, cams, newest = [], 0, {}, 0.0
    for root, _dirs, files in os.walk(session_dir):
        cam = os.path.basename(root)
        for fname in sorted(files):
            if not fname.lower().endswith(IMG_EXTS):
                continue
            src = os.path.join(root, fname)
            try:
                st = os.stat(src)
            except OSError:
                continue
            images.append((src, os.path.relpath(src, session_dir), st.st_size))
            total += st.st_size
            cams[cam] = cams.get(cam, 0) + 1
            newest = max(newest, st.st_mtime)
    return images, total, cams, newest


def is_quiet(session_dir: str) -> bool:
    _i, _b, _c, newest = scan_folder(session_dir)
    if newest == 0:
        return False
    return (time.time() - newest) > (QUIET_MINUTES * 60)


def archive_folder(uid: str, rec: dict) -> list[str]:
    """
    Zip the session into <= PART_SIZE_MB parts inside WORK_DIR, unmodified.
    Existing parts from an interrupted run are reused. Returns part paths.
    """
    session_dir = os.path.join(RAW_PATH, uid)
    images, total_bytes, cams, _newest = scan_folder(session_dir)
    if not images:
        return []

    rec["images"]  = len(images)
    rec["size_mb"] = round(total_bytes / (1024 * 1024), 2)
    rec["cameras"] = cams

    os.makedirs(WORK_DIR, exist_ok=True)
    limit = PART_SIZE_MB * 1024 * 1024
    split = total_bytes > limit

    def part_path(i: int) -> str:
        name = f"{uid}_raw.zip" if not split else f"{uid}_raw_part{i:03d}.zip"
        return os.path.join(WORK_DIR, name)

    # Restart-safe: reuse parts already built by an interrupted run
    if rec.get("zip_complete"):
        existing = [os.path.join(WORK_DIR, n) for n in sorted(rec.get("parts", {}))]
        # Parts already uploaded were deleted - only missing+unuploaded is a problem
        if all(os.path.exists(p) or rec["parts"][os.path.basename(p)].get("link")
               for p in existing):
            log(f"{uid}: reusing {len(existing)} zip part(s) from the previous run")
            return [p for p in existing if os.path.exists(p)]

    log(f"{uid}: archiving {len(images)} images ({rec['size_mb']} MB)")
    parts, idx, part_bytes = [], 1, 0
    zf = zipfile.ZipFile(part_path(idx), "w", zipfile.ZIP_STORED, allowZip64=True)
    parts.append(part_path(idx))

    try:
        for src, arcname, size in images:
            if split and part_bytes > 0 and (part_bytes + size) > limit:
                zf.close()
                idx += 1
                zf = zipfile.ZipFile(part_path(idx), "w", zipfile.ZIP_STORED, allowZip64=True)
                parts.append(part_path(idx))
                part_bytes = 0
            try:
                zf.write(src, arcname)      # ZIP_STORED -> verbatim bytes
                part_bytes += size
            except Exception:
                logger.error(f"[TRAIN_SYNC] {uid}: skipped {src}:\n{traceback.format_exc()}")
    finally:
        zf.close()

    parts = [p for p in parts if os.path.exists(p) and os.path.getsize(p) > 0]
    rec["parts"] = {
        os.path.basename(p): {"size": os.path.getsize(p), "link": None} for p in parts
    }
    rec["zip_complete"] = True
    log(f"{uid}: {len(parts)} zip part(s) ready")
    return parts


# ═══════════════════════════════════════════════════════════════════════════════
# MAIL  (one per folder, via the project's Azure helper)
# ═══════════════════════════════════════════════════════════════════════════════

def send_folder_mail(uid: str, rec: dict) -> bool:
    if not RECIPIENTS:
        logger.error("[TRAIN_SYNC] ALERT_RECIPIENTS is empty - mail not sent")
        return False

    parts = rec.get("parts", {})
    links, missing = [], []
    for name in sorted(parts):
        size_mb = parts[name].get("size", 0) / (1024 * 1024)
        link = parts[name].get("link")
        if link:
            links.append(f"  {name}  ({size_mb:.1f} MB)\n    {link}")
        else:
            missing.append(f"  {name}  ({size_mb:.1f} MB)  - NOT UPLOADED")

    cams = rec.get("cameras", {})
    cam_lines = "\n".join(f"  {c:<6} {n} images" for c, n in sorted(cams.items())) or "  -"
    status = "COMPLETE" if not missing else "PARTIAL"

    body = (
        f"TRAINING DATA SYNCED ({status})\n\n"
        f"Session folder : {uid}\n"
        f"Images         : {rec.get('images', 0)}\n"
        f"Size on disk   : {rec.get('size_mb', 0)} MB\n"
        f"Zip parts      : {len(parts)}\n"
        f"Synced at      : {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
        f"Per camera:\n{cam_lines}\n\n"
        "Images are the original camera frames - full resolution, unmodified.\n\n"
        "Download link(s):\n" + ("\n".join(links) if links else "  none") + "\n"
    )
    if missing:
        body += ("\nThese parts failed to upload and remain on the sampling PC:\n"
                 + "\n".join(missing)
                 + f"\n\nLocal folder: {os.path.join(RAW_PATH, uid)}\n")

    try:
        return azure_send_mail(
            f"[COAL SAMPLING DHAR] Training data - {uid} ({status})",
            body,
            RECIPIENTS,
        )
    except Exception:
        logger.error(f"[TRAIN_SYNC] {uid}: mail failed:\n{traceback.format_exc()}")
        return False


# ═══════════════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════════════

def in_upload_window() -> bool:
    if not UPLOAD_WINDOW:
        return True
    try:
        start_s, end_s = UPLOAD_WINDOW.split("-")
        now   = datetime.now().time()
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end   = datetime.strptime(end_s.strip(), "%H:%M").time()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end          # window crosses midnight
    except Exception:
        logger.warning(f"[TRAIN_SYNC] Bad window '{UPLOAD_WINDOW}' - ignoring")
        return True


def candidate_folders(state: dict, only_uid: str | None = None) -> list[str]:
    if not os.path.isdir(RAW_PATH):
        logger.warning(f"[TRAIN_SYNC] RAW_IMG not found: {RAW_PATH}")
        return []

    out = []
    for name in sorted(os.listdir(RAW_PATH)):
        path = os.path.join(RAW_PATH, name)
        if not os.path.isdir(path) or name.startswith("_"):
            continue
        if only_uid:
            if name == only_uid:
                out.append(name)
            continue

        rec = state["folders"].get(name)
        if rec:
            if rec.get("status") == "done" and rec.get("mailed"):
                continue
            if rec.get("attempts", 0) >= MAX_FOLDER_ATTEMPTS:
                continue
        out.append(name)
    return out


def process_folder(uid: str, state: dict) -> bool:
    session_dir = os.path.join(RAW_PATH, uid)
    rec = folder_record(state, uid)

    if not is_quiet(session_dir):
        log(f"{uid}: still active (quiet period {QUIET_MINUTES} min) - skipping")
        return False

    rec["attempts"] = rec.get("attempts", 0) + 1
    rec["status"] = "zipping"
    save_state(state)

    try:
        parts = archive_folder(uid, rec)
    except Exception:
        rec["status"] = "failed"
        rec["last_error"] = "archive failed"
        logger.error(f"[TRAIN_SYNC] {uid}: archive failed:\n{traceback.format_exc()}")
        save_state(state)
        return False

    if not parts and not rec.get("parts"):
        rec.update({"status": "done", "mailed": True, "last_error": "no images",
                    "completed": datetime.now().isoformat(timespec="seconds")})
        log(f"{uid}: no images - marked done")
        save_state(state)
        return True

    rec["status"] = "uploading"
    save_state(state)

    all_ok = True
    for part in parts:
        name  = os.path.basename(part)
        pinfo = rec["parts"].setdefault(name, {"size": os.path.getsize(part), "link": None})

        if pinfo.get("link"):
            continue                                   # already uploaded

        if not in_upload_window():
            log("Outside the upload window - pausing until the next pass")
            save_state(state)
            return False

        log(f"{uid}: uploading {name} ({pinfo['size'] / (1024*1024):.1f} MB)")
        try:
            link = azure_upload_file(part)
        except Exception:
            logger.error(f"[TRAIN_SYNC] {name}: upload failed:\n{traceback.format_exc()}")
            link = None

        if link:
            pinfo["link"] = link
            save_state(state)                          # never re-upload this part
            try:
                os.remove(part)                        # zip copy only; originals kept
            except OSError:
                pass
        else:
            all_ok = False
            rec["last_error"] = f"upload failed: {name}"
            log(f"{uid}: {name} failed - will retry on the next pass")

    if not rec.get("mailed"):
        if send_folder_mail(uid, rec):
            rec["mailed"] = True

    rec["status"] = "done" if all_ok else "failed"
    if all_ok:
        rec["completed"] = datetime.now().isoformat(timespec="seconds")
        rec["last_error"] = None
        log(f"{uid}: SYNC COMPLETE ({rec['images']} images, {rec['size_mb']} MB)")
    save_state(state)
    return all_ok


def sync_once(only_uid: str | None = None):
    """One scan+sync pass. Never raises."""
    try:
        state   = load_state()
        folders = candidate_folders(state, only_uid)
        if not folders:
            log("Nothing to sync")
            return

        log(f"{len(folders)} folder(s) to sync: {', '.join(folders)}")
        for uid in folders:
            try:
                process_folder(uid, state)
            except Exception:
                logger.error(f"[TRAIN_SYNC] {uid}: unexpected error:\n{traceback.format_exc()}")
                rec = folder_record(state, uid)
                rec["status"] = "failed"
                rec["last_error"] = "unexpected error"
                save_state(state)
    except Exception:
        logger.error(f"[TRAIN_SYNC] Pass failed:\n{traceback.format_exc()}")


def run_training_sync():
    """
    Thread entry point used by DATA_SYNC.py.
    Loops forever; every failure is contained so the thread never dies.
    """
    log(f"Training data sync started | source={RAW_PATH}")
    log(f"state={STATE_FILE} | part={PART_SIZE_MB} MB | quiet={QUIET_MINUTES} min"
        + (f" | window={UPLOAD_WINDOW}" if UPLOAD_WINDOW else ""))

    while True:
        try:
            sync_once()
        except Exception:
            logger.error(f"[TRAIN_SYNC] Loop error:\n{traceback.format_exc()}")
        time.sleep(INTERVAL_MIN * 60)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI  (manual collection, independent of DATA_SYNC)
# ═══════════════════════════════════════════════════════════════════════════════

def print_status():
    state = load_state()
    folders = state.get("folders", {})
    if not folders:
        print("No folders recorded yet.")
        return

    print(f"\nState file  : {STATE_FILE}")
    print(f"Last updated: {state.get('updated')}\n")
    print(f"{'FOLDER':<24} {'STATUS':<10} {'IMAGES':>7} {'MB':>9} {'PARTS':>8} {'MAILED':>7}")
    print("-" * 72)
    for uid in sorted(folders):
        r = folders[uid]
        parts = r.get("parts", {})
        done  = sum(1 for p in parts.values() if p.get("link"))
        print(f"{uid:<24} {r.get('status','?'):<10} {r.get('images',0):>7} "
              f"{r.get('size_mb',0):>9} {f'{done}/{len(parts)}':>8} "
              f"{'yes' if r.get('mailed') else 'no':>7}")
        if r.get("last_error"):
            print(f"    last error: {r['last_error']}")
    print()


def retry_failed():
    state = load_state()
    n = 0
    for _uid, rec in state.get("folders", {}).items():
        if rec.get("status") == "failed" or rec.get("attempts", 0) >= MAX_FOLDER_ATTEMPTS:
            rec.update({"attempts": 0, "status": "pending", "last_error": None})
            n += 1
    save_state(state)
    print(f"Reset {n} folder(s) for retry.")


def main():
    ap = argparse.ArgumentParser(description="Training data sync (raw images)")
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--uid", help="sync one specific session folder")
    ap.add_argument("--status", action="store_true", help="show state summary")
    ap.add_argument("--retry-failed", action="store_true", help="clear failures and retry")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    if args.status:       print_status();  return
    if args.retry_failed: retry_failed();  return
    if args.once or args.uid:
        sync_once(args.uid)
        return
    run_training_sync()


if __name__ == "__main__":
    main()