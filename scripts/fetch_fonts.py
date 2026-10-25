"""Download the Cairo font (SIL Open Font License) into src/story_service/assets/fonts/.

    python scripts/fetch_fonts.py

S10 embeds a subset of it in every bundle (Section 7.2). Without it the assembler falls back to a local stand-in font
and adds the warning FONT_FALLBACK, which is fine for development but not for production."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

FONTS = Path(__file__).resolve().parents[1] / "src" / "story_service" / "assets" / "fonts"
BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/cairo/"
FILES = {"Cairo[slnt,wght].ttf": "Cairo%5Bslnt%2Cwght%5D.ttf", "OFL.txt": "OFL.txt"}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (fixed https URL)
        return r.read()


def main() -> int:
    FONTS.mkdir(parents=True, exist_ok=True)
    for name, remote in FILES.items():
        data = fetch(BASE + remote)
        if name.endswith(".ttf") and (len(data) < 50_000 or data[:4] not in (b"\x00\x01\x00\x00", b"true")):
            print(f"{name}: the download is not a TrueType font ({len(data)} bytes)", file=sys.stderr)
            return 1
        (FONTS / name).write_bytes(data)
        print(f"saved {FONTS / name} ({len(data) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
