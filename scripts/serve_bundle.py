"""Serve a built bundle folder locally with the production headers and HTTP Range support, for manual testing.

    python scripts/serve_bundle.py out/golden            # then open http://localhost:8000
    python scripts/serve_bundle.py out/golden --port 8080

Do not use `python -m http.server` for this: it ignores Range requests, so the player cannot seek inside a line's MP3.
See src/story_service/devtools/bundle_server.py."""

from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from story_service.devtools.bundle_server import make_server  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve a story bundle folder with the production headers")
    ap.add_argument("folder")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument(
        "--media-src", default="'self'", help="CSP media-src; the spec's fallback is the explicit PUBLIC_BASE_URL"
    )
    args = ap.parse_args()
    server = make_server(args.folder, args.port, args.media_src)
    print(f"serving {args.folder} on http://localhost:{args.port}  (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
