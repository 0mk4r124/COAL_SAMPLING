import socket
import time
import uuid
from datetime import datetime

from DEPENDANT.MQTT import MQTT

TCP_IP = "192.168.1.200"
TCP_PORT = 100
BUFFER_SIZE = 1024
DEBOUNCE_WINDOW_SEC = 5.0
TOPIC_OUT = "manager/rfid"

def main():
    mq = MQTT("RFID_READER")

    session_active = False
    session_uid = None
    session_start = None
    last_seen = time.time()
    rfids = set()

    print(f"[RFID_READER] Connecting to RFID reader at {TCP_IP}:{TCP_PORT}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        s.connect((TCP_IP, TCP_PORT))
        print("[RFID_READER] TCP connected.")

        while True:

            try:
                data = s.recv(BUFFER_SIZE)
                if data:
                    raw  = data.hex().upper()
                    rfid = data.decode(errors="ignore").strip()
                    rfid = str(rfid)[1:]

                    if len(rfid)<30:

                        print(f"[RFID_READER] Tag raw={raw}  decoded={rfid!r}")

                        if not session_active:
                            session_uid   = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
                            last_seen = time.time()
                            session_active = True
                            rfids = set()
                            print(f"[RFID_READER] Session started: {session_uid}")

                        if rfid not in rfids:
                            last_seen = time.time() 
                            rfids.add(rfid)

            except socket.timeout: pass
            except Exception as e:
                print(f"[RFID_READER] Recv error: {e}")
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
                    print(f"[RFID_READER] Published {TOPIC_OUT}: {payload}")

                    # reset
                    session_active = False
                    session_uid = None
                    last_seen = None
                    rfids = set()
                    end_time = time.time()
                    while (time.time() - end_time) < 500:
                        try: data = s.recv(BUFFER_SIZE)
                        except: pass


if __name__ == "__main__":
    main()
