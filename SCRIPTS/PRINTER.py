import socket
import time
import os
from datetime import datetime
from urllib.parse import quote

from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
LOGS_PATH = BASE_FILE_PATH + "LOGS/"

logger = initializeLogger("PRINTER_MANAGER", LOGS_PATH=LOGS_PATH)

IP   = "192.168.1.168"
PORT = 8080

TOPIC_IN         = "manager/printer"
STOP_TIMEOUT_SEC = 15 * 60   # 15 minutes — auto-stop if no explicit stop arrives

# ── connection tuning ────────────────────────────────────────────────────── #
# The board rejects a new TCP session opened immediately after the previous one
# is closed (it accepts the connect, then resets it -> WinError 10054).
# Every 10054 in the log happened on a session created <1 s after a teardown;
# every session created a few seconds later succeeded.
MIN_RECONNECT_GAP    = 3.0    # seconds to wait between closing and reopening
CONNECT_TIMEOUT      = 10     # seconds for the TCP handshake
SOCKET_TIMEOUT       = 15     # seconds for send operations
CONNECT_RETRIES      = 3
DATA_RETRIES         = 2      # full START+DATA retries on a reset
START_TO_DATA_DELAY  = 10     # board needs the message selected before data

# ── data validation ──────────────────────────────────────────────────────── #
DT_OUTPUT_FORMAT = "%Y%m%d%H%M%S"
DT_INPUT_FORMATS = (
    "%Y%m%d%H%M%S",        # MAIN_MANAGER  -> 20260819102918
    "%d/%m/%Y %H:%M",      # WEB_APP (old) -> 19/08/2026 09:54
    "%d/%m/%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)
DUPLICATE_WINDOW = 5.0   # ignore an identical send_data repeated within 5 s

# Timestamp of the last socket close, shared across sessions (each print job
# builds a brand new PrintSession, so this cannot live on the instance).
_last_disconnect_at = 0.0


# ──────────────────────────────────────────────────────────────────────────────
# PrintSession
#   Created once per print job (on "send_data").
#   Destroyed (by the service) after stop — whether explicit or by timeout.
# ──────────────────────────────────────────────────────────────────────────────

class PrintSession:

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        print("[PRINTER] Session created.")
        logger.info("PrintSession created.")

    # ───────────────────────── CRC ───────────────────────────────────────── #

    def _reflect8(self, x: int) -> int:
        return int(f"{x:08b}"[::-1], 2)

    def _reflect16(self, x: int) -> int:
        return int(f"{x:016b}"[::-1], 2)

    def _crc16(self, data: bytes) -> bytes:
        crc  = 0xFFFF
        poly = 0x1021
        for b in data:
            b    = self._reflect8(b)
            crc ^= (b << 8)
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ poly) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF
        crc  = self._reflect16(crc)
        crc ^= 0xFFFF
        return crc.to_bytes(2, "big")

    # ───────────────────────── PACKET ────────────────────────────────────── #

    def _build_packet(self, header_hex: str, json_text: str) -> bytes:
        header       = bytes.fromhex(header_hex)
        # errors="strict" would blow up on any non-ASCII char that sneaks in
        # from a vendor name / pasted URL; drop them instead of crashing.
        json_bytes   = json_text.encode("ascii", errors="ignore")
        length_bytes = len(json_bytes).to_bytes(4, "big")
        crc1         = self._crc16(header + length_bytes)
        crc2         = self._crc16(json_bytes)
        return header + length_bytes + crc1 + crc2 + json_bytes

    # ───────────────────────── VALIDATION ────────────────────────────────── #

    @staticmethod
    def clean_url(raw: str) -> str:
        """
        Return a printable, ASCII-safe http(s) URL, or "" if the value is not
        usable. Spaces / newlines are stripped out (a space inside the URL
        makes the QR unscannable and shifts the JSON length).
        """
        url = (raw or "").strip()
        if not url:
            return ""
        url = url.replace("\r", "").replace("\n", "").replace("\t", "")
        # a literal space breaks the QR and shifts the JSON byte length —
        # encode it rather than dropping it, so a real path is not corrupted
        url = url.replace(" ", "%20")
        if not url.lower().startswith(("http://", "https://")):
            return ""
        # percent-encode anything left that is not URL-legal ('%' is in `safe`
        # so an already-encoded URL is not double-encoded)
        url = quote(url, safe=":/?#[]@!$&'()*+,;=%-._~")
        try:
            url.encode("ascii")
        except UnicodeEncodeError:
            return ""
        return url

    @staticmethod
    def clean_dt(raw: str) -> str:
        """
        Normalise every timestamp to YYYYMMDDHHMMSS — no spaces, no slashes,
        no colons. Falls back to 'now' rather than sending a blank field.
        """
        dt = (raw or "").strip()
        if not dt:
            logger.warning("Empty dtstamp received — using current time instead.")
            return datetime.now().strftime(DT_OUTPUT_FORMAT)
        for fmt in DT_INPUT_FORMATS:
            try:
                return datetime.strptime(dt, fmt).strftime(DT_OUTPUT_FORMAT)
            except ValueError:
                continue
        logger.warning(f"Unrecognised dtstamp format '{dt}' — stripping whitespace only.")
        return "".join(dt.split())

    # ───────────────────────── JSON payloads ─────────────────────────────── #

    def _start_json(self) -> str:
        return '[{"GroupName":"G1","SelectMessage":"VAHICALDATA","ResetMessage":true,"Action":"Start"}]'

    def _stop_json(self) -> str:
        return '[{"GroupName":"G1","SelectMessage":"VAHICALDATA","ResetMessage":true,"Action":"Stop"}]'

    def _data_json(self, pdf_url: str, dt: str) -> str:
        logger.info(f"Preparing JSON — dt={dt}, url={pdf_url}")
        return (
            '{"MessageName":"VAHICALDATA","KeyValue":[{'
            f'"Eth_0":"{pdf_url}",'
            f'"Eth_1":"-",'
            f'"Eth_2":"{dt}"'
            '}]}'
        )

    # ───────────────────────── SOCKET ────────────────────────────────────── #

    def connect(self):
        global _last_disconnect_at

        if self.sock:
            return

        gap = time.time() - _last_disconnect_at
        if gap < MIN_RECONNECT_GAP:
            wait = MIN_RECONNECT_GAP - gap
            print(f"[PRINTER] Cooling down {wait:.1f}s before reconnecting...")
            logger.info(f"Reconnect cooldown {wait:.1f}s (board rejects immediate reconnects).")
            time.sleep(wait)

        last_err = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(CONNECT_TIMEOUT)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                print(f"[PRINTER] Connecting to printer (attempt {attempt}/{CONNECT_RETRIES})...")
                s.connect((self.host, self.port))
                s.settimeout(SOCKET_TIMEOUT)
                # only publish the socket AFTER the handshake succeeds, otherwise
                # a failed connect leaves a half-open socket behind -> WinError 10057
                self.sock = s
                print("[PRINTER] Connected.")
                logger.info(f"Connected to {self.host}:{self.port}")
                return
            except OSError as e:
                last_err = e
                try:
                    s.close()
                except Exception:
                    pass
                print(f"[PRINTER] Connect attempt {attempt} failed: {e}")
                logger.warning(f"Connect attempt {attempt}/{CONNECT_RETRIES} failed: {e}")
                if attempt < CONNECT_RETRIES:
                    time.sleep(2 * attempt)

        self.sock = None
        raise ConnectionError(
            f"Could not connect to {self.host}:{self.port} after "
            f"{CONNECT_RETRIES} attempts — last error: {last_err}"
        )

    def disconnect(self):
        global _last_disconnect_at
        if getattr(self, "sock", None):
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            _last_disconnect_at = time.time()
            print("[PRINTER] Disconnected.")
            logger.info("Socket disconnected.")

    def _send(self, packet: bytes, tag: str):
        # Never silently reconnect here — that is what produced the 10057
        # "socket is not connected" errors on the STOP frame.
        if not self.sock:
            raise ConnectionError(f"{tag} aborted — socket is not connected.")
        print(f"[PRINTER] {tag}: {packet.hex().upper()}")
        logger.info(f"{tag}: {packet.hex().upper()}")
        self.sock.sendall(packet)

    # ───────────────────────── PUBLIC API ────────────────────────────────── #

    def send_data(self, pdf_url: str, dtstamp: str):
        """
        Connect, send START, wait, then push the data frame.
        Refuses to run at all if the PDF link is missing or malformed, so the
        board is never handed a blank QR code.
        """
        url = self.clean_url(pdf_url)
        dt  = self.clean_dt(dtstamp)

        if not url:
            logger.error(
                f"Refusing to print — pdf_url is missing or invalid "
                f"(received: {pdf_url!r}). Nothing sent to the board."
            )
            raise ValueError("pdf_url is empty or not a valid http(s) link")

        last_err = None
        for attempt in range(1, DATA_RETRIES + 1):
            try:
                self.connect()
                self._send(self._build_packet("A55A02050100", self._start_json()), "START")
                time.sleep(START_TO_DATA_DELAY)
                self._send(
                    self._build_packet("A55A01100100", self._data_json(url, dt)),
                    "DATA"
                )
                return
            except (ConnectionResetError, ConnectionAbortedError,
                    TimeoutError, ConnectionError, OSError) as e:
                last_err = e
                print(f"[PRINTER] send_data attempt {attempt}/{DATA_RETRIES} failed: {e}")
                logger.warning(f"send_data attempt {attempt}/{DATA_RETRIES} failed: {e}")
                self.disconnect()
                if attempt < DATA_RETRIES:
                    time.sleep(MIN_RECONNECT_GAP)

        raise last_err

    def stop(self):
        """Send STOP frame and disconnect."""
        if not self.sock:
            print("[PRINTER] stop() called but socket already closed — skipping.")
            return
        try:
            self._send(self._build_packet("A55A02050100", self._stop_json()), "STOP")
            time.sleep(1)
        except Exception as e:
            print(f"[PRINTER] Error sending STOP: {e}")
            logger.error(f"Error sending STOP: {e}")
        finally:
            self.disconnect()

    def __del__(self):
        # Safety net — make sure socket is always closed when object is GC'd
        self.disconnect()
        print("[PRINTER] Session destroyed.")
        logger.info("PrintSession destroyed.")


