import uuid
from datetime import datetime

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, UTCDateTime, utcnow


def _id() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="ADMIN")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Vehicle(Base):
    """Locally maintained, consent-based registry. Plate -> owner phone comes ONLY from here."""

    __tablename__ = "vehicles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    plate: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # normalized
    owner_name: Mapped[str] = mapped_column(String(120))
    owner_phone: Mapped[str] = mapped_column(String(20))  # E.164
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Zone(Base):
    __tablename__ = "zones"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    site_label: Mapped[str] = mapped_column(String(200))


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    zone_id: Mapped[str] = mapped_column(ForeignKey("zones.id"))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    present: Mapped[bool] = mapped_column(Boolean, default=False)
    last_presence_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    rssi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_sats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gps_fix_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    gsm_status: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON from device
    last_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)  # learned from heartbeats (live camera view)


class Detection(Base):
    __tablename__ = "detections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"))
    zone_id: Mapped[str] = mapped_column(ForeignKey("zones.id"))
    captured_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)  # server time
    image_path: Mapped[str] = mapped_column(String(400))
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # PENDING_ANPR | ACCEPTED | NO_SESSION | NEEDS_REVIEW | REJECTED
    status: Mapped[str] = mapped_column(String(20), default="PENDING_ANPR", index=True)
    raw_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # anpr | manual
    __table_args__ = (UniqueConstraint("device_id", "idempotency_key", name="uq_detection_idem"),)


class ParkingSession(Base):
    __tablename__ = "parking_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    zone_id: Mapped[str] = mapped_column(ForeignKey("zones.id"))
    plate: Mapped[str] = mapped_column(String(16), index=True)
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    entry_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_presence_at: Mapped[datetime] = mapped_column(UTCDateTime)
    exit_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE", index=True)  # ACTIVE | CLOSED
    close_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)


# At most one ACTIVE session per zone (single-bay prototype); enforced by the database.
Index(
    "uq_active_session_zone",
    ParkingSession.zone_id,
    unique=True,
    sqlite_where=(ParkingSession.status == "ACTIVE"),
)


class Violation(Base):
    __tablename__ = "violations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("parking_sessions.id"), unique=True)  # one per session
    plate: Mapped[str] = mapped_column(String(16))
    vehicle_id: Mapped[str | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    registered: Mapped[bool] = mapped_column(Boolean)
    confirmed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    elapsed_seconds: Mapped[int] = mapped_column(Integer)


class Notification(Base):
    """Doubles as the SMS command queue for the device."""

    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    violation_id: Mapped[str] = mapped_column(ForeignKey("violations.id"))
    kind: Mapped[str] = mapped_column(String(12))  # OWNER_SMS | ADMIN_SMS | DASHBOARD
    recipient_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    message: Mapped[str] = mapped_column(String(300))
    # PENDING | LEASED | SUBMITTED | DELIVERED | RETRYING | FAILED | UNCERTAIN | CANCELLED | SHOWN
    status: Mapped[str] = mapped_column(String(12), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    modem_response: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("violation_id", "kind", name="uq_notification_once"),)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(500))


class AuditLog(Base):
    """Who changed what. Never stores passwords or request bodies."""

    __tablename__ = "audit_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_id)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(120))
    detail: Mapped[str | None] = mapped_column(String(300), nullable=True)
