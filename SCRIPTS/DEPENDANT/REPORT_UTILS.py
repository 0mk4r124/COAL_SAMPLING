"""
SCRIPTS/DEPENDANT/REPORT_UTILS.py

Shared building blocks for the two reporting paths:

  * the ONE end-of-session mail per vehicle   (EMAIL_ALERTS.send_vehicle_summary_mail)
  * the daily 08:00 CSV report for Insightzz  (SCRIPTS/DAILY_REPORT.py)

Both must describe a session the same way, so the issue detection, the remark
wording and the vendor-mismatch rule live here once.

Nothing in this module writes to the database. Every DB call opens and closes
its own connection (DEPENDANT.PDF_UTILS._db_connect, env-configured), so it is
safe to call from the mail threads of MAIN_MANAGER and from the report service.

Vendor-mismatch rule
--------------------
The "vendor on record" is the ORIGINAL registration of the vehicle number:
the oldest VEHICLE_MASTER row (lowest ID) carrying it. A session is a VENDOR
MISMATCH only when it arrived under a different, later registration whose
vendor really differs from that original one:

  * the vehicle arriving under its original registration -> not a mismatch
  * a later registration with the same vendor code      -> not a mismatch
  * a later registration under the same company name
    (duplicate vendor code, case/punctuation ignored)    -> not a mismatch
  * a later registration under another company          -> MISMATCH
"""

import os
import json
import html
import shutil
import logging
import traceback
from datetime import datetime

import pymysql

from DEPENDANT.PDF_UTILS import _db_connect

logger = logging.getLogger(__name__)

# How the attempt reasons read in a remark / mail
REASON_TEXT = {
    "AI_OBSTRUCTION":   "auger position could not be confirmed",
    "HARD_MATERIAL":    "hard material",
    "POSITION_TIMEOUT": "auger did not confirm position",
    "CYCLE_ERROR":      "PLC cycle error",
    "CYCLE_TIMEOUT":    "sample cycle timed out",
}

# Mail image handling
MAIL_IMG_MAX_WIDTH = int(os.getenv("MAIL_IMG_MAX_WIDTH", "960"))
MAIL_IMG_QUALITY   = int(os.getenv("MAIL_IMG_QUALITY", "70"))


# =====================================================================
# SMALL PARSERS
# =====================================================================

def parse_sample_info(raw) -> dict:
    """SAMPLE_INFO column -> dict. Never raises."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except Exception:
        return {}


def parse_stamp(value) -> datetime | None:
    """datetime / 'YYYY-mm-dd HH:MM:SS' string -> naive datetime, or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def cycle_window(create_time, update_time, info: dict | None) -> tuple:
    """
    (start, end) for one session - same rule as the dashboard History tab
    (views._cycle_window): the manager's cycle_started / cycle_ended marks,
    falling back to CREATE_TIME / UPDATE_TIME for older rows.
    """
    info = info if isinstance(info, dict) else {}
    start = parse_stamp(info.get("cycle_started")) or parse_stamp(create_time)
    end   = parse_stamp(info.get("cycle_ended"))   or parse_stamp(update_time)
    return start, end


def fmt_duration(start, end) -> str:
    """MM:SS, blank when either end is missing or the order is wrong."""
    if not start or not end or end < start:
        return ""
    secs = int((end - start).total_seconds())
    return f"{secs // 60:02d}:{secs % 60:02d}"


