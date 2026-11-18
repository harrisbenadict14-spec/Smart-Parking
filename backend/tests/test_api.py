from conftest import DEVICE_KEY

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"


# ---------------------------------------------------------------- auth
def test_admin_routes_require_login(client):
    for path in ("/api/v1/vehicles", "/api/v1/sessions", "/api/v1/dashboard", "/api/v1/settings"):
        assert client.get(path).status_code == 401


def test_login_rejects_wrong_password(client):
    r = client.post("/api/v1/auth/login", json={"email": "admin@test.local", "password": "nope"})
    assert r.status_code == 401


def test_device_routes_reject_missing_or_bad_key(client, device):
    assert client.post("/api/v1/device/heartbeat", json={"present": True}).status_code == 401
    assert client.post("/api/v1/device/heartbeat", json={"present": True}, headers={"X-Device-Key": "bad"}).status_code == 401
    assert client.post("/api/v1/device/heartbeat", json={"present": True}, headers={"X-Device-Key": DEVICE_KEY}).status_code == 200


def test_device_key_cannot_open_admin_routes(client, device):
    assert client.get("/api/v1/vehicles", headers={"X-Device-Key": DEVICE_KEY}).status_code == 401


# ---------------------------------------------------------------- vehicles
def test_vehicle_crud_and_validation(client, admin_headers):
    body = {"plate": "tn 45 ab 1234", "owner_name": "A", "owner_phone": "+919876543210", "consent": True}
    r = client.post("/api/v1/vehicles", json=body, headers=admin_headers)
    assert r.status_code == 201 and r.json()["plate"] == "TN45AB1234"
    assert client.post("/api/v1/vehicles", json=body, headers=admin_headers).status_code == 409
    bad_phone = {**body, "plate": "KA01AB9999", "owner_phone": "98765"}
    assert client.post("/api/v1/vehicles", json=bad_phone, headers=admin_headers).status_code == 422
    bad_plate = {**body, "plate": "HELLO"}
    assert client.post("/api/v1/vehicles", json=bad_plate, headers=admin_headers).status_code == 422
    vid = r.json()["id"]
    assert client.delete(f"/api/v1/vehicles/{vid}", headers=admin_headers).status_code == 204
    assert client.get("/api/v1/vehicles", headers=admin_headers).json()[0]["active"] is False


# ---------------------------------------------------------------- device provisioning
def test_create_zone_and_device_returns_working_key_once(client, admin_headers):
    z = client.post("/api/v1/zones", json={"name": "Z1", "site_label": "Gate"}, headers=admin_headers).json()
    d = client.post("/api/v1/devices", json={"name": "cam", "zone_id": z["id"]}, headers=admin_headers).json()
    assert d["device_key"].startswith("dk_")
    assert client.post("/api/v1/device/heartbeat", json={"present": False}, headers={"X-Device-Key": d["device_key"]}).status_code == 200
    listed = client.get("/api/v1/devices", headers=admin_headers).json()
    assert "device_key" not in listed[0] and listed[0]["online"] is True


# ---------------------------------------------------------------- capture
def test_capture_rejects_non_jpeg_and_oversize(client, device, dev_headers):
    r = client.post("/api/v1/device/capture", files={"image": ("a.jpg", b"not a jpeg", "image/jpeg")}, headers=dev_headers)
    assert r.status_code == 415
    big = b"\xff\xd8\xff" + b"\x00" * (5 * 1024 * 1024 + 10)
    r = client.post("/api/v1/device/capture", files={"image": ("a.jpg", big, "image/jpeg")}, headers=dev_headers)
    assert r.status_code == 413


def test_capture_stores_image_and_is_idempotent(client, device, dev_headers, admin_headers):
    h = {**dev_headers, "Idempotency-Key": "cap-1"}
    r1 = client.post("/api/v1/device/capture", files={"image": ("a.jpg", JPEG, "image/jpeg")}, headers=h)
    r2 = client.post("/api/v1/device/capture", files={"image": ("a.jpg", JPEG, "image/jpeg")}, headers=h)
    assert r1.status_code == 201 and r1.json()["duplicate"] is False
    assert r2.json() == {"detection_id": r1.json()["detection_id"], "duplicate": True}
    dets = client.get("/api/v1/detections", headers=admin_headers).json()
    assert len(dets) == 1 and dets[0]["status"] == "PENDING_ANPR"
    img = client.get(dets[0]["image_url"], headers=admin_headers)
    assert img.status_code == 200 and img.content == JPEG


