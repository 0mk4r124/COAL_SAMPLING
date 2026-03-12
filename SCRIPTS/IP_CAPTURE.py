import time
import os
import cv2

from DEPENDANT.IP import IPCamera
from DEPENDANT.MQTT import MQTT


SAVE_PATH = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/"
config_cam_1 = {
    "ip": "192.168.1.201",
    "user": "admin",
    "password": "insightzz@123",
    "name": "CAM1"
}

config_cam_2 = {
    "ip": "192.168.1.202",
    "user": "admin",
    "password": "insightzz@123",
    "name": "CAM2"
}

config_cam_3 = {
    "ip": "192.168.1.203",
    "user": "admin",
    "password": "insightzz@123",
    "name": "CAM3"
}


def save_frame(img, uid, cam):

    path = os.path.join(SAVE_PATH, uid, cam)

    os.makedirs(path, exist_ok=True)

    filename = os.path.join(
        path,
        f"{cam}_{int(time.time()*1000)}.jpg"
    )

    cv2.imwrite(filename, img)


def main():

    print("INIT CAMS")

    mqtt = MQTT("CAM_CONTROLLER")
    mqtt.subscribe("rfid/session")
    stage = None
    uid = None

    cam1 = IPCamera(config_cam_1)
    cam2 = IPCamera(config_cam_2)
    cam3 = IPCamera(config_cam_3)

    cams = [cam1, cam2, cam3]

    for cam in cams:
        cam.initialize()

    print("CAMERAS READY")

    while True:

        try:
            
            print(f"TIME : {time.time()}")
            data = mqtt.data
            if data:
                stage = data["stage"]
                uid = data["uid"]
                print("STAGE:", stage, uid)

        except Exception: pass

        if stage == "cam2":

            img = cam2.capture()
            if img is not None: save_frame(img, uid, "CAM2")

        elif stage == "cam13":

            for cam in [cam1, cam3]:
                img = cam.capture()
                if img is not None: save_frame(img, uid, cam.config["name"])

        elif stage == "end":
            stage = None
            uid = None
            time.sleep(5)

        time.sleep(0.05)

if __name__ == "__main__":
    main()