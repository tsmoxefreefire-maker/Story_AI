"""Run the module 6 API locally (FastAPI + uvicorn).

    (PowerShell)  $env:API_KEYS = "dev-key-123"
                  python scripts/run_api.py                 # http://localhost:8000/docs
                  python scripts/run_api.py --port 8080

API_KEYS is required and comes from the environment only. Without PLAY_TOKEN_SECRET a random one is made for this run
(play links then stop working when the server restarts); never write either into a file in the repo.
PUBLIC_BASE_URL and ALLOWED_EMBED_ORIGINS default to this machine here (http://localhost:<port> and 'self')."""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to a code page without Arabic
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Run the module 6 API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    if not os.environ.get("API_KEYS"):
        print('API_KEYS is not set. In PowerShell:  $env:API_KEYS = "dev-key-123"   then run this again.')
        return 1
    if not os.environ.get("PLAY_TOKEN_SECRET"):
        os.environ["PLAY_TOKEN_SECRET"] = secrets.token_urlsafe(48)  # this run only, never printed or saved
        print("PLAY_TOKEN_SECRET is not set: using a random one for this run.")
    os.environ.setdefault("PUBLIC_BASE_URL", f"http://localhost:{args.port}")  # local defaults; on a server set both
    os.environ.setdefault("ALLOWED_EMBED_ORIGINS", "'self'")

    import uvicorn

    from story_service.api.app import create_app
    from story_service.api.settings import SettingsError

    try:
        app = create_app()
    except SettingsError as e:
        print(f"setting problem: {e}")
        return 1
    print(f"module 6 API on http://localhost:{args.port}  (docs: http://localhost:{args.port}/docs, Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
