# import socket
# import time

# HOST = "192.168.1.168"
# PORT = 8080

# # Start
# hex_data = """A55A0205010000000065C70BF7F05B20207B202247726F75704E616D65223A20224731222C202253656C6563744D657373616765223A20225641484943414C44415441222C202252657365744D657373616765223A20747275652C2022416374696F6E223A202253746172742220207D20205D"""
# # DATA
# # hex_data = """A55A011001000000005AC1C574777B224D6573736167654E616D65223A225641484943414C44415441222C224B657956616C7565223A5B7B224574685F30223A2242433131222C224574685F31223A2258595856222C224574685F32223A224D4E424F50227D5D7D"""
# # Stop
# # hex_data = """A55A0205010000000062B3B4C3325B207B2247726F75704E616D65223A20224731222C202253656C6563744D657373616765223A20225641484943414C44415441222C202252657365744D657373616765223A20747275652C2022416374696F6E223A202253746F702220207D20205D"""
# try:
#     # Clean HEX string
#     hex_data = hex_data.replace(" ", "").replace("\n", "")

#     # Convert to bytes
#     payload = bytes.fromhex(hex_data)

#     with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
#         s.settimeout(10)

#         print("Connecting...")
#         s.connect((HOST, PORT))
#         print("Connected")

#         # Send exactly like Hercules HEX mode
#         s.sendall(payload)
#         print("Sent")

#         # Wait (important for your device)
#         time.sleep(2)

#         # Receive response
#         try:
#             while True:
#                 resp = s.recv(4096*5)
#                 if not resp:
#                     break
#                 print("Received (HEX):", resp.hex().upper())
#                 try:
#                     print("STR :", resp.decode("utf-8"))
#                 except:
#                     print("STR :", resp.decode("utf-8", errors="ignore"))
#         except socket.timeout:
#             print("No more response")

# except Exception as e:
#     print("Error:", e)


# import socket
# import time
# import json


# HOST = "192.168.1.168"
# PORT = 8080


# # ---------------- CRC16 ----------------
# def crc16(data: bytes, poly=0xA001):
#     crc = 0xFFFF
#     for b in data:
#         crc ^= b
#         for _ in range(8):
#             if crc & 1:
#                 crc = (crc >> 1) ^ poly
#             else:
#                 crc >>= 1
#     return crc.to_bytes(2, byteorder="little")


# # ---------------- Packet Builder ----------------
# def build_packet(header_hex: str, json_obj):
#     json_str = json.dumps(json_obj, separators=(",", ":"))
#     json_bytes = json_str.encode("utf-8")

#     length_bytes = len(json_bytes).to_bytes(4, byteorder="little")

#     header = bytes.fromhex(header_hex)

#     crc1 = crc16(header + length_bytes)
#     crc2 = crc16(json_bytes)

#     return header + length_bytes + crc1 + crc2 + json_bytes


# # ---------------- Wait for Trigger ----------------
# def wait_for_ready(sock, timeout=30):
#     sock.settimeout(2)
#     start = time.time()

#     while time.time() - start < timeout:
#         try:
#             resp = sock.recv(4096)
#             if resp:
#                 decoded = resp.decode("utf-8", errors="ignore")
#                 print("RX:", decoded)

#                 if '"MessageName":"VAHICALDATA"' in decoded:
#                     return True

#         except socket.timeout:
#             continue

#     return False


# # ---------------- MAIN FUNCTION ----------------
# def send_vehicle_data(eth_0: str, eth_1: str, eth_2: str):
#     START_HEADER = "A55A02050100"
#     DATA_HEADER  = "A55A01100100"
#     STOP_HEADER  = "A55A02050100"

#     start_payload = [{
#         "GroupName": "G1",
#         "SelectMessage": "VAHICALDATA",
#         "ResetMessage": True,
#         "Action": "Start"
#     }]

#     data_payload = {
#         "MessageName": "VAHICALDATA",
#         "KeyValue": [{
#             "Eth_0": eth_0,
#             "Eth_1": eth_1,
#             "Eth_2": eth_2
#         }]
#     }

#     stop_payload = [{
#         "GroupName": "G1",
#         "SelectMessage": "VAHICALDATA",
#         "ResetMessage": True,
#         "Action": "Stop"
#     }]

#     with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
#         print("Connecting...")
#         s.connect((HOST, PORT))
#         print("Connected")

#         # -------- START --------
#         start_packet = build_packet(START_HEADER, start_payload)
#         print("START HEX:", start_packet.hex().upper())
#         s.sendall(start_packet)

