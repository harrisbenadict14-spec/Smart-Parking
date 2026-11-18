# 05 — Test Report (2026-11-18)

## Automated tests
`backend\.venv\Scripts\python.exe -m pytest -q` → **63 passed, 0 failed** (16 s), run after the audit log, dashboard and login changes.

| File | Covers |
|---|---|
| `tests/test_logic.py` | strict `>` threshold (900 s and 901 s), grace period, one active session per zone, one violation per session, notification rules (owner / admin / no admin phone), SMS lease/ack/expiry/backoff, `capture_needed` |
| `tests/test_api.py` | login, device auth, heartbeat, capture validation (JPEG magic, size, idempotency), review/reject, settings, snapshot errors, password change, audit |
| `tests/test_anpr.py` | plate normalisation and validation, look-alike repair, "IND" hologram text, two-line plates, low-confidence routing |

## ANPR accuracy (`backend/tools/benchmark.py`)
25 labelled web plate images: **22 correct, 0 wrong accepted**, the rest sent to review. One real photo from the ESP32-CAM (TN90H2446, screen shown to the camera) was read correctly.
**Not measured:** night, angle, distance, real plates on real vehicles.

## Hardware tests on the real board
| Test | Result |
|---|---|
| IR → photo → plate → session | pass (TN90H2446) |
| 5-minute stability | pass: 0 resets, 0 failed requests |
| Early exit | pass: session closed EXITED, no violation, no SMS |
| Live camera view | pass |
| GPS fix | pass (9–11 satellites) |
| SIM800L AT commands | pass; `SIM not inserted` (nano-SIM in a micro-SIM slot) |
| Violation after > threshold (live) | **not run** |
| SMS delivery | **not run**: no usable SIM |

## Remaining to test
1. Live violation in demo mode (60 s) on the board.
2. SMS with a micro-SIM that has 2G service on the local network.
3. Plate accuracy with the real camera in several lighting and angle conditions.
4. Return `VIOLATION_THRESHOLD_SECONDS` to 900 and run one real 15-minute check.
