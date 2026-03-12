import os
import time
import threading
import cv2

from DEPENDANT.IP   import IPCamera
from DEPENDANT.MQTT import MQTT

SAVE_PATH = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/"

CAM_CONFIGS = {
    "CAM1": {"ip": "192.168.1.201", "user": "admin", "password": "insightzz@123", "name": "CAM1"},
    "CAM2": {"ip": "192.168.1.202", "user": "admin", "password": "insightzz@123", "name": "CAM2"},
    "CAM3": {"ip": "192.168.1.203", "user": "admin", "password": "insightzz@123", "name": "CAM3"},
}

TOPIC_IN  = "manager/camera"
TOPIC_OUT = "camera/status"
CAM13_INTERVAL = 0.5


def save_frame(img, uid: str, cam_name: str, cycle: int = 0) -> str:
    folder = os.path.join(SAVE_PATH, uid, cam_name, f"cycle_{cycle}" if cycle else "")
    os.makedirs(folder, exist_ok=True)
    filename = os.path.join(folder, f"{cam_name}_{int(time.time() * 1000)}.jpg")
    cv2.imwrite(filename, img)

    return filename

class CamController:

    def __init__(self):
        self.cams   = {k: IPCamera(v) for k, v in CAM_CONFIGS.items()}
        self.mqtt   = MQTT("CAM_CAPTURE")
        self._lock  = threading.Lock()

        self._cam13_active = False
        self._cam13_uid    = None
        self._cam13_cycle  = 0

        self._thread: threading.Thread | None = None

    def initialize(self):
        for name, cam in self.cams.items():
            ok = cam.initialize()
            print(f"[CAM_CAPTURE] {name} init → {'OK' if ok else 'FAILED'}")

    def _on_command(self, action: str, uid: str, cycle: int):

        print(f"[CAM_CAPTURE] Command  action={action}  uid={uid}  cycle={cycle}")

        if action == "cam2_single":
            self._capture_cam2(uid)
        elif action == "cam13_start":
            self._start_cam13(uid, cycle)
        elif action in ("cam13_stop", "reset"):
            self._stop_cam13()
        else:
            print(f"[CAM_CAPTURE] Unknown action: {action}")

    def _capture_cam2(self, uid: str):
        try:
            img = self.cams["CAM2"].capture(save=False)
            if img is None:
                raise RuntimeError("CAM2 returned None")
            path = save_frame(img, uid, "CAM2", cycle=0)
            print(f"[CAM_CAPTURE] CAM2 saved → {path}")
            self.mqtt.publish(TOPIC_OUT, {
                "action": "cam2_done", "uid": uid, "cycle": 0, "path": path
            })
        except Exception as e:
            print(f"[CAM_CAPTURE] CAM2 error: {e}")
            self.mqtt.publish(TOPIC_OUT, {"action": "error", "msg": str(e)})

    def _start_cam13(self, uid: str, cycle: int):
        with self._lock:
            self._cam13_active = True
            self._cam13_uid    = uid
            self._cam13_cycle  = cycle

        # Re-use existing thread if already running, else spawn a new one
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._cam13_loop, daemon=True)
            self._thread.start()

    def _stop_cam13(self):
        with self._lock:
            self._cam13_active = False
        print("[CAM_CAPTURE] CAM1/CAM3 capture stopped.")

    def _cam13_loop(self):
        print("[CAM_CAPTURE] CAM1/CAM3 loop started.")

        while True:
            with self._lock:
                active = self._cam13_active
                uid    = self._cam13_uid
                cycle  = self._cam13_cycle

            if not active:
                break

            for name in ("CAM1", "CAM3"):
                try:
                    img = self.cams[name].capture(save=False)
                    if img is not None:
                        save_frame(img, uid, name, cycle)
                except Exception as e:
                    print(f"[CAM_CAPTURE] {name} capture error: {e}")

            time.sleep(CAM13_INTERVAL)

        # Notify manager that continuous capture has finished
        with self._lock:
            uid   = self._cam13_uid
            cycle = self._cam13_cycle
        self.mqtt.publish(TOPIC_OUT, {
            "action": "cam13_done", "uid": uid, "cycle": cycle
        })
        print(f"[CAM_CAPTURE] CAM1/CAM3 done — uid={uid}, cycle={cycle}")

    def run(self):
        self.initialize()
        self.mqtt.subscribe(TOPIC_IN)

        print("[CAM_CAPTURE] Waiting for commands …")

        while True:
            try:
                data = self.mqtt.data
                if data and data.get("_consumed") is not True:
                    action = data.get("action", "")
                    uid    = data.get("uid",    "")
                    cycle  = data.get("cycle",  0)
                    
                    # Mark consumed to avoid reprocessing the same message
                    self.mqtt.data = {**data, "_consumed": True}
                    self._on_command(action, uid, cycle)
            except Exception as e:
                print(f"[CAM_CAPTURE] Loop error: {e}")

            time.sleep(0.05)


def main():
    controller = CamController()
    controller.run()


if __name__ == "__main__":
    main()
