import time
import threading

from DEPENDANT.SNAP7 import PLCCOMMINCATION
from DEPENDANT.MQTT  import MQTT

# ── PLC connection ────────────────────────────────────────────────────────────
PLC_IP   = "192.168.1.11"    # ← update
PLC_RACK = 0                 # ← update
PLC_SLOT = 1                 # ← update

# ── Data Block numbers ────────────────────────────────────────────────────────
DB_READ  = 2                 # ← update
DB_WRITE = 2                 # ← update

# ── Byte offsets inside the Data Block ───────────────────────────────────────
OFFSET_X            = 0     # ← update: INT (2 bytes)
OFFSET_Y            = 2     # ← update: INT (2 bytes)
OFFSET_CYCLE_START  = 4     # ← update: INT (2 bytes)
OFFSET_DISCHARGE    = 6     # ← update: BYTE (1 byte), bit 0 = done
OFFSET_GREEN_SIGNAL = 8     # ← update: INT (2 bytes)

# ── Tuning ────────────────────────────────────────────────────────────────────
DISCHARGE_BIT     = 0       # bit index in OFFSET_DISCHARGE byte
DISCHARGE_TIMEOUT = 120     # seconds before giving up on a cycle
DISCHARGE_POLL_S  = 0.2     # poll interval

# ── MQTT topics ───────────────────────────────────────────────────────────────
TOPIC_IN  = "manager/plc_sampler"
TOPIC_OUT = "plc_sampler/status"


class SamplerController:

    def __init__(self):
        self.plc  = PLCCOMMINCATION(
            ip_address=PLC_IP,
            db_read=DB_READ,
            db_write=DB_WRITE,
            rack=PLC_RACK,
            slot=PLC_SLOT,
        )
        self.mqtt = MQTT("PLC_SAMPLER")
        self._monitor_thread: threading.Thread | None = None
        self._monitoring = False

    # ── Set X / Y position ────────────────────────────────────────────────────
    def set_position(self, x, y, cycle: int):
        try:
            print(f"[PLC_SAMPLER] Position  X={x}  Y={y}  cycle={cycle}")
            self.plc.write(OFFSET_X, int(x))
            self.plc.write(OFFSET_Y, int(y))
            print("[PLC_SAMPLER] Position written.")
            self.mqtt.publish(TOPIC_OUT, {
                "status": "position_set", "cycle": cycle
            })
        except Exception as e:
            msg = f"set_position error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "error", "msg": msg})

    # ── Start a sampling cycle ────────────────────────────────────────────────
    def start_cycle(self, cycle: int):
        try:
            print(f"[PLC_SAMPLER] Starting cycle {cycle} …")
            self.plc.write(OFFSET_CYCLE_START, 0)   # ensure reset first
            time.sleep(0.2)
            self.plc.write(OFFSET_CYCLE_START, 1)   # rising edge = start
            print(f"[PLC_SAMPLER] Cycle {cycle} start pulse sent.")
            self.mqtt.publish(TOPIC_OUT, {
                "status": "cycle_started", "cycle": cycle
            })

            # Monitor discharge in a background thread
            self._stop_monitor()
            self._monitor_thread = threading.Thread(
                target=self._wait_for_discharge,
                args=(cycle,),
                daemon=True
            )
            self._monitoring = True
            self._monitor_thread.start()

        except Exception as e:
            msg = f"start_cycle error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "error", "msg": msg})

    # ── Background: wait for discharge signal ─────────────────────────────────
    def _wait_for_discharge(self, cycle: int):
        deadline = time.time() + DISCHARGE_TIMEOUT
        print(f"[PLC_SAMPLER] Monitoring discharge for cycle {cycle} …")

        while time.time() < deadline and self._monitoring:
            try:
                bit = self.plc.read_bit(DISCHARGE_BIT, OFFSET_DISCHARGE)
                if bit == 1:
                    self.plc.write(OFFSET_CYCLE_START, 0)   # reset start bit
                    print(f"[PLC_SAMPLER] Discharge received for cycle {cycle}.")
                    self._monitoring = False
                    self.mqtt.publish(TOPIC_OUT, {
                        "status": "discharge_received", "cycle": cycle
                    })
                    return
            except Exception as e:
                print(f"[PLC_SAMPLER] Discharge monitor error: {e}")

            time.sleep(DISCHARGE_POLL_S)

        if self._monitoring:
            msg = f"Discharge timeout for cycle {cycle}"
            print(f"[PLC_SAMPLER] {msg}")
            self._monitoring = False
            self.mqtt.publish(TOPIC_OUT, {"status": "error", "msg": msg})

    def _stop_monitor(self):
        self._monitoring = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)

    # ── Send green (all sampling done) signal ─────────────────────────────────
    def send_green(self):
        try:
            print("[PLC_SAMPLER] Sending GREEN signal …")
            self.plc.write(OFFSET_GREEN_SIGNAL, 1)
            print("[PLC_SAMPLER] GREEN signal written.")
            self.mqtt.publish(TOPIC_OUT, {"status": "green_sent"})
        except Exception as e:
            msg = f"send_green error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "error", "msg": msg})

    # ── Reset all PLC outputs ─────────────────────────────────────────────────
    def reset_plc(self):
        try:
            self._stop_monitor()
            self.plc.write(OFFSET_CYCLE_START,  0)
            self.plc.write(OFFSET_GREEN_SIGNAL, 0)
            print("[PLC_SAMPLER] PLC outputs reset.")
        except Exception as e:
            print(f"[PLC_SAMPLER] reset_plc error: {e}")

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self):
        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_SAMPLER] Ready, waiting for commands …")

        while True:
            try:
                data = self.mqtt.data
                if data and data.get("_consumed") is not True:
                    action = data.get("action", "")
                    x      = data.get("x",     0)
                    y      = data.get("y",     0)
                    cycle  = data.get("cycle", 0)
                    self.mqtt.data = {**data, "_consumed": True}

                    if action == "set_position":
                        self.set_position(x, y, cycle)
                    elif action == "start_cycle":
                        self.start_cycle(cycle)
                    elif action == "send_green":
                        self.send_green()
                    elif action == "reset":
                        self.reset_plc()
                    else:
                        print(f"[PLC_SAMPLER] Unknown action: {action}")

            except Exception as e:
                print(f"[PLC_SAMPLER] Loop error: {e}")

            time.sleep(0.05)


def main():
    controller = SamplerController()
    controller.run()


if __name__ == "__main__":
    main()