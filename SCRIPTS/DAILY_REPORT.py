"""
SCRIPTS/DAILY_REPORT.py

Daily COAL SAMPLING REPORT for the Insightzz team.

Every morning at INSIGHTZZ_REPORT_TIME (default 08:00, PC local time) this
service builds the previous day's report in the same layout as the manual
sheet and mails it as a CSV:

    COAL SAMPLING REPORT
    Sr.No. | Date | vendor code | Vehicle Number | Vendor Name | Cycle Status |
    Vendor mismatch | Hard Material | AI Block | TP/FP | Remark |
    Cycle Start | Cycle Time | UID                     <- appended, for tracing

One row per vehicle session (VEHICLE_LOGS row). The issue columns and the
Remark are produced by DEPENDANT/REPORT_UTILS.py - the same code that writes
the end-of-session vehicle mail - so the two always agree:

    Vendor mismatch : "Vendor mismatch"   remark: "Vendor on record <name> (<code>)"
    Hard Material   : "Hard Material"     remark: "1 Failed (hard material) - A2 @10:14:05"
    AI Block        : "AI Block"          remark: "1 Failed (auger position could not be confirmed) - A1 @..."
    TP/FP           : left blank for the Insightzz review

Run as a service (registered in SERVICE_MANAGER/MANAGE.py as "Daily Report"):
    python DAILY_REPORT.py

Manual / backfill runs (no scheduler):
    python DAILY_REPORT.py --date 2026-10-27            build + mail one day
    python DAILY_REPORT.py --from 2026-10-01 --to 2026-10-27 --no-mail
    python DAILY_REPORT.py --now                        yesterday, right now

Env:
    INSIGHTZZ_RECIPIENTS   "a@insightzz.com,b@insightzz.com"
                           (falls back to ALERT_RECIPIENTS with a warning)
    INSIGHTZZ_REPORT_TIME  "08:00"
    DB_HOST / DB_USER / DB_PASSWORD / DB_NAME, AZURE_* (as every service)

A state file (REPORTS/DAILY/DAILY_REPORT_STATE.json) records the last day
mailed, so a restart never sends a day twice and a PC that was off at 08:00
sends the missed report as soon as the service comes back up. A failed mail
is retried every 15 minutes; the CSV is always written to REPORTS/DAILY first.
"""

import os
import csv
import json
import time
import argparse
import logging
import traceback
from datetime import datetime, timedelta, date

import pymysql

from DEPENDANT.LOGGING import initializeLogger
from DEPENDANT.PDF_UTILS import _db_connect
from DEPENDANT.AZURE_SERVICE import azure_send_mail
from DEPENDANT.REPORT_UTILS import (
    parse_sample_info, summarize_session, remark_parts, find_vendor_mismatch,
    cycle_window, fmt_duration, html_table, esc, HTML_FONT,
)

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
LOGS_PATH      = os.path.join(BASE_FILE_PATH, "LOGS")
REPORT_DIR     = os.path.join(BASE_FILE_PATH, "REPORTS", "DAILY")
STATE_FILE     = os.path.join(REPORT_DIR, "DAILY_REPORT_STATE.json")

REPORT_TIME    = os.getenv("INSIGHTZZ_REPORT_TIME", "08:00")
SITE_TAG       = os.getenv("MAIL_SITE_TAG", "COAL SAMPLING DHAR")
RETRY_MINUTES  = 15
POLL_SECONDS   = 30

logger = initializeLogger("DAILY_REPORT", LOGS_PATH=LOGS_PATH)
# Route the shared modules' loggers (REPORT_UTILS, AZURE_SERVICE) into this file
_root = logging.getLogger()
if logger:
    for _h in logger.handlers:
        if _h not in _root.handlers:
            _root.addHandler(_h)
_root.setLevel(logging.INFO)

TITLE   = "COAL SAMPLING REPORT"
HEADERS = ["Sr.No.", "Date", "vendor code", "Vehicle Number", "Vendor Name",
           "Cycle Status", "Vendor mismatch", "Hard Material", "AI Block",
           "TP/FP", "Remark", "Cycle Start", "Cycle Time", "UID"]


