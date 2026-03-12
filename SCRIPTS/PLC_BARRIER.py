import time

from DEPENDANT.LOGIX import PLCCOMMINCATION
from DEPENDANT.MQTT  import MQTT

PLC_IP = "192.168.1.10"   # ← update

BARRIER_CMD_TAG = "BARRIER_CMD"       # ← update to your actual tag name
BARRIER_STATUS_TAG = "BARRIER_STATUS"    # ← update
BUCKET_NO_TAG = "BUCKET_NUMBER"     # ← update
BARRIER_STATUS_BIT = 0          # bit index that goes HIGH when barrier is open
BARRIER_OPEN_TIMEOUT = 15        # seconds to wait for confirmation

TOPIC_IN  = "manager/plc_barrier"
TOPIC_OUT = "plc_barrier/status"

class BarrierController:

    def __init__(self):
        self.plc  = PLCCOMMINCATION(PLC_IP)
        self.mqtt = MQTT("PLC_BARRIER")

    def open_barrier(self):
        try:
            print("[PLC_BARRIER] Opening barrier …")
            self.plc.plc.write(BARRIER_CMD_TAG, 1)

            # Wait for the status bit to confirm open
            deadline = time.time() + BARRIER_OPEN_TIMEOUT
            while time.time() < deadline:
                bit = self.plc.read_bit(BARRIER_STATUS_BIT, BARRIER_STATUS_TAG)
                if bit == 1:
                    print("[PLC_BARRIER] Barrier OPEN confirmed.")
                    self.mqtt.publish(TOPIC_OUT, {"status": "barrier_opened"})
                    return
                time.sleep(0.5)

            raise TimeoutError("Barrier did not open within timeout.")

        except Exception as e:
            msg = f"open_barrier error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "barrier_error", "msg": msg})

    def close_barrier(self):
        try:
            print("[PLC_BARRIER] Closing barrier …")
            self.plc.plc.write(BARRIER_CMD_TAG, 0)
            print("[PLC_BARRIER] Barrier CLOSE command sent.")
            self.mqtt.publish(TOPIC_OUT, {"status": "barrier_closed"})
        except Exception as e:
            msg = f"close_barrier error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "barrier_error", "msg": msg})

    def set_bucket(self, bucket_no: int):
        try:
            print(f"[PLC_BARRIER] Setting bucket number → {bucket_no}")
            self.plc.plc.write(BUCKET_NO_TAG, int(bucket_no))
            print(f"[PLC_BARRIER] Bucket number {bucket_no} set.")
            self.mqtt.publish(TOPIC_OUT, {
                "status": "bucket_set", "bucket_no": bucket_no
            })
        except Exception as e:
            msg = f"set_bucket error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "bucket_error", "msg": msg})

    def run(self):
        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_BARRIER] Ready, waiting for commands …")

        while True:
            try:
                data = self.mqtt.data
                if data and data.get("_consumed") is not True:
                    action = data.get("action", "")
                    bucket_no = data.get("bucket_no", 0)
                    self.mqtt.data = {**data, "_consumed": True}

                    if action == "open_barrier":
                        self.open_barrier()
                    elif action == "close_barrier":
                        self.close_barrier()
                    elif action == "set_bucket":
                        self.set_bucket(bucket_no)
                    else:
                        print(f"[PLC_BARRIER] Unknown action: {action}")

            except Exception as e:
                print(f"[PLC_BARRIER] Loop error: {e}")

            time.sleep(0.05)


def main():
    controller = BarrierController()
    controller.run()


if __name__ == "__main__":
    main()
