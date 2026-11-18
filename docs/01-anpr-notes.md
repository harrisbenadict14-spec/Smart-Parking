# 01 — Plate Reading (ANPR) Notes

## How it works (`backend/app/anpr.py`, `anpr_worker.py`)
1. ESP32 uploads a JPEG → saved, detection status `PENDING_ANPR`.
2. A background job (every 5 s, separate from the violation timer) picks up pending detections.
3. Image is upscaled and tried in 3 variants (original, contrast-enhanced, sharpened). OCR = RapidOCR (PP-OCR models, onnxruntime, CPU, fully offline).
4. Candidate strings come from single text lines and from joining lines top-to-bottom (two-line plates).
5. Look-alike characters are repaired **by position** (O/0, I/1, S/5, B/8 …), then the string must match the Indian format and start with a valid state/UT code.
6. Confidence = OCR score × 0.93 per repaired character, × 0.8 if a near-identical runner-up exists. It is **not** a calibrated probability.
7. Below `ANPR_CONFIDENCE_THRESHOLD` (0.75), unreadable, or any error → `NEEDS_REVIEW` (human enters the plate). Never guesses an owner.

No trained plate-localisation model is used; OCR runs on the whole frame and the plate pattern is searched in the text. This works for a plate that fills a reasonable part of the frame.

## What has been measured
| Check | Result |
|---|---|
| Unit tests (parsing, repair, ambiguity, worker, failure paths) | pass |
| Real OCR engine on synthetic plates, 35–100 px tall, blur, noise, tilt up to 8° | 4/4 each setting, 0 wrong reads |
| Real camera image with no plate (ceiling) | no plate read (correct) |
| 25 web/shop photos of Indian plates (`v2/benchmark/web_plates`, labelled by eye) | **22/25 read correctly, 0 wrong auto-accepted**, 3 sent to review (invalid state code `TH`, textured green EV plate, non-standard `UP16CC108X`). ~1.4 s/image |
| **Real plates from your GC2145 camera** | **NOT MEASURED YET** — no ESP32-CAM plate photos exist |

Bug found by that test and fixed (regression-tested): the "IND" hologram text was read as series letters
(`MN03` + `IND` + `1472` → `MN03IND1472` at 0.80, above the auto-accept threshold).
The web photos are clean, large and front-on, so they say little about your camera at night, at an angle or at distance.
Two web images with several plates in one frame were excluded: with 2+ plates in view the reader may pick either one.

Synthetic results do **not** predict real-world accuracy (glare, dirt, fonts, angle, night, motion).

## Known limits
- A plate missing its last digit can be misparsed as a different valid plate (e.g. `TN45AB123` → `TN45A8123`); mitigated by lower confidence and the registry match, not eliminated.
- Single-line pattern assumes standard Indian format; BH-series and special plates go to review.
- ~1–4 s per image on CPU (first image slower while models load).
- GC2145 sensor: no hardware JPEG, 640×480, weak low-light performance.

## To get real numbers
Put 30+ real photos taken **with the ESP32-CAM at the real mounting distance/angle** (day, evening, different vehicles) in a folder, named `TN45AB1234_01.jpg` (true plate first; `NONE_xx.jpg` for no plate), then:

```
cd v2\backend
.venv\Scripts\python.exe tools\benchmark.py <folder>
```
Pick the threshold where "WRONG accepted" is 0 on a varied sample, and set `ANPR_CONFIDENCE_THRESHOLD`.
