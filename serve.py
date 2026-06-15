#!/usr/bin/env python3
"""Serve the newest capture from a directory as a simple webpage."""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

CAPTURES_DIR = Path(os.getenv("CAPTURES_DIR", "/data/captures"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}


def newest_image(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None

    images = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    if not images:
        return None

    return max(images, key=lambda path: path.stat().st_mtime)


def last_successful_update_timestamp(directory: Path) -> float | None:
    image = newest_image(directory)
    if image is None:
        return None
    return image.stat().st_mtime


class CaptureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._serve_page()
        elif path == "/latest.jpg":
            self._serve_image()
        elif path == "/metrics":
            self._serve_metrics()
        else:
            self.send_error(404)

    def _serve_page(self) -> None:
        image = newest_image(CAPTURES_DIR)
        if image is None:
            body = (
                "<!DOCTYPE html>"
                '<html><head><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="30">'
                "<title>Blink Snap</title></head>"
                "<body><p>No captures yet.</p></body></html>"
            ).encode()
        else:
            body = (
                "<!DOCTYPE html>"
                '<html><head><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="30">'
                "<title>Blink Snap</title>"
                "<style>"
                "body{margin:0;background:#111;color:#eee;"
                "font-family:system-ui,sans-serif;display:flex;"
                "flex-direction:column;align-items:center;"
                "min-height:100vh;padding:1rem;box-sizing:border-box}"
                "img{max-width:100%;max-height:calc(100vh - 4rem);object-fit:contain}"
                ".caption{margin-top:0.5rem;font-size:0.875rem;color:#999}"
                "</style></head>"
                '<body><img src="/latest.jpg" alt="Latest capture">'
                f'<p class="caption">{image.name}</p></body></html>'
            ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_image(self) -> None:
        image = newest_image(CAPTURES_DIR)
        if image is None:
            self.send_error(404, "No captures found")
            return

        data = image.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _serve_metrics(self) -> None:
        timestamp = last_successful_update_timestamp(CAPTURES_DIR)
        value = f"{timestamp:.6f}" if timestamp is not None else "0"
        body = (
            "# HELP blink_snap_last_successful_update_timestamp_seconds "
            "Unix timestamp of the last successful capture update.\n"
            "# TYPE blink_snap_last_successful_update_timestamp_seconds gauge\n"
            f"blink_snap_last_successful_update_timestamp_seconds {value}\n"
        ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), CaptureHandler)
    print(f"Serving latest capture from {CAPTURES_DIR} on port {WEB_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
