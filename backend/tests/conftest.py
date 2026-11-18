import os
import tempfile
from datetime import datetime, timedelta, timezone

_tmp = tempfile.mkdtemp()
os.environ.update(
    DATABASE_URL="sqlite://",
    SCHEDULER_ENABLED="false",
    STORAGE_DIR=_tmp,
    ADMIN_EMAIL="admin@test.local",
    ADMIN_PASSWORD="test-password-123",
    # Pin the real rules so a demo-mode .env can never change test behaviour
    VIOLATION_THRESHOLD_SECONDS="900",
    PRESENCE_GRACE_SECONDS="60",
    ANPR_CONFIDENCE_THRESHOLD="0.75",
    SITE_LABEL="No-Parking Zone",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import logic  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app, init_db  # noqa: E402
from app.models import Device, Vehicle, Zone  # noqa: E402
from app.security import hash_device_key  # noqa: E402

T0 = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
DEVICE_KEY = "dk_test_key"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    yield


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def zone(db):
    z = Zone(name="Gate", site_label="Main Gate")
    db.add(z)
    db.commit()
    return z


@pytest.fixture
def device(db, zone):
    d = Device(name="cam1", zone_id=zone.id, key_hash=hash_device_key(DEVICE_KEY))
    db.add(d)
    db.commit()
    return d


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers(client):
    r = client.post("/api/v1/auth/login", json={"email": "admin@test.local", "password": "test-password-123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def dev_headers():
    return {"X-Device-Key": DEVICE_KEY}


def add_vehicle(db, plate="TN45AB1234", phone="+919876543210", consent=True, active=True):
    v = Vehicle(plate=plate, owner_name="Test Owner", owner_phone=phone, consent=consent, active=active)
    db.add(v)
    db.commit()
    return v


def start_session(db, device, zone, plate="TN45AB1234", at=T0, confidence=0.95):
    """Vehicle present + plate read at time `at`."""
    from app.models import Detection

    logic.record_presence(db, device, True, at)
    det = Detection(device_id=device.id, zone_id=zone.id, captured_at=at, image_path="x.jpg")
    db.add(det)
    db.commit()
    return logic.ingest_plate(db, det, plate, confidence, at), det


def keep_present(db, device, start, end, step=10):
    t = start
    while t <= end:
        logic.record_presence(db, device, True, t)
        t += timedelta(seconds=step)
