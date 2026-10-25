"""Screenshot a built bundle at the start of every line and paste the frames into one contact sheet PNG.

    python scripts/contact_sheet.py out/golden                 # -> out/golden/contact_sheet.png
    python scripts/contact_sheet.py out/golden --every 2       # every second line

A quick way to review a whole story by eye (characters, props, captions) without watching it. Needs Playwright with
Chromium and Pillow."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

from story_service.devtools.bundle_server import serving

THUMB = (640, 400)


def main() -> int:
    ap = argparse.ArgumentParser(description="Contact sheet of a story bundle")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--every", type=int, default=1, help="take every Nth line")
    ap.add_argument("--cols", type=int, default=3)
    args = ap.parse_args()
    timeline = json.loads((args.folder / "timeline.json").read_text(encoding="utf-8"))
    shots = [(ln["start_ms"] + 300, f"{s['id']} {ln['id']}") for s in timeline["scenes"] for ln in s["lines"]]
    shots = shots[:: max(1, args.every)]
    frames = []
    with sync_playwright() as p, serving(str(args.folder)) as url:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        page.goto(url + "/index.html?freeze=1")
        for t, label in shots:
            page.evaluate(f"window.__story.seek({t})")
            page.wait_for_timeout(450)  # let the scene crossfade finish
            img = Image.open(io.BytesIO(page.screenshot())).convert("RGB").resize(THUMB)
            ImageDraw.Draw(img).text((10, 8), label, fill=(30, 42, 58))
            frames.append(img)
        browser.close()
    rows = (len(frames) + args.cols - 1) // args.cols
    sheet = Image.new("RGB", (THUMB[0] * args.cols, THUMB[1] * rows), "white")
    for i, img in enumerate(frames):
        sheet.paste(img, ((i % args.cols) * THUMB[0], (i // args.cols) * THUMB[1]))
    out = args.folder / "contact_sheet.png"
    sheet.save(out)
    print(f"{len(frames)} frames -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