# ---------------------------------------------------------------- full flow over HTTP
def test_manual_review_starts_session_and_device_receives_sms_command(client, device, dev_headers, admin_headers, db):
    from datetime import timedelta
    from app import logic
    from app.db import utcnow
    from app.models import Notification, ParkingSession
    from sqlalchemy import select

    client.put("/api/v1/settings", json={"admin_phone": "+911111111111"}, headers=admin_headers)
    client.post("/api/v1/device/heartbeat", json={"present": True}, headers=dev_headers)
    cap = client.post("/api/v1/device/capture", files={"image": ("a.jpg", JPEG, "image/jpeg")}, headers=dev_headers).json()
    rv = client.post(f"/api/v1/detections/{cap['detection_id']}/review", json={"plate": "KA01AB9999"}, headers=admin_headers)
    assert rv.status_code == 200 and rv.json()["session_id"]

    # fast-forward: age the session past 900 s while the vehicle stays present
    s = db.scalar(select(ParkingSession))
    s.entry_at = utcnow() - timedelta(seconds=950)
    db.commit()
    client.post("/api/v1/device/heartbeat", json={"present": True}, headers=dev_headers)
    logic.evaluate_sessions(db, utcnow())

    cmd = client.post("/api/v1/device/commands/lease", headers=dev_headers).json()["command"]
    assert cmd["to"] == "+911111111111" and "KA01AB9999" in cmd["message"]
    assert client.post("/api/v1/device/commands/lease", headers=dev_headers).json() == {"command": None}
    ack = client.post(f"/api/v1/device/commands/{cmd['id']}/ack", json={"outcome": "SUBMITTED", "modem_response": "+CMGS: 5"}, headers=dev_headers)
    assert ack.json()["status"] == "SUBMITTED"
    assert client.get("/api/v1/dashboard", headers=admin_headers).json()["violations_total"] == 1


# ---------------------------------------------------------------- capture_needed hint for the camera
def _hb(client, dev_headers, present):
    return client.post("/api/v1/device/heartbeat", json={"present": present}, headers=dev_headers).json()


def test_capture_needed_only_when_present_and_no_session(client, device, dev_headers):
    assert _hb(client, dev_headers, False)["capture_needed"] is False
    assert _hb(client, dev_headers, True)["capture_needed"] is True


def test_no_capture_requested_while_photo_pending_or_too_recent(client, device, dev_headers):
    assert _hb(client, dev_headers, True)["capture_needed"] is True
    client.post("/api/v1/device/capture", files={"image": ("a.jpg", JPEG, "image/jpeg")}, headers=dev_headers)
    assert _hb(client, dev_headers, True)["capture_needed"] is False  # PENDING_ANPR exists


def test_no_capture_requested_once_session_is_active(client, device, dev_headers, db, zone):
    from conftest import start_session
    from app.db import utcnow
    start_session(db, device, zone, at=utcnow())
    assert _hb(client, dev_headers, True)["capture_needed"] is False


# ---------------------------------------------------------------- live camera snapshot
def test_snapshot_needs_login_and_known_address(client, device, dev_headers, admin_headers):
    assert client.get(f"/api/v1/devices/{device.id}/snapshot").status_code == 401
    assert client.get(f"/api/v1/devices/{device.id}/snapshot", headers=admin_headers).status_code == 409  # no heartbeat yet
    client.post("/api/v1/device/heartbeat", json={"present": False}, headers=dev_headers)
    # "testclient" is not an IP address -> refused cleanly instead of fetching something arbitrary
    assert client.get(f"/api/v1/devices/{device.id}/snapshot", headers=admin_headers).status_code == 409


def test_snapshot_proxies_frame_and_reports_camera_down(client, device, dev_headers, admin_headers, monkeypatch, db):
    from app import routes_admin
    client.post("/api/v1/device/heartbeat", json={"present": False}, headers=dev_headers)
    device.last_ip = "192.168.1.50"  # the test client's own host is not an IP; use a real private one
    db.merge(device); db.commit()
    monkeypatch.setattr(routes_admin, "_fetch_snapshot", lambda ip, tok: JPEG)
    r = client.get(f"/api/v1/devices/{device.id}/snapshot", headers=admin_headers)
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"

    def down(ip, tok):
        raise RuntimeError("timeout")

    monkeypatch.setattr(routes_admin, "_fetch_snapshot", down)
    assert client.get(f"/api/v1/devices/{device.id}/snapshot", headers=admin_headers).status_code == 502