#         # Wait for device ready
#         # if not wait_for_ready(s, timeout=30):
#         #     print("No trigger response. Aborting.")

#         print("Device ready sending DATA")

#         # -------- DATA --------
#         data_packet = build_packet(DATA_HEADER, data_payload)
#         print("DATA HEX:", data_packet.hex().upper())
#         s.sendall(data_packet)

#         # Wait 1 minute
#         time.sleep(60)

#         # -------- STOP --------
#         stop_packet = build_packet(STOP_HEADER, stop_payload)
#         print("STOP HEX:", stop_packet.hex().upper())
#         s.sendall(stop_packet)

#         print("Completed sequence")

# send_vehicle_data("MH12AB1234", "INSIGHTZZ", "202604021030AM")

# import socket
# import time
# import json


# HOST = "192.168.1.168"
# PORT = 8080


# # ---------------- CRC16 (for CRC1) ----------------
# def crc16_modbus(data: bytes):
#     crc = 0xFFFF
#     for b in data:
#         crc ^= b
#         for _ in range(8):
#             if crc & 1:
#                 crc = (crc >> 1) ^ 0xA001
#             else:
#                 crc >>= 1
#     return crc.to_bytes(2, byteorder="little")


# # ---------------- CRC2 (MATCH DEVICE BEHAVIOR) ----------------
# def crc16_device(data: bytes):
#     """
#     Reverse-engineered behavior:
#     Device expects swapped-byte CRC compared to modbus
#     """
#     crc = crc16_modbus(data)
#     return crc[::-1]   # KEY FIX


# # ---------------- Packet Builder ----------------
# def build_packet(header_hex: str, json_str: str):
#     json_bytes = json_str.encode("utf-8")

#     # BIG endian length
#     length_bytes = len(json_bytes).to_bytes(4, byteorder="big")

#     header = bytes.fromhex(header_hex)

#     # CRC1 header + length
#     crc1 = crc16_modbus(header + length_bytes)

#     # CRC2 JSON (device-specific)
#     crc2 = crc16_device(json_bytes)

#     return header + length_bytes + crc1 + crc2 + json_bytes


# # ---------------- FIXED JSON FORMAT (IMPORTANT) ----------------
# def build_data_json(eth0, eth1, eth2):
#     # EXACT formatting like your working hex (no compact json!)
#     return (
#         '{"MessageName":"VAHICALDATA","KeyValue":[{'
#         f'"Eth_0":"{eth0}",'
#         f'"Eth_1":"{eth1}",'
#         f'"Eth_2":"{eth2}"'
#         '}]}'
#     )

# def build_start_json():
#     return '[  { "GroupName": "G1", "SelectMessage": "VAHICALDATA", "ResetMessage": true, "Action": "Start"  }  ]'

# def build_stop_json():
#     return '[  { "GroupName": "G1", "SelectMessage": "VAHICALDATA", "ResetMessage": true, "Action": "Stop"  }  ]'


# # ---------------- MAIN FUNCTION ----------------
# def send_vehicle_data(eth_0, eth_1, eth_2):
#     START_HEADER = "A55A02050100"
#     DATA_HEADER  = "A55A01100100"
#     STOP_HEADER  = "A55A02050100"

#     with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
#         print("Connecting...")
#         s.connect((HOST, PORT))
#         print("Connected")

#         # -------- START --------
#         start_packet = build_packet(START_HEADER, build_start_json())
#         print("START:", start_packet.hex().upper())
#         s.sendall(start_packet)

#         # Receive response
#         counter = 0
#         try:
#             while True:
#                 if counter>5: break
#                 resp = s.recv(4096*5)
#                 if not resp:
#                     break
#                 print("Received (HEX):", resp.hex().upper())
#                 try:
#                     counter += 1
#                     print("STR :", resp.decode("utf-8"))
#                 except:
#                     print("STR :", resp.decode("utf-8", errors="ignore"))
#         except socket.timeout:
#             print("No more response")

#         # -------- DATA --------
#         data_json = build_data_json(eth_0, eth_1, eth_2)
#         data_packet = build_packet(DATA_HEADER, data_json)

#         print("DATA:", data_packet.hex().upper())
#         s.sendall(data_packet)

        
#         # Receive response
#         counter = 0
#         try:
#             while True:
#                 if counter>5: break
#                 resp = s.recv(4096*5)
#                 if not resp:
#                     break
#                 print("Received (HEX):", resp.hex().upper())
#                 try:
#                     counter += 1
#                     print("STR :", resp.decode("utf-8"))
#                 except:
#                     print("STR :", resp.decode("utf-8", errors="ignore"))
#         except socket.timeout:
#             print("No more response")