def _log(msg: str, level: str = "info"):
    print(f"[DAILY_REPORT] {msg}")
    getattr(logger, level, logger.info)(msg)


def _recipients() -> list[str]:
    raw = os.getenv("INSIGHTZZ_RECIPIENTS", "")
    rec = [r.strip() for r in raw.split(",") if r.strip()]
    if not rec:
        rec = [r.strip() for r in os.getenv("ALERT_RECIPIENTS", "").split(",") if r.strip()]
        if rec:
            _log("INSIGHTZZ_RECIPIENTS not set - using ALERT_RECIPIENTS", "warning")
    return rec


# =====================================================================
# DATA
# =====================================================================

def fetch_sessions(day_from: date, day_to: date) -> list[dict]:
    """Every VEHICLE_LOGS session created between day_from and day_to (inclusive)."""
    start = datetime.combine(day_from, datetime.min.time())
    end   = datetime.combine(day_to + timedelta(days=1), datetime.min.time())

    db = _db_connect()
    try:
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT vl.ID, vl.UID, vl.RFIDS, vl.STATUS, vl.BUCKET_NO,
                   vl.ERROR_MESSAGE, vl.SAMPLE_INFO, vl.CREATE_TIME, vl.UPDATE_TIME,
                   vm.RFID, vm.VEHICLE_NUMBER, vm.VENDOR_CODE,
                   (SELECT vr.VENDER_NAME FROM VENDOR_MASTER vr
                     WHERE vr.VENDOR_CODE = vm.VENDOR_CODE LIMIT 1) AS VENDER_NAME
            FROM VEHICLE_LOGS vl
            LEFT JOIN VEHICLE_MASTER vm ON vm.RFID = vl.RFIDS
            WHERE vl.CREATE_TIME >= %s AND vl.CREATE_TIME < %s
            ORDER BY vl.CREATE_TIME ASC, vl.ID ASC
            """,
            (start, end),
        )
        rows = cur.fetchall() or []
    finally:
        db.close()

    seen, out = set(), []
    for r in rows:                       # duplicate RFID rows in the master
        if r["ID"] in seen:
            continue
        seen.add(r["ID"])
        out.append(r)
    return out


def build_report_rows(sessions: list[dict]) -> list[dict]:
    """Session rows -> report rows (dicts keyed by HEADERS)."""
    mismatch_cache: dict[tuple, dict | None] = {}
    out = []

    for s in sessions:
        info    = parse_sample_info(s.get("SAMPLE_INFO"))
        summary = summarize_session(info, s.get("STATUS"), s.get("ERROR_MESSAGE"))

        vn, vc, rfid = s.get("VEHICLE_NUMBER"), s.get("VENDOR_CODE"), s.get("RFID")
        key = (vn, vc, rfid)
        if vn and key not in mismatch_cache:
            mismatch_cache[key] = find_vendor_mismatch(vn, vc, rfid)
        mismatch = mismatch_cache.get(key) if vn else None

        remarks = remark_parts(summary, mismatch)
        if not vn:
            remarks.insert(0, f"RFID not found in VEHICLE_MASTER ({s.get('RFIDS') or '-'})")

        start, end = cycle_window(s.get("CREATE_TIME"), s.get("UPDATE_TIME"), info)
        n_ai, n_hm = len(summary["ai"]), len(summary["hard"])
        created = s.get("CREATE_TIME")

        out.append({
            "_date":           created.date() if isinstance(created, datetime) else None,
            "_issue":          bool(mismatch or n_ai or n_hm or summary["status"] in ("ERROR", "FAILED")),
            "Date":            created.strftime("%d-%m-%Y") if isinstance(created, datetime) else "",
            "vendor code":     vc or "",
            "Vehicle Number":  vn or "",
            "Vendor Name":     s.get("VENDER_NAME") or "",
            "Cycle Status":    summary["cycle_status"],
            "Vendor mismatch": "Vendor mismatch" if mismatch else "",
            "Hard Material":   ("Hard Material" + (f" x{n_hm}" if n_hm > 1 else "")) if n_hm else "",
            "AI Block":        ("AI Block" + (f" x{n_ai}" if n_ai > 1 else "")) if n_ai else "",
            "TP/FP":           "",
            "Remark":          "; ".join(remarks),
            "Cycle Start":     start.strftime("%H:%M:%S") if start else "",
            "Cycle Time":      fmt_duration(start, end),
            "UID":             s.get("UID") or "",
        })
    return out


def write_csv(rows: list[dict], path: str) -> str:
    """Same layout as the manual sheet: title, blank, header, a blank row between dates."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:   # BOM: Excel opens it cleanly
        w = csv.writer(f)
        w.writerow([TITLE])
        w.writerow([])
        w.writerow(HEADERS)
        prev_date, sr = None, 0
        for r in rows:
            if prev_date is not None and r["_date"] != prev_date:
                w.writerow([])
                sr = 0
            prev_date = r["_date"]
            sr += 1
            r["Sr.No."] = sr
            w.writerow([r.get(h, "") for h in HEADERS])
    return path


