"""Core business rules. Every time-dependent function takes `now` so behaviour is testable.

Rules implemented here (see docs 01/11):
  * violation only when (now - entry) is STRICTLY greater than the threshold (900 s)
  * session closes without violation if presence is lost beyond the grace period
  * one ACTIVE session per zone, one violation per session, one notification per kind per violation
  * registered+consented vehicle -> owner SMS; readable unregistered -> admin SMS; unreadable -> review
  * SMS status never claims delivery from a modem OK: SUBMITTED != DELIVERED
"""
import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Detection, Device, Notification, ParkingSession, Setting, Vehicle, Violation

# Indian registration: SS NN [A-Z]{0,3} NNNN, e.g. TN45AB1234, KA01A1234, DL3CAB1234
_PLATE_RE = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}$")
_PHONE_RE = re.compile(r"^\+\d{8,15}$")


def normalize_plate(raw: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (raw or "").upper())


def is_plausible_plate(plate: str) -> bool:
    return bool(_PLATE_RE.match(plate))


def is_valid_phone(p: str) -> bool:
    return bool(_PHONE_RE.match(p or ""))


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


# ---------------------------------------------------------------- device presence
def record_presence(db: Session, device: Device, present: bool, now: datetime) -> None:
    device.last_seen_at = now
    device.present = present
    if present:
        device.last_presence_at = now
        for s in _active_sessions(db, device.zone_id):
            s.last_presence_at = now  # presence confirmed -> session stays alive
    db.commit()


def capture_needed(db: Session, device: Device, now: datetime, min_gap_seconds: int = 15) -> bool:
    """Tell the camera whether a photo would help: vehicle present, no active session yet,
    nothing already waiting for OCR, and not asked for too recently (prevents review-queue spam)."""
    if not device.present or _active_sessions(db, device.zone_id):
        return False
    if db.scalar(select(Detection.id).where(Detection.zone_id == device.zone_id, Detection.status == "PENDING_ANPR")):
        return False
    last = db.scalar(select(func.max(Detection.captured_at)).where(Detection.zone_id == device.zone_id))
    return not (last and (now - last).total_seconds() < min_gap_seconds)


def _active_sessions(db: Session, zone_id: str | None = None):
    q = select(ParkingSession).where(ParkingSession.status == "ACTIVE")
    if zone_id:
        q = q.where(ParkingSession.zone_id == zone_id)
    return list(db.scalars(q))


def _zone_present(db: Session, zone_id: str, now: datetime) -> bool:
    grace = timedelta(seconds=get_settings().presence_grace_seconds)
    for dev in db.scalars(select(Device).where(Device.zone_id == zone_id)):
        if dev.present and dev.last_presence_at and now - dev.last_presence_at <= grace:
            return True
    return False


# ---------------------------------------------------------------- plate -> session
def ingest_plate(db: Session, det: Detection, raw_text: str, confidence: float, now: datetime, source: str = "anpr"):
    """Apply an OCR (or manual) reading to a detection. Returns the new/updated session or None."""
    s = get_settings()
    plate = normalize_plate(raw_text)
    det.raw_text, det.plate, det.confidence, det.source = raw_text[:64], plate or None, confidence, source

    if not plate or not is_plausible_plate(plate) or confidence < s.anpr_confidence_threshold:
        det.status = "NEEDS_REVIEW"  # never guess an owner
        db.commit()
        return None

    if not _zone_present(db, det.zone_id, now):
        det.status = "NO_SESSION"  # reading is fine but nothing is confirmed present right now
        db.commit()
        return None

    det.status = "ACCEPTED"
    active = _active_sessions(db, det.zone_id)
    session = active[0] if active else None
    if session and session.plate == plate:
        session.last_presence_at = now  # same vehicle seen again: never a duplicate session
    else:
        if session:
            _close(db, session, "REPLACED", now, exit_at=now)
            db.flush()
        vehicle = db.scalar(select(Vehicle).where(Vehicle.plate == plate, Vehicle.active.is_(True)))
        session = ParkingSession(
            zone_id=det.zone_id,
            plate=plate,
            vehicle_id=vehicle.id if vehicle else None,
            entry_at=now,
            last_presence_at=now,
        )
        db.add(session)
    db.commit()
    return session


def _close(db: Session, session: ParkingSession, reason: str, now: datetime, exit_at: datetime | None = None):
    session.status = "CLOSED"
    session.close_reason = reason
    session.exit_at = exit_at or now


# ---------------------------------------------------------------- timer
def evaluate_sessions(db: Session, now: datetime) -> dict:
    """Run on a schedule (and in tests). Safe to call repeatedly: idempotent."""
    s = get_settings()
    grace = timedelta(seconds=s.presence_grace_seconds)
    closed, violations = 0, 0
    for sess in _active_sessions(db):
        if now - sess.last_presence_at > grace:
            _close(db, sess, "EXITED", now, exit_at=sess.last_presence_at)  # left before/without violation
            closed += 1
            continue
        elapsed = (now - sess.entry_at).total_seconds()
        if elapsed > s.violation_threshold_seconds:  # STRICTLY greater: exactly 900 s must not trigger
            if create_violation(db, sess, now, int(elapsed)) is not None:
                violations += 1
    db.commit()
    return {"closed": closed, "violations": violations}


