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

        # ── Current auger position, in SECONDS of travel from home ────────────
        # None = position unknown (after boot / reset / emergency / cycle stop)
        # 0.0  = at home
        self.current_x_time: float | None = None
        self.current_y_time: float | None = None

        # ── Z-UP feedback cycle phase machine ─────────────────────────────────
        # "idle"      -> no cycle armed
        # "armed"     -> cycle start given, waiting for Z_UP FB to go DOWN (1 -> 0)
        # "down_seen" -> Z went down, waiting for Z_UP FB to come back UP (0 -> 1)
        # "complete"  -> Z came back up -> the sampling cycle is complete
        self._z_phase = "idle"
        # Push sample_cycle_complete to the manager the moment Z comes back up
        # (instead of waiting for the manager's next poll)
        self._z_complete_published = False

        # Drive X and Y at the same time during positioning (much faster than
        # sequential Y-then-X). Set False to fall back to sequential moves.
        self.simultaneous_xy = True

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

    def check_emergency(self):
        """
        Read the LIVE emergency bit (1 = normal, 0 = emergency).
        Used by the manager to verify actual emergency state instead of
        relying on possibly-stale queued MQTT messages.
        """
        try:
            emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
            print(f"[PLC_SAMPLER] Live emergency status: {emergency} (1=normal, 0=emergency)")
            logger.debug(f"Live emergency status: {emergency} (1=normal, 0=emergency)")
            return emergency
        except Exception as e:
            print(f"[PLC_SAMPLER] check_emergency error: {e}")
            logger.error(f"{traceback.format_exc()}")
            raise

    # ── Z-UP FB cycle tracking ────────────────────────────────────────────────

    def arm_z_cycle(self):
        """Arm the Z-UP FB watcher. Called right after CYCLE_START is pulsed."""
        self._z_phase = "armed"
        self._z_complete_published = False
        print("[PLC_SAMPLER] Z-cycle armed — waiting for Z_UP FB 1 -> 0 -> 1")
        logger.debug("Z-cycle armed — waiting for Z_UP FB 1 -> 0 -> 1")

    def update_z_cycle(self):
        """
        Poll Z_UP_SENSOR_FB and advance the phase machine.
        Called on every iteration of run() so the DOWN (0) state is never
        missed between the manager's status polls.
        """
        if self._z_phase in ("idle", "complete"):
            return

        try:
            z_up = self.plc.readIntFromPLC(self.client, Z_UP_SENSOR_FB)
        except Exception as e:
            print(f"[PLC_SAMPLER] update_z_cycle read error: {e}")
            logger.error(f"{traceback.format_exc()}")
            return

        if self._z_phase == "armed" and z_up == 0:
            self._z_phase = "down_seen"
            print("[PLC_SAMPLER] Z_UP FB went 1 -> 0 (auger down)")
            logger.debug("Z_UP FB went 1 -> 0 (auger down)")

        elif self._z_phase == "down_seen" and z_up == 1:
            self._z_phase = "complete"
            print("[PLC_SAMPLER] Z_UP FB went 0 -> 1 (auger back up) — cycle COMPLETE")
            logger.debug("Z_UP FB went 0 -> 1 — cycle COMPLETE")

    def check_sample_cycle_complete(self):
        """
        Cycle is complete when the Z-UP FB has gone 1 -> 0 -> 1 since the
        cycle start was given (instead of the old CYCLE_STATUS tag).
        """
        try:
            self.update_z_cycle()
            print(f"[PLC_SAMPLER] Z-cycle phase: {self._z_phase}")
            logger.debug(f"Z-cycle phase: {self._z_phase}")
        except Exception as e:
            print(f"[PLC_SAMPLER] check_sample_cycle_complete error: {e}")
            logger.error(f"{traceback.format_exc()}")
            raise

        return 1 if self._z_phase == "complete" else 0

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
                    # Auger confirmed at home — position is now known
                    self.current_x_time = 0.0
                    self.current_y_time = 0.0
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

    # ── Positioning (absolute via home / relative from current position) ─────

    def move_xy(self, dx, dy):
        """
        Drive X and Y axes SIMULTANEOUSLY (single loop) — travel time becomes
        max(|dx|, |dy|) instead of |dx| + |dy|.

        dx > 0 -> X reverse for dx seconds   |  dx < 0 -> X forward for |dx| s
        dy > 0 -> Y left    for dy seconds   |  dy < 0 -> Y right   for |dy| s

        Each axis stops on its own timer or when its limit sensor trips.
        """
        x_dur = abs(dx)
        y_dur = abs(dy)
        x_out = X_AXIS_REVERSE      if dx > 0 else X_AXIS_FORWORD
        x_fb  = X_REVERSE_SENSOR_FB if dx > 0 else X_FORWORD_SENSOR_FB
        y_out = Y_AXIS_LEFT         if dy > 0 else Y_AXIS_RIGHT
        y_fb  = Y_LEFT_SENSOR_FB    if dy > 0 else Y_RIGHT_SENSOR_FB
        x_name = "X_AXIS_REVERSE" if dx > 0 else "X_AXIS_FORWORD"
        y_name = "Y_AXIS_LEFT"    if dy > 0 else "Y_AXIS_RIGHT"

        x_active = x_dur > 0.2
        y_active = y_dur > 0.2

        try:
            print(f"[PLC_SAMPLER] Simultaneous move  X({x_name}): {x_dur:.1f}s  Y({y_name}): {y_dur:.1f}s")
            logger.debug(f"Simultaneous move  X({x_name}): {x_dur:.1f}s  Y({y_name}): {y_dur:.1f}s")

            start = time.time()
            heart = 0

            while x_active or y_active:
                emergency = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
                if emergency == 0:
                    print(f"[PLC_SAMPLER] Emergency stop activated during move")
                    logger.warning("Emergency stop activated during simultaneous move")
                    self.plc.writeIntToPLC(self.client, x_out, 0)
                    self.plc.writeIntToPLC(self.client, y_out, 0)
                    self.reset()
                    return False

                elapsed = time.time() - start

                # X axis
                if x_active:
                    x_hit = self.plc.readIntFromPLC(self.client, x_fb)
                    if (elapsed >= x_dur) or (x_hit == 1):
                        self.plc.writeIntToPLC(self.client, x_out, 0)
                        logger.debug(f"0 - {x_name}")
                        x_active = False
                    else:
                        self.plc.writeIntToPLC(self.client, x_out, 1)
                        logger.debug(f"1 - {x_name}")

                # Y axis
                if y_active:
                    y_hit = self.plc.readIntFromPLC(self.client, y_fb)
                    if (elapsed >= y_dur) or (y_hit == 1):
                        self.plc.writeIntToPLC(self.client, y_out, 0)
                        logger.debug(f"0 - {y_name}")
                        y_active = False
                    else:
                        self.plc.writeIntToPLC(self.client, y_out, 1)
                        logger.debug(f"1 - {y_name}")

                # Heartbeat toggle
                heart = 1 - heart
                self.plc.writeIntToPLC(self.client, HEARTBIT, heart)
                time.sleep(0.5)

            # Make sure both outputs are off
            time.sleep(0.5)
            self.plc.writeIntToPLC(self.client, x_out, 0)
            self.plc.writeIntToPLC(self.client, y_out, 0)
            time.sleep(0.5)
            return True

        except Exception as e:
            msg = f"Simultaneous XY movement error: {e}"
            print(f"[PLC_SAMPLER] {msg}")
            logger.error(f"{traceback.format_exc()}")
            self.mqtt.publish(TOPIC_OUT, {"status": "sampler_error", "msg": msg})
            raise

    def move_to_position(self, x_pct, y_pct, direct=False):
        """
        Move the auger to (x_pct, y_pct) — percentages of full travel.

        direct=False (or position unknown): go HOME first, then absolute move
                                            (old behaviour — used for cycle 1).
        direct=True  and position known:    move RELATIVE from the current
                                            position WITHOUT homing
                                            (new behaviour — cycles 2 & 3).

        Sign convention (seconds of travel measured from home):
            +dX -> move_x_reverse   |  -dX -> move_x_forward
            +dY -> move_y_left      |  -dY -> move_y_right
        """
        target_x = (x_pct * self.total_x) / 100.0
        target_y = (y_pct * self.total_y) / 100.0

        # Fall back to homing if a direct move was not requested,
        # or if we don't know where the auger currently is.
        if (not direct) or (self.current_x_time is None) or (self.current_y_time is None):
            if not self.move_home():
                return False
            # move_home() sets current_x_time / current_y_time to 0.0

        dx = target_x - self.current_x_time
        dy = target_y - self.current_y_time

        print(f"[PLC_SAMPLER] Move to ({x_pct}%, {y_pct}%) -> "
              f"target ({target_x:.1f}s, {target_y:.1f}s), "
              f"delta (dX={dx:+.1f}s, dY={dy:+.1f}s), direct={direct}")
        logger.debug(f"Move to ({x_pct}%,{y_pct}%) target ({target_x:.1f}s,{target_y:.1f}s) "
                     f"delta ({dx:+.1f}s,{dy:+.1f}s) direct={direct}")

        if self.simultaneous_xy:
            # Drive both axes at once — travel time = max(|dX|, |dY|)
            if (abs(dx) > 0.2) or (abs(dy) > 0.2):
                if not self.move_xy(dx, dy): return False
        else:
            # Sequential fallback: Y first, then X (old behaviour)
            if dy > 0.2:
                if not self.move_y_left(dy):     return False
            elif dy < -0.2:
                if not self.move_y_right(-dy):   return False

            if dx > 0.2:
                if not self.move_x_reverse(dx):  return False
            elif dx < -0.2:
                if not self.move_x_forward(-dx): return False

        # Update tracked position (clamped to physical travel limits)
        self.current_x_time = max(0.0, min(target_x, float(self.total_x)))
        self.current_y_time = max(0.0, min(target_y, float(self.total_y)))
        return True

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

            # Arm the Z-UP FB watcher: cycle completes when Z goes 1 -> 0 -> 1
            self.arm_z_cycle()

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

            # After cycle stop the machine handles the auger on its own —
            # tracked position can no longer be trusted.
            self.current_x_time = None
            self.current_y_time = None
            self._z_phase = "idle"

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

            # Invalidate tracked state — position unknown after reset
            self.current_x_time = None
            self.current_y_time = None
            self._z_phase = "idle"

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
                    if time.time() - counter > 3:  # Log every 3 seconds if still in emergency
                        print("[PLC_SAMPLER] Emergency stop activated!")
                        logger.warning("Emergency stop still active — waiting for clearance")
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
                logger.warning("Emergency stop activated — resetting outputs and waiting for clearance")
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
                    # direct=True  -> travel from the current position (no homing)
                    # direct=False -> home first, then absolute move (cycle 1)
                    x = data.get("x", 0)
                    y = data.get("y", 0)
                    direct = data.get("direct", False)
                    if not self.move_to_position(x, y, direct=direct): continue
                    time.sleep(0.5)
                    self.mqtt.publish(TOPIC_OUT, {"status": "position_set"})
                elif action == "check_emergency":
                    # Live emergency verification for the manager — answers with
                    # the ACTUAL current state so stale queued messages can't
                    # deadlock the manager's emergency-wait state.
                    if self.check_emergency() == 1:
                        self.mqtt.publish(TOPIC_OUT, {"status": "emergency_cleared"})
                    else:
                        self.mqtt.publish(TOPIC_OUT, {"status": "emergency_stop"})
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

            # Keep the Z-UP FB watcher running on every loop pass (~0.5 s)
            # so the 1 -> 0 dip is caught even between manager polls.
            self.update_z_cycle()

            # PUSH completion to the manager the moment Z comes back up —
            # removes the ~10 s poll-detection lag per cycle.
            if self._z_phase == "complete" and not self._z_complete_published:
                self._z_complete_published = True
                self.mqtt.publish(TOPIC_OUT, {"status": "sample_cycle_complete"})
                print("[PLC_SAMPLER] sample_cycle_complete pushed to manager")
                logger.debug("sample_cycle_complete pushed to manager")

            self._emergency_state_last = self.plc.readIntFromPLC(self.client, EMERGENCY_STOP)
            if self._emergency_state_last == 0:
                print(f"[PLC_SAMPLER] Emergency stop activated, cannot move to home")
                logger.warning("Emergency stop activated — resetting outputs and waiting for clearance")
                self.mqtt.publish(TOPIC_OUT, {"status": "emergency_stop", "msg": "Code stopped manually due to emergency !"})
                self.reset()
                self.wait_for_emergency_clearance()

            if not self.plc.writeIntToPLC(self.client, HEARTBIT, 1): break
            time.sleep(0.5)

def main():
    total_x = 28
    total_y = 13

    while True:
        controller = SamplerController(total_x, total_y)
        controller.run()

        time.sleep(1)
        del controller
        time.sleep(2)


if __name__ == "__main__":
    main()