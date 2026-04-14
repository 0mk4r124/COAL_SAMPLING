import time
import traceback
import os

from DEPENDANT.SNAP7 import PLCCOMMINCATION
from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
LOGS_PATH = BASE_FILE_PATH + "LOGS/"

# Initialize logger
logger = initializeLogger("PLC_SAMPLER", LOGS_PATH=LOGS_PATH)

PLC_IP = "192.168.1.1"

DB_READ_1 = 24
DB_READ_2 = 20
DB_WRITE = 23

# INPUT OFFSETS
X_FORWORD_SENSOR_FB = 0
X_REVERSE_SENSOR_FB = 2
Y_LEFT_SENSOR_FB = 4
Y_RIGHT_SENSOR_FB = 6
Z_UP_SENSOR_FB = 8
Z_DOWN_SENSOR_FB = 10
EMERGENCY_STOP = 14
AUTO_MANUAL = 16
CYCLE_COMPLETE = 18
CYCLE_STATUS = 20

# OUTPUT OFFSETS
CYCLE_START = 0
CYCLE_STOP = 2
X_AXIS_FORWORD = 4
X_AXIS_REVERSE = 6
Y_AXIS_LEFT = 8
Y_AXIS_RIGHT = 10
HEARTBIT = 12

TOPIC_IN = "manager/plc_sampler"
TOPIC_OUT = "plc_sampler/status"

