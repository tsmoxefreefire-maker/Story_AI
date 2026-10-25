"""Library acceptance (Section 6.8): contact sheets of every asset, the joints check, and an optional VLM review.

python scripts/library_sheets.py out/library            # PNG sheets + joints check
python scripts/library_sheets.py out/library --review   # also scored by the vision model (needs its key)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from story_service.devtools.library import joints_problems, render_sheets


def main() -> int:
    ap = argparse.ArgumentParser(description="Contact sheets and checks of the asset library")
    ap.add_argument("out", type=Path)
    ap.add_argument("--review", action="store_true", help="score every sheet with the vision model")
    args = ap.parse_args()
    paths = render_sheets(args.out)
    print(f"{len(paths)} sheets in {args.out}")
    problems = joints_problems()
    print("joints: ok" if not problems else "joints: " + "; ".join(problems))
    if args.review:
        # the real vision client is milestone M1; with it: devtools.library.review_sheets(paths, vlm)
        print("--review needs the vision client of milestone M1 (devtools.library.review_sheets)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
