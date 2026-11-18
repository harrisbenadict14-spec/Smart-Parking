"""Endpoints called by the ESP32-CAM. Auth: `X-Device-Key` header."""
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import logic
from .config import get_settings
from .db import get_db, utcnow
from .models import Detection, Device
from .security import current_device

router = APIRouter(prefix="/api/v1/device", tags=["device"])


class Gps(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    sats: int | None = Field(default=None, ge=0, le=60)


class Heartbeat(BaseModel):
    present: bool
    rssi: int | None = Field(default=None, ge=-130, le=0)
    gps: Gps | None = None  # send only with a VALID fix
    gsm: dict | None = None


def _apply_heartbeat(db: Session, dev: Device, hb: Heartbeat, ip: str | None = None):
    now = utcnow()
    if ip:
        dev.last_ip = ip[:45]
    if hb.rssi is not None:
        dev.rssi = hb.rssi
    if hb.gps:
        dev.gps_lat, dev.gps_lng, dev.gps_sats, dev.gps_fix_at = hb.gps.lat, hb.gps.lng, hb.gps.sats, now
    if hb.gsm is not None:
        dev.gsm_status = json.dumps(hb.gsm)[:1000]
    logic.record_presence(db, dev, hb.present, now)
    return {"server_time": now.isoformat(), "capture_needed": logic.capture_needed(db, dev, now)}


@router.post("/heartbeat")
def heartbeat(hb: Heartbeat, request: Request, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    """Periodic health + presence (send every ~10 s while the IR sensor says present)."""
    return _apply_heartbeat(db, dev, hb, request.client.host if request.client else None)


class PresenceEvent(BaseModel):
    present: bool


@router.post("/presence")
def presence(ev: PresenceEvent, request: Request, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    """Immediate IR transition (debounced on the device)."""
    return _apply_heartbeat(db, dev, Heartbeat(present=ev.present), request.client.host if request.client else None)


@router.post("/capture", status_code=201)
async def capture(
    image: UploadFile = File(...),
    idempotency_key: str | None = Header(default=None, max_length=80),
    dev: Device = Depends(current_device),
    db: Session = Depends(get_db),
):
    s = get_settings()
    if idempotency_key:
        dup = db.scalar(select(Detection).where(Detection.device_id == dev.id, Detection.idempotency_key == idempotency_key))
        if dup:
            return {"detection_id": dup.id, "duplicate": True}
    data = await image.read(s.max_image_bytes + 1)
    if len(data) > s.max_image_bytes:
        raise HTTPException(413, "Image too large")
    if not data.startswith(b"\xff\xd8\xff") or not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        raise HTTPException(415, "Not a valid JPEG")
    now = utcnow()
    rel = f"captures/{now:%Y/%m/%d}/{uuid.uuid4()}.jpg"
    path = s.storage_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    det = Detection(device_id=dev.id, zone_id=dev.zone_id, captured_at=now, image_path=rel, idempotency_key=idempotency_key)
    db.add(det)
    try:
        db.commit()
    except IntegrityError:  # racing duplicate upload
        db.rollback()
        path.unlink(missing_ok=True)
        dup = db.scalar(select(Detection).where(Detection.device_id == dev.id, Detection.idempotency_key == idempotency_key))
        return {"detection_id": dup.id, "duplicate": True}
    return {"detection_id": det.id, "duplicate": False}


@router.post("/commands/lease")
def lease_command(dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    """Poll for the next SMS job. Returns {"command": null} if nothing is due or one is already in flight."""
    note = logic.lease_next_command(db, utcnow())
    if note is None:
        return {"command": None}
    return {"command": {"id": note.id, "type": "SEND_SMS", "to": note.recipient_phone, "message": note.message,
                        "attempt": note.attempts, "lease_until": note.lease_until.isoformat()}}


class Ack(BaseModel):
    # SUBMITTED = modem/network accepted it (+CMGS). It is NOT proof the handset received it.
    outcome: Literal["SUBMITTED", "DELIVERED", "FAILED", "FAILED_RETRYABLE"]
    modem_response: str = Field(default="", max_length=300)


@router.post("/commands/{command_id}/ack")
def ack(command_id: str, body: Ack, dev: Device = Depends(current_device), db: Session = Depends(get_db)):
    note = logic.ack_command(db, command_id, body.outcome, body.modem_response, utcnow())
    if note is None:
        raise HTTPException(404, "Unknown command")
    return {"id": note.id, "status": note.status}
