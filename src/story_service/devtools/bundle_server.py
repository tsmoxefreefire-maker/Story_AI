"""Serve a bundle folder the way /v1/play serves it (00_shared, "Play URL and security headers").

Python's built-in `http.server` ignores HTTP Range requests, and browsers cannot seek inside an MP3 without them:
a story seeked to the middle of a line would play that line from its start. This server answers Range requests and
sends exactly the production headers, `sandbox allow-scripts` included, so what works here works in production.
The S11 render validator and the browser tests use it; `scripts/serve_bundle.py` is its command line."""

from __future__ import annotations

import functools
import os
import posixpath
import re
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; img-src data:; "
    "media-src {media}; connect-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors {ancestors}; "
    "sandbox allow-scripts"
)


class BundleHandler(SimpleHTTPRequestHandler):
    media_src = "'self'"
    frame_ancestors = "'self'"
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mp3": "audio/mpeg",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json",
    }

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "private, max-age=3600")
        path = posixpath.normpath(unquote(self.path.split("?", 1)[0].split("#", 1)[0]))
        if path == "/" or path.endswith(".html"):  # every page gets the CSP, however its path is spelled
            self.send_header(
                "Content-Security-Policy", CSP.format(media=self.media_src, ancestors=self.frame_ancestors)
            )
        super().end_headers()

    def list_directory(self, path):  # no directory listings
        self.send_error(404, "Not Found")
        return None

    def send_head(self):
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        path = self.translate_path(self.path)
        if not m or not os.path.isfile(path):
            return super().send_head()
        size = os.path.getsize(path)
        if m.group(1):
            start, end = int(m.group(1)), int(m.group(2)) if m.group(2) else size - 1
        else:  # "bytes=-500": the last 500 bytes
            start, end = max(0, size - int(m.group(2) or 0)), size - 1
        if start >= size or end < start:
            self.send_error(416, "Range Not Satisfiable")
            return None
        end = min(end, size - 1)
        f = open(path, "rb")  # noqa: SIM115 (closed by the base class after copyfile)
        f.seek(start)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._remaining = None

    def log_message(self, fmt, *args):
        pass


def make_server(folder: str, port: int = 0, media_src: str = "'self'") -> ThreadingHTTPServer:
    handler_cls = type("Handler", (BundleHandler,), {"media_src": media_src})
    handler = functools.partial(handler_cls, directory=os.path.abspath(folder))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


@contextmanager
def serving(folder: str, media_src: str = "'self'"):
    """`with serving(folder) as url:` serves the folder on a free port in a background thread."""
    server = make_server(folder, 0, media_src)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
