#!/usr/bin/env python3
"""Serve the newest capture from a directory as a simple webpage."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from export import (
    ExportOptions,
    build_timelapse_mp4,
    capture_interval_seconds,
    default_fps_from_interval,
    filter_images,
    ffmpeg_available,
)
from export_jobs import (
    create_job,
    ensure_exports_dir,
    list_jobs,
    load_job,
    update_job,
)

CAPTURES_DIR = Path(os.getenv("CAPTURES_DIR", "/data/captures"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
EXPORT_PASSWORD = os.getenv("EXPORT_PASSWORD", "")
EXPORT_TIMEZONE = os.getenv("EXPORT_TIMEZONE", "UTC")
EXPORT_COOKIE_NAME = "export_auth"
EXPORT_COOKIE_MAX_AGE = int(os.getenv("EXPORT_COOKIE_MAX_AGE", str(7 * 24 * 3600)))
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}

EXPORT_CSS = (
    "body{margin:0;background:#111;color:#eee;font-family:system-ui,sans-serif;"
    "display:flex;flex-direction:column;align-items:center;"
    "min-height:100vh;padding:1.5rem;box-sizing:border-box}"
    "main{width:100%;max-width:28rem}"
    "h1{font-size:1.25rem;font-weight:600;margin:0 0 1rem}"
    "label{display:block;margin-bottom:0.75rem;font-size:0.875rem}"
    "input[type=password],input[type=date],input[type=number],select{"
    "width:100%;padding:0.5rem;margin-top:0.25rem;box-sizing:border-box;"
    "background:#222;border:1px solid #444;color:#eee;border-radius:4px}"
    "fieldset{border:1px solid #333;border-radius:6px;padding:0.75rem;margin:0 0 1rem}"
    "legend{padding:0 0.25rem;color:#ccc}"
    ".row{display:flex;gap:0.5rem;align-items:center}"
    ".row select{flex:1}"
    ".hint{font-size:0.8125rem;color:#999;margin:0 0 1rem}"
    ".error{background:#3a1f1f;border:1px solid #733;color:#fbb;"
    "padding:0.75rem;border-radius:4px;margin-bottom:1rem;font-size:0.875rem}"
    "button,.btn-link{display:inline-block;padding:0.6rem 1rem;background:#2a5;"
    "color:#fff;border:none;border-radius:4px;font-size:0.875rem;"
    "text-decoration:none;cursor:pointer}"
    "button:hover,.btn-link:hover{background:#3b6}"
    ".actions{display:flex;gap:0.75rem;align-items:center;margin-top:1rem}"
    "a{color:#8cf}"
    ".disabled{color:#666}"
    ".success{background:#1f3a1f;border:1px solid #373;color:#bfb;"
    "padding:0.75rem;border-radius:4px;margin-bottom:1rem;font-size:0.875rem}"
    ".exports{width:100%;border-collapse:collapse;font-size:0.8125rem;margin-top:0.5rem}"
    ".exports th,.exports td{border-bottom:1px solid #333;padding:0.5rem;text-align:left;"
    "vertical-align:top}"
    ".exports th{color:#999;font-weight:500}"
    ".status-running{color:#9cf}"
    ".status-error{color:#fbb}"
    "main.wide{max-width:40rem}"
)

EXPORT_JS = """
function toggleScope() {
  const range = document.querySelector('input[name=scope][value=range]').checked;
  document.getElementById('from_date').disabled = !range;
  document.getElementById('to_date').disabled = !range;
}
function toggleSkipHours() {
  const on = document.getElementById('skip_hours').checked;
  document.getElementById('skip_from').disabled = !on;
  document.getElementById('skip_to').disabled = !on;
}
function toggleCustomFps() {
  const on = document.getElementById('custom_fps').checked;
  document.getElementById('fps').disabled = !on;
}
function disableExportSubmit(form) {
  const btn = form.querySelector('button[type="submit"]');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Queuing…';
  }
  return true;
}
document.addEventListener('DOMContentLoaded', function() {
  toggleScope();
  toggleSkipHours();
  toggleCustomFps();
  pollExports();
});
function pollExports() {
  var table = document.getElementById('exports-table');
  if (!table) return;
  var active = table.querySelectorAll('[data-status="queued"],[data-status="running"]');
  if (active.length === 0) return;
  fetch('/export/status', { credentials: 'same-origin' })
    .then(function(response) {
      if (response.status === 401) return null;
      return response.json();
    })
    .then(function(jobs) {
      if (!jobs) return;
      jobs.forEach(function(job) {
        var row = document.getElementById('export-' + job.export_id);
        if (!row) return;
        row.dataset.status = job.status;
        var statusCell = row.querySelector('.export-status');
        if (!statusCell) return;
        if (job.status === 'done') {
          statusCell.innerHTML = '<a href="/export/download/' + encodeURIComponent(job.export_id) + '">Download</a>';
        } else if (job.status === 'error') {
          statusCell.textContent = job.error || 'Failed';
          statusCell.className = 'export-status status-error';
        } else {
          statusCell.className = 'export-status status-running';
          statusCell.textContent = Math.round(job.percent) + '% — ' + job.message;
        }
      });
      var stillActive = table.querySelectorAll('[data-status="queued"],[data-status="running"]');
      if (stillActive.length > 0) {
        setTimeout(pollExports, 1500);
      }
    })
    .catch(function() { setTimeout(pollExports, 3000); });
}
"""


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


def _field(params: dict[str, list[str]], name: str) -> str:
    values = params.get(name, [""])
    return values[0] if values else ""


def _checkbox(params: dict[str, list[str]], name: str) -> bool:
    return name in params


def _hour_options(selected: int) -> str:
    return "".join(
        f'<option value="{h}"{" selected" if h == selected else ""}>{h:02d}:00</option>'
        for h in range(24)
    )


def _default_fps_hint() -> str:
    interval = capture_interval_seconds()
    if interval is None:
        return "No capture interval configured — enter a custom frame rate."
    fps = default_fps_from_interval(interval)
    fps_text = f"{fps:g}" if fps == int(fps) else f"{fps:.2f}"
    return (
        f"Default: {fps_text} fps — 1 second of video ≈ 1 day "
        f"({interval}s capture interval)"
    )


def _export_label(options: ExportOptions, image_count: int) -> str:
    if options.scope == "whole":
        desc = "Whole timelapse"
    else:
        desc = f"{options.from_date} — {options.to_date}"
    return f"{desc} ({image_count} pictures)"


def _format_created_at(created_at: str) -> str:
    try:
        return datetime.fromisoformat(created_at).strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return created_at


def _export_status_cell(job) -> str:
    if job.status == "done":
        return (
            f'<a href="/export/download/{html.escape(job.export_id)}">Download</a>'
        )
    if job.status == "error":
        return f'<span class="status-error">{html.escape(job.error or "Failed")}</span>'
    return (
        f'<span class="status-running">{round(job.percent)}% — '
        f"{html.escape(job.message)}</span>"
    )


def exports_list_html() -> str:
    jobs = list_jobs()
    if not jobs:
        return '<p class="hint">No exports yet.</p>'

    rows = []
    for job in jobs:
        rows.append(
            f'<tr id="export-{html.escape(job.export_id)}" '
            f'data-status="{html.escape(job.status)}">'
            f"<td>{html.escape(_format_created_at(job.created_at))}</td>"
            f"<td>{html.escape(job.label)}</td>"
            f'<td class="export-status">{_export_status_cell(job)}</td>'
            "</tr>"
        )

    return (
        '<table class="exports" id="exports-table">'
        "<thead><tr><th>Created</th><th>Export</th><th>Status</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _run_export_job(
    export_id: str,
    images: list[Path],
    options: ExportOptions,
    export_timezone: str,
) -> None:
    job = load_job(export_id)
    if job is None:
        return

    def on_progress(percent: float, message: str) -> None:
        update_job(
            export_id,
            status="running",
            percent=percent,
            message=message,
        )

    update_job(export_id, status="running", message="Starting export…", percent=0)
    try:
        build_timelapse_mp4(
            images,
            job.mp4_path,
            options.fps,
            export_timezone,
            options.burn_in_timestamp,
            progress=on_progress,
        )
        update_job(
            export_id,
            status="done",
            percent=100,
            message="Export complete",
        )
    except Exception as exc:
        if job.mp4_path.exists():
            job.mp4_path.unlink(missing_ok=True)
        update_job(
            export_id,
            status="error",
            error=str(exc),
            message="Export failed",
        )


def _export_auth_token() -> str:
    return hmac.new(
        EXPORT_PASSWORD.encode("utf-8"),
        b"blink-snap-export-v1",
        hashlib.sha256,
    ).hexdigest()


def export_login_html(error: str | None = None) -> str:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return (
        "<!DOCTYPE html>"
        '<html><head><meta charset="utf-8">'
        "<title>Export Timelapse</title>"
        f"<style>{EXPORT_CSS}</style>"
        "</head><body><main>"
        "<h1>Export Timelapse</h1>"
        f"{error_html}"
        '<p class="hint">Enter the export password to queue and download timelapses.</p>'
        '<form method="post" action="/export/login">'
        "<label>Password"
        '<input type="password" name="password" required autocomplete="current-password">'
        "</label>"
        '<div class="actions">'
        '<button type="submit">Continue</button>'
        '<a href="/">Back</a>'
        "</div>"
        "</form></main></body></html>"
    )


def export_form_html(
    error: str | None = None,
    values: dict[str, str] | None = None,
    queued: bool = False,
) -> str:
    values = values or {}
    interval = capture_interval_seconds()
    default_fps = default_fps_from_interval(interval) if interval else 24.0
    scope = values.get("scope", "whole")
    skip_hours = values.get("skip_hours") == "1"
    custom_fps = values.get("custom_fps") == "1"
    if not values:
        burn_in_timestamp = True
    else:
        burn_in_timestamp = values.get("burn_in_timestamp") == "1"
    skip_from = int(values.get("skip_from", "22"))
    skip_to = int(values.get("skip_to", "6"))
    fps_value = values.get("fps", f"{default_fps:g}")

    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    queued_html = (
        '<div class="success">Export queued. It will appear in the list below when ready.</div>'
        if queued
        else ""
    )
    hint = html.escape(_default_fps_hint())
    exports_html = exports_list_html()

    return (
        "<!DOCTYPE html>"
        '<html><head><meta charset="utf-8">'
        "<title>Export Timelapse</title>"
        f"<style>{EXPORT_CSS}</style>"
        f"<script>{EXPORT_JS}</script>"
        '</head><body><main class="wide">'
        "<h1>Export Timelapse</h1>"
        f"{queued_html}"
        f"{error_html}"
        f'<p class="hint">{hint}</p>'
        '<form method="post" action="/export" onsubmit="return disableExportSubmit(this)">'
        "<fieldset>"
        "<legend>Date range</legend>"
        f'<label><input type="radio" name="scope" value="whole"{" checked" if scope == "whole" else ""} '
        'onchange="toggleScope()"> Export whole timelapse</label>'
        f'<label><input type="radio" name="scope" value="range"{" checked" if scope == "range" else ""} '
        'onchange="toggleScope()"> Date range</label>'
        '<label>From'
        f' <input type="date" name="from_date" id="from_date" value="{html.escape(values.get("from_date", ""))}">'
        "</label>"
        '<label>To'
        f' <input type="date" name="to_date" id="to_date" value="{html.escape(values.get("to_date", ""))}">'
        "</label>"
        "</fieldset>"
        "<fieldset>"
        "<legend>Skip hours (optional)</legend>"
        f'<label><input type="checkbox" name="skip_hours" id="skip_hours" value="1"'
        f'{" checked" if skip_hours else ""} onchange="toggleSkipHours()"> '
        "Exclude pictures during these hours</label>"
        '<div class="row">'
        f'<select name="skip_from" id="skip_from">{_hour_options(skip_from)}</select>'
        "<span>to</span>"
        f'<select name="skip_to" id="skip_to">{_hour_options(skip_to)}</select>'
        "</div>"
        "</fieldset>"
        "<fieldset>"
        "<legend>Overlay</legend>"
        f'<label><input type="checkbox" name="burn_in_timestamp" id="burn_in_timestamp" value="1"'
        f'{" checked" if burn_in_timestamp else ""}> '
        "Burn in timestamp (dd/mm/YYYY HH:00, local time)</label>"
        "</fieldset>"
        "<fieldset>"
        "<legend>Frame rate</legend>"
        f'<label><input type="checkbox" name="custom_fps" id="custom_fps" value="1"'
        f'{" checked" if custom_fps else ""} onchange="toggleCustomFps()"> '
        "Use custom frame rate</label>"
        '<label>FPS'
        f' <input type="number" name="fps" id="fps" min="1" max="240" step="0.01"'
        f' value="{html.escape(fps_value)}">'
        "</label>"
        "</fieldset>"
        '<div class="actions">'
        '<button type="submit">Queue Export</button>'
        '<a href="/export/logout">Log out</a>'
        '<a href="/">Back</a>'
        "</div>"
        "</form>"
        "<h2 style=\"font-size:1rem;margin:1.5rem 0 0.5rem\">Available exports</h2>"
        f"{exports_html}"
        "</main></body></html>"
    )


def export_disabled_html() -> str:
    return (
        "<!DOCTYPE html>"
        '<html><head><meta charset="utf-8">'
        "<title>Export Timelapse</title>"
        f"<style>{EXPORT_CSS}</style>"
        "</head><body><main>"
        "<h1>Export Timelapse</h1>"
        '<p class="hint">Export is disabled. Set EXPORT_PASSWORD to enable.</p>'
        '<div class="actions"><a href="/">Back</a></div>'
        "</main></body></html>"
    )


class CaptureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self._serve_page()
        elif path == "/latest.jpg":
            self._serve_image()
        elif path == "/metrics":
            self._serve_metrics()
        elif path == "/export":
            query = parse_qs(urlparse(self.path).query)
            queued = _field(query, "queued") == "1"
            self._serve_export_form(queued=queued)
        elif path == "/export/logout":
            self._handle_export_logout()
        elif path == "/export/status":
            self._serve_exports_status()
        elif path.startswith("/export/download/"):
            self._serve_export_download(path.removeprefix("/export/download/"))
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/export/login":
            self._handle_export_login()
        elif path == "/export":
            self._handle_export()
        else:
            self.send_error(404)

    def _parse_cookies(self) -> dict[str, str]:
        cookies: dict[str, str] = {}
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if "=" in part:
                name, value = part.split("=", 1)
                cookies[name.strip()] = value.strip()
        return cookies

    def _export_authenticated(self) -> bool:
        if not EXPORT_PASSWORD:
            return False
        token = self._parse_cookies().get(EXPORT_COOKIE_NAME, "")
        expected = _export_auth_token()
        return bool(token) and secrets.compare_digest(token, expected)

    def _export_cookie_header(self, token: str, max_age: int) -> str:
        parts = [
            f"{EXPORT_COOKIE_NAME}={token}",
            "Path=/export",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if os.getenv("EXPORT_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
            parts.append("Secure")
        return "; ".join(parts)

    def _set_export_auth_cookie(self) -> None:
        self.send_header("Set-Cookie", self._export_cookie_header(_export_auth_token(), EXPORT_COOKIE_MAX_AGE))

    def _clear_export_auth_cookie(self) -> None:
        self.send_header("Set-Cookie", self._export_cookie_header("", 0))

    def _require_export_auth(self) -> bool:
        if self._export_authenticated():
            return True
        return False

    def _handle_export_login(self) -> None:
        if not EXPORT_PASSWORD:
            self._serve_export_form()
            return

        params = self._read_form()
        password = _field(params, "password")
        if not secrets.compare_digest(password, EXPORT_PASSWORD):
            self._serve_export_login(error="Incorrect password.")
            return

        self.send_response(303)
        self.send_header("Location", "/export")
        self._set_export_auth_cookie()
        self.end_headers()

    def _handle_export_logout(self) -> None:
        self.send_response(303)
        self.send_header("Location", "/export")
        self._clear_export_auth_cookie()
        self.end_headers()

    def _serve_export_login(self, error: str | None = None) -> None:
        body = export_login_html(error=error).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_page(self) -> None:
        image = newest_image(CAPTURES_DIR)
        export_link = (
            '<div class="actions" style="margin-top:1rem">'
            '<a class="btn-link" href="/export">Export Timelapse</a>'
            "</div>"
        )
        if image is None:
            body = (
                "<!DOCTYPE html>"
                '<html><head><meta charset="utf-8">'
                '<meta http-equiv="refresh" content="30">'
                "<title>Blink Snap</title>"
                f"<style>{EXPORT_CSS}</style>"
                "</head>"
                "<body><main><p>No captures yet.</p>"
                f"{export_link}</main></body></html>"
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
                "img{max-width:100%;max-height:calc(100vh - 6rem);object-fit:contain}"
                ".caption{margin-top:0.5rem;font-size:0.875rem;color:#999}"
                f"{EXPORT_CSS}"
                "</style></head>"
                '<body><main style="display:flex;flex-direction:column;align-items:center">'
                '<img src="/latest.jpg" alt="Latest capture">'
                f'<p class="caption">{html.escape(image.name)}</p>'
                f"{export_link}"
                "</main></body></html>"
            ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_export_form(
        self,
        error: str | None = None,
        values: dict[str, str] | None = None,
        queued: bool = False,
    ) -> None:
        if not EXPORT_PASSWORD:
            body = export_disabled_html().encode()
        elif not self._export_authenticated():
            self._serve_export_login(error=error)
            return
        else:
            body = export_form_html(error=error, values=values, queued=queued).encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_form(self) -> dict[str, list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return parse_qs(raw, keep_blank_values=True)

    def _form_values(self, params: dict[str, list[str]]) -> dict[str, str]:
        values: dict[str, str] = {
            "scope": _field(params, "scope") or "whole",
            "from_date": _field(params, "from_date"),
            "to_date": _field(params, "to_date"),
            "skip_from": _field(params, "skip_from") or "22",
            "skip_to": _field(params, "skip_to") or "6",
            "fps": _field(params, "fps"),
        }
        if _checkbox(params, "skip_hours"):
            values["skip_hours"] = "1"
        if _checkbox(params, "custom_fps"):
            values["custom_fps"] = "1"
        values["burn_in_timestamp"] = "1" if _checkbox(params, "burn_in_timestamp") else "0"
        return values

    def _parse_export_options(
        self, params: dict[str, list[str]]
    ) -> tuple[ExportOptions | None, str | None]:
        values = self._form_values(params)
        scope = values["scope"]
        if scope not in ("whole", "range"):
            return None, "Invalid date range option."

        from_date: date | None = None
        to_date: date | None = None
        if scope == "range":
            from_raw = values["from_date"]
            to_raw = values["to_date"]
            if not from_raw or not to_raw:
                return None, "From and to dates are required for a date range export."
            try:
                from_date = date.fromisoformat(from_raw)
                to_date = date.fromisoformat(to_raw)
            except ValueError:
                return None, "Invalid date format."
            if from_date > to_date:
                return None, "From date must be on or before to date."

        skip_hours = _checkbox(params, "skip_hours")
        try:
            skip_from = int(values["skip_from"])
            skip_to = int(values["skip_to"])
        except ValueError:
            return None, "Invalid skip hours."
        if not (0 <= skip_from <= 23 and 0 <= skip_to <= 23):
            return None, "Skip hours must be between 0 and 23."

        custom_fps = _checkbox(params, "custom_fps")
        interval = capture_interval_seconds()
        if custom_fps:
            fps_raw = values["fps"]
            if not fps_raw:
                return None, "Custom frame rate is required when enabled."
            try:
                fps = float(fps_raw)
            except ValueError:
                return None, "Invalid frame rate."
            if not (1 <= fps <= 240):
                return None, "Frame rate must be between 1 and 240."
        elif interval is None:
            return None, "No capture interval configured — enable custom frame rate."
        else:
            fps = default_fps_from_interval(interval)

        burn_in_timestamp = _checkbox(params, "burn_in_timestamp")

        return (
            ExportOptions(
                scope=scope,
                from_date=from_date,
                to_date=to_date,
                skip_hours=skip_hours,
                skip_from=skip_from,
                skip_to=skip_to,
                fps=fps,
                burn_in_timestamp=burn_in_timestamp,
            ),
            None,
        )

    def _handle_export(self) -> None:
        if not EXPORT_PASSWORD:
            self._serve_export_form()
            return

        if not self._export_authenticated():
            self.send_response(303)
            self.send_header("Location", "/export")
            self.end_headers()
            return

        params = self._read_form()
        values = self._form_values(params)

        options, error = self._parse_export_options(params)
        if error or options is None:
            self._serve_export_form(error=error or "Invalid export options.", values=values)
            return

        if not ffmpeg_available():
            self._serve_export_form(
                error="ffmpeg is not installed or not on PATH.",
                values=values,
            )
            return

        images = filter_images(CAPTURES_DIR, options, EXPORT_TIMEZONE)
        if not images:
            self._serve_export_form(
                error="No images match the selected filters.",
                values=values,
            )
            return

        ensure_exports_dir()
        label = _export_label(options, len(images))
        job = create_job(label, len(images))
        thread = threading.Thread(
            target=_run_export_job,
            args=(job.export_id, images, options, EXPORT_TIMEZONE),
            daemon=True,
        )
        thread.start()

        self.send_response(303)
        self.send_header("Location", "/export?queued=1")
        self._set_export_auth_cookie()
        self.end_headers()

    def _serve_exports_status(self) -> None:
        if not self._require_export_auth():
            self.send_error(401, "Unauthorized")
            return
        jobs = list_jobs()
        payload = [
            {
                "export_id": job.export_id,
                "status": job.status,
                "percent": round(job.percent, 1),
                "message": job.message,
                "error": job.error,
            }
            for job in jobs
        ]
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_export_download(self, export_id: str) -> None:
        if not self._require_export_auth():
            self.send_error(401, "Unauthorized")
            return

        job = load_job(export_id)
        if job is None or job.status != "done" or not job.mp4_path.is_file():
            self.send_error(404, "Export not found")
            return

        data = job.mp4_path.read_bytes()
        filename = job.filename or f"{export_id}.mp4"
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Disposition",
            f'attachment; filename="{filename}"',
        )
        self.end_headers()
        self.wfile.write(data)

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
    ensure_exports_dir()
    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), CaptureHandler)
    print(f"Serving latest capture from {CAPTURES_DIR} on port {WEB_PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
