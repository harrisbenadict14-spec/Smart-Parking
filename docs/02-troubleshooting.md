# 02 — Troubleshooting and Known Limitations

## Quick checks
| Symptom | Likely cause → fix |
|---|---|
| Dashboard says "Cannot reach the server" | Backend not running → run `start.bat`. |
| Camera unit shows **Offline** | Board off / Wi-Fi name or password wrong / 5 GHz network / laptop IP changed (`ipconfig`, update `BACKEND_URL`, re-flash) / Windows Firewall blocks port 8000 for Python. |
| Cam tab: "Camera not reachable" | Board offline, or the `WEB_TOKEN` in the firmware differs from `CAM_VIEW_TOKEN` in `backend\.env`. |
| Cam tab slow (< 1 frame/s) | Weak Wi-Fi (check dBm on the System page) or several Cam tabs open. Move the board closer to the router. |
| Plate never read | Plate too small/blurry/angled, or glare. Aim with the Cam tab; plate should fill a good part of the frame. Use **Review** to type it. |
| Violation never appears | Vehicle must stay detected by the **IR sensor** (not just the camera) for longer than the limit. Check the limit (demo vs 15 min) on the System page. |
| Violation appears but SMS "failed/pending" | Normal until a micro-SIM with 2G service is fitted. See SMS below. |
| Board restarts | Power. Log shows the reset reason: `http://<ip>/log?k=<WEB_TOKEN>` (`15`=brown-out, `5/6`=watchdog). SIM800L bursts need a strong supply (3.4–4.4 V, ~2 A peaks). |
| USB upload fails | Unplug the SIM800L wire(s) on GPIO1/GPIO3 while flashing; the COM port number may change after a USB glitch. |

## SMS (SIM800L)
- Needs a **micro-SIM** and **2G (GSM 900/1800)** service. 4G-only SIMs never register. Check: System page / board `/status` → `sim`, `reg`.
- `SIM not inserted` → reseat the SIM (gold contacts toward the board); a nano-SIM in an adapter often does not make contact.
- If the modem stops answering, unplug its VBAT for 5 s (its speed is saved at 9600).
- "Submitted" means the network accepted the message; it is **not** proof of delivery. "Uncertain" means the board never confirmed; it is not resent automatically (use **Retry** knowing a duplicate is possible).

## Demo mode
`backend\.env`: `VIOLATION_THRESHOLD_SECONDS=60` (any value) then restart. Set `900` for the real 15-minute rule. A yellow banner shows while it differs from 900.

## Limitations (prototype)
- One IR sensor → one bay / one vehicle at a time; no multi-vehicle tracking.
- OCR accuracy on this camera at night, at an angle or at distance is unmeasured (`backend\tools\benchmark.py`).
- GC2145 camera: 640×480, no hardware JPEG, ~1 s per photo; live view ≈ 1 frame/s.
- Local HTTP (not HTTPS); do not expose port 8000 to the internet. Anyone on the same Wi-Fi can reach the login page, so use a strong password.
- Device key and camera token are stored in plain text in the firmware config.
- No delivery reports; no data-retention clean-up job; single admin account; SQLite (swap to PostgreSQL by changing `DATABASE_URL` if needed).
- Government owner lookup, automatic fines and cloud services are intentionally out of scope.
