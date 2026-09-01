"""
SCRIPTS/DEPENDANT/PDF_UPLOAD_WORKER.py

Background PDF upload queue, fully isolated from the sampling cycle.

Design:
  - enqueue_upload(vehicle) returns immediately. The caller already knows the
    final URL (deterministic, via BLOB_NAMING.blob_url_for), so it does NOT
    wait for this.
  - A single daemon worker thread processes the queue. Each job is retried
    with backoff for up to MAX_RETRY_HOURS (default 24h / 1 day).
  - On success: VEHICLE_MASTER.PDF_URL is set and IS_PDF_SYNCED = 1.
  - If a job never succeeds within the window: VEHICLE_MASTER.IS_PDF_SYNCED = 0
    (marked unsynced) and the job is dropped. The weekly sync will retry it.

No MQTT, no contact with the manager/sampler beyond being imported. Pure
background threads behind a queue.

One-time schema (run once):
  ALTER TABLE VEHICLE_MASTER ADD COLUMN IS_PDF_SYNCED TINYINT(1) DEFAULT 0;
  (PDF_URL column already added earlier.)

Env:
  PDF_UPLOAD_MAX_RETRY_HOURS   default 24
  PDF_UPLOAD_RETRY_BASE_SEC    default 30   (backoff base)
  PDF_UPLOAD_RETRY_MAX_SEC     default 900  (cap between attempts: 15 min)
"""

import os
import time
import queue
import threading
import traceback
import logging
from datetime import datetime

import pymysql

from DEPENDANT.PDF_UTILS import create_and_upload_vehicle_pdf, _db_connect

logger = logging.getLogger(__name__)

MAX_RETRY_HOURS = float(os.getenv("PDF_UPLOAD_MAX_RETRY_HOURS", "24"))
RETRY_BASE_SEC  = float(os.getenv("PDF_UPLOAD_RETRY_BASE_SEC", "30"))
RETRY_MAX_SEC   = float(os.getenv("PDF_UPLOAD_RETRY_MAX_SEC", "900"))

_job_queue: "queue.Queue[dict]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_inflight = set()               # vehicle numbers currently queued/processing
_inflight_lock = threading.Lock()


# =====================================================================
# DB helpers
# =====================================================================

def _mark_synced(rfid: str, url: str) -> None:
    db = _db_connect()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE VEHICLE_MASTER SET PDF_URL=%s, IS_PDF_SYNCED=1 WHERE RFID=%s",
            (url, rfid),
        )
        db.commit()
    finally:
        db.close()


def _mark_unsynced(rfid: str) -> None:
    db = _db_connect()
    try:
        cur = db.cursor()
        cur.execute(
            "UPDATE VEHICLE_MASTER SET IS_PDF_SYNCED=0 WHERE RFID=%s",
            (rfid,),
        )
        db.commit()
    finally:
        db.close()


# =====================================================================
# Worker
# =====================================================================

def _process_job(job: dict) -> None:
    """
    Retry create+upload for one vehicle until success or the 1-day deadline.
    Runs on the worker thread; blocking here does not affect the cycle.
    """
    vehicle   = job["vehicle"]
    rfid      = vehicle.get("RFID")
    vn        = vehicle.get("VEHICLE_NUMBER")
    deadline  = job["deadline"]
    attempt   = 0

    while True:
        attempt += 1
        try:
            url = create_and_upload_vehicle_pdf(vehicle)   # build+encrypt+upload
            if rfid:
                _mark_synced(rfid, url)
            logger.info(f"[UPLOAD-WORKER] Synced {vn} on attempt {attempt} -> {url}")
            return
        except Exception:
            now = time.time()
            if now >= deadline:
                logger.error(
                    f"[UPLOAD-WORKER] GAVE UP on {vn} after {attempt} attempts "
                    f"(>{MAX_RETRY_HOURS}h). Marking unsynced.\n{traceback.format_exc()}"
                )
                if rfid:
                    try:
                        _mark_unsynced(rfid)
                    except Exception:
                        logger.error(f"[UPLOAD-WORKER] failed to mark unsynced:\n{traceback.format_exc()}")
                return

            # exponential backoff, capped, but never past the deadline
            wait = min(RETRY_MAX_SEC, RETRY_BASE_SEC * (2 ** min(attempt - 1, 8)))
            wait = min(wait, max(1.0, deadline - now))
            logger.warning(
                f"[UPLOAD-WORKER] {vn} attempt {attempt} failed; retrying in "
                f"{int(wait)}s (deadline in {int(deadline - now)}s)"
            )
            time.sleep(wait)


def _worker_loop() -> None:
    logger.info("[UPLOAD-WORKER] Started.")
    while True:
        job = _job_queue.get()
        vn = job["vehicle"].get("VEHICLE_NUMBER")
        try:
            _process_job(job)
        except Exception:
            logger.error(f"[UPLOAD-WORKER] unexpected error on {vn}:\n{traceback.format_exc()}")
        finally:
            with _inflight_lock:
                _inflight.discard(vn)
            _job_queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            t = threading.Thread(target=_worker_loop, name="pdf-upload-worker", daemon=True)
            t.start()
            _worker_started = True


# =====================================================================
# Public API
# =====================================================================

def enqueue_upload(vehicle: dict) -> None:
    """
    Queue a vehicle's PDF for background upload. Returns immediately.
    vehicle needs: VEHICLE_NUMBER, VENDOR_CODE, VENDER_NAME, RFID.
    Deduped by vehicle number so repeated arrivals don't pile up duplicates.
    """
    vn = vehicle.get("VEHICLE_NUMBER")
    if not vn:
        logger.warning("[UPLOAD-WORKER] enqueue skipped: no vehicle number")
        return

    _ensure_worker()

    with _inflight_lock:
        if vn in _inflight:
            logger.info(f"[UPLOAD-WORKER] {vn} already queued/in-flight; skipping enqueue")
            return
        _inflight.add(vn)

    job = {
        "vehicle":  vehicle,
        "deadline": time.time() + MAX_RETRY_HOURS * 3600.0,
        "queued_at": datetime.now().isoformat(timespec="seconds"),
    }
    _job_queue.put(job)
    logger.info(f"[UPLOAD-WORKER] Enqueued {vn} (retry window {MAX_RETRY_HOURS}h)")