import sys
import os
import psutil
import subprocess
import shlex
import time
from PyQt5.QtWidgets import (
    QApplication, QWidget, QTabWidget, QVBoxLayout, QPushButton,
    QPlainTextEdit, QLabel, QHBoxLayout, QMainWindow, QFrame, QSpinBox
)
from PyQt5.QtCore import QProcess, QTimer, QProcessEnvironment
from PyQt5.QtGui import QFont

from tendo import singleton
from dotenv import load_dotenv
from pathlib import Path
from DEPENDANT.MQTT import MQTT

BASE_FILE_PATH = r"c:\\Users\\COAL_SAMPLING_1\\PRODUCTION_CODE\\COAL_SAMPLING\\"
SERVICES = {
    # "PLC": fr"{BASE_FILE_PATH}SCRIPTS\PLC_COMM.py",
    "Image Capture": fr"{BASE_FILE_PATH}SCRIPTS\\CAM_CAPTURE.py",
    "Data Sync": fr"{BASE_FILE_PATH}SCRIPTS\\DATA_SYNC.py",
    "Printer": fr"{BASE_FILE_PATH}SCRIPTS\\PRINTER.py",
    "Logic": fr"{BASE_FILE_PATH}SCRIPTS\\MAIN_MANAGER.py",
    "Boom Barrier PLC": fr"{BASE_FILE_PATH}SCRIPTS\\PLC_BARRIER.py",
    "Sampler PLC": fr"{BASE_FILE_PATH}SCRIPTS\\PLC_SAMPLER.py",
    "RFID Reader": fr"{BASE_FILE_PATH}SCRIPTS\\RFID_READER.py",
    "Health Monitor": fr"{BASE_FILE_PATH}SCRIPTS\\HEALTH_STATUS.py",
    "PDF Sync": fr"{BASE_FILE_PATH}SCRIPTS\\FILL_VEHICLES.py",
    # "TEST": fr"{BASE_FILE_PATH}SCRIPTS\\TEST.py",
    # "Algorithm": fr"{BASE_FILE_PATH}SCRIPTS\ALGORITHM.py",
    "Django": fr"{BASE_FILE_PATH}WEB_APP\\manage.py runserver"
}

me = singleton.SingleInstance()

BASE_PATH = Path(BASE_FILE_PATH)
load_dotenv(BASE_PATH / ".env")

PYTHON_EXE = r"c:\Users\COAL_SAMPLING_1\miniconda3\envs\detectron2_cpu\python.exe"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"  # Adjust if different

BARRIER_TOPIC_IN  = "manager/plc_barrier"
BARRIER_TOPIC_OUT = "plc_barrier/status"

# ── App / Azure / DB configuration passed to every service ────────────────────
SERVICE_ENV = {
    # Azure
    "AZURE_TENANT_ID": os.getenv("AZURE_TENANT_ID"),
    "AZURE_CLIENT_ID": os.getenv("AZURE_CLIENT_ID"),
    "AZURE_CLIENT_SECRET": os.getenv("AZURE_CLIENT_SECRET"),
    "AZURE_SENDER_EMAIL": os.getenv("AZURE_SENDER_EMAIL"),
    "AZURE_BLOB_CONNECTION_STRING": os.getenv("AZURE_BLOB_CONNECTION_STRING"),
    "AZURE_BLOB_CONTAINER": os.getenv("AZURE_BLOB_CONTAINER"),

    # Application
    "VEHICLE_PDF_PASSWORD": os.getenv("VEHICLE_PDF_PASSWORD"),
    "ALERT_RECIPIENTS": os.getenv("ALERT_RECIPIENTS"),

    # Database
    "DB_HOST": os.getenv("DB_HOST"),
    "DB_USER": os.getenv("DB_USER"),
    "DB_PASSWORD": os.getenv("DB_PASSWORD"),
    "DB_NAME": os.getenv("DB_NAME"),

    # Scheduler
    "DAILY_REPORT_TIME": os.getenv("DAILY_REPORT_TIME", "08:00"),
    "WEEKLY_SYNC_DAY": os.getenv("WEEKLY_SYNC_DAY", "monday"),
    "WEEKLY_SYNC_TIME": os.getenv("WEEKLY_SYNC_TIME", "06:00"),
}

