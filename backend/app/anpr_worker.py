"""Processes detections that are still PENDING_ANPR. Queue state lives in the database, so a restart
never loses an uploaded photo. A failure never leaves a photo stuck: it goes to human review."""
import logging
from datetime import datetime
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import logic
from .config import get_settings
from .db import utcnow
from .models import Detection

log = logging.getLogger("noparking.anpr")


def _default_reader():
    from .anpr import read_plate

    return read_plate


def process_pending(db: Session, reader: Callable | None = None, limit: int = 3, now: datetime | None = None) -> int:
    reader = reader or _default_reader()
    dets = list(db.scalars(select(Detection).where(Detection.status == "PENDING_ANPR")
                           .order_by(Detection.captured_at).limit(limit)))
    for det in dets:
        try:
            data = (get_settings().storage_dir / det.image_path).read_bytes()
            reading = reader(data)
            logic.ingest_plate(db, det, reading.plate or "", reading.confidence if reading.plate else 0.0,
                               now or utcnow(), source="anpr")
            det.raw_text = (reading.raw_text or "")[:64] or None  # keep what OCR really saw
            db.commit()
        except Exception:
            log.exception("ANPR failed for detection %s; sending to review", det.id)
            db.rollback()
            det = db.get(Detection, det.id)
            det.status, det.confidence, det.source = "NEEDS_REVIEW", 0.0, "anpr"
            db.commit()
    return len(dets)