# =====================================================================
# MAIL
# =====================================================================

def _counts(rows: list[dict]) -> dict:
    return {
        "total":     len(rows),
        "success":   sum(1 for r in rows if r["Cycle Status"].startswith("Successful")),
        "manual":    sum(1 for r in rows if r["Cycle Status"] == "Successful (Manual)"),
        "failed":    sum(1 for r in rows if r["Cycle Status"] == "Failed"),
        "ai":        sum(1 for r in rows if r["AI Block"]),
        "hard":      sum(1 for r in rows if r["Hard Material"]),
        "mismatch":  sum(1 for r in rows if r["Vendor mismatch"]),
    }


def _mail_body(rows: list[dict], label: str, c: dict, csv_name: str) -> str:
    stats = html_table(
        ["Vehicles", "Successful", "of which Manual", "Failed",
         "AI Block", "Hard Material", "Vendor mismatch"],
        [[c["total"], c["success"], c["manual"], c["failed"],
          c["ai"], c["hard"], c["mismatch"]]],
    )
    flagged = [r for r in rows if r["_issue"]]
    if flagged:
        multi_day = len({r["_date"] for r in rows}) > 1
        cols = (["Sr.No."] + (["Date"] if multi_day else []) +
                ["Vehicle Number", "Vendor Name", "Cycle Status",
                 "Vendor mismatch", "Hard Material", "AI Block", "Remark", "UID"])
        detail = (f"<div style='margin:12px 0 2px 0;font-weight:700;'>Vehicles with issues "
                  f"({len(flagged)})</div>"
                  + html_table(cols, [[r.get(h, "") for h in cols] for r in flagged],
                               ["background:#fff3cd;"] * len(flagged)))
    else:
        detail = "<p>No vendor mismatch, AI block or hard material in this period.</p>"

    return (
        f'<div style="{HTML_FONT}">'
        f'<div style="font-size:16px;font-weight:700;margin-bottom:6px;">'
        f'{esc(TITLE)} - {esc(label)}</div>'
        + stats + detail +
        f'<p>The full report ({esc(csv_name)}) is attached in the sheet layout. '
        f'The <b>TP/FP</b> column is left blank for review. UID identifies the session '
        f'folder (RESULT\\&lt;date&gt;\\&lt;UID&gt;\\ATTEMPTS holds the AI-block and '
        f'hard-material pictures).</p>'
        f'<div style="font-size:11px;color:#6c757d;">Automated mail from {esc(SITE_TAG)} - '
        f'{datetime.now().strftime("%d/%m/%Y %H:%M:%S")}</div></div>'
    )


