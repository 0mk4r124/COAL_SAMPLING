import os
import time
import threading
import cv2
import traceback
from datetime import datetime

from DEPENDANT.IP   import IPCamera
from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
TEMP_PATH = BASE_FILE_PATH + "TEMP_IMG/"
RAW_PATH = BASE_FILE_PATH + "RAW_IMG/"
LOGS_PATH = BASE_FILE_PATH + "LOGS/"

# Initialize logger
logger = initializeLogger("CAM_MANAGER", LOGS_PATH=LOGS_PATH)

CAM_CONFIGS = {
    "CAM1": {"ip": "192.168.1.201", "user": "admin", "password": "insightzz@123", "name": "CAM1"},
    "CAM2": {"ip": "192.168.1.202", "user": "admin", "password": "insightzz@123", "name": "CAM2"},
    "CAM3": {"ip": "192.168.1.203", "user": "admin", "password": "insightzz@123", "name": "CAM3"},
}

TOPIC_IN  = "manager/camera"
TOPIC_OUT = "camera/status"
CAM_CAPTURE_INTERVAL = 1.0  # Capture every 1 second
THREAD_HEALTH_CHECK_INTERVAL = 5.0  # Check thread health every 5 seconds

def save_frame(img, path: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cv2.imwrite(path, img)
    return path

def save_frame_reduced(img, path: str, scale: float = 0.5) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h, w = img.shape[:2]
    reduced = cv2.resize(img, (int(w * scale), int(h * scale)))
    cv2.imwrite(path, reduced)
    return path

class CamController:

    def __init__(self):
        self.cams   = {k: IPCamera(v) for k, v in CAM_CONFIGS.items()}
        self.mqtt   = MQTT("CAM_CAPTURE")
        self._lock  = threading.Lock()

        # Background capture threads for each camera
        self._bg_threads = {
            "CAM1": None,
            "CAM2": None,
            "CAM3": None,
        }
        self._bg_active = True
        self._last_frame = {
            "CAM1": None,
            "CAM2": None,
            "CAM3": None,
        }
        self._thread_last_alive = {
            "CAM1": time.time(),
            "CAM2": time.time(),
            "CAM3": time.time(),
        }

        # Sample capture state (saves to RAW path)
        self._sample_capture_active = False
        self._sample_capture_uid = None

    def initialize(self):
        for name, cam in self.cams.items():
            ok = cam.initialize()
            logger.info(f"{name} init  {'OK' if ok else 'FAILED'}")

    def _capture(self, cam_name: str, save_path: str) -> str:
        try:
            with self._lock:
                img = self._last_frame.get(cam_name)

            if img is None:
                logger.warning(f"{cam_name} no frame available from background thread")
                return None

            path = save_frame(img, save_path)
            logger.info(f"{cam_name} single capture (from buffer) saved: {path}")
            return path

        except Exception as e:
            logger.error(f"{cam_name} single capture error: {e}", exc_info=True)
            print(f"ERROR: {cam_name} single capture error: {e}")
            return None

    def _bg_capture_loop(self, cam_name: str):
        logger.info(f"{cam_name} background capture thread started")
        
        while self._bg_active:
            try:
                img = self.cams[cam_name].capture(save=False)
                if cam_name in ["CAM3", "CAM2"]: img = cv2.flip(img, 1)
                if img is not None:
                    with self._lock:
                        self._last_frame[cam_name] = img
                        self._thread_last_alive[cam_name] = time.time()
                    
                    # Generate filename with timestamp
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                    temp_full_path = os.path.join(TEMP_PATH, f"{cam_name}_FULL.jpg")
                    temp_reduced_path = os.path.join(TEMP_PATH, f"{cam_name}_REDUCED.jpg")
                    
                    # Save full resolution
                    # save_frame(img, temp_full_path)
                    # Save 50% reduced resolution for quick loading
                    save_frame_reduced(img, temp_reduced_path, scale=0.5)
                    
                    logger.debug(f"{cam_name} captured: full={temp_full_path}, reduced={temp_reduced_path}")
                
            except Exception as e:
                logger.error(f"{cam_name} background capture error: {e}", exc_info=True)
                print(f"ERROR: {cam_name} background capture error: {e}")
            
            time.sleep(CAM_CAPTURE_INTERVAL)
        
        logger.info(f"{cam_name} background capture thread stopped")

    def _sample_capture_loop(self, cam_name: str, uid: str):
        logger.info(f"{cam_name} sample capture thread started (uid={uid})")
        
        count = 0
        while self._sample_capture_active:
            try:
                img = self.cams[cam_name].capture(save=False)
                if img is not None:
                    # Generate filename: uid_TIMESTAMP.jpg
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                    raw_path = os.path.join(RAW_PATH, uid, cam_name)
                    raw_file = os.path.join(raw_path, f"{uid}_{timestamp}_{count:04d}.jpg")
                    
                    # Save full resolution to raw path
                    save_frame(img, raw_file)
                    count += 1
                    
                    logger.debug(f"{cam_name} sampled ({count}): {raw_file}")
                
            except Exception as e:
                logger.error(f"{cam_name} sample capture error: {e}", exc_info=True)
                print(f"ERROR: {cam_name} sample capture error: {e}")
            
            time.sleep(0.1)  # Capture frequently for sample
        
        logger.info(f"{cam_name} sample capture thread stopped (total: {count})")

    def _start_sample_capture(self, uid: str):
        with self._lock:
            if self._sample_capture_active:
                logger.warning("Sample capture already active")
                return
            
            self._sample_capture_active = True
            self._sample_capture_uid = uid
        
        # Start sample capture threads for all 3 cameras
        for cam_name in ("CAM1", "CAM2", "CAM3"):
            thread = threading.Thread(
                target=self._sample_capture_loop,
                args=(cam_name, uid),
                daemon=True
            )
            thread.start()
        
        logger.info(f"Sample capture started (uid={uid})")
        self.mqtt.publish(TOPIC_OUT, {
            "action": "sample_capture_started", "uid": uid
        })

    def _stop_sample_capture(self):
        with self._lock:
            if not self._sample_capture_active:
                logger.warning("Sample capture not active")
                return
            
            uid = self._sample_capture_uid
            self._sample_capture_active = False
        
        # Wait a bit for threads to finish
        time.sleep(0.5)
        
        logger.info(f"Sample capture stopped (uid={uid})")
        self.mqtt.publish(TOPIC_OUT, {
            "action": "sample_capture_stopped", "uid": uid
        })

    def _on_command(self, action: str, uid: str, cam: str = "", path: str = ""):
        logger.debug(f"Command: action={action}, uid={uid}, cam={cam}, path={path}")

        if action == "cam1_single":
            if path:
                filepath = self._capture("CAM1", path)
                if filepath:
                    self.mqtt.publish(TOPIC_OUT, {
                        "action": "cam1_done", "uid": uid, "path": filepath
                    })
            else:
                logger.warning("cam1_single: path not provided")
        
        elif action == "cam2_single":
            if path:
                filepath = self._capture("CAM2", path)
                if filepath:
                    self.mqtt.publish(TOPIC_OUT, {
                        "action": "cam2_done", "uid": uid, "path": filepath
                    })
            else:
                logger.warning("cam2_single: path not provided")
        
        elif action == "cam3_single":
            if path:
                filepath = self._capture("CAM3", path)
                if filepath:
                    self.mqtt.publish(TOPIC_OUT, {
                        "action": "cam3_done", "uid": uid, "path": filepath
                    })
            else:
                logger.warning("cam3_single: path not provided")
        
        elif action == "sample_capture_start":
            self._start_sample_capture(uid)
        
        elif action == "sample_capture_stop":
            self._stop_sample_capture()
        
        elif action == "reset":
            self._stop_sample_capture()
            logger.info("Reset complete")
        
        else:
            logger.warning(f"Unknown action: {action}")

    def _check_thread_health(self):
        current_time = time.time()
        
        for cam_name in ("CAM1", "CAM2", "CAM3"):
            thread = self._bg_threads[cam_name]
            
            # Check if thread is dead or hasn't reported alive recently
            time_since_alive = current_time - self._thread_last_alive[cam_name]
            is_thread_dead = thread is None or not thread.is_alive()
            is_stale = time_since_alive > (THREAD_HEALTH_CHECK_INTERVAL * 2)
            
            if is_thread_dead or is_stale:
                logger.warning(f"{cam_name} thread is dead or stale — restarting …")
                
                # Reset alive timestamp
                self._thread_last_alive[cam_name] = current_time
                
                # Create and start new background thread
                thread = threading.Thread(
                    target=self._bg_capture_loop,
                    args=(cam_name,),
                    daemon=True
                )
                thread.start()
                self._bg_threads[cam_name] = thread

    def run(self):
        self.initialize()
        self.mqtt.subscribe(TOPIC_IN)

        # Start background capture threads for all 3 cameras (always running)
        logger.info("Starting background capture threads for all cameras …")
        for cam_name in ("CAM1", "CAM2", "CAM3"):
            thread = threading.Thread(
                target=self._bg_capture_loop,
                args=(cam_name,),
                daemon=True
            )
            thread.start()
            self._bg_threads[cam_name] = thread

        logger.info("Ready, waiting for commands …")
        last_health_check = time.time()

        while True:
            try:
                # Check thread health periodically and restart if dead
                current_time = time.time()
                if current_time - last_health_check >= THREAD_HEALTH_CHECK_INTERVAL:
                    self._check_thread_health()
                    last_health_check = current_time

                # Process incoming MQTT commands
                data = self.mqtt.data
                if data and data.get("_consumed") is not True:
                    action = data.get("action", "")
                    uid    = data.get("uid",    "")
                    path   = data.get("path",   "")  # Path provided from MQTT for single capture
                    cam    = data.get("cam",    "")
                    
                    # Mark consumed immediately
                    self.mqtt.data = {**data, "_consumed": True}
                    
                    # Process command in separate thread to not block health checks
                    cmd_thread = threading.Thread(
                        target=self._on_command,
                        args=(action, uid, cam, path),
                        daemon=True
                    )
                    cmd_thread.start()

            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
                print(f"ERROR: Loop error: {e}")

            time.sleep(0.05)

def main():
    controller = CamController()
    controller.run()

if __name__ == "__main__":
    main()