# BASE_FILE_PATH = "/home/deepali/OMKAR/CODES/COAL_SAMPLING/COAL_SAMPLING/"
# SERVICES = {
#     # "PLC": fr"{BASE_FILE_PATH}SCRIPTS\PLC_COMM.py",
#     # "Image Capture": fr"{BASE_FILE_PATH}SCRIPTS/CAM_CAPTURE.py",
#     # "Printer": fr"{BASE_FILE_PATH}SCRIPTS/PRINTER.py",
#     "Logic": fr"{BASE_FILE_PATH}SCRIPTS/MAIN_MANAGER.py",
#     # "Boom Barrier PLC": fr"{BASE_FILE_PATH}SCRIPTS/PLC_BARRIER.py",
#     # "Sampler PLC": fr"{BASE_FILE_PATH}SCRIPTS/PLC_SAMPLER.py",
#     # "RFID Reader": fr"{BASE_FILE_PATH}SCRIPTS/RFID_READER.py",
#     "TEST": fr"{BASE_FILE_PATH}SCRIPTS/TEST.py",
#     # "Algorithm": fr"{BASE_FILE_PATH}SCRIPTS\ALGORITHM.py",
#     "Django": fr"{BASE_FILE_PATH}WEB_APP/manage.py runserver 0.0.0.0:8080"
# }
# PYTHON_EXE = "/usr/bin/python3"
# CHROME_PATH = "/usr/bin/google-chrome"  # Adjust if different

def set_status_color(label: QLabel, status: str, name: str = ""):
    """Helper to color status labels."""
    if "Running" in status:
        label.setStyleSheet("color: lightgreen; font-weight: bold;")
    elif "Stopped" in status:
        label.setStyleSheet("color: red; font-weight: bold;")
    elif "Exited" in status:
        label.setStyleSheet("color: orange; font-weight: bold;")
    else:
        label.setStyleSheet("color: gray; font-weight: bold;")
    label.setText(f"{name}: {status}")


