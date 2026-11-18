# 03 — Software Requirements Specification (prototype)

## 1. Purpose and scope
A prototype that detects a vehicle parked in a no-parking bay, reads its number plate, and alerts the vehicle owner and an administrator by SMS when the vehicle stays **strictly more than 15 minutes (900 s)**. Out of scope: government owner lookup, automatic fines, cloud services, multi-bay tracking.

## 2. Actors
- **Administrator** — signs in to the dashboard, manages vehicles, reviews plates, sees violations and messages.
- **Camera unit** — ESP32-CAM with IR sensor, NEO-6M GPS and SIM800L; talks to the backend over Wi-Fi.
- **Vehicle owner** — receives an SMS (only if registered, active, with consent and a valid phone).

## 3. Functional requirements
| ID | Requirement |
|---|---|
| FR-1 | The IR sensor signals presence; the unit sends a heartbeat every 10 s including presence, Wi-Fi strength, GPS and modem status. |
| FR-2 | When a vehicle is present with no active session and no plate being read, the backend asks for a photo (`capture_needed`); photos are at least 15 s apart. |
| FR-3 | Uploaded photos must be real JPEG, ≤ 5 MB, and are idempotent (same `Idempotency-Key` is not stored twice). |
| FR-4 | The backend reads the plate offline (RapidOCR), repairs look-alike characters by position, and validates the Indian plate format and state code. |
| FR-5 | Plates below 0.75 confidence or implausible go to **Review**; an administrator can confirm, correct or reject them. |
| FR-6 | A confirmed plate starts one parking session per zone (only one ACTIVE session per zone). |
| FR-7 | A session closes (reason EXITED) when presence is stale for more than the 60 s grace period. |
| FR-8 | A violation is created only when elapsed time is **strictly greater** than the threshold (900 s; configurable for demo). One violation per session. |
| FR-9 | Owner SMS only for an active, consenting, registered vehicle with a valid phone; otherwise an administrator SMS plus a dashboard alert. One notification per kind per violation. |
| FR-10 | SMS are queued and leased to the unit one at a time; a lease expiry becomes UNCERTAIN and is never resent automatically; retryable failures back off; SUBMITTED is not DELIVERED. |
| FR-11 | The dashboard shows overview, live camera, review queue, sessions, violations, messages, vehicles, activity log and system settings. |
| FR-12 | Administrators can register vehicles (soft delete), zones and camera units (key shown once), change settings and their password. |
| FR-13 | Every administrator action and sign-in attempt is audit-logged. |
| FR-14 | Demo mode: a shorter threshold set in `.env`, shown with a yellow banner. |

## 4. Non-functional requirements
- **Time:** all stored times are UTC and timezone-aware.
- **Security:** JWT for the dashboard; per-device secret key (stored hashed); passwords hashed with argon2; all untrusted text escaped in the page; local HTTP only.
- **Reliability:** unit watchdog (90 s), unit auto-reconnect, SQLite WAL, idempotent uploads.
- **Privacy:** phone numbers and plates stay on the laptop; no external services used.
- **Performance (prototype):** ~1 photo per 1.3 s capture; live view ~1 frame/s.

## 5. Constraints
GC2145 camera (640×480, no hardware JPEG); SIM800L is 2G only and needs a micro-SIM; UART0 shared with the modem so no serial debug; one IR sensor means one vehicle at a time.

## 6. Acceptance criteria
- A vehicle held in front of the sensor produces a photo, a plate and a session within ~30 s.
- A vehicle leaving before the threshold produces no violation and no SMS.
- A vehicle staying past the threshold produces exactly one violation and its notifications.
- Low-confidence plates are never silently accepted.
