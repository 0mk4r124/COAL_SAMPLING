import time

from DEPENDANT.SNAP7 import PLCCOMMINCATION
from DEPENDANT.MQTT  import MQTT


PLC_IP   = "192.168.1.2"        
DB_READ  = 10
DB_WRITE = 11

BOOM_BARRIER_OPEN = 0
BOOM_BARRIER_CLOSE = 2
TRUN_TABLE_COUNT = 4
TRUN_TABLE_CLOCKWISE = 6
TRUN_TABLE_VALUE_WRITE = 8
GREEN_SIGNAL = 10
RED_SIGNAL = 12
HEARTBIT = 14

TRUCK_PRESENT1 = 0
TRUCK_PRESENT2 = 2
BOOM_BARRIER_OPEN_FB = 4
BOOM_BARRIER_CLOSE_FB = 6
TRUN_TABLE_HOME_P = 16
TRUN_TABLE_COUNT_READ = 30

BARRIER_OPEN_TIMEOUT = 10
SET_BUCKET_TIMEOUT = 10

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
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                open_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_OPEN_FB)
                close_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_CLOSE_FB)
                if (open_bit == 1) and (close_bit == 0):
                    print("[PLC_BARRIER] Barrier OPEN confirmed.")
                    self.mqtt.publish(TOPIC_OUT, {"status": "barrier_opened"})
                    return
                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
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
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                open_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_OPEN_FB)
                close_bit = self.plc.readIntFromPLC(self.client, BOOM_BARRIER_CLOSE_FB)
                if (open_bit == 0) and (close_bit == 1):
                    print("[PLC_BARRIER] Barrier OPEN confirmed.")
                    self.mqtt.publish(TOPIC_OUT, {"status": "barrier_closed"})
                    return
                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
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
            self.mqtt.publish(TOPIC_OUT, {"status": "green_sent"})
        except Exception as e:
            msg = f"Green Signal error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "signal_error", "msg": msg})

    def red_signal(self):
        try:
            print("[PLC_BARRIER] Red Signal …")
            self.plc.writeIntToPLC(self.client, GREEN_SIGNAL, 0)
            self.plc.writeIntToPLC(self.client, RED_SIGNAL, 1)
            self.mqtt.publish(TOPIC_OUT, {"status": "red_sent"})
        except Exception as e:
            msg = f"Green Signal error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "signal_error", "msg": msg})

    def set_bucket(self, bucket_number):
        try:
            
            print("[PLC_BARRIER] Red Signal …")
            self.plc.writeIntToPLC(self.client, TRUN_TABLE_CLOCKWISE, 1)

            while True:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                home_bit = self.plc.readIntFromPLC(self.client, TRUN_TABLE_HOME_P)
                if home_bit == 1:
                    self.plc.writeIntToPLC(self.client, TRUN_TABLE_CLOCKWISE, 0)
                    time.sleep(2)
                    break
                else: self.plc.writeIntToPLC(self.client, TRUN_TABLE_CLOCKWISE, 1)

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)
                
            self.plc.writeIntToPLC(self.client, TRUN_TABLE_VALUE_WRITE, bucket_number)
            self.plc.writeIntToPLC(self.client, TRUN_TABLE_COUNT, 1)

            while True:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                bucket_bit = self.plc.readIntFromPLC(self.client, TRUN_TABLE_COUNT_READ)
                if bucket_bit == bucket_number: 
                    self.plc.writeIntToPLC(self.client, TRUN_TABLE_COUNT, 0)
                    self.mqtt.publish(TOPIC_OUT, {"status": "bucket_set"})
                    break
                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

        except Exception as e:
            msg = f"Green Signal error: {e}"
            print(f"[PLC_BARRIER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "signal_error", "msg": msg})

    def truck_present(self):
        present = False
        try:

            first_bit = self.plc.readIntFromPLC(self.client, TRUCK_PRESENT1)
            second_bit = self.plc.readIntFromPLC(self.client, TRUCK_PRESENT2)

            print(first_bit, "--",second_bit)
            if first_bit and second_bit: present = True
            
        except Exception as e:
            print(f"[PLC_BARRIER] Loop error: {e}")

        return present

    def run(self):
        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_BARRIER] Ready, waiting for commands …")

        while True:
            try:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)

                data = self.mqtt.data

                if data and data.get("_consumed") is not True:
                    action    = data.get("action", "")
                    bucket_no = data.get("bucket_no", 0)
                    self.mqtt.data = {**data, "_consumed": True}

                    if action == "open_barrier":
                        self.open_barrier()
                    elif action == "close_barrier":
                        self.close_barrier()
                    elif action == "check_truck":
                        if self.truck_present():
                            self.mqtt.publish(TOPIC_OUT, {"status": "truck", "present": True})
                            time.sleep(2)
                    elif action == "set_bucket":
                        print(f"[PLC_BARRIER] Setting Bucket")
                        self.set_bucket(bucket_no)
                    elif action == "green_signal":
                        self.green_signal()
                    elif action == "red_signal":
                        self.red_signal()
                    else:
                        print(f"[PLC_BARRIER] Unknown action: {action}")

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            except Exception as e:
                print(f"[PLC_BARRIER] Loop error: {e}")

            time.sleep(0.05)

def main():
    controller = BarrierController()
    controller.run()

if __name__ == "__main__":
    main()