from datetime import timedelta

from sqlalchemy import func, select

from app import logic
from app.db import SessionLocal
from app.models import Detection, Notification, ParkingSession, Violation
from conftest import T0, add_vehicle, keep_present, start_session


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


# ---------------------------------------------------------------- threshold: strictly > 900 s
def test_exactly_900_seconds_does_not_trigger(db, device, zone):
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=900))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=900))
    assert count(db, Violation) == 0


def test_899_seconds_does_not_trigger(db, device, zone):
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=899))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=899))
    assert count(db, Violation) == 0


def test_901_seconds_triggers_exactly_one_violation(db, device, zone):
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=901))
    now = T0 + timedelta(seconds=901)
    logic.evaluate_sessions(db, now)
    logic.evaluate_sessions(db, now + timedelta(seconds=5))  # scheduler runs again
    logic.evaluate_sessions(db, now + timedelta(seconds=10))
    assert count(db, Violation) == 1
    assert count(db, Notification) == 2  # ADMIN_SMS (failed: no admin phone) + DASHBOARD, never duplicated


def test_exit_at_14_59_closes_without_violation(db, device, zone):
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(minutes=14, seconds=59))
    # vehicle leaves; no more presence heartbeats
    logic.evaluate_sessions(db, T0 + timedelta(minutes=14, seconds=59) + timedelta(seconds=120))
    s = db.scalar(select(ParkingSession))
    assert s.status == "CLOSED" and s.close_reason == "EXITED"
    assert count(db, Violation) == 0
    assert count(db, Notification) == 0


def test_single_missed_heartbeat_does_not_close_session(db, device, zone):
    start_session(db, device, zone)
    logic.evaluate_sessions(db, T0 + timedelta(seconds=30))  # 30 s silence < 60 s grace
    assert db.scalar(select(ParkingSession)).status == "ACTIVE"


def test_presence_lost_beyond_grace_before_threshold_never_violates_later(db, device, zone):
    start_session(db, device, zone)
    logic.evaluate_sessions(db, T0 + timedelta(seconds=200))  # silence > grace -> closed
    logic.evaluate_sessions(db, T0 + timedelta(seconds=2000))
    assert count(db, Violation) == 0


# ---------------------------------------------------------------- notification routing
def test_registered_vehicle_gets_owner_sms_only(db, device, zone):
    add_vehicle(db, phone="+919876543210")
    logic.set_setting(db, "admin_phone", "+911111111111")
    db.commit()
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=901))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=901))
    kinds = {n.kind: n for n in db.scalars(select(Notification))}
    assert set(kinds) == {"OWNER_SMS", "DASHBOARD"}
    assert kinds["OWNER_SMS"].recipient_phone == "+919876543210"
    assert kinds["OWNER_SMS"].status == "PENDING"
    assert "not a fine" in kinds["OWNER_SMS"].message


def test_unregistered_plate_goes_to_admin_and_dashboard(db, device, zone):
    logic.set_setting(db, "admin_phone", "+911111111111")
    db.commit()
    start_session(db, device, zone, plate="KA01AB9999")
    keep_present(db, device, T0, T0 + timedelta(seconds=901))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=901))
    kinds = {n.kind: n for n in db.scalars(select(Notification))}
    assert set(kinds) == {"ADMIN_SMS", "DASHBOARD"}
    assert kinds["ADMIN_SMS"].recipient_phone == "+911111111111"
    v = db.scalar(select(Violation))
    assert v.registered is False


def test_no_admin_phone_is_recorded_as_failed_not_silently_dropped(db, device, zone):
    start_session(db, device, zone, plate="KA01AB9999")
    keep_present(db, device, T0, T0 + timedelta(seconds=901))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=901))
    admin = db.scalar(select(Notification).where(Notification.kind == "ADMIN_SMS"))
    assert admin.status == "FAILED" and "admin phone" in admin.modem_response


def test_registered_without_consent_is_treated_as_unregistered(db, device, zone):
    add_vehicle(db, consent=False)
    logic.set_setting(db, "admin_phone", "+911111111111")
    db.commit()
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=901))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=901))
    assert not db.scalar(select(Notification).where(Notification.kind == "OWNER_SMS"))


# ---------------------------------------------------------------- plate handling
def test_low_confidence_goes_to_review_and_creates_no_session(db, device, zone):
    session, det = start_session(db, device, zone, confidence=0.40)
    assert session is None and det.status == "NEEDS_REVIEW"
    assert count(db, ParkingSession) == 0


def test_implausible_plate_goes_to_review(db, device, zone):
    session, det = start_session(db, device, zone, plate="HELLO")
    assert session is None and det.status == "NEEDS_REVIEW"


