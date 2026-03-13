import time

from DEPENDANT.SNAP7 import PLCCOMMINCATION
from DEPENDANT.MQTT  import MQTT

# ── PLC connection ────────────────────────────────────────────────────────────
PLC_IP   = "192.168.1.10"    # ← update
PLC_RACK = 0                 # ← update if needed (0 for S7-1200/1500)
PLC_SLOT = 1                 # ← update if needed (1 for S7-300/400, 0 for S7-1200)

# ── Data Block numbers ────────────────────────────────────────────────────────
DB_READ  = 10                 # ← update: DB number for reads
DB_WRITE = 10                 # ← update: DB number for writes (can be same DB)

# ── Byte offsets inside the Data Block ───────────────────────────────────────
OFFSET_BARRIER_CMD    = 0    # ← update: INT (2 bytes) — write 1=open / 0=close
OFFSET_BARRIER_STATUS = 2    # ← update: BYTE (1 byte) — bit 0 HIGH = barrier open
OFFSET_BUCKET_NO      = 4    # ← update: INT (2 bytes) — bucket number

# ── Tuning ────────────────────────────────────────────────────────────────────
BARRIER_STATUS_BIT   = 0     # bit index in OFFSET_BARRIER_STATUS byte
BARRIER_OPEN_TIMEOUT = 15    # seconds to wait for open confirmation

# ── MQTT topics ───────────────────────────────────────────────────────────────
TOPIC_IN  = "manager/plc_barrier"
TOPIC_OUT = "plc_barrier/status"


class BarrierController:

    def __init__(self):
        self.plc  = PLCCOMMINCATION(
            ip_address=PLC_IP,
            db_read=DB_READ,
            db_write=DB_WRITE,
            rack=PLC_RACK,
            slot=PLC_SLOT,
        )
        self.mqtt = MQTT("PLC_BARRIER")

    # ── Open boom barrier ─────────────────────────────────────────────────────
    def open_barrier(self):
        try:
            print("[PLC_BARRIER] Opening barrier …")
            self.plc.write(OFFSET_BARRIER_CMD, 1)

            # Poll status bit until confirmed or timeout
            deadline = time.time() + BARRIER_OPEN_TIMEOUT
            while time.time() < deadline:
                bit = self.plc.read_bit(BARRIER_STATUS_BIT, OFFSET_BARRIER_STATUS)
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

    # ── Close boom barrier ────────────────────────────────────────────────────
    def close_barrier(self):
        try:
            print("[PLC_BARRIER] Closing barrier …")
            self.plc.write(OFFSET_BARRIER_CMD, 0)
            print("[PLC_BARRIER] Barrier CLOSE command sent.")
            self.mqtt.publish(TOPIC_OUT, {"status": "barrier_closed"})
        except Exception as e:
            msg = f"close_barrier error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "barrier_error", "msg": msg})

    # ── Set bucket number ─────────────────────────────────────────────────────
    def set_bucket(self, bucket_no: int):
        try:
            print(f"[PLC_BARRIER] Setting bucket number → {bucket_no}")
            self.plc.write(OFFSET_BUCKET_NO, int(bucket_no))
            print(f"[PLC_BARRIER] Bucket number {bucket_no} written.")
            self.mqtt.publish(TOPIC_OUT, {
                "status": "bucket_set", "bucket_no": bucket_no
            })
        except Exception as e:
            msg = f"set_bucket error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "bucket_error", "msg": msg})

    # ── Main loop ─────────────────────────────────────────────────────────────
    def run(self):
        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_BARRIER] Ready, waiting for commands …")

        while True:
            try:
                data = self.mqtt.data
                if data and data.get("_consumed") is not True:
                    action    = data.get("action", "")
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