import time
import threading

from DEPENDANT.LOGIX import PLCCOMMINCATION
from DEPENDANT.MQTT  import MQTT

PLC_IP = "192.168.1.11"   # ← update (can be same PLC as barrier, different slot)

X_TAG = "SAMPLER_X"           # ← update
Y_TAG = "SAMPLER_Y"           # ← update
CYCLE_START_TAG = "CYCLE_START"         # ← update
DISCHARGE_TAG = "DISCHARGE_STATUS"    # ← update
GREEN_SIGNAL_TAG = "GREEN_SIGNAL"        # ← update
DISCHARGE_BIT = 0       # bit index that goes HIGH on discharge complete
DISCHARGE_TIMEOUT = 120     # seconds before giving up on a cycle
DISCHARGE_POLL_MS = 0.2     # poll interval
TOPIC_IN = "manager/plc_sampler"
TOPIC_OUT = "plc_sampler/status"


class SamplerController:

    def __init__(self):
        self.plc  = PLCCOMMINCATION(PLC_IP)
        self.mqtt = MQTT("PLC_SAMPLER")
        self._monitor_thread: threading.Thread | None = None
        self._monitoring = False

    def set_position(self, x, y, cycle: int):
        try:
            print(f"[PLC_SAMPLER] Setting position  X={x}  Y={y}  cycle={cycle}")
            self.plc.plc.write(X_TAG, float(x))
            self.plc.plc.write(Y_TAG, float(y))
            print(f"[PLC_SAMPLER] Position set.")
            self.mqtt.publish(TOPIC_OUT, {
                "status": "position_set", "cycle": cycle
            })
        except Exception as e:
            msg = f"set_position error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "error", "msg": msg})

    def start_cycle(self, cycle: int):
        try:
            print(f"[PLC_SAMPLER] Starting cycle {cycle} …")
            # Ensure start bit is reset first
            self.plc.plc.write(CYCLE_START_TAG, 0)
            time.sleep(0.2)
            self.plc.plc.write(CYCLE_START_TAG, 1)
            print(f"[PLC_SAMPLER] Cycle {cycle} start pulse sent.")
            self.mqtt.publish(TOPIC_OUT, {
                "status": "cycle_started", "cycle": cycle
            })

            # Begin monitoring for discharge in a background thread
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

    def _wait_for_discharge(self, cycle: int):
        deadline = time.time() + DISCHARGE_TIMEOUT
        print(f"[PLC_SAMPLER] Monitoring discharge for cycle {cycle} …")

        while time.time() < deadline and self._monitoring:
            try:
                bit = self.plc.read_bit(DISCHARGE_BIT, DISCHARGE_TAG)
                if bit == 1:
                    # Reset cycle start and discharge bits
                    self.plc.plc.write(CYCLE_START_TAG, 0)
                    print(f"[PLC_SAMPLER] Discharge received for cycle {cycle}.")
                    self._monitoring = False
                    self.mqtt.publish(TOPIC_OUT, {
                        "status": "discharge_received", "cycle": cycle
                    })
                    return
            except Exception as e:
                print(f"[PLC_SAMPLER] Discharge monitor error: {e}")

            time.sleep(DISCHARGE_POLL_MS)

        if self._monitoring:
            msg = f"Discharge timeout for cycle {cycle}"
            print(f"[PLC_SAMPLER] {msg}")
            self._monitoring = False
            self.mqtt.publish(TOPIC_OUT, {"status": "error", "msg": msg})

    def _stop_monitor(self):
        self._monitoring = False
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=2)

    def send_green(self):
        try:
            print("[PLC_SAMPLER] Sending GREEN signal …")
            self.plc.plc.write(GREEN_SIGNAL_TAG, 1)
            print("[PLC_SAMPLER] GREEN signal sent.")
            self.mqtt.publish(TOPIC_OUT, {"status": "green_sent"})
        except Exception as e:
            msg = f"send_green error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "error", "msg": msg})

    def reset_plc(self):
        try:
            self._stop_monitor()
            self.plc.plc.write(CYCLE_START_TAG, 0)
            self.plc.plc.write(GREEN_SIGNAL_TAG, 0)
            print("[PLC_SAMPLER] PLC reset done.")
        except Exception as e:
            print(f"[PLC_SAMPLER] reset_plc error: {e}")

    def run(self):
        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_SAMPLER] Ready, waiting for commands …")

        while True:
            try:
                data = self.mqtt.data
                if data and data.get("_consumed") is not True:
                    action = data.get("action", "")
                    x = data.get("x",     0)
                    y = data.get("y",     0)
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
