import socket
import time
import pymysql
from datetime import datetime

from DEPENDANT.MQTT import MQTT


TCP_IP = "192.168.1.200"
TCP_PORT = 100
BUFFER_SIZE = 1024
SAVE_PATH = "C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/TEMP_IMG/"

db_user = "root"
db_pass = "insightzz@123"
db_host = "127.0.0.1"
db_name = "COAL_SAMPLING_DHAR"

def getdbConn():
    db = None
    try:
        db = pymysql.connect(host=db_host, user=db_user, passwd=db_pass, db=db_name)
    except Exception as e:
        print(f"SQLClass() Exception is: {e}")
    return db

def save_RFIDs(uid, rfids):
    cur = None
    dbConnection = None

    try:
        dbConnection = getdbConn()
        if not dbConnection:
            return
        
        cur = dbConnection.cursor()
        query = """INSERT INTO VEHICLE_LOGS 
            (UID, RFIDS, IMG_1_PATH, IMG_2_PATH, IMG_3_PATH, CREATE_TIME) 
            VALUES (%s, %s, %s, %s, %s, %s)"""
        cur.execute(query, (uid, "|".join(rfids), f"{SAVE_PATH}{uid}/CAM1/", f"{SAVE_PATH}{uid}/CAM2/", f"{SAVE_PATH}{uid}/CAM3/", datetime.now()))
        dbConnection.commit()
        
    except Exception as e:
        print(f"save_RFIDs() Exception is: {str(e)}")
    finally:
        if cur: cur.close()
        if dbConnection: dbConnection.close()

def main():

    mq = MQTT("RFID_SESSION")

    session_active = False
    session_uid = None
    session_start = None
    rfids = set()
    first = False

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:

        s.settimeout(2)

        print("Connecting to RFID reader...")
        s.connect((TCP_IP, TCP_PORT))
        print("Connected")

        while True:

            try:
                print(f"TIME : {time.time()}")
                data = s.recv(BUFFER_SIZE)

                if data:

                    print("Received (raw):", data.hex().upper())
                    print("As text:", data.decode(errors='ignore'))
                    rfid = data.decode(errors='ignore').strip()

                    if not session_active:

                        session_uid = datetime.now().strftime("%Y%m%d_%H%M%S")
                        session_start = time.time()
                        session_active = True
                        rfids = set()
                        first = True

                        print("SESSION START:", session_uid)
                        mq.publish(
                            "rfid/session",
                            {
                                "stage": "cam2",
                                "uid": session_uid
                            }
                        )

                    rfids.add(rfid)

                    # mq.publish(
                    #     "rfid/rfid",
                    #     {
                    #         "uid": session_uid,
                    #         "rfid": rfid
                    #     }
                    # )
                else:
                    time.sleep(5)

            except socket.timeout:
                pass

            except Exception as e:
                print("RFID error:", e)

            # manage session timing
            if session_active:

                elapsed = time.time() - session_start

                # after 10 sec start cam1 + cam3
                if elapsed > 10 and elapsed < 15:

                    print("START CAM1 CAM3")

                    mq.publish(
                        "rfid/session",
                        {
                            "stage": "cam13",
                            "uid": session_uid
                        }
                    )

                    if first: 
                        save_RFIDs(session_uid, rfids)
                        first = False

                # after 5 minutes stop session
                if elapsed > 300 and elapsed < 310:

                    print("SESSION END")

                    mq.publish(
                        "rfid/session",
                        {
                            "stage": "end",
                            "uid": session_uid
                        }
                    )

                    print("RFIDS:", rfids)

                    session_active = False
                    session_uid = None
                    rfids = set()


if __name__ == "__main__":
    main()