import time

from DEPENDANT.SNAP7 import PLCCOMMINCATION
from DEPENDANT.MQTT  import MQTT


PLC_IP   = "192.168.1.2"        
DB_READ  = 10
DB_WRITE = 11

BOOM_BARRIER_OPEN = 0
BOOM_BARRIER_CLOSE = 1
TRUN_TABLE_COUNT = 2
TRUN_TABLE_CLOCKWISE = 3
TRUN_TABLE_VALUE_WRITE = 4
GREEN_SIGNAL = 5
RED_SIGNAL = 6

TRUCK_PRESENT1 = 0
TRUCK_PRESENT2 = 0
BOOM_BARRIER_OPEN_FB = 4
BOOM_BARRIER_CLOSE_FB = 6

BARRIER_OPEN_TIMEOUT = 15

TOPIC_IN  = "manager/plc_barrier"
TOPIC_OUT = "plc_barrier/status"

class BarrierController:

    def __init__(self):
        self.plc  = PLCCOMMINCATION(PLC_IP, DB_READ, DB_WRITE, "REED")
        self.mqtt = MQTT("PLC_BARRIER")
        self.client = self.plc.createConnection()

    def open_barrier(self):
        try:
            print("[PLC_BARRIER] Opening barrier …")
            self.plc.writeIntToPLC(self.client, BOOM_BARRIER_OPEN, 1)
            self.plc.writeIntToPLC(self.client, BOOM_BARRIER_CLOSE, 0)

            # Poll status bit until confirmed or timeout
            deadline = time.time() + BARRIER_OPEN_TIMEOUT
            while time.time() < deadline:
                open_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_OPEN_FB)
                close_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_CLOSE_FB)
                if (open_bit == 1) and (close_bit == 0):
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
            
            self.plc.writeIntToPLC(self.client, BOOM_BARRIER_OPEN, 0)
            self.plc.writeIntToPLC(self.client, BOOM_BARRIER_CLOSE, 1)
            print("[PLC_BARRIER] Barrier CLOSE command sent.")

            deadline = time.time() + BARRIER_OPEN_TIMEOUT
            while time.time() < deadline:
                open_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_OPEN_FB)
                close_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_CLOSE_FB)
                if (open_bit == 0) and (close_bit == 1):
                    print("[PLC_BARRIER] Barrier OPEN confirmed.")
                    self.mqtt.publish(TOPIC_OUT, {"status": "barrier_opened"})
                    return
                time.sleep(0.5)
            self.mqtt.publish(TOPIC_OUT, {"status": "barrier_closed"})
        except Exception as e:
            msg = f"close_barrier error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "barrier_error", "msg": msg})

    def green_signal(self):
        try:
            print("[PLC_BARRIER] Green Signal …")
            self.plc.writeIntToPLC(self.client, GREEN_SIGNAL, 1)
            self.plc.writeIntToPLC(self.client, RED_SIGNAL, 0)

        except Exception as e:
            msg = f"Green Signal error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "signal_error", "msg": msg})

    def red_signal(self):
        try:
            print("[PLC_BARRIER] Red Signal …")
            self.plc.writeIntToPLC(self.client, GREEN_SIGNAL, 0)
            self.plc.writeIntToPLC(self.client, RED_SIGNAL, 1)

        except Exception as e:
            msg = f"Green Signal error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "signal_error", "msg": msg})

    def truck_present(self):
        present = False
        try:
            print("[PLC_BARRIER] Checking Truck …")

            first_bit = self.plc.readIntFromPLC(self.client, TRUCK_PRESENT1)
            second_bit = self.plc.readIntFromPLC(self.client, TRUCK_PRESENT2)

            if first_bit and second_bit: present = True
            
        except Exception as e:
            print(f"[PLC_BARRIER] Loop error: {e}")

        return present


    def run(self):
        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_BARRIER] Ready, waiting for commands …")

        while True:
            try:
                data = self.mqtt.data
                print(f"[PLC_BARRIER] Truck Present -- {self.truck_present()}")
                if data and data.get("_consumed") is not True:
                    action    = data.get("action", "")
                    bucket_no = data.get("bucket_no", 0)
                    self.mqtt.data = {**data, "_consumed": True}
                    # self.red_signal()
                    # time.sleep(15)
                    # self.green_signal()

                    if action == "open_barrier":
                        self.open_barrier()
                        self.red_signal()
                    elif action == "close_barrier":
                        # if self.truck_present():
                        self.close_barrier()
                        self.green_signal()
                        # else:
                        #     print(f"[PLC_BARRIER] Couldn't close truck present !")
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