def run_report(day_from: date, day_to: date, send: bool = True) -> bool:
    """Build the CSV for the period and (optionally) mail it. True = done."""
    label = (day_from.strftime("%d-%m-%Y") if day_from == day_to
             else f"{day_from.strftime('%d-%m-%Y')} to {day_to.strftime('%d-%m-%Y')}")
    stem  = (day_from.strftime("%Y-%m-%d") if day_from == day_to
             else f"{day_from.strftime('%Y-%m-%d')}_to_{day_to.strftime('%Y-%m-%d')}")
    csv_path = os.path.join(REPORT_DIR, f"COAL_SAMPLING_REPORT_{stem}.csv")

    sessions = fetch_sessions(day_from, day_to)
    rows     = build_report_rows(sessions)
    write_csv(rows, csv_path)
    c = _counts(rows)
    _log(f"Report {label}: {c['total']} vehicles, AI block {c['ai']}, hard material "
         f"{c['hard']}, vendor mismatch {c['mismatch']} -> {csv_path}")

    if not send:
        return True

    recipients = _recipients()
    if not recipients:
        _log("No INSIGHTZZ_RECIPIENTS / ALERT_RECIPIENTS - report written, not mailed", "error")
        return False

    subject = (f"[{SITE_TAG}] Daily coal sampling report - {label} | {c['total']} vehicles, "
               f"{c['ai']} AI block, {c['hard']} hard material, {c['mismatch']} vendor mismatch")
    try:
        azure_send_mail(subject, _mail_body(rows, label, c, os.path.basename(csv_path)),
                        recipients, attachments=[csv_path], html=True)
        _log(f"Report {label} mailed to {recipients}")
        return True
    except Exception:
        _log(f"Mail failed for {label}:\n{traceback.format_exc()}", "error")
        return False


# =====================================================================
# SCHEDULER
# =====================================================================

def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    try:
        os.makedirs(REPORT_DIR, exist_ok=True)
        tmp = STATE_FILE + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:
        _log(f"Could not save state:\n{traceback.format_exc()}", "error")


def _report_time_today(now: datetime) -> datetime:
    try:
        hh, mm = (int(x) for x in REPORT_TIME.strip().split(":")[:2])
    except Exception:
        hh, mm = 8, 0
    return now.replace(hour=hh, minute=mm, second=0, microsecond=0)


def scheduler_loop():
    _log(f"Started - daily report at {REPORT_TIME}, state file {STATE_FILE}")
    next_retry = None

    while True:
        try:
            now   = datetime.now()
            day   = (now - timedelta(days=1)).date()        # report covers yesterday
            state = _load_state()
            due   = now >= _report_time_today(now)
            done  = state.get("last_sent_for") == day.isoformat()

            if due and not done and (next_retry is None or now >= next_retry):
                ok = run_report(day, day, send=True)
                if ok:
                    state["last_sent_for"] = day.isoformat()
                    state["sent_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                    _save_state(state)
                    next_retry = None
                else:
                    next_retry = now + timedelta(minutes=RETRY_MINUTES)
                    _log(f"Will retry at {next_retry.strftime('%H:%M')}", "warning")

        except Exception:
            _log(f"Scheduler error:\n{traceback.format_exc()}", "error")
            next_retry = datetime.now() + timedelta(minutes=RETRY_MINUTES)

        time.sleep(POLL_SECONDS)


def main():
    ap = argparse.ArgumentParser(description="Daily coal sampling report")
    ap.add_argument("--date", help="report one day (YYYY-MM-DD) now and exit")
    ap.add_argument("--from", dest="date_from", help="period start (YYYY-MM-DD)")
    ap.add_argument("--to", dest="date_to", help="period end (YYYY-MM-DD), default = --from")
    ap.add_argument("--now", action="store_true", help="report yesterday now and exit")
    ap.add_argument("--no-mail", action="store_true", help="write the CSV only")
    args = ap.parse_args()

    parse = lambda s: datetime.strptime(s, "%Y-%m-%d").date()

    if args.date or args.date_from or args.now:
        if args.date:
            d0 = d1 = parse(args.date)
        elif args.date_from:
            d0 = parse(args.date_from)
            d1 = parse(args.date_to) if args.date_to else d0
        else:
            d0 = d1 = (datetime.now() - timedelta(days=1)).date()
        ok = run_report(d0, d1, send=not args.no_mail)
        raise SystemExit(0 if ok else 1)

    scheduler_loop()


if __name__ == "__main__":
    main()
