import cv2
import time
from flask import Flask, Response

app = Flask(__name__)

rtsp_url1 = "rtsp://admin:insightzz@123@192.168.1.201:554/Streaming/Channels/101"
rtsp_url2 = "rtsp://admin:insightzz@123@192.168.1.202:554/Streaming/Channels/101"
rtsp_url3 = "rtsp://admin:insightzz@123@192.168.1.203:554/Streaming/Channels/101"

cap1 = cv2.VideoCapture(rtsp_url1, cv2.CAP_FFMPEG)
cap2 = cv2.VideoCapture(rtsp_url2, cv2.CAP_FFMPEG)
cap3 = cv2.VideoCapture(rtsp_url3, cv2.CAP_FFMPEG)

cap1.set(cv2.CAP_PROP_BUFFERSIZE, 1)
cap2.set(cv2.CAP_PROP_BUFFERSIZE, 1)
cap3.set(cv2.CAP_PROP_BUFFERSIZE, 1)

nocam = cv2.imread("C:/Users/COAL_SAMPLING_1/PRODUCTION_CODE/COAL_SAMPLING/WEB_APP/static/img/nocam.jpg")


def generate(cap, rtsp_url, num):
    while True:

        success, frame = cap.read()
        print(f"CAM{num} OPEN:", cap.isOpened())

        if (not success) or (frame is None) or (frame.size == 0):
            print(f"CAM{num} invalid frame, reconnecting")

            cap.release()
            time.sleep(1)
            cap.open(rtsp_url)

            frame = nocam

        frame = cv2.resize(frame, (480, 270))
        # resized = cv2.resize(frame, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
        ret, buffer = cv2.imencode(".jpg", frame)
        frame_bytes = buffer.tobytes()

        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")

        time.sleep(2)   # 0.5 FPS


@app.route("/cam1")
def cam1():
    return Response(generate(cap1, rtsp_url1, 1), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/cam2")
def cam2():
    return Response(generate(cap2, rtsp_url2, 2), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/cam3")
def cam3():
    return Response(generate(cap3, rtsp_url3, 3), mimetype="multipart/x-mixed-replace; boundary=frame")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, threaded=True)