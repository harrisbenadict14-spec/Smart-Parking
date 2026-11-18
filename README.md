# Smart No-Parking Alert System (v2 rewrite)

ESP32-CAM + IR sensor detect a vehicle and upload photos over Wi-Fi to a laptop. The laptop reads the plate (offline OCR),
runs a 15-minute timer (violation only when **strictly more than 900 s**), and queues SMS through the SIM800L.
The old implementation in the parent folder is untouched.

## Status (2026-11-18)
| Part | State |
|---|---|
| Backend (FastAPI + SQLite), 63 tests (all pass, 2026-11-18) | working |
| Plate reading (RapidOCR) | working; 22/25 web photos correct, 0 wrong accepted. **Not yet measured on your camera** |
| Dashboard (`http://<laptop>:8000/`) | working |
| Firmware: Wi-Fi, IR, camera, GPS, backend link | verified on the real board (IR → photo → plate TN90H2446 → session) |
| Dashboard **Cam** tab (live view, ~1 frame/s, only while the tab is open; each open tab loads the camera) | working |
| Demo mode (`VIOLATION_THRESHOLD_SECONDS=60` in `backend\.env`, yellow banner; set back to `900` for the real 15-minute rule) | currently ON |
| SMS sending | **not tested**: SIM800L answers but reports `SIM not inserted` (needs a micro-SIM with 2G service) |
| Full 15-minute violation on the real board | not yet run |

## Quick start
Double-click **`start.bat`** (opens the dashboard; **`stop.bat`** stops it). Dashboard tabs: Overview, **Cam** (live view + recent photos), Review, Sessions, Violations, Messages, Vehicles (add/edit/deactivate), **Activity** (audit log), System (camera units, add zone, register unit, settings, change password). A red banner appears on every page when a new violation is created. See `docs/02-troubleshooting.md` for fixes and limits, `firmware/nopark_cam/config.example.h` for the firmware settings.

## Run the backend (Windows, from `v2\backend`)
```
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```
Open `http://localhost:8000/`. Login = `ADMIN_EMAIL` and `ADMIN_PASSWORD` from `.env` (currently `varni`; if `.env` has no password, a random one is printed once at first start). Changing `.env` later does not change an existing account; use System > Change password.
Tests: `.venv\Scripts\python.exe -m pytest -q`

Fresh setup: `py -3.14 -m venv .venv` then `.venv\Scripts\python.exe -m pip install -r requirements.lock.txt`.

## First-time device setup
1. Dashboard → System, or API `POST /api/v1/zones` then `POST /api/v1/devices` (the device key is shown **once**).
2. Paste the key into `firmware/nopark_cam/config.h` (`DEVICE_KEY`), set `WIFI_*` and `BACKEND_URL` (laptop IP from `ipconfig`; a phone hotspot may change it).
3. Dashboard → System: set the administrator phone (`+91…`).

## Flash the firmware
Arduino IDE: board **AI Thinker ESP32-CAM**, Core Debug Level **None**, Partition **Huge APP**; libraries TinyGPSPlus, ArduinoJson.
Or CLI: `arduino-cli compile/upload --fqbn esp32:esp32:esp32cam:PartitionScheme=huge_app,DebugLevel=none -p COMxx firmware\nopark_cam`.
No serial debug (UART0 is the modem): use `http://<board-ip>/log?k=<WEB_TOKEN>`, `/status`, and `/cam.jpg?k=<WEB_TOKEN>` (aim the camera).
If upload fails, unplug the SIM800L wire(s) on GPIO1/GPIO3 while flashing. A USB glitch may renumber the COM port.

## Wiring — verified on the bench
| Module | ESP32-CAM | Verified |
|---|---|---|
| IR OUT | GPIO13 (3V3 power) | yes: OUT=0 when object present |
| GPS TX | GPIO14 (3V3 power) | yes: valid NMEA, fix with 9–11 satellites |
| SIM800L TX → | GPIO1 (U0T) | yes: modem only answers with ESP RX=GPIO1, TX=GPIO3 (**opposite** of the original diagram labels; handled in code) |
| SIM800L RX ← | GPIO3 (U0R) | yes |
| SIM800L VBAT | battery/buck, measured **4.0–4.3 V** | measured; keep ≤ 4.4 V |
| All GND | common | required |
Camera sensor is a **GC2145** (no hardware JPEG): firmware captures RGB565 640×480 and converts to JPEG (~1.3 s).

## Known limits
- One IR sensor = one bay / one vehicle at a time.
- SMS "submitted" ≠ delivered; a lost acknowledgement becomes "uncertain" and is not resent automatically.
- The SIM800L can hang; power-cycling VBAT recovers it (its speed is saved at 9600).
- HTTP on a local network, not HTTPS; do not expose port 8000 to the internet.
- Dashboard is a single static page (no React build); no audit log or in-place vehicle editing yet.
- OCR accuracy on the real camera at night/angle/distance is unmeasured: use `backend\tools\benchmark.py` with real captures.

More detail: `docs/00-wiring-and-bench-guide.md`, `docs/01-anpr-notes.md`, `docs/02-troubleshooting.md`, `docs/03-requirements-srs.md`, `docs/04-architecture-and-data-model.md`, `docs/05-test-report.md`.
Sign-in: username `Varni` (any capitalisation), password as set on the System page.
