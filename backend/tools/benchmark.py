"""Measure plate-reading accuracy on YOUR camera's images.

Usage (from v2/backend):
    .venv\\Scripts\\python.exe tools\\benchmark.py <folder> [--threshold 0.75]

Put real captures in <folder>, named  <TRUE_PLATE>_<anything>.jpg  e.g.  TN45AB1234_01.jpg
Use  NONE_<anything>.jpg  for photos that contain no plate (to measure false reads).

Reports exact-match rate, how many would go to human review, and - the number that matters for
SMS safety - how many WRONG plates would be auto-accepted at each confidence threshold.
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.anpr import read_plate  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--threshold", type=float, default=0.75)
    a = ap.parse_args()

    files = sorted(p for p in Path(a.folder).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not files:
        sys.exit(f"No images in {a.folder}")
    rows = []
    for p in files:
        truth = p.stem.split("_")[0].upper()
        t = time.time()
        r = read_plate(p.read_bytes())
        rows.append((p.name, truth, r.plate, r.confidence, time.time() - t, r.raw_text))
        ok = "OK " if (r.plate == truth or (truth == "NONE" and r.plate is None)) else "BAD"
        print(f"{ok} {p.name:32} truth={truth:11} read={str(r.plate):11} conf={r.confidence:.2f} {rows[-1][4]:.1f}s raw={r.raw_text!r}")

    plates = [r for r in rows if r[1] != "NONE"]
    nones = [r for r in rows if r[1] == "NONE"]
    print("\n==== SUMMARY ====")
    print(f"images: {len(rows)} ({len(plates)} with plate, {len(nones)} without)   avg {sum(r[4] for r in rows)/len(rows):.1f}s/image")
    if plates:
        exact = sum(1 for r in plates if r[2] == r[1])
        print(f"exact match (any confidence): {exact}/{len(plates)} = {100*exact/len(plates):.0f}%")
    print(f"\n{'threshold':>9} {'auto-accepted':>14} {'correct':>8} {'WRONG accepted':>15} {'to review':>10}")
    for th in (0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95):
        acc = [r for r in rows if r[2] and r[3] >= th]
        wrong = [r for r in acc if r[2] != r[1]]
        print(f"{th:>9.2f} {len(acc):>14} {len(acc)-len(wrong):>8} {len(wrong):>15} {len(rows)-len(acc):>10}"
              + ("   <- current" if abs(th - a.threshold) < 1e-9 else ""))
    print("\nChoose the lowest threshold where 'WRONG accepted' is 0 on a large, varied sample.")
    print("A wrong auto-accepted plate can text the wrong owner, so prefer a higher threshold.")


if __name__ == "__main__":
    main()