def test_plate_normalisation_keeps_raw_text(db, device, zone):
    session, det = start_session(db, device, zone, plate="tn 45-ab 1234")
    assert session.plate == "TN45AB1234" and det.raw_text == "tn 45-ab 1234"


def test_reading_without_confirmed_presence_creates_no_session(db, device, zone):
    det = Detection(device_id=device.id, zone_id=zone.id, captured_at=T0, image_path="x.jpg")
    db.add(det)
    db.commit()
    assert logic.ingest_plate(db, det, "TN45AB1234", 0.95, T0) is None
    assert det.status == "NO_SESSION"


def test_repeated_detections_do_not_create_duplicate_sessions(db, device, zone):
    start_session(db, device, zone)
    for i in range(1, 6):
        t = T0 + timedelta(seconds=10 * i)
        start_session(db, device, zone, at=t)
    assert count(db, ParkingSession) == 1
    assert db.scalar(select(ParkingSession)).entry_at == T0  # timer was never reset


def test_different_plate_replaces_session_without_violation(db, device, zone):
    start_session(db, device, zone, plate="TN45AB1234")
    start_session(db, device, zone, plate="KA01AB9999", at=T0 + timedelta(seconds=30))
    sessions = list(db.scalars(select(ParkingSession).order_by(ParkingSession.entry_at)))
    assert [s.status for s in sessions] == ["CLOSED", "ACTIVE"]
    assert sessions[0].close_reason == "REPLACED"


# ---------------------------------------------------------------- persistence
def test_restart_does_not_reset_timer(db, device, zone):
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=800))
    # "restart": brand-new DB session / no in-memory state
    with SessionLocal() as fresh:
        keep_present(fresh, fresh.get(type(device), device.id), T0 + timedelta(seconds=810), T0 + timedelta(seconds=901))
        logic.evaluate_sessions(fresh, T0 + timedelta(seconds=901))
        assert count(fresh, Violation) == 1


# ---------------------------------------------------------------- SMS queue truthfulness
def _violation_with_owner_sms(db, device, zone):
    add_vehicle(db)
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=901))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=901))
    return T0 + timedelta(seconds=905)


def test_lease_hands_out_one_command_and_duplicate_poll_gets_nothing(db, device, zone):
    now = _violation_with_owner_sms(db, device, zone)
    first = logic.lease_next_command(db, now)
    again = logic.lease_next_command(db, now + timedelta(seconds=1))
    assert first is not None and again is None


def test_submitted_is_not_delivered(db, device, zone):
    now = _violation_with_owner_sms(db, device, zone)
    n = logic.lease_next_command(db, now)
    logic.ack_command(db, n.id, "SUBMITTED", "+CMGS: 12 OK", now)
    assert n.status == "SUBMITTED" and n.status != "DELIVERED"


def test_duplicate_ack_is_idempotent(db, device, zone):
    now = _violation_with_owner_sms(db, device, zone)
    n = logic.lease_next_command(db, now)
    logic.ack_command(db, n.id, "SUBMITTED", "ok", now)
    logic.ack_command(db, n.id, "FAILED", "late contradictory", now)
    assert n.status == "SUBMITTED"


def test_retryable_failure_backs_off_then_gives_up(db, device, zone):
    now = _violation_with_owner_sms(db, device, zone)
    for attempt in (1, 2, 3):
        n = logic.lease_next_command(db, now)
        assert n is not None and n.attempts == attempt
        logic.ack_command(db, n.id, "FAILED_RETRYABLE", "+CME ERROR: SIM not inserted", now)
        if attempt < 3:
            assert n.status == "RETRYING"
            assert logic.lease_next_command(db, now) is None  # backoff not elapsed
            now += timedelta(seconds=600)
    assert n.status == "FAILED"


def test_lost_ack_becomes_uncertain_and_is_not_resent(db, device, zone):
    now = _violation_with_owner_sms(db, device, zone)
    n = logic.lease_next_command(db, now)
    later = now + timedelta(seconds=500)  # lease (120 s) expired, no ack
    assert logic.lease_next_command(db, later) is None
    assert n.status == "UNCERTAIN"


def test_dashboard_alert_wording_matches_registration(db, device, zone):
    add_vehicle(db)  # TN45AB1234 registered + consent
    start_session(db, device, zone)
    keep_present(db, device, T0, T0 + timedelta(seconds=901))
    logic.evaluate_sessions(db, T0 + timedelta(seconds=901))
    dash = db.scalar(select(Notification).where(Notification.kind == "DASHBOARD"))
    assert "registered vehicle TN45AB1234" in dash.message and "unregistered" not in dash.message
