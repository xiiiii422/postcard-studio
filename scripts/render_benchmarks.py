#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Render the five benchmark cards and print tone/EDGE polygon counts.

The benchmark photos are not part of this repository (privacy/size). Put your own
photos next to this repo or pass directories:

    python scripts/render_benchmarks.py --photos <dir-with-photo-A.img/photo-B.img>
    python scripts/render_benchmarks.py --real-dir <dir-with-R1-R3 jpgs> --out bench/

Expected card ids (all optional - missing files are skipped):
    A  photo-A.img                  landscape / misty mountains
    B  photo-B.img                  landscape / high contrast sky
    R1 000103040003.jpg             busy market street (hard case)
    R2 DJI_20260207112724_0096_D.jpg  people + scenery
    R3 000103040004.jpg             two people

Every rendered SVG is XML-validated with ElementTree; the script exits non-zero if
any card fails. Use it as a smoke test after changing make_postcard_svg.py.
"""
import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image                     # noqa: E402
import make_postcard_svg as S             # noqa: E402

CARDS = [
    ("A", "photo-A.img", "ABOVE THE CLOUDS"),
    ("B", "photo-B.img", "HIGH CONTRAST"),
    ("R1", "000103040003.jpg", "THE OVERLOOK"),
    ("R2", "DJI_20260207112724_0096_D.jpg", "RIDGE LINE"),
    ("R3", "000103040004.jpg", "DEEP VALLEY"),
]


def find(tag, fname, photos_dir, real_dir):
    if tag in ("A", "B"):
        candidate = Path(photos_dir) / fname
    else:
        candidate = Path(real_dir) / fname if real_dir else None
    return candidate if candidate and candidate.is_file() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--photos", default=str(ROOT), help="directory with photo-A.img / photo-B.img")
    ap.add_argument("--real-dir", default="", help="directory with the R1/R2/R3 jpgs")
    ap.add_argument("--out", default=str(ROOT / "bench"), help="output directory")
    ap.add_argument("--title-prefix", default="BENCH")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, failed = [], 0

    for tag, fname, title in CARDS:
        path = find(tag, fname, args.photos, args.real_dir)
        if path is None:
            print("%-3s skip (missing %s)" % (tag, fname))
            continue
        im = Image.open(path)
        t0 = time.time()
        layers, gw, gh, _idx, _pal = S.vectorize_photo(im)
        tone = sum(len(l[2]) for l in layers if l[1] != "EDGE")
        edge = sum(len(l[2]) for l in layers if l[1] == "EDGE")
        href = S.embed(im)
        pal = S.sample_palette(im)
        ratio = im.size[1] / im.size[0]
        front = S.front_svg(href, pal, title, date_str="2026-10-18", ph_ratio=ratio,
                            photo=im, number="N° " + tag)
        back = S.back_svg(href, pal, title, idx="N° " + tag, ph_ratio=ratio)
        try:
            ET.fromstring(front)
            ET.fromstring(back)
        except ET.ParseError as e:
            print("%-3s FAIL xml: %s" % (tag, e))
            failed += 1
            continue
        (out / ("%s-front.svg" % tag)).write_text(front, encoding="utf-8")
        (out / ("%s-back.svg" % tag)).write_text(back, encoding="utf-8")
        dt = time.time() - t0
        rows.append((tag, gw, gh, tone, edge, len(front) / 1024.0, dt))
        print("%-3s grid=%dx%d tone=%d edge=%d front=%.0fKB %.1fs" % (tag, gw, gh, tone, edge, len(front) / 1024.0, dt))

    print("\n%-3s %-11s %-6s %-5s %-9s %-6s" % ("id", "grid", "tone", "edge", "front", "time"))
    for tag, gw, gh, tone, edge, kb, dt in rows:
        print("%-3s %-11s %-6d %-5d %-9s %-6.1fs" % (tag, "%dx%d" % (gw, gh), tone, edge, "%.0fKB" % kb, dt))
    if failed:
        print("\n%d card(s) FAILED" % failed)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
