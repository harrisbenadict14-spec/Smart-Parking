from datetime import timedelta

import pytest
from sqlalchemy import select

from app import anpr, anpr_worker
from app.anpr import PlateReading, pick_plate, repair_and_validate
from app.config import get_settings
from app.models import Detection, ParkingSession
from conftest import T0, keep_present
from app import logic


# ---------------------------------------------------------------- pure parsing (no OCR engine needed)
@pytest.mark.parametrize("text,expected", [
    ("TN45AB1234", "TN45AB1234"),
    ("TN 45 AB 1234", "TN45AB1234"),
    ("KA01A1234", "KA01A1234"),
    ("DL3CAB1234", "DL3CAB1234"),
    ("TN45AB1234.", "TN45AB1234"),
])
def test_valid_plates_pass(text, expected):
    assert repair_and_validate(text)[0] == expected


def test_lookalike_characters_are_repaired_by_position():
    # letter O in the digit slots, digit 0 in the letter slots
    assert repair_and_validate("TN4SAB1234")[0] == "TN45AB1234"   # S -> 5 in district
    assert repair_and_validate("TN45A81234")[0] == "TN45AB1234"   # 8 -> B in series
    plate, repairs = repair_and_validate("TN45AB12O4")
    assert plate == "TN45AB1204" and repairs == 1


@pytest.mark.parametrize("text", ["HELLO", "BHARAT", "12345678", "ZZ45AB1234", "TN45AB12345678", "TN45AB12"])
def test_non_plates_and_bad_state_codes_rejected(text):
    assert repair_and_validate(text) is None  # ZZ is not a state code


def test_known_limit_truncated_plate_can_be_misparsed_but_is_penalised():
    # "TN45AB123" (last digit lost) can be read as TN45A + 8123 by repairing B->8. No repair rule can tell
    # these apart; the safeguards are the confidence penalty and the registry match, not rejection.
    plate, repairs = repair_and_validate("TN45AB123")
    assert repairs >= 1
    assert pick_plate([("TN45AB123", 0.95, 0.0)]).confidence < pick_plate([("TN45AB1234", 0.95, 0.0)]).confidence


def test_two_line_plate_is_joined_top_to_bottom():
    lines = [("AB1234", 0.90, 200.0), ("TN45", 0.94, 100.0)]  # given out of order
    r = pick_plate(lines)
    assert r.plate == "TN45AB1234" and r.confidence > 0.85


def test_repairs_lower_confidence():
    clean = pick_plate([("TN45AB1234", 0.95, 0.0)])
    repaired = pick_plate([("TN45AB12O4", 0.95, 0.0)])
    assert repaired.confidence < clean.confidence


def test_ambiguous_runner_up_lowers_confidence():
    unambiguous = pick_plate([("TN45AB1234", 0.95, 0.0)])
    ambiguous = pick_plate([("TN45AB1234", 0.95, 0.0), ("TN45AB1284", 0.94, 50.0)])
    assert ambiguous.confidence < unambiguous.confidence


def test_no_text_gives_no_plate():
    assert pick_plate([]).plate is None
    assert pick_plate([("WELCOME TO CAMPUS", 0.99, 0.0)]).plate is None


# ---------------------------------------------------------------- worker with an injected reader
def _detection(db, device, zone, t=T0):
    (get_settings().storage_dir / "captures").mkdir(parents=True, exist_ok=True)
    (get_settings().storage_dir / "captures/a.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    d = Detection(device_id=device.id, zone_id=zone.id, captured_at=t, image_path="captures/a.jpg")
    db.add(d)
    db.commit()
    return d


def test_worker_accepts_confident_reading_and_starts_session(db, device, zone):
    logic.record_presence(db, device, True, T0)
    d = _detection(db, device, zone)
    n = anpr_worker.process_pending(db, reader=lambda b: PlateReading("TN45AB1234", "TN 45 AB 1234", 0.92), now=T0)
    assert n == 1 and d.status == "ACCEPTED" and d.source == "anpr" and d.raw_text == "TN 45 AB 1234"
    assert db.scalar(select(ParkingSession)).plate == "TN45AB1234"


def test_worker_sends_low_confidence_to_review(db, device, zone):
    logic.record_presence(db, device, True, T0)
    d = _detection(db, device, zone)
    anpr_worker.process_pending(db, reader=lambda b: PlateReading("TN45AB1234", "TN45AB1234", 0.50), now=T0)
    assert d.status == "NEEDS_REVIEW" and db.scalar(select(ParkingSession)) is None


def test_worker_sends_unreadable_image_to_review(db, device, zone):
    logic.record_presence(db, device, True, T0)
    d = _detection(db, device, zone)
    anpr_worker.process_pending(db, reader=lambda b: PlateReading(None, "", 0.0), now=T0)
    assert d.status == "NEEDS_REVIEW"


def test_worker_crash_never_leaves_detection_stuck(db, device, zone):
    d = _detection(db, device, zone)

    def boom(b):
        raise RuntimeError("engine died")

    anpr_worker.process_pending(db, reader=boom, now=T0)
    db.refresh(d)
    assert d.status == "NEEDS_REVIEW"


def test_worker_ignores_already_processed(db, device, zone):
    d = _detection(db, device, zone)
    d.status = "ACCEPTED"
    db.commit()
    assert anpr_worker.process_pending(db, reader=lambda b: 1 / 0) == 0


# ---------------------------------------------------------------- real OCR engine on a synthetic plate
def _synthetic_plate_jpeg(text: str) -> bytes:
    import cv2
    import numpy as np

    img = np.full((480, 640, 3), 90, np.uint8)
    cv2.rectangle(img, (120, 190), (520, 290), (255, 255, 255), -1)
    cv2.rectangle(img, (120, 190), (520, 290), (0, 0, 0), 3)
    cv2.putText(img, text, (140, 262), cv2.FONT_HERSHEY_DUPLEX, 2.0, (0, 0, 0), 4, cv2.LINE_AA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def test_real_engine_reads_synthetic_plate():
    pytest.importorskip("rapidocr")
    r = anpr.read_plate(_synthetic_plate_jpeg("TN45AB1234"))
    assert r.plate == "TN45AB1234", f"got {r}"
    assert 0.0 < r.confidence <= 1.0


def test_real_engine_on_blank_image_finds_nothing():
    pytest.importorskip("rapidocr")
    import cv2
    import numpy as np

    blank = cv2.imencode(".jpg", np.full((480, 640, 3), 128, np.uint8))[1].tobytes()
    assert anpr.read_plate(blank).plate is None
    assert anpr.read_plate(b"not an image").plate is None


# ---------------------------------------------------------------- regression: found on real plate photos
def test_ind_hologram_text_is_not_read_as_series_letters():
    # Real photo "MN03 / 1472" produced lines MN03 + IND + 1472 and was wrongly read as MN03IND1472 at 0.80.
    r = pick_plate([("MN03", 0.99, 100.0), ("IND", 0.98, 60.0), ("1472", 0.99, 200.0)])
    assert r.plate != "MN03IND1472"
    assert "IND" not in (r.plate or "")


def test_ind_glued_to_number_is_stripped():
    assert repair_and_validate("INDTN76AR6003")[0] == "TN76AR6003"
    assert pick_plate([("IND", 0.9, 0.0), ("TN76 AR6003", 0.99, 10.0)]).plate == "TN76AR6003"