class ServiceTab(QWidget):
    def __init__(self, name, command):
        super().__init__()
        self.name = name
        self.command = command
        self.process = None
        self.user_requested_stop = False

        # NEW — watchdog timestamp
        self.last_output_time = None

        layout = QVBoxLayout()

        # Disk + status info
        top_layout = QHBoxLayout()
        self.disk_label = QLabel("Disk: -- | Free: --")
        self.status_label = QLabel("Status: Inactive")
        set_status_color(self.status_label, "Inactive", "Status")
        top_layout.addWidget(self.disk_label)
        top_layout.addWidget(self.status_label)
        layout.addLayout(top_layout)

        # Log area
        self.log_area = QPlainTextEdit()
        self.log_area.setReadOnly(True)

        # Consolas maybe not present on linux; fallback handled by Qt automatically.
        self.log_area.setFont(QFont("Consolas", 10))
        self.log_area.setStyleSheet("background-color: #1e1e1e; color: #dcdcdc; padding: 6px; border-radius: 6px;")
        layout.addWidget(self.log_area)

        # Buttons
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.stop_btn = QPushButton("Stop")
        self.restart_btn = QPushButton("Restart")

        for btn in (self.start_btn, self.stop_btn, self.restart_btn):
            btn.setStyleSheet(
                "QPushButton { padding: 6px; border-radius: 6px; background-color: #333; color: white; }"
                "QPushButton:hover { background-color: #555; }"
            )

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.stop_btn)
        btn_layout.addWidget(self.restart_btn)
        layout.addLayout(btn_layout)

        self.setLayout(layout)

        # Connect signals
        self.start_btn.clicked.connect(self.start_service)
        self.stop_btn.clicked.connect(self.stop_service)
        self.restart_btn.clicked.connect(self.restart_service)

        # Timer to refresh disk info
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_disk_status)
        self.timer.start(3000)  # every 3s

        # Auto clear logs every hour
        self.clear_timer = QTimer()
        self.clear_timer.timeout.connect(self.clear_logs)
        self.clear_timer.start(3600 * 1000)  # 1 hour

        # NEW — WATCHDOG TIMER
        # self.watchdog_timer = QTimer()
        # self.watchdog_timer.timeout.connect(self.check_output_timeout)
        # self.watchdog_timer.start(10000)  # check every 10 seconds

        # Auto start services
        if "Web" not in self.name: self.start_service()

    def append_log(self, text: str):
        """Safe append to log area and auto-scroll."""
        if not text:
            return
        self.log_area.appendPlainText(text)
        self.log_area.verticalScrollBar().setValue(self.log_area.verticalScrollBar().maximum())

    def start_service(self):
        self.user_requested_stop = False

        # Prevent double-starts
        if self.process is not None:
            self.append_log("[INFO] Service already running.")
            return

        # Validate python exe
        if not os.path.isfile(PYTHON_EXE) or not os.access(PYTHON_EXE, os.X_OK):
            self.append_log(f"[ERROR] PYTHON_EXE not found or not executable: {PYTHON_EXE}")
            set_status_color(self.status_label, "Stopped", "Status")
            return

        # Create process
        self.process = QProcess(self)
        
        # Set environment variables for the process
        env = QProcessEnvironment.systemEnvironment()  # <-- CRITICAL
        env.insert("BASE_FILE_PATH", BASE_FILE_PATH)

        # Ensure HOME resolution works (extra safety)
        env.insert("USERPROFILE", r"C:\Users\COAL_SAMPLING_1")
        env.insert("HOMEDRIVE", "C:")
        env.insert("HOMEPATH", r"\Users\COAL_SAMPLING_1")

        # Optional but recommended for matplotlib stability
        env.insert("MPLCONFIGDIR", r"C:\temp\matplotlib")

        for key, value in SERVICE_ENV.items():
            env.insert(key, value)

        self.process.setProcessEnvironment(env)

        # merge channels so stdout/stderr come together
        try:
            self.process.setProcessChannelMode(QProcess.MergedChannels)
        except Exception:
            pass

        # Connect signals for debugging
        self.process.readyReadStandardOutput.connect(self.read_stdout)
        self.process.readyReadStandardError.connect(self.read_stderr)
        self.process.finished.connect(self.service_finished)
        self.process.errorOccurred.connect(self.process_error)
        self.process.started.connect(self.process_started)

        # Build args safely using shlex.split
        args = ["-u"] + shlex.split(self.command)

        # For Django, set working dir to project root
        if "Django" in self.name:
            # project root is BASE_FILE_PATH (strip trailing slash)
            project_root = BASE_FILE_PATH.rstrip("/\\")
            if os.path.isdir(project_root):
                self.process.setWorkingDirectory(project_root)
            else:
                # fallback: dirname of manage.py if present
                candidate = os.path.dirname(self.command.split("manage.py")[0])
                if candidate:
                    self.process.setWorkingDirectory(candidate)

        # Log what we are about to run (very useful for debugging)
        self.append_log(f"[INFO] Starting service '{self.name}'")
        self.append_log(f"[DEBUG] PYTHON_EXE: {PYTHON_EXE}")
        self.append_log(f"[DEBUG] WorkingDir: {self.process.workingDirectory()}")
        self.append_log(f"[DEBUG] Command args: {args}")

        # Start the process
        try:
            self.process.start(PYTHON_EXE, args)
            # don't assume started immediately; wait is not blocking here
        except Exception as e:
            self.append_log(f"[EXCEPTION] start() failed: {e}")
            set_status_color(self.status_label, "Stopped", "Status")
            self.process = None
            return

        set_status_color(self.status_label, "Running", "Status")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.restart_btn.setEnabled(True)

    def stop_service(self):
        if not self.process:
            self.append_log("[INFO] service not running")
            return

        self.append_log(f"[INFO] Stopping service '{self.name}' ...")
        self.user_requested_stop = True

        self.process.terminate()
        # give some time to quit gracefully; if not, kill
        if not self.process.waitForFinished(3000):
            self.append_log("[WARN] terminate timeout — killing process")
            self.process.kill()
            self.process.waitForFinished(2000)

        self.process = None
        set_status_color(self.status_label, "Stopped", "Status")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def restart_service(self):
        self.append_log(f"[INFO] Restarting service '{self.name}' ...")
        self.stop_service()
        QTimer.singleShot(1000, self.start_service)

    def read_stdout(self):
        if not self.process:
            return
        try:
            text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="ignore")
        except Exception:
            text = "<could not decode stdout>"
        if text.strip():
            self.last_output_time = time.time()     # NEW
            self.append_log(text.rstrip())

    def read_stderr(self):
        if not self.process:
            return
        try:
            text = bytes(self.process.readAllStandardError()).decode("utf-8", errors="ignore")
        except Exception:
            text = "<could not decode stderr>"
        if text.strip():
            self.last_output_time = time.time()     # NEW
            self.append_log("[STDERR] " + text.rstrip())

    def process_error(self, error):
        # QProcess.ProcessError enumerations -> convert to text
        try:
            err_text = str(error)
        except Exception:
            err_text = "Unknown QProcess error"
        self.append_log(f"[QPROCESS ERROR] {err_text}")
        set_status_color(self.status_label, "Stopped", "Status")
        self.process = None
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)

    def process_started(self):
        self.append_log("[INFO] process started")
        self.last_output_time = time.time()  # NEW
        set_status_color(self.status_label, "Running", "Status")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

    def service_finished(self):
        self.append_log("[INFO] process finished")

        # If stop button was pressed -> don't restart
        if self.user_requested_stop:
            set_status_color(self.status_label, "Stopped", "Status")
            self.process = None
            self.start_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.user_requested_stop = False
            return

        # Otherwise: crash detected -> auto restart
        set_status_color(self.status_label, "Exited", "Status")
        self.append_log("[WARN] Service crashed — Restarting in 5 seconds...")
        self.process = None
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        # Auto restart after 5 sec
        QTimer.singleShot(5000, self.start_service)

    def update_disk_status(self):
        # cross-platform root path
        root_path = "/" if os.name != "nt" else "C:\\"
        try:
            usage = psutil.disk_usage(root_path)
            self.disk_label.setText(
                f"Disk: {usage.total // (1024**3)} GB | Free: {usage.free // (1024**3)} GB"
            )
        except Exception as e:
            self.disk_label.setText(f"Disk: error ({e})")

    def clear_logs(self):
        self.log_area.clear()
        self.append_log("[Logs cleared automatically]")

    def check_output_timeout(self):
        """NEW — Restart service if no output for 5 minutes."""
        if not self.process:
            return

        if self.last_output_time is None:
            return

        silence = time.time() - self.last_output_time
        if "Web" in self.name or "Data" in self.name:
            if silence > 60*60*8:  # 8 hours
                self.append_log("[WATCHDOG] No output for 5 minutes — Restarting...")
                self.restart_service()
        else:
            if silence > 360:  # 6 minutes
                self.append_log("[WATCHDOG] No output for 5 minutes — Restarting...")
                self.restart_service()

    def open_chrome(self, url="http://127.0.0.1:8000"):
        # attempt to open browser; log error if fails
        try:
            subprocess.Popen([CHROME_PATH, "--kiosk", url])
            self.append_log(f"[INFO] Opened browser to {url}")
        except Exception as e:
            self.append_log(f"[ERROR] Could not open Chrome: {e}")


