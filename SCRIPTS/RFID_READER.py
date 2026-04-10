import socket
import time
import traceback
import os

from datetime import datetime

from DEPENDANT.MQTT import MQTT
from DEPENDANT.LOGGING import initializeLogger

BASE_FILE_PATH = os.environ.get('BASE_FILE_PATH', 'C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/')
LOGS_PATH = BASE_FILE_PATH + "LOGS/"

# Initialize logger
logger = initializeLogger("RFID_READER", LOGS_PATH=LOGS_PATH)

TCP_IP = "192.168.1.200"
TCP_PORT = 100
BUFFER_SIZE = 1024
DEBOUNCE_WINDOW_SEC = 5.0
TOPIC_OUT = "manager/rfid"

ig_rfids = ["C80700000000000001F8"]
# ig_rfids = []

def main():
    mq = MQTT("RFID_READER")

    session_active = False
    session_uid = None
    session_start = None
    last_seen = time.time()
    rfids = set()

    logger.info(f"Connecting to RFID reader at {TCP_IP}:{TCP_PORT}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        s.connect((TCP_IP, TCP_PORT))
        logger.info("TCP connected.")

        while True:

            try:
                data = s.recv(BUFFER_SIZE)
                if data:
                    raw  = data.hex().upper()
                    rfid = data.decode(errors="ignore").strip()
                    rfid = str(rfid)[1:]

                    if len(rfid)<30 and rfid not in ig_rfids:

                        logger.debug(f"Tag raw={raw}  decoded={rfid!r}")

                        if not session_active:
                            session_uid   = datetime.now().strftime("%Y%m%d%H%M%S")
                            last_seen = time.time()
                            session_active = True
                            rfids = set()
                            print(f"[RFID] Session started: {session_uid}")
                            logger.info(f"Session started: {session_uid}")

                        if rfid not in rfids:
                            last_seen = time.time() 
                            rfids.add(rfid)

            except socket.timeout: pass
            except Exception as e:
                logger.error(f"Recv error: {e}", exc_info=True)
                print(f"ERROR: Recv error: {e}")
                time.sleep(1)

            if session_active and last_seen is not None:
                idle = time.time() - last_seen
                if idle >= DEBOUNCE_WINDOW_SEC:
                    payload = {
                        "uid" : session_uid,
                        "rfids" : list(rfids),
                        "timestamp" : datetime.now().isoformat(),
                    }
                    mq.publish(TOPIC_OUT, payload)
                    logger.info(f"Published {TOPIC_OUT}: {payload}")

                    # reset
                    session_active = False
                    session_uid = None
                    last_seen = None
                    rfids = set()
                    end_time = time.time()
                    while (time.time() - end_time) < 300:
                        try: data = s.recv(BUFFER_SIZE)
                        except: pass


if __name__ == "__main__":
    main()
