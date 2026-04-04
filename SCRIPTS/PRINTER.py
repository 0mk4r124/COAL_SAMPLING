import socket
import time
import threading
import os

from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', '/home/omkar/INSIGHTZZ/PROJECTS/COAL_SAMPLING/COAL_SAMPLING/')
LOGS_PATH = BASE_FILE_PATH + "LOGS/"

# Initialize logger
logger = initializeLogger("PRINTER_MANAGER", LOGS_PATH=LOGS_PATH)

IP = "192.168.1.168"
PORT = 8080
TOPIC_IN = "manager/printer"

class Printer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None
        self.mqtt = MQTT("PRINTER")

        self.mqtt.subscribe(TOPIC_IN)

        self._running = True
        self._lock = threading.Lock()

    # ───────────────────────────────────────── CRC ───────────────────────── #

    def _reflect8(self, x: int) -> int:
        return int(f"{x:08b}"[::-1], 2)

    def _reflect16(self, x: int) -> int:
        return int(f"{x:016b}"[::-1], 2)

    def _crc16(self, data: bytes) -> bytes:
        crc = 0xFFFF
        poly = 0x1021

        for b in data:
            b = self._reflect8(b)
            crc ^= (b << 8)
            for _ in range(8):
                if crc & 0x8000:
                    crc = ((crc << 1) ^ poly) & 0xFFFF
                else:
                    crc = (crc << 1) & 0xFFFF

        crc = self._reflect16(crc)
        crc ^= 0xFFFF
        return crc.to_bytes(2, "big")

    # ───────────────────────────────────────── PACKET ───────────────────────── #

    def _build_packet(self, header_hex: str, json_text: str) -> bytes:
        header = bytes.fromhex(header_hex)
        json_bytes = json_text.encode("ascii")
        length_bytes = len(json_bytes).to_bytes(4, "big")

        crc1 = self._crc16(header + length_bytes)
        crc2 = self._crc16(json_bytes)

        return header + length_bytes + crc1 + crc2 + json_bytes

    # ───────────────────────────────────────── JSON ───────────────────────── #

    def _start_json(self):
        return '[{"GroupName":"G1","SelectMessage":"VAHICALDATA","ResetMessage":true,"Action":"Start"}]'

    def _stop_json(self):
        return '[{"GroupName":"G1","SelectMessage":"VAHICALDATA","ResetMessage":true,"Action":"Stop"}]'

    def _data_json(self, vendor, vehicle, dt):
        return (
            '{"MessageName":"VAHICALDATA","KeyValue":[{'
            f'"Eth_0":"{vehicle}",'
            f'"Eth_1":"{vendor}",'
            f'"Eth_2":"{dt}"'
            '}]}'
        )

    # ───────────────────────────────────────── SOCKET ───────────────────────── #

    def connect(self):
        if self.sock:
            return

        print("[PRINTER] Connecting...")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        print("[PRINTER] Connected")

    def disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock = None
            print("[PRINTER] Disconnected")

    def _send(self, packet: bytes, tag: str):
        if not self.sock:
            self.connect()

        print(f"[PRINTER] {tag}:", packet.hex().upper())
        self.sock.sendall(packet)

    # ───────────────────────────────────────── ACTIONS ───────────────────────── #

    def start(self):
        with self._lock:
            self.connect()
            pkt = self._build_packet("A55A02050100", self._start_json())
            self._send(pkt, "START")

    def send_data(self, vendor_name: str, vehicle_number: str, dtstamp: str):
        with self._lock:
            self.connect()

            # Start
            self.start()
            time.sleep(2)

            # Data
            pkt = self._build_packet(
                "A55A01100100",
                self._data_json(vendor_name, vehicle_number, dtstamp)
            )
            self._send(pkt, "DATA")

    def stop(self):
        with self._lock:
            if not self.sock:
                return

            pkt = self._build_packet("A55A02050100", self._stop_json())
            self._send(pkt, "STOP")

            time.sleep(1)
            self.disconnect()

    # ───────────────────────────────────────── MQTT LOOP ───────────────────────── #

    def _handle_msg(self, msg: dict):
        action = msg.get("action")

        if action == "send_data":
            self.send_data(
                msg.get("vendor_name", ""),
                msg.get("vehicle_number", ""),
                msg.get("dtstamp", "")
            )

        elif action == "stop":
            self.stop()

    def run(self):
        print("[PRINTER] Service started")

        while self._running:
            msg = self.mqtt.pop("manager/printer")
            if msg:
                try:
                    self._handle_msg(msg)
                except Exception as e:
                    print("[PRINTER] Error:", e)

            time.sleep(0.05)


def main():
    printer = Printer(IP, PORT)
    printer.run()


if __name__ == "__main__":
    main()