class DashboardTab(QWidget):
    def __init__(self, service_tabs):
        super().__init__()
        self.service_tabs = service_tabs

        # Shared MQTT client for barrier commands
        self.mqtt = MQTT("MANAGE_DASHBOARD")
        self.mqtt.subscribe(BARRIER_TOPIC_OUT)

        layout = QVBoxLayout()

        # ── Disk info ──────────────────────────────────────────────────────────
        self.disk_label = QLabel("Disk: -- | Free: --")
        layout.addWidget(self.disk_label)

        # ── Divider ────────────────────────────────────────────────────────────
        layout.addWidget(self._divider())

        # ── Service statuses ───────────────────────────────────────────────────
        self.status_labels = {}
        for name in service_tabs:
            lbl = QLabel(f"{name}: Inactive")
            set_status_color(lbl, "Inactive", name)
            layout.addWidget(lbl)
            self.status_labels[name] = lbl

        # ── Divider ────────────────────────────────────────────────────────────
        layout.addWidget(self._divider())

        # ── Barrier Controls ───────────────────────────────────────────────────
        barrier_title = QLabel("🚧  Boom Barrier Controls")
        barrier_title.setStyleSheet("color: #f0c040; font-weight: bold; font-size: 13px; margin-top: 6px;")
        layout.addWidget(barrier_title)

        barrier_btn_layout = QHBoxLayout()

        self.open_btn = QPushButton("🟢  Open Barrier")
        self.close_btn = QPushButton("🔴  Close Barrier")
        for btn in (self.open_btn, self.close_btn):
            btn.setStyleSheet(
                "QPushButton { padding: 8px 16px; border-radius: 6px; background-color: #333; color: white; font-weight: bold; }"
                "QPushButton:hover { background-color: #555; }"
            )
        barrier_btn_layout.addWidget(self.open_btn)
        barrier_btn_layout.addWidget(self.close_btn)
        layout.addLayout(barrier_btn_layout)

        # ── Set Bucket ─────────────────────────────────────────────────────────
        bucket_layout = QHBoxLayout()

        bucket_lbl = QLabel("Set Bucket:")
        bucket_lbl.setStyleSheet("color: white; font-weight: bold;")
        self.bucket_spin = QSpinBox()
        self.bucket_spin.setRange(1, 10)
        self.bucket_spin.setValue(1)
        self.bucket_spin.setFixedWidth(70)
        self.bucket_spin.setStyleSheet(
            "QSpinBox { background-color: #333; color: white; padding: 4px; border-radius: 4px; border: 1px solid #666; }"
            "QSpinBox::up-button, QSpinBox::down-button { background-color: #444; }"
        )

        self.set_bucket_btn = QPushButton("⚙️  Set Bucket")
        self.set_bucket_btn.setStyleSheet(
            "QPushButton { padding: 8px 16px; border-radius: 6px; background-color: #333; color: white; font-weight: bold; }"
            "QPushButton:hover { background-color: #555; }"
        )

        bucket_layout.addWidget(bucket_lbl)
        bucket_layout.addWidget(self.bucket_spin)
        bucket_layout.addWidget(self.set_bucket_btn)
        bucket_layout.addStretch()
        layout.addLayout(bucket_layout)

        # ── MQTT feedback label ────────────────────────────────────────────────
        self.barrier_feedback = QLabel("Barrier status: —")
        self.barrier_feedback.setStyleSheet("color: #aaaaaa; font-style: italic; margin-top: 4px;")
        layout.addWidget(self.barrier_feedback)

        layout.addStretch()
        self.setLayout(layout)

        # Connect barrier buttons
        self.open_btn.clicked.connect(self.cmd_open_barrier)
        self.close_btn.clicked.connect(self.cmd_close_barrier)
        self.set_bucket_btn.clicked.connect(self.cmd_set_bucket)

        # Timer to update dashboard + poll MQTT feedback
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_dashboard)
        self.timer.start(3000)

        # Poll MQTT feedback more frequently
        self.mqtt_poll_timer = QTimer()
        self.mqtt_poll_timer.timeout.connect(self._poll_mqtt_feedback)
        self.mqtt_poll_timer.start(500)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _divider(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        return line

    def _publish_barrier(self, payload: dict):
        """Publish a command to the barrier topic."""
        try:
            self.mqtt.publish(BARRIER_TOPIC_IN, payload)
            self.barrier_feedback.setStyleSheet("color: #88ccff; font-style: italic;")
            self.barrier_feedback.setText(f"Barrier status: sent → {payload}")
        except Exception as e:
            self.barrier_feedback.setStyleSheet("color: red; font-style: italic;")
            self.barrier_feedback.setText(f"Barrier status: MQTT error — {e}")

    # ── barrier command slots ──────────────────────────────────────────────────

    def cmd_open_barrier(self):
        self._publish_barrier({"action": "open_barrier"})

    def cmd_close_barrier(self):
        self._publish_barrier({"action": "close_barrier"})

    def cmd_set_bucket(self):
        bucket_no = self.bucket_spin.value()
        self._publish_barrier({"action": "set_bucket", "bucket_no": bucket_no})

    # ── polling / update ───────────────────────────────────────────────────────

    def _poll_mqtt_feedback(self):
        """Read any incoming status message from plc_barrier/status."""
        try:
            data = self.mqtt.data
            if data and data.get("_consumed") is not True:
                self.mqtt.data = {**data, "_consumed": True}
                status = data.get("status", "unknown")
                msg    = data.get("msg", "")
                text   = f"Barrier status: {status}" + (f" — {msg}" if msg else "")

                if "error" in status:
                    color = "red"
                elif status in ("barrier_opened", "green_sent", "bucket_set"):
                    color = "lightgreen"
                elif status in ("barrier_closed", "red_sent"):
                    color = "orange"
                else:
                    color = "#aaaaaa"

                self.barrier_feedback.setStyleSheet(f"color: {color}; font-style: italic;")
                self.barrier_feedback.setText(text)
        except Exception:
            pass

    def update_dashboard(self):
        root_path = "/" if os.name != "nt" else "C:\\"
        try:
            usage = psutil.disk_usage(root_path)
            self.disk_label.setText(
                f"Disk: {usage.total // (1024**3)} GB | Free: {usage.free // (1024**3)} GB"
            )
        except Exception as e:
            self.disk_label.setText(f"Disk: error ({e})")

        for name, tab in self.service_tabs.items():
            status = tab.status_label.text().replace(f"{name}: ", "")
            set_status_color(self.status_labels[name], status, name)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("🚀 Service Manager")
        self.setStyleSheet("background-color: #2b2b2b; color: white;")

        self.tabs = QTabWidget()
        self.tabs.setStyleSheet(
            "QTabWidget::pane { border: 1px solid #444; }"
            "QTabBar::tab { background: #333; padding: 6px; }"
            "QTabBar::tab:selected { background: #555; }"
        )

        self.service_tabs = {}

        # Create service tabs
        for name, cmd in SERVICES.items():
            tab = ServiceTab(name, cmd)
            self.service_tabs[name] = tab
            self.tabs.addTab(tab, name)

        # Dashboard
        dashboard = DashboardTab(self.service_tabs)
        self.tabs.insertTab(0, dashboard, "Dashboard")

        self.setCentralWidget(self.tabs)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1000, 650)
    window.show()
    sys.exit(app.exec_())