"""
SCRIPTS/AUTO_CLEANUP.py

Daily disk cleanup for the sampling PC. Runs as its own service tab in
SERVICE_MANAGER/MANAGE.py and, once a day at AUTO_CLEANUP_TIME (default
23:00, PC local time), deletes files older than AUTO_CLEANUP_DAYS (default 7)
from:

    RESULT/        image files only (.jpg/.jpeg/.png/.bmp) - everything else stays
    INF/           all files except PDFs
    RAW_IMG/       all files except PDFs (camera frames and raw-image zips)
    RAW_SYNC_TMP/  all files except PDFs (TRAINING_SYNC zip parts)

Rules
  * PDFs are NEVER deleted, in any folder.
  * Age only: a file goes when its last-modified time is older than the cutoff.
    Sync state (RAW_SYNC_STATE.json, Azure/OneDrive status) is not consulted.
  * Database rows are not touched. Dashboard links to deleted images will
    simply show no image.
  * Folders left empty after the sweep are removed (the four roots are kept).
  * No MQTT, no DB, no contact with MAIN_MANAGER / PLC / cameras: it only
    reads and deletes files, so it can fail or be stopped without touching
    the sampling flow.
  * A file that is locked (Windows PermissionError) is skipped and retried on
    the next day's run.

Manual use (does not need the service running):
    python AUTO_CLEANUP.py --dry-run     # list what would go, delete nothing
    python AUTO_CLEANUP.py --now         # run one cleanup now and exit
    python AUTO_CLEANUP.py --now --days 14
"""

import os
import json
import time
import logging
import argparse
import traceback
from datetime import datetime, timedelta

from DEPENDANT.LOGGING import initializeLogger

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════════════

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
LOGS_PATH      = os.path.join(BASE_FILE_PATH, "LOGS")
STATE_FILE     = os.path.join(BASE_FILE_PATH, "AUTO_CLEANUP_STATE.json")

CLEANUP_TIME   = os.getenv("AUTO_CLEANUP_TIME", "23:00")
try:
    KEEP_DAYS = int(os.getenv("AUTO_CLEANUP_DAYS", "7"))
except ValueError:
    KEEP_DAYS = 7

POLL_SECONDS   = 30
RETRY_MINUTES  = 30

IMG_EXTS = (".jpg", ".jpeg", ".png", ".bmp")
PDF_EXTS = (".pdf",)

# folder name -> True = delete images only, False = delete everything but PDFs
TARGETS = {
    "RESULT":       True,
    "INF":          False,
    "RAW_IMG":      False,
    "RAW_SYNC_TMP": False,
}

logger = initializeLogger("AUTO_CLEANUP", LOGS_PATH=LOGS_PATH)


def _log(msg: str, level: str = "info"):
    print(f"[AUTO_CLEANUP] {msg}")
    if logger:
        getattr(logger, level, logger.info)(msg)


# ═══════════════════════════════════════════════════════════════════════════════
# STATE FILE (remembers the last day a cleanup ran)
# ═══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        _log(f"State file unreadable, starting fresh:\n{traceback.format_exc()}", "warning")
        return {}


def _save_state(state: dict):
    """Atomic write - a power cut can never leave a half-written state file."""
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        _log(f"Could not save state:\n{traceback.format_exc()}", "error")


# ═══════════════════════════════════════════════════════════════════════════════
# CLEANUP
# ═══════════════════════════════════════════════════════════════════════════════

def _deletable(filename: str, images_only: bool) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    if ext in PDF_EXTS:
        return False
    if images_only:
        return ext in IMG_EXTS
    return True


def _remove_empty_dirs(root: str, dry_run: bool) -> int:
    """Remove empty sub-folders bottom-up. The root itself is kept."""
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        if os.path.normcase(os.path.abspath(dirpath)) == os.path.normcase(os.path.abspath(root)):
            continue
        try:
            if not os.listdir(dirpath):
                if not dry_run:
                    os.rmdir(dirpath)
                removed += 1
        except OSError:
            pass    # not empty any more, or locked - leave it
    return removed


