"""Dashboard/admin API. Auth: Bearer JWT."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import logic
from .config import get_settings
from .db import get_db, utcnow
from .models import AuditLog, Detection, Device, Notification, ParkingSession, User, Vehicle, Violation, Zone
from .security import create_token, current_admin, hash_device_key, new_device_key, verify_password

def record_audit(actor: str, action: str, detail: str | None = None) -> None:
    from .db import SessionLocal

    with SessionLocal() as db:
        db.add(AuditLog(actor=actor, action=action, detail=(detail or None)))
        db.commit()


router = APIRouter(prefix="/api/v1")
auth_router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
admin = APIRouter(prefix="/api/v1", tags=["admin"], dependencies=[Depends(current_admin)])


# ------------------------------------------------------------------ auth
class Login(BaseModel):
    email: str
    password: str


@auth_router.post("/login")
def login(body: Login, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.strip().lower()))
    if not user or not verify_password(body.password, user.password_hash):
        record_audit(body.email.strip().lower()[:200], "Failed sign-in")
        raise HTTPException(401, "Email or password is incorrect")
    record_audit(user.email, "Signed in")
    return {"access_token": create_token(user), "token_type": "bearer"}


# ------------------------------------------------------------------ vehicles
class VehicleIn(BaseModel):
    plate: str
    owner_name: str = Field(min_length=1, max_length=120)
    owner_phone: str
    consent: bool
    active: bool = True

    @field_validator("plate")
    @classmethod
    def _plate(cls, v):
        p = logic.normalize_plate(v)
        if not logic.is_plausible_plate(p):
            raise ValueError("Plate format not recognised (expected e.g. TN45AB1234)")
        return p

    @field_validator("owner_phone")
    @classmethod
    def _phone(cls, v):
        if not logic.is_valid_phone(v):
            raise ValueError("Phone must be in international format, e.g. +919876543210")
        return v


def _vehicle(v: Vehicle):
    return {"id": v.id, "plate": v.plate, "owner_name": v.owner_name, "owner_phone": v.owner_phone,
            "consent": v.consent, "active": v.active}


@admin.get("/vehicles")
def list_vehicles(db: Session = Depends(get_db)):
    return [_vehicle(v) for v in db.scalars(select(Vehicle).order_by(Vehicle.plate))]


@admin.post("/vehicles", status_code=201)
def create_vehicle(body: VehicleIn, db: Session = Depends(get_db)):
    v = Vehicle(**body.model_dump())
    db.add(v)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"{body.plate} is already registered")
    return _vehicle(v)


@admin.put("/vehicles/{vid}")
def update_vehicle(vid: str, body: VehicleIn, db: Session = Depends(get_db)):
    v = db.get(Vehicle, vid) or _nf()
    for k, val in body.model_dump().items():
        setattr(v, k, val)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, f"{body.plate} is already registered")
    return _vehicle(v)


@admin.delete("/vehicles/{vid}", status_code=204)
def delete_vehicle(vid: str, db: Session = Depends(get_db)):
    v = db.get(Vehicle, vid) or _nf()
    v.active = False  # soft delete keeps history intact
    db.commit()


def _nf():
    raise HTTPException(404, "Not found")


# ------------------------------------------------------------------ zones & devices
class ZoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    site_label: str = Field(min_length=1, max_length=200)


@admin.get("/zones")
def list_zones(db: Session = Depends(get_db)):
    return [{"id": z.id, "name": z.name, "site_label": z.site_label} for z in db.scalars(select(Zone))]


@admin.post("/zones", status_code=201)
def create_zone(body: ZoneIn, db: Session = Depends(get_db)):
    z = Zone(**body.model_dump())
    db.add(z)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Zone name already exists")
    return {"id": z.id, "name": z.name, "site_label": z.site_label}


class DeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    zone_id: str


@admin.post("/devices", status_code=201)
def create_device(body: DeviceIn, db: Session = Depends(get_db)):
    if not db.get(Zone, body.zone_id):
        raise HTTPException(422, "Unknown zone")
    key = new_device_key()
    d = Device(name=body.name, zone_id=body.zone_id, key_hash=hash_device_key(key))
    db.add(d)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Device name already exists")
    return {"id": d.id, "name": d.name, "device_key": key, "note": "Shown once. Put it in the firmware config."}


@admin.get("/devices")
def list_devices(db: Session = Depends(get_db)):
    now = utcnow()
    out = []
    for d in db.scalars(select(Device)):
        online = bool(d.last_seen_at and (now - d.last_seen_at).total_seconds() < 3 * get_settings().presence_grace_seconds)
        out.append({"id": d.id, "name": d.name, "zone_id": d.zone_id, "online": online, "present": d.present,
                    "last_seen_at": d.last_seen_at, "rssi": d.rssi, "gps": {"lat": d.gps_lat, "lng": d.gps_lng,
                    "sats": d.gps_sats, "fix_at": d.gps_fix_at}})
    return out


def _fetch_snapshot(ip: str, token: str) -> bytes:
    import httpx

    r = httpx.get(f"http://{ip}/cam.jpg", params={"k": token, "s": "1"}, timeout=8.0)
    r.raise_for_status()
    return r.content


@admin.get("/devices/{did}/snapshot")
def device_snapshot(did: str, db: Session = Depends(get_db)):
    """One live frame from the camera, fetched by the backend so the camera token never reaches the browser.
    The dashboard calls this in a loop (~1 frame/s: the GC2145 sensor needs software JPEG conversion)."""
    import ipaddress

    from fastapi import Response

    d = db.get(Device, did) or _nf()
    if not d.last_ip:
        raise HTTPException(409, "Camera address not known yet (waiting for its first heartbeat)")
    try:
        private = ipaddress.ip_address(d.last_ip).is_private
    except ValueError:
        private = False
    if not private:
        raise HTTPException(409, "Camera address is not a private network address")
    try:
        data = _fetch_snapshot(d.last_ip, get_settings().cam_view_token)
    except Exception:
        raise HTTPException(502, "Camera did not answer")
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ detections / review
@admin.get("/detections")
def list_detections(status: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
    q = select(Detection).order_by(Detection.captured_at.desc()).limit(min(limit, 200))
    if status:
        q = q.where(Detection.status == status)
    return [{"id": d.id, "captured_at": d.captured_at, "status": d.status, "plate": d.plate, "raw_text": d.raw_text,
             "confidence": d.confidence, "source": d.source, "image_url": f"/api/v1/detections/{d.id}/image"}
            for d in db.scalars(q)]


@admin.get("/detections/{did}/image")
def detection_image(did: str, db: Session = Depends(get_db)):
    d = db.get(Detection, did) or _nf()
    path = (get_settings().storage_dir / d.image_path).resolve()
    if not path.is_file() or get_settings().storage_dir.resolve() not in path.parents:
        raise HTTPException(404, "Image missing")
    return FileResponse(path, media_type="image/jpeg")


class Review(BaseModel):
    plate: str


@admin.post("/detections/{did}/review")
def review_detection(did: str, body: Review, db: Session = Depends(get_db)):
    """Human reads the plate from the image. Treated as confidence 1.0."""
    d = db.get(Detection, did) or _nf()
    if d.status not in ("NEEDS_REVIEW", "PENDING_ANPR"):
        raise HTTPException(409, "Detection already resolved")
    plate = logic.normalize_plate(body.plate)
    if not logic.is_plausible_plate(plate):
        raise HTTPException(422, "Plate format not recognised")
    session = logic.ingest_plate(db, d, plate, 1.0, utcnow(), source="manual")
    return {"status": d.status, "session_id": session.id if session else None}


@admin.post("/detections/{did}/reject", status_code=204)
def reject_detection(did: str, db: Session = Depends(get_db)):
    d = db.get(Detection, did) or _nf()
    d.status = "REJECTED"
    db.commit()


# ------------------------------------------------------------------ sessions / violations / notifications
@admin.get("/sessions")
def list_sessions(active: bool | None = None, limit: int = 100, db: Session = Depends(get_db)):
    q = select(ParkingSession).order_by(ParkingSession.entry_at.desc()).limit(min(limit, 500))
    if active is not None:
        q = q.where(ParkingSession.status == ("ACTIVE" if active else "CLOSED"))
    now = utcnow()
    return [{"id": s.id, "plate": s.plate, "registered": s.vehicle_id is not None, "entry_at": s.entry_at,
             "exit_at": s.exit_at, "status": s.status, "close_reason": s.close_reason,
             "elapsed_seconds": int(((s.exit_at if s.status == "CLOSED" else now) - s.entry_at).total_seconds())}
            for s in db.scalars(q)]


@admin.get("/violations")
def list_violations(limit: int = 100, db: Session = Depends(get_db)):
    q = select(Violation).order_by(Violation.confirmed_at.desc()).limit(min(limit, 500))
    return [{"id": v.id, "plate": v.plate, "registered": v.registered, "confirmed_at": v.confirmed_at,
             "elapsed_seconds": v.elapsed_seconds} for v in db.scalars(q)]


@admin.get("/notifications")
def list_notifications(limit: int = 100, db: Session = Depends(get_db)):
    q = select(Notification).order_by(Notification.created_at.desc()).limit(min(limit, 500))
    return [{"id": n.id, "violation_id": n.violation_id, "kind": n.kind, "recipient_phone": n.recipient_phone,
             "status": n.status, "attempts": n.attempts, "message": n.message, "modem_response": n.modem_response,
             "created_at": n.created_at} for n in db.scalars(q)]


@admin.post("/notifications/{nid}/retry")
def retry_notification(nid: str, db: Session = Depends(get_db)):
    """Manual retry of an UNCERTAIN/FAILED SMS. Admin accepts the risk of a duplicate text."""
    n = db.get(Notification, nid) or _nf()
    if n.status not in ("UNCERTAIN", "FAILED") or not n.recipient_phone:
        raise HTTPException(409, "Only UNCERTAIN or FAILED SMS with a recipient can be retried")
    n.status, n.attempts, n.next_attempt_at, n.lease_until = "PENDING", 0, None, None
    db.commit()
    return {"id": n.id, "status": n.status}


# ------------------------------------------------------------------ dashboard & settings
@admin.get("/dashboard")
def dashboard(db: Session = Depends(get_db)):
    c = lambda q: db.scalar(q) or 0  # noqa: E731
    return {
        "active_sessions": c(select(func.count()).select_from(ParkingSession).where(ParkingSession.status == "ACTIVE")),
        "violations_total": c(select(func.count()).select_from(Violation)),
        "unregistered_violations": c(select(func.count()).select_from(Violation).where(Violation.registered.is_(False))),
        "review_queue": c(select(func.count()).select_from(Detection).where(Detection.status.in_(("NEEDS_REVIEW", "PENDING_ANPR")))),
        "sms_pending": c(select(func.count()).select_from(Notification).where(Notification.status.in_(("PENDING", "LEASED", "RETRYING")))),
        "sms_uncertain_or_failed": c(select(func.count()).select_from(Notification).where(Notification.status.in_(("UNCERTAIN", "FAILED")))),
    }


class SettingsIn(BaseModel):
    admin_phone: str | None = None
    site_label: str | None = Field(default=None, max_length=200)

    @field_validator("admin_phone")
    @classmethod
    def _p(cls, v):
        if v and not logic.is_valid_phone(v):
            raise ValueError("Phone must be in international format, e.g. +919876543210")
        return v


@admin.get("/settings")
def get_settings_api(db: Session = Depends(get_db)):
    s = get_settings()
    return {"admin_phone": logic.get_setting(db, "admin_phone"), "site_label": logic.get_setting(db, "site_label", s.site_label),
            "violation_threshold_seconds": s.violation_threshold_seconds, "presence_grace_seconds": s.presence_grace_seconds,
            "demo_mode": s.violation_threshold_seconds != 900}


@admin.put("/settings")
def put_settings(body: SettingsIn, db: Session = Depends(get_db)):
    if body.admin_phone is not None:
        logic.set_setting(db, "admin_phone", body.admin_phone)
    if body.site_label is not None:
        logic.set_setting(db, "site_label", body.site_label)
    db.commit()
    return get_settings_api(db)


# ------------------------------------------------------------------ account & audit
class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


@admin.post("/account/password", status_code=204)
def change_password(body: PasswordChange, user: User = Depends(current_admin), db: Session = Depends(get_db)):
    from .security import hash_password

    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(403, "Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    db.commit()


@admin.get("/audit")
def list_audit(limit: int = 100, db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.at.desc()).limit(min(limit, 500))
    return [{"id": a.id, "at": a.at, "actor": a.actor, "action": a.action, "detail": a.detail} for a in db.scalars(q)]
