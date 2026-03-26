import time

from DEPENDANT.SNAP7 import PLCCOMMINCATION
from DEPENDANT.MQTT import MQTT


PLC_IP = "192.168.1.1"

DB_READ = 24
DB_WRITE = 23

# INPUT OFFSETS
X_FORWORD_SENSOR_FB = 0
X_REVERSE_SENSOR_FB = 2
Y_LEFT_SENSOR_FB = 4
Y_RIGHT_SENSOR_FB = 6
Z_UP_SENSOR_FB = 8
Z_DOWN_SENSOR_FB = 10
HEARTBIT = 12
AUTO_MANUAL = 14
CYCLE_COMPLETE = 16

# OUTPUT OFFSETS
CYCLE_START = 0
CYCLE_STOP = 2
X_AXIS_FORWORD = 4
X_AXIS_REVERSE = 6
Y_AXIS_LEFT = 8
Y_AXIS_RIGHT = 10

TOPIC_IN = "manager/plc_sampler"
TOPIC_OUT = "plc_sampler/status"

class SamplerController:

    def __init__(self, total_x, total_y):

        self.total_x = total_x
        self.total_y = total_y
        self.plc = PLCCOMMINCATION(PLC_IP, DB_READ, DB_WRITE, "REED")
        self.mqtt = MQTT("PLC_SAMPLER")
        self.client = self.plc.createConnection()

    def check_auto_manual(self):
        auto_manual = 0
        try:

            auto_manual = self.plc.readIntFromPLC(self.client, AUTO_MANUAL)
            
        except Exception as e:
            print(f"[PLC_BARRIER] Truck presence error: {e}")

        return auto_manual
    
    def check_sample_cycle_complete(self):
        sample_cycle_complete = 0
        try:

            sample_cycle_complete = self.plc.readIntFromPLC(self.client, CYCLE_COMPLETE)
            
        except Exception as e:
            print(f"[PLC_BARRIER] Truck presence error: {e}")

        return sample_cycle_complete
    
    def sensors_ready(self):

        try:
            x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
            y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)
            z_up = self.plc.readIntFromPLC(self.client, Z_UP_SENSOR_FB)
            # x_reverse = self.plc.readIntFromPLC(self.client, X_REVERSE_SENSOR_FB)
            # y_left = self.plc.readIntFromPLC(self.client, Y_LEFT_SENSOR_FB)
            # z_down = self.plc.readIntFromPLC(self.client, Z_DOWN_SENSOR_FB)
            if x_forward == 1 and y_right == 1 and z_up == 1:
                return True

        except Exception as e:
            print(f"[PLC_SAMPLER] Sensor read error: {e}")

        return False

    def set_position(self, x: int, y: int):
        try:
            print(f"[PLC_SAMPLER] Setting position X={x}, Y={y}")
            
            # Set X axis
            if x == 1:  # Forward/Home
                self.move_x_forward(30)
            else:  # Reverse
                self.move_x_reverse(30)
            
            # Set Y axis
            if y == 1:  # Right/Home
                self.move_y_right(30)
            else:  # Left
                self.move_y_left(30)
            
            self.mqtt.publish(TOPIC_OUT, {"status": "position_set"})
            print("[PLC_SAMPLER] Position set complete.")
        except Exception as e:
            msg = f"Set position error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})

    def move_home(self):

        try:
            while True:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)

                time.sleep(0.5)
                if x_forward == 1: 
                    self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 0)
                    print(f"[PLC_SAMPLER] X forward is at Home")
                    break
                else: self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 1)

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)
                
            while True:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)

                time.sleep(0.5)
                if y_right == 1: 
                    self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
                    print(f"[PLC_SAMPLER] Y right is at Home")
                    break
                else: self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 1)
                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            while True:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)
                z_up = self.plc.readIntFromPLC(self.client, Z_UP_SENSOR_FB)

                time.sleep(0.5)
                print(f"[PLC_SAMPLER] Sensor states  x_forward={x_forward}  y_right={y_right}  z_up={z_up}")
                if (x_forward == 1) and (y_right == 1) and (z_up == 1):
                    return True 

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)
                    
        except Exception as e:
            print(f"[PLC_SAMPLER] Sensor read error: {e}")

        return False

    def move_x_reverse(self, duration):

        try:
            print("[PLC_SAMPLER] Moving X Axis Reverse")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                x_reverse = self.plc.readIntFromPLC(self.client, X_REVERSE_SENSOR_FB)
                if x_reverse == 0: self.plc.writeIntToPLC(self.client, X_AXIS_REVERSE, 1)
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, X_AXIS_REVERSE, 0)
            time.sleep(0.5)

        except Exception as e:

            msg = f"X reverse movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def move_x_forward(self, duration):

        try:
            print("[PLC_SAMPLER] Moving X Axis Forward")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
                if x_forward == 0: self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 1)
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 0)
            time.sleep(0.5)

        except Exception as e:

            msg = f"X forward movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def move_y_left(self, duration):

        try:
            print("[PLC_SAMPLER] Moving Y Axis Left")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                y_left = self.plc.readIntFromPLC(self.client, Y_LEFT_SENSOR_FB)
                if y_left == 0: self.plc.writeIntToPLC(self.client, Y_AXIS_LEFT, 1)
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, Y_AXIS_LEFT, 0)
            time.sleep(0.5)

        except Exception as e:

            msg = f"Y left movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def move_y_right(self, duration):

        try:
            print("[PLC_SAMPLER] Moving Y Axis Right")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)
                if y_right == 0: self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 1)
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
            time.sleep(0.5)

        except Exception as e:

            msg = f"Y right movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def start_cycle(self, cycle: int = 1):

        try:
            print(f"[PLC_SAMPLER] Starting sampling cycle {cycle}")

            self.plc.writeIntToPLC(self.client, CYCLE_START, 1)
            time.sleep(1)
            self.plc.writeIntToPLC(self.client, CYCLE_START, 0)
            print(f"[PLC_SAMPLER] Cycle {cycle} initiated.")

        except Exception as e:

            msg = f"Cycle start error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "cycle_error", "msg": msg})

    def stop_cycle(self):

        try:

            print("[PLC_SAMPLER] Stop cycle requested")

            time.sleep(120)
            self.plc.writeIntToPLC(self.client, CYCLE_STOP, 1)
            time.sleep(1)
            self.plc.writeIntToPLC(self.client, CYCLE_STOP, 0)

        except Exception as e:

            msg = f"Cycle stop error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})

    def reset(self):
        """Reset all PLC outputs to 0"""
        try:
            print("[PLC_SAMPLER] Resetting PLC outputs …")
            self.plc.writeIntToPLC(self.client, CYCLE_START, 0)
            self.plc.writeIntToPLC(self.client, CYCLE_STOP, 0)
            self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 0)
            self.plc.writeIntToPLC(self.client, X_AXIS_REVERSE, 0)
            self.plc.writeIntToPLC(self.client, Y_AXIS_LEFT, 0)
            self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
            self.mqtt.publish(TOPIC_OUT, {"status": "reset_done"})
            print("[PLC_SAMPLER] PLC reset complete.")
        except Exception as e:
            msg = f"Reset error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "reset_error", "msg": msg})

    def run_cycle(self, x_duration):

        try:
            print("[PLC_SAMPLER] Waiting for sensors")
            duration = 120
            start_time = time.time()
            while True:
                if (time.time() - start_time) > duration:
                    print("[PLC_SAMPLER] Sensors not ready")
                    msg = "Error"
                    self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
                    break

                if self.sensors_ready():
                    print("[PLC_SAMPLER] Sensors ready")
                    break
                time.sleep(0.2)

            self.move_x_reverse(x_duration)
            self.move_y_left(self.total_y / 2)
            self.start_cycle()

        except Exception as e:

            msg = f"Cycle execution error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})


    def run(self):

        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_SAMPLER] Ready, waiting for commands …")

        while True:
            try:

                data = self.mqtt.data
                # X_FORWORD_SENSOR_FB
                # self.plc.writeIntToPLC(self.client, 0, 1)
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                if data and data.get("_consumed") is not True:

                    action = data.get("action", "")
                    self.mqtt.data = {**data, "_consumed": True}

                    if action == "auto_manual":
                        if self.check_auto_manual():
                            self.mqtt.publish(TOPIC_OUT, {"status": "auto_manual_on"})
                        else:
                            self.mqtt.publish(TOPIC_OUT, {"status": "auto_manual_off"})
                    elif action == "move_home":
                        if self.move_home():
                            self.mqtt.publish(TOPIC_OUT, {"status": "auger_home"})
                            

                    elif action == "start_cycle":
                        cycle = data.get("cycle", 1)
                        self.start_cycle(cycle)
                    elif action == "sample_cycle":
                        if self.move_home():
                            x = data.get("x", 0)
                            y = data.get("y", 0)
                            self.move_y_left((x*self.total_y)/100)
                            self.move_x_reverse((y*self.total_x)/100)
                            time.sleep(2)
                            self.mqtt.publish(TOPIC_OUT, {"status": "position_set"})
                    elif action == "check_sample_cycle_complete":
                        if self.check_sample_cycle_complete():
                            self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_complete"})
                    elif action == "sample_cycle_stop":
                        self.stop_cycle()
                        self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_stop_comp"})

                    elif action == "reset":
                        self.reset()

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            except Exception as e:
                msg = f"Loop error: {e}"
                print(f"[PLC_SAMPLER] {msg}")
                self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})

            time.sleep(0.05)

def main():
    total_x = 70
    total_y = 30

    controller = SamplerController(total_x, total_y)
    controller.run()


if __name__ == "__main__":
    main()