# ──────────────────────────────────────────────────────────────────────────────
# PrinterService
#   Owns the single, persistent MQTT subscription.
#   Non-blocking event loop — processes one message per iteration, then checks
#   the session timeout on every pass.
#
#   Lifecycle per job:
#     1. Wait for a "send_data" message.
#     2. Validate it. A job with no pdf_url is REJECTED and the running
#        session is left untouched (previously a blank web-app job would tear
#        down a perfectly good manager job and print an empty QR).
#     3. If a valid session is already active → stop it, then start fresh.
#     4. Keep looping; on each pass check for "stop" message or 15-min timeout.
#     5. On stop/timeout → call session.stop(), del session → back to step 1.
# ──────────────────────────────────────────────────────────────────────────────

class PrinterService:

    def __init__(self):
        self.mqtt = MQTT("PRINTER")
        self.mqtt.subscribe(TOPIC_IN)
        self._last_job    = None    # (url, dt) of the last accepted job
        self._last_job_at = 0.0
        print("[PRINTER] Service initialised — waiting for print jobs.")
        logger.info("PrinterService started.")

    # ───────────────────────── internal helpers ───────────────────────────── #

    def _pop(self) -> dict | None:
        return self.mqtt.pop(TOPIC_IN)

    def _is_duplicate(self, job_key) -> bool:
        return (
            job_key == self._last_job
            and (time.time() - self._last_job_at) < DUPLICATE_WINDOW
        )

    # ───────────────────────── main loop ─────────────────────────────────── #

    def run(self):
        print("[PRINTER] Ready — waiting for 'send_data' command.")

        session: PrintSession | None = None

        while True:
            # ── process one MQTT message if available ─────────────────────── #
            msg = self._pop()

            if msg:
                action = msg.get("action", "")

                if action == "stop":
                    if session:
                        print("[PRINTER] Stop command received — stopping active session.")
                        logger.info("Explicit stop received, tearing down session.")
                        try:
                            session.stop()
                        except Exception as e:
                            print(f"[PRINTER] Error stopping session: {e}")
                            logger.error(f"Error stopping session: {e}", exc_info=True)
                        finally:
                            del session
                            session = None
                        self._last_job = None
                        print("[PRINTER] Session closed — waiting for next job.")
                        logger.info("Session closed, back to idle.")
                    else:
                        print("[PRINTER] Stop received but no active session — ignoring.")

                elif action == "send_data":
                    raw_url = msg.get("pdf_url", "")
                    raw_dt  = msg.get("dtstamp", "")

                    url = PrintSession.clean_url(raw_url)
                    dt  = PrintSession.clean_dt(raw_dt)

                    # ── reject bad jobs BEFORE touching the live session ──── #
                    if not url:
                        print(f"[PRINTER] send_data REJECTED — no valid pdf_url "
                              f"(got {raw_url!r}). Active session left untouched.")
                        logger.error(
                            f"send_data rejected — missing/invalid pdf_url {raw_url!r} "
                            f"(publisher must include 'pdf_url'). Nothing sent to the board."
                        )
                        time.sleep(0.05)
                        continue

                    if self._is_duplicate((url, dt)):
                        print("[PRINTER] Duplicate send_data within "
                              f"{DUPLICATE_WINDOW}s — ignoring.")
                        logger.warning("Duplicate send_data ignored (same url+dt).")
                        time.sleep(0.05)
                        continue

                    # Stop the old session first if one is running
                    if session:
                        print("[PRINTER] New send_data received — stopping previous session first.")
                        logger.info("New send_data while session active — stopping old session.")
                        try:
                            session.stop()
                        except Exception as e:
                            print(f"[PRINTER] Error stopping old session: {e}")
                            logger.error(f"Error stopping old session: {e}", exc_info=True)
                        finally:
                            del session
                            session = None

                    # Start fresh session
                    session = PrintSession(IP, PORT)
                    try:
                        session.send_data(url, dt)
                        session._started_at = time.time()
                        self._last_job      = (url, dt)
                        self._last_job_at   = time.time()
                        print("[PRINTER] Data sent — session active, waiting for stop or timeout.")
                        logger.info("Data sent successfully. Waiting for stop/timeout.")
                    except Exception as e:
                        print(f"[PRINTER] send_data failed: {e}")
                        logger.error(f"send_data failed: {e}", exc_info=True)
                        try:
                            session.stop()
                        except Exception:
                            pass
                        finally:
                            del session
                            session = None

                else:
                    print(f"[PRINTER] Unknown action '{action}' — ignoring.")
                    logger.warning(f"Unknown action: {action}")

            # ── timeout check: runs every loop pass ───────────────────────── #
            if session and hasattr(session, '_started_at'):
                if time.time() - session._started_at >= STOP_TIMEOUT_SEC:
                    print(f"[PRINTER] Session timeout ({STOP_TIMEOUT_SEC // 60} min) — auto-stopping.")
                    logger.warning(f"Auto-stop triggered after {STOP_TIMEOUT_SEC // 60} min timeout.")
                    try:
                        session.stop()
                    except Exception as e:
                        print(f"[PRINTER] Error during auto-stop: {e}")
                        logger.error(f"Error during auto-stop: {e}", exc_info=True)
                    finally:
                        del session
                        session = None
                    self._last_job = None
                    print("[PRINTER] Session auto-closed — waiting for next job.")
                    logger.info("Session auto-closed, back to idle.")

            time.sleep(0.05)


# ──────────────────────────────────────────────────────────────────────────────

def main():
    while True:
        try:
            service = PrinterService()
            service.run()
        except Exception as e:
            print(f"[PRINTER] Service crashed: {e}")
            logger.error(f"Service crashed: {e}", exc_info=True)
            time.sleep(5)
            print("[PRINTER] Restarting service...")


if __name__ == "__main__":
    main()