"""Plate reading (runs on the laptop, never on the ESP32).

Pipeline: decode JPEG -> several preprocessing variants -> OCR (RapidOCR / PP-OCR via onnxruntime)
-> candidate strings from single lines and top-to-bottom line joins -> positional character repair
-> Indian format + valid state-code check -> best candidate with an honest confidence.

Confidence is the OCR engine's own score, reduced when characters had to be repaired. It is NOT a
calibrated probability: the auto-accept threshold must be tuned with tools/benchmark.py on real
captures from this camera. Anything below the threshold goes to the human review queue.
"""
from __future__ import annotations

import itertools
import logging
import re
import threading
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger("noparking.anpr")

STATE_CODES = set(
    "AN AP AR AS BR CH CG DD DL DN GA GJ HP HR JH JK KA KL LA LD MH ML MN MP MZ NL OD OR PB PY RJ SK TN TR TS UK UP WB".split()
)
_FORMAT = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{0,3})(\d{4})$")
# Look-alike repair, by expected character class at that position
_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "T": "7"}
_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G"}
REPAIR_PENALTY = 0.93  # per repaired character


@dataclass
class PlateReading:
    plate: str | None  # normalised, only if it passed format + state-code checks
    raw_text: str  # what OCR actually produced for the chosen candidate (or best guess)
    confidence: float  # 0..1, see module docstring
    candidates: list[tuple[str, float]] = field(default_factory=list)
    engine: str = "rapidocr"


_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR  # imported lazily: heavy and optional in tests

            _engine = RapidOCR()
        return _engine


def _variants(img: np.ndarray) -> list[np.ndarray]:
    import cv2

    h, w = img.shape[:2]
    scale = 2.0 if w < 1000 else 1.0
    base = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC) if scale != 1 else img
    gray = cv2.cvtColor(base, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
    blur = cv2.GaussianBlur(clahe, (0, 0), 1.2)
    sharp = cv2.addWeighted(clahe, 1.6, blur, -0.6, 0)
    return [base, cv2.cvtColor(clahe, cv2.COLOR_GRAY2BGR), cv2.cvtColor(sharp, cv2.COLOR_GRAY2BGR)]


def _ocr_lines(img: np.ndarray) -> list[tuple[str, float, float]]:
    """Return (text, score, top_y) for each detected text line."""
    res = _get_engine()(img)
    if res is None or res.txts is None:
        return []
    out = []
    for box, txt, score in zip(res.boxes, res.txts, res.scores):
        out.append((txt, float(score), float(np.min(np.asarray(box)[:, 1]))))
    return out


def _clean(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


_NOISE_WORDS = {"IND", "INDIA"}  # printed on every HSRP plate beside the hologram; never part of the number


def repair_and_validate(s: str) -> tuple[str, int] | None:
    """Try to turn `s` into a valid plate by fixing look-alike characters by position.
    Returns (plate, number_of_repairs) or None."""
    s = _clean(s)
    r = _repair(s)
    if r is None and s.startswith("IND"):  # OCR glued the "IND" tag onto the number
        r = _repair(s[3:])
    return r


def _repair(s: str) -> tuple[str, int] | None:
    if not 8 <= len(s) <= 11:
        return None
    best: tuple[str, int] | None = None
    best_cost = 99.0
    # plate = 2 letters + 1-2 digits + 0-3 letters + 4 digits
    # Prior: 2-digit districts and 1-2 letter series are by far the most common; rarer shapes cost extra
    # so that "one repaired character in a common shape" beats "zero repairs in a rare shape".
    for d_len in (2, 1):
        for l_len in range(0, 4):
            if 2 + d_len + l_len + 4 != len(s):
                continue
            prior = (0.5 if d_len == 1 else 0.0) + {0: 0.25, 1: 0.0, 2: 0.0, 3: 0.75}[l_len]
            kinds = "L" * 2 + "D" * d_len + "L" * l_len + "D" * 4
            fixed, repairs, ok = [], 0, True
            for ch, k in zip(s, kinds):
                if k == "L":
                    if ch.isalpha():
                        fixed.append(ch)
                    elif ch in _TO_LETTER:
                        fixed.append(_TO_LETTER[ch]); repairs += 1
                    else:
                        ok = False; break
                else:
                    if ch.isdigit():
                        fixed.append(ch)
                    elif ch in _TO_DIGIT:
                        fixed.append(_TO_DIGIT[ch]); repairs += 1
                    else:
                        ok = False; break
            plate = "".join(fixed)
            if ok and repairs <= 2 and _FORMAT.match(plate) and plate[:2] in STATE_CODES:
                if repairs + prior < best_cost:
                    best, best_cost = (plate, repairs), repairs + prior
    return best


def pick_plate(lines: list[tuple[str, float, float]]) -> PlateReading:
    """Choose the best plate from OCR lines. Pure function (unit-tested without OCR)."""
    lines = [ln for ln in lines if _clean(ln[0]) not in _NOISE_WORDS]  # drop the "IND" tag line
    lines = sorted(lines, key=lambda t: t[2])  # top to bottom (two-line plates)
    pool: list[tuple[str, float, str]] = []  # (text, score, raw)
    for txt, sc, _ in lines:
        pool.append((txt, sc, txt))
    for n in (2, 3):  # join adjacent lines: "TN45" + "AB1234"
        for combo in itertools.combinations(lines, n):
            raw = " ".join(c[0] for c in combo)
            pool.append((raw, sum(c[1] for c in combo) / n, raw))
    cands: dict[str, tuple[float, str]] = {}
    for txt, sc, raw in pool:
        r = repair_and_validate(txt)
        if r is None:
            continue
        plate, repairs = r
        conf = sc * (REPAIR_PENALTY ** repairs)
        if plate not in cands or conf > cands[plate][0]:
            cands[plate] = (conf, raw)
    if not cands:
        best_raw = max(lines, key=lambda t: t[1])[0] if lines else ""
        return PlateReading(plate=None, raw_text=best_raw, confidence=0.0)
    ranked = sorted(((p, c, r) for p, (c, r) in cands.items()), key=lambda t: -t[1])
    plate, conf, raw = ranked[0]
    if len(ranked) > 1 and ranked[1][1] > conf - 0.05:  # runner-up too close: be unsure
        conf *= 0.8
    return PlateReading(plate=plate, raw_text=raw, confidence=round(conf, 4),
                        candidates=[(p, round(c, 4)) for p, c, _ in ranked[:5]])


def read_plate(jpeg: bytes) -> PlateReading:
    import cv2

    img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return PlateReading(plate=None, raw_text="", confidence=0.0)
    best = PlateReading(plate=None, raw_text="", confidence=0.0)
    for variant in _variants(img):
        reading = pick_plate(_ocr_lines(variant))
        if reading.plate and reading.confidence > best.confidence:
            best = reading
        elif not best.plate and not reading.plate and len(reading.raw_text) > len(best.raw_text):
            best = reading
    return best
