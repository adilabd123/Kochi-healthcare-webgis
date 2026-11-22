#!/usr/bin/env python3
"""
Q12 - Local static server with HTTP Range support (like GitHub Pages)
=====================================================================

PMTiles needs HTTP Range requests ("give me bytes 1200-5400 of the file").
Python's built-in `python -m http.server` ignores Range headers, so this small
server adds them. It serves the assembled _site folder.

    python pmtiles\\build_site.py
    python pmtiles\\serve_static.py                # http://localhost:8090
    python pmtiles\\serve_static.py --port 8091 --dir _site
"""
from __future__ import annotations

import argparse
import os
import re
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                      ".pmtiles": "application/octet-stream", ".js": "text/javascript",
                      ".json": "application/json"}

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, ETag")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path):
            return super().send_head()
        m = RANGE_RE.match(rng.strip())
        size = os.path.getsize(path)
        if not m or (m.group(1) == "" and m.group(2) == ""):
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return None
        if m.group(1) == "":                       # suffix range: last N bytes
            start, end = max(0, size - int(m.group(2))), size - 1
        else:
            start = int(m.group(1))
            end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
        if start >= size or start > end:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        f = open(path, "rb")
        f.seek(start)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self._range_remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_range_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)
        self._range_remaining = None


def main() -> None:
    p = argparse.ArgumentParser(description="Range-capable static server for the PMTiles site")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--dir", type=Path, default=ROOT / "_site")
    a = p.parse_args()
    if not (a.dir / "index.html").exists():
        raise SystemExit(f"{a.dir} has no index.html - run 'python pmtiles/build_site.py' first.")
    handler = partial(RangeHandler, directory=str(a.dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    print(f"Serving {a.dir} at http://localhost:{a.port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