def fmt_dt(value, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    dt = parse_stamp(value)
    return dt.strftime(fmt) if dt else ""


def _attempt_time(a: dict) -> str:
    dt = parse_stamp(a.get("time"))
    return dt.strftime("%H:%M:%S") if dt else ""


def _norm_number(vn) -> str:
    return "".join(str(vn or "").split()).upper()


def _norm_name(name) -> str:
    """Vendor name for comparison: case, spaces and punctuation ignored."""
    return "".join(ch for ch in str(name or "").upper() if ch.isalnum())


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


# =====================================================================
# VENDOR MISMATCH
# =====================================================================

def find_vendor_mismatch(vehicle_number: str, vendor_code: str,
                         rfid: str | None = None) -> dict | None:
    """
    Apply the vendor-mismatch rule (module docstring) for one arrival.

    Returns None when there is no mismatch, otherwise:
        {
          "vehicle_number":  "HP12T9255",
          "current_vendor":  "KAPIL ENTERPRISES",
          "current_code":    "748996",
          "on_record": [ {"vendor_name": "ANAND BUILDERS",
                          "vendor_code": "2122727",
                          "registered":  datetime | None}, ... ]   oldest first
        }
    Never raises - a DB problem means "unknown", reported as no mismatch.
    """
    if not vehicle_number:
        return None
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT vm.ID, vm.RFID, vm.VEHICLE_NUMBER, vm.VENDOR_CODE, vm.CREATE_TIME,
                   (SELECT vr.VENDER_NAME FROM VENDOR_MASTER vr
                     WHERE vr.VENDOR_CODE = vm.VENDOR_CODE LIMIT 1) AS VENDER_NAME
            FROM VEHICLE_MASTER vm
            WHERE UPPER(REPLACE(vm.VEHICLE_NUMBER, ' ', '')) = %s
            ORDER BY vm.ID ASC
            """,
            (_norm_number(vehicle_number),),
        )
        rows = cur.fetchall() or []
    except Exception:
        logger.error(f"[REPORT] find_vendor_mismatch failed:\n{traceback.format_exc()}")
        return None
    finally:
        if db:
            db.close()

    if not rows:
        return None

    code = str(vendor_code or "")
    current = None
    if rfid:
        current = next((r for r in rows if str(r.get("RFID") or "") == str(rfid)), None)
    if current is not None:
        code = str(current.get("VENDOR_CODE") or "")

    # The vendor on record is the ORIGINAL (oldest) registration of the number
    original = rows[0]
    if current is not None and current["ID"] == original["ID"]:
        return None                                   # this IS the original
    if str(original.get("VENDOR_CODE") or "") == code:
        return None

    # Same company under another code is not a mismatch
    cur_name = _norm_name((current or {}).get("VENDER_NAME"))
    if not cur_name:
        cur_name = _norm_name(next((r.get("VENDER_NAME") for r in rows
                                    if str(r.get("VENDOR_CODE") or "") == code), ""))
    if cur_name and _norm_name(original.get("VENDER_NAME")) == cur_name:
        return None

    oc = str(original.get("VENDOR_CODE") or "")
    on_record = [{
        "vendor_name": original.get("VENDER_NAME") or oc or "-",
        "vendor_code": oc or "-",
        "registered":  original.get("CREATE_TIME"),
    }]

    current_name = (current or {}).get("VENDER_NAME") if current else None
    return {
        "vehicle_number": vehicle_number,
        "current_vendor": current_name or "",
        "current_code":   code or "-",
        "on_record":      on_record,
    }


def mismatch_remark(mm: dict | None) -> str:
    """'Vendor on record ANAND BUILDERS (2122727)'."""
    if not mm or not mm.get("on_record"):
        return ""
    names = ", ".join(f"{o['vendor_name']} ({o['vendor_code']})" for o in mm["on_record"])
    return f"Vendor on record {names}"


def vehicle_history(vehicle_number: str, include_uid: str | None = None,
                    limit: int = 20) -> list[dict]:
    """
    Every COMPLETED session of this vehicle NUMBER across all its RFID rows,
    newest first - the same rows the dashboard History tab shows when you
    search the number. `include_uid` adds the session being mailed even
    though it is not marked COMPLETED yet.
    """
    if not vehicle_number:
        return []
    db = None
    try:
        db  = _db_connect()
        cur = db.cursor(pymysql.cursors.DictCursor)
        cur.execute(
            """
            SELECT vl.ID, vl.UID, vl.STATUS, vl.BUCKET_NO, vl.SAMPLE_INFO,
                   vl.CREATE_TIME, vl.UPDATE_TIME,
                   vm.VEHICLE_NUMBER, vm.VENDOR_CODE,
                   (SELECT vr.VENDER_NAME FROM VENDOR_MASTER vr
                     WHERE vr.VENDOR_CODE = vm.VENDOR_CODE LIMIT 1) AS VENDER_NAME
            FROM VEHICLE_LOGS vl
            JOIN VEHICLE_MASTER vm ON vm.RFID = vl.RFIDS
            WHERE UPPER(REPLACE(vm.VEHICLE_NUMBER, ' ', '')) = %s
              AND (vl.STATUS = 'COMPLETED' OR vl.UID = %s)
            ORDER BY vl.CREATE_TIME DESC
            LIMIT %s
            """,
            (_norm_number(vehicle_number), include_uid or "", int(limit) * 2),
        )
        raw = cur.fetchall() or []
    except Exception:
        logger.error(f"[REPORT] vehicle_history failed:\n{traceback.format_exc()}")
        return []
    finally:
        if db:
            db.close()

    out, seen = [], set()
    for r in raw:
        if r["ID"] in seen:                 # duplicate RFID rows in the master
            continue
        seen.add(r["ID"])
        info = parse_sample_info(r.get("SAMPLE_INFO"))
        start, end = cycle_window(r.get("CREATE_TIME"), r.get("UPDATE_TIME"), info)
        out.append({
            "uid":            r.get("UID"),
            "cycle_start":    start,
            "cycle_end":      end,
            "cycle_time":     fmt_duration(start, end),
            "vehicle_number": r.get("VEHICLE_NUMBER"),
            "vendor_name":    r.get("VENDER_NAME") or "",
            "vendor_code":    r.get("VENDOR_CODE") or "",
            "bucket":         r.get("BUCKET_NO") or "",
            "status":         r.get("STATUS") or "",
        })
        if len(out) >= limit:
            break
    return out


# =====================================================================
# SESSION ISSUE SUMMARY
# =====================================================================

def summarize_session(info: dict | None, status: str | None = None,
                      error_message: str | None = None) -> dict:
    """
    Classify one session from its SAMPLE_INFO.

    Returns
        ai        : FAILED attempts with reason AI_OBSTRUCTION
        hard      : FAILED attempts with reason HARD_MATERIAL
        other     : every other FAILED attempt (timeouts, PLC errors)
        manual    : True when the sample was collected by hand
        cycle_status : "Successful" | "Successful (Manual)" | "Failed" | "In progress"
    """
    info = info if isinstance(info, dict) else {}
    attempts = info.get("attempts") if isinstance(info.get("attempts"), list) else []

    failed = [a for a in attempts if isinstance(a, dict) and a.get("status") != "SUCCESS"]
    ai     = [a for a in failed if a.get("reason") == "AI_OBSTRUCTION"]
    hard   = [a for a in failed if a.get("reason") == "HARD_MATERIAL"]
    other  = [a for a in failed if a.get("reason") not in ("AI_OBSTRUCTION", "HARD_MATERIAL")]

    manual = str(info.get("collection_mode") or "AUTO").upper() == "MANUAL"
    st = str(status or "").upper()

    if st == "COMPLETED":
        cycle_status = "Successful (Manual)" if manual else "Successful"
    elif st in ("ERROR", "FAILED"):
        cycle_status = "Failed"
    elif st:
        cycle_status = "In progress"
    else:
        cycle_status = "Successful (Manual)" if manual else "Successful"

    return {
        "ai":            ai,
        "hard":          hard,
        "other":         other,
        "manual":        manual,
        "manual_reason": info.get("manual_reason"),
        "successful":    info.get("successful", sum(1 for a in attempts
                                                     if isinstance(a, dict) and a.get("status") == "SUCCESS")),
        "total":         info.get("total_cycles", "-"),
        "failed":        info.get("failed", len(failed)),
        "cycle_status":  cycle_status,
        "status":        st,
        "error_message": error_message or "",
    }


def _where(attempts: list[dict]) -> str:
    """'A2 @10:14:05, A3 @10:20:11'"""
    bits = []
    for a in attempts:
        area = a.get("area")
        t    = _attempt_time(a)
        bits.append(f"A{area} @{t}" if area is not None and t else (f"A{area}" if area is not None else t))
    return ", ".join(b for b in bits if b)


def remark_parts(summary: dict, mismatch: dict | None = None) -> list[str]:
    """
    Remark lines in the style of the manual sheet, e.g.
      '1 Failed (auger position could not be confirmed) - A2 @10:14:05'
      'Vendor on record MSP VENTURES (706068)'
    """
    parts: list[str] = []

    if summary["ai"]:
        line = f"{len(summary['ai'])} Failed ({REASON_TEXT['AI_OBSTRUCTION']})"
        where = _where(summary["ai"])
        parts.append(f"{line} - {where}" if where else line)

    if summary["hard"]:
        line = f"{len(summary['hard'])} Failed ({REASON_TEXT['HARD_MATERIAL']})"
        where = _where(summary["hard"])
        parts.append(f"{line} - {where}" if where else line)

    # Group the rest by reason so 3 timeouts read as one line
    by_reason: dict[str, list] = {}
    for a in summary["other"]:
        by_reason.setdefault(a.get("reason") or "FAILED", []).append(a)
    for reason, rows in by_reason.items():
        txt = REASON_TEXT.get(reason, reason.replace("_", " ").lower())
        parts.append(f"{len(rows)} Failed ({txt})")

    if summary["manual"]:
        why = summary.get("manual_reason")
        if why == "HARD_MATERIAL":
            parts.append("Sample collected manually after hard material")
        elif summary["ai"]:
            parts.append("Operator chose manual collection after AI block")
        else:
            parts.append("Sample collected manually")

    mm = mismatch_remark(mismatch)
    if mm:
        parts.append(mm)

    if summary["status"] in ("ERROR", "FAILED") and summary.get("error_message"):
        msg = str(summary["error_message"])
        for prefix in ("Session reset due to error :", "Session reset due to error:"):
            if msg.startswith(prefix):
                msg = msg[len(prefix):].strip()
        parts.append(f"Session error: {msg}")

    return parts


# =====================================================================
# IMAGES
# =====================================================================

def prepare_image(src: str | None, out_dir: str, name: str | None = None,
                  max_width: int = MAIL_IMG_MAX_WIDTH,
                  quality: int = MAIL_IMG_QUALITY) -> str | None:
    """
    Mail-sized copy of an image: longest side capped at max_width, JPEG.
    Falls back to a plain copy when no imaging library is usable. Returns the
    new path or None when the source is missing.
    """
    if not src or not os.path.exists(src):
        return None
    os.makedirs(out_dir, exist_ok=True)
    dest = os.path.join(out_dir, name or os.path.basename(src))
    if not dest.lower().endswith((".jpg", ".jpeg")):
        dest = os.path.splitext(dest)[0] + ".jpg"

    try:
        import cv2
        img = cv2.imread(src)
        if img is not None:
            h, w = img.shape[:2]
            scale = min(1.0, float(max_width) / float(max(h, w)))
            if scale < 1.0:
                img = cv2.resize(img, (int(w * scale), int(h * scale)),
                                 interpolation=cv2.INTER_AREA)
            if cv2.imwrite(dest, img, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]):
                return dest
    except Exception:
        logger.debug(f"[REPORT] cv2 resize failed for {src}:\n{traceback.format_exc()}")

    try:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((max_width, max_width))
            im.save(dest, "JPEG", quality=quality, optimize=True)
            return dest
    except Exception:
        logger.debug(f"[REPORT] PIL resize failed for {src}:\n{traceback.format_exc()}")

    try:
        shutil.copy2(src, dest)
        return dest
    except Exception:
        logger.warning(f"[REPORT] Could not stage image {src}")
        return None


# =====================================================================
# HTML
# =====================================================================

HTML_FONT = "font-family:Segoe UI,Arial,sans-serif;font-size:13px;color:#212529;"
TH = ("background:#212529;color:#ffffff;padding:6px 8px;border:1px solid #dee2e6;"
      "font-size:12px;text-transform:uppercase;white-space:nowrap;")
TD = "padding:5px 8px;border:1px solid #dee2e6;font-size:12px;text-align:center;"


def html_table(headers: list[str], rows: list[list], row_styles: list[str] | None = None) -> str:
    """Bordered table in the dashboard History style (dark header)."""
    out = ['<table cellspacing="0" cellpadding="0" '
           'style="border-collapse:collapse;margin:6px 0 12px 0;">', "<tr>"]
    out += [f'<th style="{TH}">{esc(h)}</th>' for h in headers]
    out.append("</tr>")
    for i, row in enumerate(rows):
        style = (row_styles[i] if row_styles and i < len(row_styles) else "") or ""
        out.append(f'<tr style="{style}">')
        out += [f'<td style="{TD}">{esc(c)}</td>' for c in row]
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def vendor_table(vehicle_number: str, mismatch: dict | None,
                 current_code: str | None, current_name: str | None,
                 include_uid: str | None = None) -> list[dict]:
    """
    One row per UNIQUE vendor this vehicle number has been seen with -
    registered in VEHICLE_MASTER and/or sampled under. Oldest first.
        vendor_name, vendor_code, registered, first_seen, last_seen,
        sessions, last_bucket, role ("On record" | "This arrival" | "")
    """
    history = vehicle_history(vehicle_number, include_uid=include_uid, limit=1000)
    by_code: dict[str, dict] = {}

    def _row(code, name):
        return by_code.setdefault(code, {
            "vendor_name": name or code or "-", "vendor_code": code or "-",
            "registered": None, "first_seen": None, "last_seen": None,
            "sessions": 0, "last_bucket": "", "role": "",
        })

    for o in (mismatch or {}).get("on_record", []):
        r = _row(str(o["vendor_code"]), o["vendor_name"])
        r["registered"] = parse_stamp(o.get("registered"))
        r["role"] = "On record"

    for h in history:                          # newest first
        r = _row(str(h["vendor_code"]), h["vendor_name"])
        r["sessions"] += 1
        if r["last_seen"] is None:
            r["last_seen"], r["last_bucket"] = h["cycle_start"], h["bucket"]
        r["first_seen"] = h["cycle_start"]

    if current_code:
        r = _row(str(current_code), current_name)
        r["role"] = "This arrival"

    rows = list(by_code.values())
    rows.sort(key=lambda r: (r["role"] != "On record", r["role"] == "This arrival",
                             r["first_seen"] or r["registered"] or datetime.max))
    return rows


def vendor_table_html(rows: list[dict]) -> str:
    """Unique-vendor table, dashboard style. This arrival is highlighted."""
    if not rows:
        return "<p><i>No vendor history found for this vehicle.</i></p>"
    f = lambda d: d.strftime("%Y-%m-%d %H:%M") if isinstance(d, datetime) else ""
    headers = ["SNo.", "Vendor Name", "Vendor Code", "Status", "First Session",
               "Last Session", "Sessions", "Last Bucket"]
    body, styles = [], []
    for i, r in enumerate(rows, start=1):
        body.append([i, r["vendor_name"], r["vendor_code"], r["role"] or "-",
                     f(r["first_seen"]) or "-", f(r["last_seen"]) or "-",
                     r["sessions"], r["last_bucket"] or "-"])
        styles.append("font-weight:bold;background:#f8d7da;" if r["role"] == "This arrival"
                      else ("background:#d1e7dd;" if r["role"] == "On record" else ""))
    return html_table(headers, body, styles)


def history_table_html(history: list[dict], mismatch: dict | None,
                       current_uid: str | None) -> str:
    """
    The vendor-mismatch history table - same columns as the dashboard
    History tab. Rows under a vendor other than the one on record are tinted
    red; the session being reported is bold on yellow.
    """
    if not history:
        return "<p><i>No earlier sessions found for this vehicle.</i></p>"

    record_codes = {o["vendor_code"] for o in (mismatch or {}).get("on_record", [])}
    headers = ["SNo.", "Cycle Start", "Cycle End", "Cycle Time (MM:SS)",
               "Vehicle Number", "Vendor Name", "Vendor Code", "Bucket"]
    rows, styles = [], []
    for i, h in enumerate(history, start=1):
        rows.append([
            i,
            h["cycle_start"].strftime("%Y-%m-%d %H:%M:%S") if h["cycle_start"] else "",
            h["cycle_end"].strftime("%Y-%m-%d %H:%M:%S") if h["cycle_end"] else "",
            h["cycle_time"], h["vehicle_number"], h["vendor_name"],
            h["vendor_code"], h["bucket"],
        ])
        style = ""
        if record_codes and str(h["vendor_code"]) not in record_codes:
            style = "background:#f8d7da;"
        if current_uid and h["uid"] == current_uid:
            style += "font-weight:bold;background:#fff3cd;"
        styles.append(style)
    legend = ('<div style="font-size:11px;color:#6c757d;">'
              '<span style="background:#fff3cd;padding:0 6px;">this session</span>&nbsp;'
              '<span style="background:#f8d7da;padding:0 6px;">vendor differs from the vendor on record</span>'
              '</div>')
    return html_table(headers, rows, styles) + legend
