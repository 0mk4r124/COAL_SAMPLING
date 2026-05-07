import socket
import time
import os

from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
LOGS_PATH = BASE_FILE_PATH + "LOGS/"

logger = initializeLogger("PRINTER_MANAGER", LOGS_PATH=LOGS_PATH)

IP   = "192.168.1.168"
PORT = 8080

TOPIC_IN         = "manager/printer"
STOP_TIMEOUT_SEC = 15 * 60   # 15 minutes — auto-stop if no explicit stop arrives


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
        json_bytes   = json_text.encode("ascii")
        length_bytes = len(json_bytes).to_bytes(4, "big")
        crc1         = self._crc16(header + length_bytes)
        crc2         = self._crc16(json_bytes)
        return header + length_bytes + crc1 + crc2 + json_bytes

    # ───────────────────────── JSON payloads ─────────────────────────────── #

    def _start_json(self) -> str:
        return '[{"GroupName":"G1","SelectMessage":"VAHICALDATA","ResetMessage":true,"Action":"Start"}]'

    def _stop_json(self) -> str:
        return '[{"GroupName":"G1","SelectMessage":"VAHICALDATA","ResetMessage":true,"Action":"Stop"}]'

    def _data_json(self, vendor: str, vehicle: str, dt: str) -> str:
        logger.info(f"Preparing JSON — vendor={vendor}, vehicle={vehicle}, dt={dt}")
        return (
            '{"MessageName":"VAHICALDATA","KeyValue":[{'
            f'"Eth_0":"{vehicle}|{vendor}|{dt}",'
            f'"Eth_1":"-",'
            f'"Eth_2":"{dt}"'
            '}]}'
        )

    # ───────────────────────── SOCKET ────────────────────────────────────── #

    def connect(self):
        if self.sock:
            return
        print("[PRINTER] Connecting to printer...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        print("[PRINTER] Connected.")
        logger.info(f"Connected to {self.host}:{self.port}")

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
            print("[PRINTER] Disconnected.")
            logger.info("Socket disconnected.")

    def _send(self, packet: bytes, tag: str):
        if not self.sock:
            self.connect()
        print(f"[PRINTER] {tag}: {packet.hex().upper()}")
        logger.info(f"{tag}: {packet.hex().upper()}")
        self.sock.sendall(packet)

    # ───────────────────────── PUBLIC API ────────────────────────────────── #

    def send_data(self, vendor_name: str, vehicle_number: str, dtstamp: str):
        """Connect, send START, wait 10 s, then push the data frame."""
        self.connect()
        self._send(self._build_packet("A55A02050100", self._start_json()), "START")
        time.sleep(10)
        self._send(
            self._build_packet(
                "A55A01100100",
                self._data_json(vendor_name, vehicle_number, dtstamp)
            ),
            "DATA"
        )

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
#   Lifecycle per job:
#     1. Wait (forever) for a "send_data" message.
#     2. Create a PrintSession and call send_data().
#     3. Wait up to STOP_TIMEOUT_SEC for a "stop" message.
#     4. Whether stop arrives or timeout fires → call session.stop(), del session.
#     5. Go back to step 1.
# ──────────────────────────────────────────────────────────────────────────────

class PrinterService:

    def __init__(self):
        self.mqtt = MQTT("PRINTER")
        self.mqtt.subscribe(TOPIC_IN)
        print("[PRINTER] Service initialised — waiting for print jobs.")
        logger.info("PrinterService started.")

    # ───────────────────────── internal helpers ───────────────────────────── #

    def _pop(self) -> dict | None:
        return self.mqtt.pop(TOPIC_IN)

    def _wait_for_stop(self) -> bool:
        """
        Poll MQTT for up to STOP_TIMEOUT_SEC.
        Returns True  if an explicit 'stop' message arrived.
        Returns False if the timeout fired.
        """
        deadline = time.time() + STOP_TIMEOUT_SEC
        while time.time() < deadline:
            msg = self._pop()
            if msg:
                action = msg.get("action", "")
                if action == "stop":
                    print("[PRINTER] Explicit stop received.")
                    logger.info("Explicit stop received.")
                    return True
                # Any other message while waiting (e.g. duplicate send_data) → ignore / log
                print(f"[PRINTER] Ignored message while session active: action={action}")
                logger.warning(f"Ignored mid-session message: {msg}")
            time.sleep(0.05)

        remaining = STOP_TIMEOUT_SEC // 60
        print(f"[PRINTER] No stop received within {remaining} min — auto-stopping.")
        logger.warning(f"Auto-stop triggered after {remaining} min timeout.")
        return False

    # ───────────────────────── main loop ─────────────────────────────────── #

    def run(self):
        print("[PRINTER] Ready — waiting for 'send_data' command.")

        while True:
            # ── Phase 1: idle — wait for send_data ───────────────────────── #
            msg = self._pop()

            if not msg:
                time.sleep(0.05)
                continue

            action = msg.get("action", "")

            if action != "send_data":
                print(f"[PRINTER] Ignored '{action}' — no active session.")
                logger.info(f"Dropped message with action='{action}' (idle, no session).")
                continue

            # ── Phase 2: initialise session and send data ─────────────────── #
            session = PrintSession(IP, PORT)
            try:
                session.send_data(
                    msg.get("vendor_name",    ""),
                    msg.get("vehicle_number", ""),
                    msg.get("dtstamp",        "")
                )
            except Exception as e:
                print(f"[PRINTER] send_data failed: {e}")
                logger.error(f"send_data failed: {e}", exc_info=True)
                session.stop()
                del session
                print("[PRINTER] Session torn down after send_data error — waiting for next job.")
                continue

            # ── Phase 3: wait for stop (or timeout) ──────────────────────── #
            try:
                _was_explicit = self._wait_for_stop()
            finally:
                # Phase 4: always stop & destroy the session
                try:
                    session.stop()
                except Exception as e:
                    print(f"[PRINTER] Error during session stop: {e}")
                    logger.error(f"Error during session stop: {e}", exc_info=True)
                del session

            print("[PRINTER] Session closed — waiting for next job.")
            logger.info("Session closed, back to idle.")


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