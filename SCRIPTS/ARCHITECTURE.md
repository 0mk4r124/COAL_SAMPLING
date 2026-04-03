# Coal Sampling Automation — Architecture Guide

## File Structure

```
project_root/
│
├── MAIN_MANAGER.py       ← Run first — orchestrates everything
├── RFID_READER.py        ← Run alongside manager
├── CAM_CAPTURE.py        ← Run alongside manager
├── PLC_BARRIER.py        ← Run alongside manager
├── PLC_SAMPLER.py        ← Run alongside manager
│
└── DEPENDANT/
    ├── MQTT.py
    ├── IP.py
    └── LOGIX.py
```

---

## Communication Architecture

All scripts talk **only to the Main Manager** — never to each other.

```
RFID_READER  ──────────► manager/rfid          ──────────► MAIN_MANAGER
CAM_CAPTURE  ◄────────── manager/camera        ◄──────────
             ──────────► camera/status         ──────────►
PLC_BARRIER  ◄────────── manager/plc_barrier   ◄──────────
             ──────────► plc_barrier/status    ──────────►
PLC_SAMPLER  ◄────────── manager/plc_sampler   ◄──────────
             ──────────► plc_sampler/status    ──────────►
```

---

## MQTT Topic Reference

| Topic                | Direction         | Payload fields                                       |
|----------------------|-------------------|------------------------------------------------------|
| `manager/rfid`       | RFID → Manager    | `uid`, `rfids: []`, `timestamp`                      |
| `manager/camera`     | Manager → Camera  | `action`, `uid`, `cycle`                             |
| `camera/status`      | Camera → Manager  | `action`, `uid`, `cycle`, `path?`                    |
| `manager/plc_barrier`| Manager → Barrier | `action`, `bucket_no?`                               |
| `plc_barrier/status` | Barrier → Manager | `status`, `bucket_no?`, `msg?`                       |
| `manager/plc_sampler`| Manager → Sampler | `action`, `x?`, `y?`, `cycle?`                       |
| `plc_sampler/status` | Sampler → Manager | `status`, `cycle?`, `msg?`                           |

### Camera actions
| action         | Meaning                                           |
|----------------|---------------------------------------------------|
| `cam2_single`  | Take one snapshot from CAM2 (vehicle arrives)     |
| `cam13_start`  | Start continuous capture from CAM1 + CAM3         |
| `cam13_stop`   | Stop continuous capture                           |
| `reset`        | Clear all state                                   |

### PLC Barrier actions / statuses
| action / status   | Meaning                          |
|-------------------|----------------------------------|
| `open_barrier`    | Command: open boom barrier       |
| `close_barrier`   | Command: close boom barrier      |
| `set_bucket`      | Command: write bucket number     |
| `barrier_opened`  | Confirmed: barrier is open       |
| `barrier_closed`  | Confirmed: barrier is closed     |
| `bucket_set`      | Confirmed: bucket number written |

### PLC Sampler actions / statuses
| action / status       | Meaning                               |
|-----------------------|---------------------------------------|
| `set_position`        | Write X, Y to PLC                     |
| `start_cycle`         | Pulse CYCLE_START tag                 |
| `send_green`          | Write 1 to GREEN_SIGNAL tag           |
| `reset`               | Zero all PLC outputs                  |
| `position_set`        | X/Y written, ready to start           |
| `cycle_started`       | Cycle start pulse sent                |
| `discharge_received`  | PLC DISCHARGE bit went HIGH           |
| `green_sent`          | GREEN_SIGNAL written                  |

---

## Process Flow (State Machine)

