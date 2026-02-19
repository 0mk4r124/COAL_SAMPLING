import socket

# === Configuration ===
TCP_IP = "192.168.1.200"   # Replace with your device IP
TCP_PORT = 100            # Replace with your device port
BUFFER_SIZE = 1024        # Bytes per read

def main():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(5)  # Prevent infinite blocking

        try:
            print("Connecting...")
            s.connect((TCP_IP, TCP_PORT))
            print(f"Connected to {TCP_IP}:{TCP_PORT}")

            while True:
                data = s.recv(BUFFER_SIZE)
                if not data:
                    print("Connection closed by server.")
                    break

                print("Received (raw):", data.hex().upper())
                print("As text:", data.decode(errors='ignore'))

        except socket.timeout:
            print("Connection timed out.")
        except KeyboardInterrupt:
            print("\nStopped by user.")
        except Exception as e:
            print("Error:", e)

if __name__ == "__main__":
    main()