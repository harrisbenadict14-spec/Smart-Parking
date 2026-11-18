# 04 — Architecture and Data Model

## System overview
```mermaid
flowchart LR
  IR[IR sensor GPIO13] --> ESP
  GPS[NEO-6M GPIO14] --> ESP
  CAM[GC2145 camera] --> ESP
  ESP[ESP32-CAM firmware] <-->|UART0 9600| GSM[SIM800L]
  ESP -- "Wi-Fi HTTP: heartbeat, capture, SMS lease/ack" --> API
  subgraph Laptop
    API[FastAPI] --> DB[(SQLite)]
    API --> FS[(storage/ photos)]
    SCH[APScheduler: violation tick, ANPR tick] --> API
    OCR[RapidOCR] --- SCH
    UI[Dashboard static page] --> API
  end
  API -- "snapshot proxy /cam.jpg" --> ESP
  GSM -- SMS --> Phones[Owner / Admin phones]
```

## Main flow
1. IR sees a vehicle → heartbeat `present=true`.
2. Backend replies `capture_needed` → unit uploads a JPEG → Detection `PENDING_ANPR`.
3. ANPR tick reads the plate → confirmed (≥ 0.75) or `NEEDS_REVIEW`.
4. Confirmed plate → ParkingSession ACTIVE (one per zone).
5. Violation tick: elapsed > threshold → Violation + Notifications (owner / admin / dashboard).
6. Unit polls `commands/lease` → sends the SMS through the SIM800L → `ack` (SUBMITTED / failure).
7. Presence stale > 60 s → session CLOSED (EXITED).

## Notification status machine
PENDING → LEASED → SUBMITTED → DELIVERED (not yet reported by the modem; reserved)
LEASED → RETRYING (retryable failure, backoff) → PENDING; LEASED + lease expiry → UNCERTAIN (never auto-resent); terminal: FAILED, CANCELLED, SHOWN (dashboard alert).

## Entity relationships
```mermaid
erDiagram
  ZONE ||--o{ DEVICE : has
  ZONE ||--o{ DETECTION : receives
  ZONE ||--o{ PARKING_SESSION : hosts
  DEVICE ||--o{ DETECTION : uploads
  VEHICLE ||--o{ PARKING_SESSION : "matched to"
  PARKING_SESSION ||--o| VIOLATION : "at most one"
  VIOLATION ||--o{ NOTIFICATION : produces
  USER { string id string email string password_hash string role }
  VEHICLE { string plate UK string owner_name string owner_phone bool consent bool active }
  ZONE { string name UK string site_label }
  DEVICE { string name UK string key_hash UK bool present float gps_lat float gps_lng string last_ip }
  DETECTION { string status string plate float confidence string source string idempotency_key }
  PARKING_SESSION { string plate datetime entry_at datetime last_presence_at datetime exit_at string status string close_reason }
  VIOLATION { string session_id UK int elapsed_seconds bool registered }
  NOTIFICATION { string kind string status int attempts datetime lease_until string modem_response }
  SETTING { string key PK string value }
  AUDIT_LOG { datetime at string actor string action string detail }
```

## Integrity rules enforced by the database
- `uq_active_session_zone`: partial unique index → one ACTIVE session per zone.
- `violations.session_id` unique → one violation per session.
- `uq_notification_once` → one notification per kind per violation.
- `devices.key_hash`, `vehicles.plate`, `zones.name` unique.

## Component notes
| Part | Tech |
|---|---|
| Backend | FastAPI, SQLAlchemy 2, SQLite (WAL), Pydantic v2, APScheduler, httpx |
| Auth | JWT (HS256) for the dashboard; `X-Device-Key` (sha256-hashed) for units |
| ANPR | RapidOCR (onnxruntime), 3 preprocessing variants, Indian plate rules |
| Dashboard | single static HTML + vanilla JS, polls every 5 s (no build step; documented deviation) |
| Firmware | Arduino C++ (esp32 core 3.3.7), non-blocking SIM800L state machine, OTA, local web UI |

## Device API
`POST /api/v1/device/heartbeat | presence | capture | commands/lease | commands/{id}/ack`. The admin API is listed interactively at `http://localhost:8000/docs`.