def clean_folder(name: str, images_only: bool, cutoff_ts: float, dry_run: bool = False) -> dict:
    root = os.path.join(BASE_FILE_PATH, name)
    stats = {"files": 0, "bytes": 0, "errors": 0, "dirs": 0}

    if not os.path.isdir(root):
        _log(f"{name}: folder not found, skipped ({root})", "warning")
        return stats

    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fn in filenames:
            if not _deletable(fn, images_only):
                continue
            path = os.path.join(dirpath, fn)
            try:
                if os.path.islink(path):
                    continue
                st = os.stat(path)
                if st.st_mtime >= cutoff_ts:
                    continue
                if dry_run:
                    _log(f"[DRY-RUN] would delete {path}", "debug")
                else:
                    os.remove(path)
                stats["files"] += 1
                stats["bytes"] += st.st_size
            except FileNotFoundError:
                pass
            except Exception as e:
                stats["errors"] += 1
                _log(f"{name}: could not delete {path}: {e}", "warning")

    stats["dirs"] = _remove_empty_dirs(root, dry_run)
    return stats


def run_cleanup(days: int = KEEP_DAYS, dry_run: bool = False) -> bool:
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()
    tag = "[DRY-RUN] " if dry_run else ""
    _log(f"{tag}Cleanup start - removing files older than {days} days "
         f"(modified before {cutoff.strftime('%Y-%m-%d %H:%M')})")

    total = {"files": 0, "bytes": 0, "errors": 0, "dirs": 0}
    for name, images_only in TARGETS.items():
        t0 = time.time()
        try:
            s = clean_folder(name, images_only, cutoff_ts, dry_run)
        except Exception:
            _log(f"{name}: cleanup failed:\n{traceback.format_exc()}", "error")
            total["errors"] += 1
            continue
        for k in total:
            total[k] += s[k]
        _log(f"{tag}{name}: {s['files']} files ({s['bytes'] / 1024**2:.1f} MB), "
             f"{s['dirs']} empty folders, {s['errors']} errors, {time.time() - t0:.1f}s")

    _log(f"{tag}Cleanup done - {total['files']} files, "
         f"{total['bytes'] / 1024**3:.2f} GB freed, {total['dirs']} empty folders removed, "
         f"{total['errors']} errors")
    return total["errors"] == 0


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ═══════════════════════════════════════════════════════════════════════════════

def _cleanup_time_today(now: datetime) -> datetime:
    try:
        hh, mm = (int(x) for x in CLEANUP_TIME.strip().split(":")[:2])
    except Exception:
        hh, mm = 23, 0
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def scheduler_loop():
    _log(f"Started - daily cleanup at {CLEANUP_TIME}, keeping {KEEP_DAYS} days, "
         f"folders: {', '.join(TARGETS)}")
    next_retry = None

    while True:
        try:
            now   = datetime.now()
            today = now.date().isoformat()
            state = _load_state()
            due   = now >= _cleanup_time_today(now)
            done  = state.get("last_run_for") == today

            if due and not done and (next_retry is None or now >= next_retry):
                ok = run_cleanup(KEEP_DAYS)
                # Mark the day done even with a few locked files - they are
                # picked up tomorrow. Only a crash in the run itself retries.
                state["last_run_for"] = today
                state["ran_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                state["clean"] = ok
                _save_state(state)
                next_retry = None

        except Exception:
            _log(f"Scheduler error:\n{traceback.format_exc()}", "error")
            next_retry = datetime.now() + timedelta(minutes=RETRY_MINUTES)

        time.sleep(POLL_SECONDS)


def main():
    ap = argparse.ArgumentParser(description="Daily disk cleanup for coal sampling")
    ap.add_argument("--now", action="store_true", help="run one cleanup now and exit")
    ap.add_argument("--dry-run", action="store_true", help="list what would be deleted, delete nothing")
    ap.add_argument("--days", type=int, default=KEEP_DAYS, help=f"age limit in days (default {KEEP_DAYS})")
    args = ap.parse_args()

    if args.days < 1:
        ap.error("--days must be at least 1")

    if args.now or args.dry_run:
        ok = run_cleanup(args.days, dry_run=args.dry_run)
        raise SystemExit(0 if ok else 1)

    scheduler_loop()


if __name__ == "__main__":
    main()
