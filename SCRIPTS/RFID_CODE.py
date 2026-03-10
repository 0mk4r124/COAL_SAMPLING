import socket
from DEPENDANT.MQTT import MQTT

# === Configuration ===
TCP_IP = "192.168.1.200"   # Replace with your device IP
TCP_PORT = 100            # Replace with your device port
BUFFER_SIZE = 1024        # Bytes per read

def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:

        mq_obj = MQTT("RFID_TRIGGER")
        s.settimeout(5)  # Prevent infinite blocking

        print("Connecting...")
        s.connect((TCP_IP, TCP_PORT))
        print(f"Connected to {TCP_IP}:{TCP_PORT}")

        while True:
            try:
                data = s.recv(BUFFER_SIZE)
                if not data:
                    print("Connection closed by server.")
                    break
                else:

                    print("Received (raw):", data.hex().upper())
                    print("As text:", data.decode(errors='ignore'))

                    mq_obj.publish( f"rfid/trigger",
                        {
                            "loc": "RFID",
                            "trigger": "ACTIVE",
                            "uid": f"{data.hex().upper()}"
                        }
                    )

            except socket.timeout:
                print("Connection timed out.")
                mq_obj.publish( f"rfid/trigger",
                    {
                        "loc": "RFID",
                        "trigger": "INACTIVE",
                        "uid": f""
                    }
                )
            except KeyboardInterrupt:
                print("\nStopped by user.")
                mq_obj.publish( f"rfid/trigger",
                    {
                        "loc": "RFID",
                        "trigger": "INACTIVE",
                        "uid": f""
                    }
                )
            except Exception as e:
                print("Error:", e)
                mq_obj.publish( f"rfid/trigger",
                    {
                        "loc": "RFID",
                        "trigger": "INACTIVE",
                        "uid": f""
                    }
                )

if __name__ == "__main__":
    main()