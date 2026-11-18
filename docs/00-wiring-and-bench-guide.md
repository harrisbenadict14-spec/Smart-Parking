# 00 — Wiring and Bench-Test Guide (Milestone 1)

Status key: **[DIAGRAM]** = from your wiring diagram, **[VERIFY]** = you must measure/confirm, **[ASSUMED]** = my assumption.

## 1. Wiring (from your diagram)

| Module pin | ESP32-CAM pin | Notes |
|---|---|---|
| IR VCC | 3V3 | [DIAGRAM] |
| IR GND | GND | |
| IR OUT | GPIO13 | [DIAGRAM] output level must be ≤3.3 V [VERIFY] |
| GPS VCC | 3V3 | [DIAGRAM] GY-GPS6MV2 accepts 3.3–5 V [ASSUMED] |
| GPS GND | GND | |
| GPS TX | GPIO14 | ESP32 reads GPS here (UART2). GPS RX unconnected |
| SIM800L TXD | GPIO3 (U0R) | [DIAGRAM] |
| SIM800L RXD | GPIO1 (U0T) | [DIAGRAM] ESP TX is 3.3 V; most SIM800L boards accept it [VERIFY] |
| SIM800L GND | GND | **Common ground is mandatory** |
| SIM800L VBAT | see §2 | **UNRESOLVED** |

microSD must stay empty/unused (GPIO13/14 are SD pins). Don't use GPIO0, 4, 12, 15, 16.

## 2. SIM800L power — UNRESOLVED, do this before powering the modem

You told me the GSM is connected to "U0R and U0T". Those are data pins, so I don't know what feeds **VBAT**.
Rules: VBAT must be **3.4–4.4 V**, source able to give **~2 A bursts**. Never from the ESP32 3V3 pin (too weak) and never 5 V (over-voltage, can destroy it).

Checklist (multimeter, modules disconnected from power first):
1. With the ESP32 board on USB, measure voltage on the wire that goes to SIM800L VBAT. Write it down.
2. 3.4–4.4 V and it comes from a battery or a buck converter you set → OK to proceed.
3. ~5 V → **stop**; set a buck converter to ~4.0 V (measure with nothing attached) or use the 3.7 V Li-ion cell.
4. ~3.3 V → too weak; use the battery/buck instead.
5. Add a 470–1000 µF capacitor across VBAT/GND, close to the module. Keep VBAT wires short and thick; don't route through the breadboard rail.

Tell me the result and I'll update this file with a **[VERIFIED]** wiring table.

## 3. Software setup

1. Arduino IDE → install **esp32 by Espressif** (core 3.x) and the **TinyGPSPlus** library.
2. Board: **AI Thinker ESP32-CAM**. Tools → Core Debug Level: **None**. Partition: Huge APP.
3. Open `v2/firmware/bench_test/bench_test.ino`; edit `config.h` with your Wi-Fi name/password (2.4 GHz only).
4. **Flashing:** unplug the SIM800L wire on GPIO3 (U0R) (and GPIO1 if the upload still fails), upload, then reconnect. Don't open Serial Monitor (UART0 belongs to the modem).
5. Find the board's IP in your router's client list, open `http://<ip>/` in a browser on the same Wi-Fi.

## 4. Test procedure — record pass/fail

| # | Test | Pass condition |
|---|---|---|
| 1 | Camera | Press Capture → image appears; status shows Camera OK, PSRAM yes |
| 2 | IR | Wave a hand/object in front: "IR raw level" flips. Note which level means present; set `IR_ACTIVE_LOW` accordingly |
| 3 | GPS | "GPS bytes seen" increases (wiring OK). "GPS fix" needs sky view; first fix can take minutes outdoors. No fix indoors is normal |
| 4 | GSM alive | Run GSM checks: `AT` → `OK` |
| 5 | SIM | `+CPIN: READY` |
| 6 | Signal | `+CSQ:` between 10 and 31 |
| 7 | Network | `+CREG: 0,1` or `0,5` — **this decides if 2G exists on your SIM** |
| 8 | Supply | `+CBC` millivolts ≥ ~3700 and stays stable during #9 |
| 9 | SMS | Send test SMS to your own number; modem returns `+CMGS` and you actually receive it |
| 10 | Stability | Board doesn't reboot during #9 (check "Free heap"/page keeps responding) |

## 5. If the GSM test fails

- `(no response)`: swap TX/RX wires; check GND; check VBAT; the modem needs 1–3 s after power-up.
- `+CPIN: NOT READY`/SIM error: check SIM seating; use a 2G-capable SIM.
- `+CREG: 0,2` forever or `0,3`/`0,0` with good CSQ: the operator has no 2G here. 4G-only SIMs (e.g. Jio) will never register. Try a different operator SIM that still has GSM 900/1800, or we switch to a 4G module (SIM7600/A7670) — tell me and I'll change the design.
- Board resets when sending: power problem (§2).

## 6. Bench results so far (2026-11-17)

| Test | Result |
|---|---|
| Compile (arduino-cli, esp32 core 3.3.7) | PASS |
| Flash over USB (COM28, CH340) | PASS |
| Wi-Fi (hotspot "Sasi", 2.4 GHz) | PASS, -53 to -61 dBm |
| Camera | PASS after fix. Sensor is **GC2145 (PID 0x2145)**, no hardware JPEG, so firmware captures RGB565 640x480 and converts to JPEG in software (~1.3 s per capture) |
| IR | Responds: raw 0 = object present, raw 1 = clear → `IR_ACTIVE_LOW 1` is correct |
| GPS wiring | PASS (NMEA sentences with valid checksum received). No fix indoors (expected) |
| GSM tests (4–10) | **Not run yet** (SIM800L TX wire was disconnected for flashing) |

Notes:
- Later flashes can be done wirelessly (ArduinoOTA, hostname `nopark-cam`), so the GSM wire can stay connected.
- The hotspot allows only 2 connections (laptop + ESP32). A phone-hotspot IP can change; recheck it if the page stops loading.

## 7. Not yet done

- Sketch **not compiled** (no Arduino toolchain on this machine) — report any compile errors verbatim.
- GPIO mapping verified only against your diagram, not against your exact board's schematic.
