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

    def move_home(self):

        try:
            while True:
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
                time.sleep(0.5)
                if x_forward == 1: 
                    self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 0)
                    print(f"[PLC_SAMPLER] X forward is at Home")
                    break
                else: self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 1)

                time.sleep(0.5)
                
            while True:
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)

                time.sleep(0.5)
                if y_right == 1: 
                    self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
                    print(f"[PLC_SAMPLER] Y right is at Home")
                    break
                else: self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 1)

            while True:
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)
                z_up = self.plc.readIntFromPLC(self.client, Z_UP_SENSOR_FB)

                print(f"[PLC_SAMPLER] Sensor states  x_forward={x_forward}  y_right={y_right}  z_up={z_up}")
                if (x_forward == 1) and (y_right == 1) and (z_up == 1):
                    return True 

                time.sleep(0.5)
                    
        except Exception as e:
            print(f"[PLC_SAMPLER] Sensor read error: {e}")

        return False

    def move_x_reverse(self, duration):

        try:
            print("[PLC_SAMPLER] Moving X Axis Reverse")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                x_reverse = self.plc.readIntFromPLC(self.client, X_REVERSE_SENSOR_FB)
                if x_reverse == 0: self.plc.writeIntToPLC(self.client, X_AXIS_REVERSE, 1)
                else: break
                time.sleep(0.05)
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
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
                if x_forward == 0: self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 1)
                else: break
                time.sleep(0.05)
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
                y_left = self.plc.readIntFromPLC(self.client, Y_LEFT_SENSOR_FB)
                if y_left == 0: self.plc.writeIntToPLC(self.client, Y_AXIS_LEFT, 1)
                else: break
                time.sleep(0.05)
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
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)
                if y_right == 0: self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 1)
                else: break
                time.sleep(0.05)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
            time.sleep(0.5)

        except Exception as e:

            msg = f"Y right movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise


    def start_cycle(self):

        try:
            print("[PLC_SAMPLER] Starting sampler cycle")

            self.plc.writeIntToPLC(self.client, CYCLE_START, 1)
            time.sleep(1)
            self.plc.writeIntToPLC(self.client, CYCLE_START, 0)
            self.mqtt.publish(TOPIC_OUT, {"status": "cycle_started"})

        except Exception as e:

            msg = f"Cycle start error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise


    def stop_cycle(self):

        try:

            print("[PLC_SAMPLER] Stop cycle requested")

            time.sleep(120)
            self.plc.writeIntToPLC(self.client, CYCLE_STOP, 1)
            time.sleep(1)
            self.plc.writeIntToPLC(self.client, CYCLE_STOP, 0)
            self.mqtt.publish(TOPIC_OUT, {"status": "cycle_stopped"})

        except Exception as e:

            msg = f"Cycle stop error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})


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
                if data and data.get("_consumed") is not True:

                    action = data.get("action", "")
                    part = data.get("part", 0)
                    self.mqtt.data = {**data, "_consumed": True}

                    # if action == "move_x_reverse":
                    #     self.move_x_reverse((part*self.total_x)/100)
                    # elif action == "move_x_forward":
                    #     self.move_x_forward((part*self.total_x)/100)
                    # elif action == "move_y_left":
                    #     self.move_y_left((part*self.total_y)/100)
                    # elif action == "move_y_right":
                    #     self.move_y_right((part*self.total_y)/100)
                    # elif action == "move_home":
                    #     self.move_home()
                    # elif action == "sensors_ready":
                    #     self.sensors_ready()

                    if action == "sample_cycle_1":
                        if self.move_home():
                            self.move_y_left((100*self.total_y)/100)
                            self.move_x_reverse((100*self.total_x)/100)
                            time.sleep(5)
                            self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_1_comp"})
                    elif action == "sample_cycle_2":
                        if self.move_home():
                            self.move_y_left((50*self.total_y)/100)
                            self.move_x_reverse((50*self.total_x)/100)
                            time.sleep(5)
                            self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_2_comp"})
                    elif action == "sample_cycle_3":
                        if self.move_home():
                            self.move_y_left((25*self.total_y)/100)
                            self.move_x_reverse((25*self.total_x)/100)
                            time.sleep(5)
                            self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_3_comp"})
                    elif action == "sample_cycle_stop":
                        # self.stop_cycle()
                        self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_stop_comp"})

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