def create_violation(db: Session, sess: ParkingSession, now: datetime, elapsed: int) -> Violation | None:
    if db.scalar(select(Violation).where(Violation.session_id == sess.id)):
        return None  # one violation per session
    vehicle = db.get(Vehicle, sess.vehicle_id) if sess.vehicle_id else None
    registered = bool(vehicle and vehicle.active and vehicle.consent and is_valid_phone(vehicle.owner_phone))
    v = Violation(
        session_id=sess.id,
        plate=sess.plate,
        vehicle_id=sess.vehicle_id,
        registered=registered,
        confirmed_at=now,
        elapsed_seconds=elapsed,
    )
    try:
        with db.begin_nested():
            db.add(v)
            db.flush()
    except IntegrityError:
        return None
    _queue_notifications(db, v, sess, vehicle if registered else None, now)
    return v


def fmt_threshold(seconds: int) -> str:
    """'15 min' for the real rule; '60 sec' etc. when demo mode shortens it."""
    return f"{seconds // 60} min" if seconds % 60 == 0 and seconds >= 120 else f"{seconds} sec"


def build_message(plate: str, site: str, since: datetime, admin: bool) -> str:
    when = since.astimezone().strftime("%d-%b %H:%M")
    limit = fmt_threshold(get_settings().violation_threshold_seconds)
    if admin:
        return f"ALERT: unregistered vehicle {plate} parked in no-parking zone '{site}' for over {limit} (since {when})."
    return (
        f"Parking alert: your vehicle {plate} has been in the no-parking zone '{site}' "
        f"for over {limit} (since {when}). Please move it. This is an alert, not a fine."
    )[:300]


def _queue_notifications(db: Session, v: Violation, sess: ParkingSession, owner: Vehicle | None, now: datetime):
    s = get_settings()
    site = get_setting(db, "site_label", s.site_label)
    if owner:
        db.add(Notification(violation_id=v.id, kind="OWNER_SMS", recipient_phone=owner.owner_phone,
                            message=build_message(v.plate, site, sess.entry_at, admin=False)))
    else:
        admin_phone = get_setting(db, "admin_phone", "")
        msg = build_message(v.plate, site, sess.entry_at, admin=True)
        if is_valid_phone(admin_phone):
            db.add(Notification(violation_id=v.id, kind="ADMIN_SMS", recipient_phone=admin_phone, message=msg))
        else:
            db.add(Notification(violation_id=v.id, kind="ADMIN_SMS", message=msg, status="FAILED",
                                modem_response="No valid admin phone configured"))
    when = sess.entry_at.astimezone().strftime("%d-%b %H:%M")
    status_word = "registered" if owner else "unregistered"
    db.add(Notification(violation_id=v.id, kind="DASHBOARD", status="SHOWN",
                        message=f"Violation: {status_word} vehicle {v.plate} in no-parking zone '{site}' for over {fmt_threshold(s.violation_threshold_seconds)} (since {when})."))


# ---------------------------------------------------------------- SMS command queue
def expire_leases(db: Session, now: datetime) -> int:
    """A lease that ran out without an acknowledgement is UNCERTAIN: the SMS may or may not have been
    sent, so it is NOT resent automatically (avoids duplicate texts). An admin can retry manually."""
    n = 0
    for note in db.scalars(select(Notification).where(Notification.status == "LEASED", Notification.lease_until < now)):
        note.status = "UNCERTAIN"
        note.modem_response = "No acknowledgement before lease expired; delivery state unknown"
        n += 1
    db.commit()
    return n


def lease_next_command(db: Session, now: datetime) -> Notification | None:
    """Hand out at most ONE command at a time (the modem handles one AT job at a time)."""
    s = get_settings()
    expire_leases(db, now)
    if db.scalar(select(Notification).where(Notification.status == "LEASED")):
        return None  # a command is already in flight; duplicate polling must not double-send
    note = db.scalar(
        select(Notification)
        .where(Notification.kind.in_(("OWNER_SMS", "ADMIN_SMS")), Notification.status.in_(("PENDING", "RETRYING")),
               (Notification.next_attempt_at.is_(None)) | (Notification.next_attempt_at <= now),
               Notification.recipient_phone.is_not(None))
        .order_by(Notification.created_at)
        .limit(1)
    )
    if note is None:
        return None
    note.status = "LEASED"
    note.attempts += 1
    note.lease_until = now + timedelta(seconds=s.sms_lease_seconds)
    db.commit()
    return note


def ack_command(db: Session, note_id: str, outcome: str, response: str, now: datetime) -> Notification | None:
    """outcome: SUBMITTED | DELIVERED | FAILED | FAILED_RETRYABLE."""
    s = get_settings()
    note = db.get(Notification, note_id)
    if note is None:
        return None
    if note.status not in ("LEASED", "UNCERTAIN"):
        return note  # duplicate/late ack on a settled command: idempotent no-op
    note.modem_response = (response or "")[:300]
    if outcome in ("SUBMITTED", "DELIVERED"):
        note.status = outcome
    elif outcome == "FAILED_RETRYABLE" and note.attempts < s.sms_max_attempts:
        note.status = "RETRYING"
        note.next_attempt_at = now + timedelta(seconds=s.sms_retry_backoff_seconds * note.attempts)
    else:
        note.status = "FAILED"
    note.lease_until = None
    db.commit()
    return note