class SamplerController:

    def __init__(self, total_x, total_y):

        self.total_x = total_x
        self.total_y = total_y
        self.plc = PLCCOMMINCATION(PLC_IP, DB_READ_1, DB_WRITE, "REED")
        self.mqtt = MQTT("PLC_SAMPLER")
        self.client = self.plc.createConnection()
        self._emergency_state_last = 1  # Track last emergency state (1=normal, 0=emergency)

    def check_auto_manual(self):
        auto_manual = 0
        try:
            auto_manual = self.plc.readIntFromPLC(self.client, AUTO_MANUAL)
            print(f"[PLC_SAMPLER] Auto/Manual status: {auto_manual}")
            logger.debug(f"Auto/Manual status: {auto_manual}")
        except Exception as e:
            print(f"[PLC_SAMPLER] check_auto_manual error: {e}")
            logger.error(f"{traceback.format_exc()}")
            raise

        return auto_manual
    
    def check_sample_cycle_complete(self):
        sample_cycle_complete = 0
        try:
            sample_cycle_complete = self.plc.readIntFromPLC(self.client, CYCLE_STATUS)
            print(f"[PLC_SAMPLER] Cycle Complete status: {sample_cycle_complete}")
            logger.debug(f"Cycle Complete status: {sample_cycle_complete}")
        except Exception as e:
            print(f"[PLC_SAMPLER] check_sample_cycle_complete error: {e}")
            logger.error(f"{traceback.format_exc()}")
            raise

        return sample_cycle_complete
    
    def check_all_samples_status(self):
        sample_cycle_complete = 0
        try:
            sample_cycle_complete = self.plc.readIntFromPLC(self.client, CYCLE_COMPLETE)
            print(f"[PLC_SAMPLER] All Samples status: {sample_cycle_complete}")
            logger.debug(f"All Samples status: {sample_cycle_complete}")
            time.sleep(0.5)
        except Exception as e:
            print(f"[PLC_SAMPLER] check_all_samples_status error: {e}")
            logger.error(f"{traceback.format_exc()}")
            raise

        return sample_cycle_complete

    def sensors_ready(self):

        try:
            x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
            y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)
            z_up = self.plc.readIntFromPLC(self.client, Z_UP_SENSOR_FB)
            if x_forward == 1 and y_right == 1 and z_up == 1:
                return True

        except Exception as e:
            print(f"[PLC_SAMPLER] Sensor read error: {e}")
            logger.error(f"{traceback.format_exc()}")
            raise

        return False

    def move_home(self):

        try:
            while True:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                    self.reset()
                    return False
                
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)

                time.sleep(0.5)
                if x_forward == 1: 
                    self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 0)
                    logger.debug("0 - X_AXIS_FORWORD")
                    print(f"[PLC_SAMPLER] X forward is at Home")
                    break
                else: 
                    self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 1)
                    logger.debug("1 - X_AXIS_FORWORD")

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)
                
            while True:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                    self.reset()
                    return False
                
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)

                time.sleep(0.5)
                if y_right == 1: 
                    self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
                    logger.debug("0 - Y_AXIS_RIGHT")
                    print(f"[PLC_SAMPLER] Y right is at Home")
                    break
                else: 
                    self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 1)
                    logger.debug("1 - Y_AXIS_RIGHT")

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            while True:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                    self.reset()
                    return False
                
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
            logger.error(f"{traceback.format_exc()}")
            raise

    def move_x_reverse(self, duration):

        try:
            print("[PLC_SAMPLER] Moving X Axis Reverse")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                    self.reset()
                    return False

                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                x_reverse = self.plc.readIntFromPLC(self.client, X_REVERSE_SENSOR_FB)
                if x_reverse == 0: 
                    self.plc.writeIntToPLC(self.client, X_AXIS_REVERSE, 1)
                    logger.debug("1 - X_AXIS_REVERSE")
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, X_AXIS_REVERSE, 0)
            logger.debug("0 - X_AXIS_REVERSE")
            time.sleep(0.5)
            return True

        except Exception as e:
            msg = f"X reverse movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def move_x_forward(self, duration):

        try:
            print("[PLC_SAMPLER] Moving X Axis Forward")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                    self.reset()
                    return False

                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                x_forward = self.plc.readIntFromPLC(self.client, X_FORWORD_SENSOR_FB)
                if x_forward == 0: 
                    self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 1)
                    logger.debug("1 - X_AXIS_FORWORD")
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 0)
            logger.debug("0 - X_AXIS_FORWORD")
            time.sleep(0.5)
            return True

        except Exception as e:
            msg = f"X forward movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def move_y_left(self, duration):

        try:
            print("[PLC_SAMPLER] Moving Y Axis Left")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                    self.reset()
                    return False
                
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                y_left = self.plc.readIntFromPLC(self.client, Y_LEFT_SENSOR_FB)
                if y_left == 0: 
                    self.plc.writeIntToPLC(self.client, Y_AXIS_LEFT, 1)
                    logger.debug("1 - Y_AXIS_LEFT")
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, Y_AXIS_LEFT, 0)
            logger.debug("0 - Y_AXIS_LEFT")
            time.sleep(0.5)
            return True

        except Exception as e:
            msg = f"Y left movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def move_y_right(self, duration):

        try:
            print("[PLC_SAMPLER] Moving Y Axis Right")
            start_time = time.time()
            while (time.time() - start_time) < duration:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                    self.reset()
                    return False
                
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                y_right = self.plc.readIntFromPLC(self.client, Y_RIGHT_SENSOR_FB)
                if y_right == 0: 
                    self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 1)
                    logger.debug("1 - Y_AXIS_RIGHT")
                else: break

                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
            logger.debug("0 - Y_AXIS_RIGHT")
            time.sleep(0.5)
            return True

        except Exception as e:
            msg = f"Y right movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def start_cycle(self, cycle: int = 1):

        try:
            print(f"[PLC_SAMPLER] Starting sampling cycle {cycle}")
            time.sleep(1)

            self.plc.writeIntToPLC(self.client, CYCLE_START, 1)
            logger.debug("1 - CYCLE_START")
            time.sleep(1)
            self.plc.writeIntToPLC(self.client, HEARTBIT, 1)

            print(f"[PLC_SAMPLER] Waiting until FB 1")
            while True:
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
                return_read = self.plc.readIntFromPLC(self.client, CYCLE_START, DB_READ_NUMBER=DB_WRITE)
                print(f"[PLC_SAMPLER] return_read -- {return_read}")
                if (return_read == 1) or (return_read == "1"): break
                else:
                    self.plc.writeIntToPLC(self.client, CYCLE_START, 1)
                    logger.debug("1 - CYCLE_START")
                time.sleep(0.5)
                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                time.sleep(0.5)

            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, CYCLE_START, 0)
            logger.debug("0 - CYCLE_START")
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
            print(f"[PLC_SAMPLER] Cycle {cycle} initiated.")

        except Exception as e:
            msg = f"Cycle start error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "cycle_error", "msg": msg})
            raise

    def stop_cycle(self):

        try:
            print("[PLC_SAMPLER] Stop cycle requested")
            self.plc.writeIntToPLC(self.client, CYCLE_STOP, 1)
            logger.debug("1 - CYCLE_STOP")
            time.sleep(1)
            self.plc.writeIntToPLC(self.client, CYCLE_STOP, 0)
            logger.debug("0 - CYCLE_STOP")

        except Exception as e:
            msg = f"Cycle stop error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def reset(self):
        """Reset all PLC outputs to 0"""
        try:
            print("[PLC_SAMPLER] Resetting PLC outputs …")
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, X_AXIS_FORWORD, 0)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, X_AXIS_REVERSE, 0)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, Y_AXIS_LEFT, 0)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, Y_AXIS_RIGHT, 0)
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
            self.mqtt.publish(TOPIC_OUT, {"status": "reset_done"})
            print("[PLC_SAMPLER] PLC reset complete.")
            logger.debug("Wrote 0 to |X_AXIS_FORWORD|X_AXIS_REVERSE|Y_AXIS_LEFT|Y_AXIS_RIGHT|")
            self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
        except Exception as e:
            msg = f"Reset error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "reset_error", "msg": msg})
            raise
    
    def wait_for_emergency_clearance(self):
        counter = time.time()
        while True:
            try:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                time.sleep(0.5)
                self.plc.writeIntToPLC(self.client, HEARTBIT, 1)
                
                # Detect transition from emergency (0) to cleared (1)
                if self._emergency_state_last == 0 and emergency == 1:
                    print("[PLC_SAMPLER] Emergency stop has been cleared!")
                    logger.debug("Emergency stop has been cleared!")
                    self.mqtt.publish(TOPIC_OUT, {"status": "emergency_cleared"})
                    break
                
                else:
                    if time.time() - counter > 2:  # Log every 2 seconds if still in emergency
                        print("[PLC_SAMPLER] Emergency stop activated!")
                        self.mqtt.publish(TOPIC_OUT, {"status": "emergency_stop"})
                        counter = time.time()
                
                self._emergency_state_last = emergency
                self.plc.writeIntToPLC(self.client, HEARTBIT, 0)
            
            except Exception as e:
                print(f"[PLC_SAMPLER] Emergency status read error: {e}")
                logger.error(f"{traceback.format_exc()}")
                raise

    def run(self):

        self.mqtt.subscribe(TOPIC_IN)
        print("[PLC_SAMPLER] Ready, waiting for commands …")

        while True:

            if not self.plc.writeIntToPLC(self.client, HEARTBIT, 1): break

            self._emergency_state_last = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
            if self._emergency_state_last == 0:
                print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                self.mqtt.publish(TOPIC_OUT, {"status": "emergency_stop", "msg": "Code stopped manually due to emergency !"})
                self.reset()
                self.wait_for_emergency_clearance()

            data = self.mqtt.data
            if data and data.get("_consumed") is not True:

                action = data.get("action", "")
                self.mqtt.data = {**data, "_consumed": True}

                if action == "move_y_right":
                    duration = data.get("duration", 0)
                    self.move_y_right(duration)
                elif action == "move_y_left":
                    duration = data.get("duration", 0)
                    self.move_y_left(duration)
                elif action == "move_x_forward":
                    duration = data.get("duration", 0)
                    self.move_x_forward(duration)
                elif action == "move_x_reverse":
                    duration = data.get("duration", 0)
                    self.move_x_reverse(duration)

                elif action == "auto_manual":
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
                    self.mqtt.publish(TOPIC_OUT, {"status": "cycle_start_given"})
                elif action == "sample_cycle":
                    if self.move_home():
                        x = data.get("x", 0)
                        y = data.get("y", 0)
                        if not self.move_y_left((y*self.total_y)/100): continue
                        if not self.move_x_reverse((x*self.total_x)/100): continue
                        time.sleep(2)
                        self.mqtt.publish(TOPIC_OUT, {"status": "position_set"})
                elif action == "check_sample_cycle_complete":
                    if self.check_sample_cycle_complete():
                        self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_complete"})
                    else:
                        self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_not_complete"})
                elif action == "check_all_samples_status":
                    if self.check_all_samples_status(): self.mqtt.publish(TOPIC_OUT, {"status": "all_samples_collected"})
                    else:  self.mqtt.publish(TOPIC_OUT, {"status": "all_samples_not_collected"})
                elif action == "sample_cycle_stop":
                    self.stop_cycle()
                    self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_stop_comp"})

                elif action == "reset":
                    self.reset()

            self._emergency_state_last = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
            if self._emergency_state_last == 0:
                print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                self.mqtt.publish(TOPIC_OUT, {"status": "emergency_stop", "msg": "Code stopped manually due to emergency !"})
                self.reset()
                self.wait_for_emergency_clearance()
            
            if not self.plc.writeIntToPLC(self.client, HEARTBIT, 1): break
            time.sleep(0.5)

def main():
    total_x = 74
    total_y = 30

    while True:
        controller = SamplerController(total_x, total_y)
        controller.run()

        time.sleep(1)
        del controller
        time.sleep(2)


if __name__ == "__main__":
    main()