```
IDLE
 │  RFID batch received
 ▼
DB_CHECK  ──────── RFID found ─────────► check already in front?
 │                                              │ no
 │ not found                                   ▼
 ▼                                       OPEN_BARRIER
WAITING_FOR_DB  (poll every 10 s)               │ barrier_opened
 |                                              | Send QR Data to printer
 │  found                                       ▼
 └─────────────────────────────────────── SET_BUCKET
                                                │ bucket_set
                                                ▼
                                        VEHICLE_PLACEMENT
                                          (resolve x,y ×3)
                                                │
                                         ┌──────┘ cycle 1,2,3
                                         ▼
                                   CYCLE_POSITION
                                    set_position →
                                         │ position_set
                                         ▼
                                   CYCLE_CAPTURE
                                    cam13_start →
                                    start_cycle →
                                         │ discharge_received
                                         │ cam13_stop →
                                         ▼
                                    CYCLE_DONE
                                    cycle < 3 → back to CYCLE_POSITION
                                    cycle = 3 →
                                         │
                                         ▼
                                   GREEN_SIGNAL
                                    send_green →
                                         │ green_sent
                                         ▼
                                     COMPLETE
                                  (DB log updated)
                                         │
                                         ▼
                                       IDLE
```

---

## PLC Tags to Update

### PLC_BARRIER.py
| Variable            | Tag Name (placeholder)  | Type | Notes                    |
|---------------------|-------------------------|------|--------------------------|
| BARRIER_CMD_TAG     | `BARRIER_CMD`           | INT  | 1=open, 0=close          |
| BARRIER_STATUS_TAG  | `BARRIER_STATUS`        | INT  | bit 0 = open confirmed   |
| BUCKET_NO_TAG       | `BUCKET_NUMBER`         | INT  | bucket index             |

### PLC_SAMPLER.py
| Variable            | Tag Name (placeholder)  | Type | Notes                         |
|---------------------|-------------------------|------|-------------------------------|
| X_TAG               | `SAMPLER_X`             | REAL | arm X co-ordinate             |
| Y_TAG               | `SAMPLER_Y`             | REAL | arm Y co-ordinate             |
| CYCLE_START_TAG     | `CYCLE_START`           | INT  | pulse 1→0 to start            |
| DISCHARGE_TAG       | `DISCHARGE_STATUS`      | INT  | bit 0 = discharge complete    |
| GREEN_SIGNAL_TAG    | `GREEN_SIGNAL`          | INT  | 1 = all sampling done         |

---

## Database Tables Required

### VEHICLES (lookup table — must exist)
```sql
CREATE TABLE VEHICLES (
    ID          INT PRIMARY KEY AUTO_INCREMENT,
    RFID        VARCHAR(64)  NOT NULL,
    VEHICLE_NO  VARCHAR(32)  NOT NULL,
    VENDOR_NAME VARCHAR(128),
    BUCKET_NO   INT          DEFAULT 1
);
```

### VEHICLE_LOGS (created by existing code + manager)
```sql
ALTER TABLE VEHICLE_LOGS ADD COLUMN VEHICLE_NO  VARCHAR(32);
ALTER TABLE VEHICLE_LOGS ADD COLUMN VENDOR_NAME VARCHAR(128);
ALTER TABLE VEHICLE_LOGS ADD COLUMN BUCKET_NO   INT;
ALTER TABLE VEHICLE_LOGS ADD COLUMN STATUS      VARCHAR(32) DEFAULT 'IN_PROGRESS';
```

---

## How to Run

Open 5 separate terminals (or use a process manager like `supervisord`):

```bash
python MAIN_MANAGER.py
python RFID_READER.py
python CAM_CAPTURE.py
python PLC_BARRIER.py
python PLC_SAMPLER.py
```

Make sure the Mosquitto MQTT broker is running on `127.0.0.1:1883`.

---

## Customisation Points

| What to change             | Where                                      |
|----------------------------|--------------------------------------------|
| X/Y positions              | `SAMPLE_POSITIONS` in `MAIN_MANAGER.py`    |
| Dynamic vehicle placement  | `_handle_vehicle_placement()` in Manager   |
| DB wait timeout            | `DB_WAIT_TIMEOUT` in `MAIN_MANAGER.py`     |
| Cam13 capture interval     | `CAM13_INTERVAL` in `CAM_CAPTURE.py`       |
| Discharge timeout          | `DISCHARGE_TIMEOUT` in `PLC_SAMPLER.py`    |
| Barrier open timeout       | `BARRIER_OPEN_TIMEOUT` in `PLC_BARRIER.py` |
| Number of sampling cycles  | `TOTAL_CYCLES` in `MAIN_MANAGER.py`        |