#         # -------- STOP --------
#         stop_packet = build_packet(STOP_HEADER, build_stop_json())
#         print("STOP:", stop_packet.hex().upper())
#         s.sendall(stop_packet)

        
#         # Receive response
#         counter = 0
#         try:
#             while True:
#                 if counter>5: break
#                 resp = s.recv(4096*5)
#                 if not resp:
#                     break
#                 print("Received (HEX):", resp.hex().upper())
#                 try:
#                     counter += 1
#                     print("STR :", resp.decode("utf-8"))
#                 except:
#                     print("STR :", resp.decode("utf-8", errors="ignore"))
#         except socket.timeout:
#             print("No more response")

#         print("Done")


# # ---------------- RUN ----------------
# send_vehicle_data("MH12", "QW23", "45IN")

import socket
import time

HOST = "192.168.1.168"
PORT = 8080


def reflect8(x: int) -> int:
    return int(f"{x:08b}"[::-1], 2)


def reflect16(x: int) -> int:
    return int(f"{x:016b}"[::-1], 2)


def crc16_ibm_sdlc(data: bytes) -> bytes:
    crc = 0xFFFF
    poly = 0x1021

    for b in data:
        b = reflect8(b)
        crc ^= (b << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF

    crc = reflect16(crc)
    crc ^= 0xFFFF
    return crc.to_bytes(2, byteorder="big")


def build_packet(header_hex: str, json_text: str) -> bytes:
    header = bytes.fromhex(header_hex)
    json_bytes = json_text.encode("ascii")  # ASCII subset, matches your hex output
    length_bytes = len(json_bytes).to_bytes(4, byteorder="big")

    crc1 = crc16_ibm_sdlc(header + length_bytes)
    crc2 = crc16_ibm_sdlc(json_bytes)

    packet = header + length_bytes + crc1 + crc2 + json_bytes
    return packet


def build_start_json() -> str:
    return '[  { "GroupName": "G1", "SelectMessage": "VAHICALDATA", "ResetMessage": true, "Action": "Start"  }  ]'


def build_stop_json() -> str:
    return '[  { "GroupName": "G1", "SelectMessage": "VAHICALDATA", "ResetMessage": true, "Action": "Stop"  }  ]'


def build_data_json(eth_0: str, eth_1: str, eth_2: str) -> str:
    return (
        '{"MessageName":"VAHICALDATA","KeyValue":[{'
        f'"Eth_0":"{eth_0}",'
        f'"Eth_1":"{eth_1}",'
        f'"Eth_2":"{eth_2}"'
        '}]}'
    )


def recv_loop(sock: socket.socket, tag: str, timeout: float = 2.0, max_reads: int = 20):
    sock.settimeout(timeout)
    reads = 0
    while reads < max_reads:
        try:
            resp = sock.recv(4096)
            if not resp:
                break
            print(f"[{tag}] Received (HEX):", resp.hex().upper())
            print(f"[{tag}] STR :", resp.decode("utf-8", errors="ignore"))
            reads += 1
        except socket.timeout:
            break


def send_vehicle_data(eth_0: str, eth_1: str, eth_2: str):
    START_HEADER = "A55A02050100"
    DATA_HEADER = "A55A01100100"
    STOP_HEADER = "A55A02050100"

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        print("Connecting...")
        s.connect((HOST, PORT))
        print("Connected")

        start_packet = build_packet(START_HEADER, build_start_json())
        print("START:", start_packet.hex().upper())
        s.sendall(start_packet)
        recv_loop(s, "START", timeout=2.0, max_reads=10)

        time.sleep(2)

        data_packet = build_packet(DATA_HEADER, build_data_json(eth_0, eth_1, eth_2))
        print("DATA :", data_packet.hex().upper())
        s.sendall(data_packet)
        recv_loop(s, "DATA", timeout=2.0, max_reads=10)

        time.sleep(15)

        stop_packet = build_packet(STOP_HEADER, build_stop_json())
        print("STOP :", stop_packet.hex().upper())
        s.sendall(stop_packet)
        recv_loop(s, "STOP", timeout=2.0, max_reads=10)

        print("Done")

if __name__ == "__main__":
    send_vehicle_data("AB12AB1234", "INS", "1130PM")