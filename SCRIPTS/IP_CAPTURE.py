import time
import os
import cv2

from datetime import datetime

from DEPENDANT.IP import IPCamera
from DEPENDANT.MQTT import MQTT


def main():

    save_path = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/"
    print("INIT CAMS")

    cam_trigger = MQTT("RFID_CAM")

    loc, trigger, uid = "RFID", "INACTIVE", "TEMP"
    cam_trigger.subscribe("rfid/trigger") 

    config_cam_1 = {
        "ip": "192.168.1.201",
        "user": "admin",
        "password": "insightzz@123",
        "save_path": f"{save_path}CAM1/",
        "name": "CAM1"
    }

    config_cam_2 = {
        "ip": "192.168.1.202",
        "user": "admin",
        "password": "insightzz@123",
        "save_path": f"{save_path}CAM2/",
        "name": "CAM2"
    }

    config_cam_3 = {
        "ip": "192.168.1.203",
        "user": "admin",
        "password": "insightzz@123",
        "save_path": f"{save_path}CAM3/",
        "name": "CAM3"
    }

    ipcam1 = IPCamera(config_cam_1)
    ipcam2 = IPCamera(config_cam_2)
    ipcam3 = IPCamera(config_cam_3)

    print("ADDING WORKING CAMS")

    list_of_cams = [ipcam1, ipcam2, ipcam3]
    working_cams = []

    for ipcam in list_of_cams:
        if ipcam.initialize():
            working_cams.append(ipcam)

    print(f"WORKING CAMS: {working_cams}")

    while True:

        print(f"CYCLE : {datetime.now()}")
        # time.sleep(8)

        try:
            data = cam_trigger.data
            if data:
                loc = data["loc"]
                trigger = data["trigger"]
                uid = data["uid"]
        except Exception as e:
            print("MQTT read error:", e)

        if trigger == "ACTIVE":

            print(f"RFID TRIGGERED: {uid}")
            trigger = "INACTIVE"
            t1 = datetime.now()

            while (datetime.now() - t1).total_seconds() < 10:

                for ipcam in working_cams:
                    try:
                        img = ipcam.capture()

                        if img is None:
                            continue

                        temp_path = f"{save_path}{loc}/{uid}/{ipcam.config['name']}"
                        os.makedirs(temp_path, exist_ok=True)

                        filename = os.path.join(
                            temp_path,
                            f"{ipcam.config['name']}_{int(time.time()*1000)}.jpg"
                        )

                        cv2.imwrite(filename, img)

                    except Exception as e:
                        print("COULDNT CAPTURE:", e)

    for ipcam in list_of_cams:
        ipcam.deinitialize()


if __name__ == "__main__":
    main()