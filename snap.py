#!/usr/bin/env python3
"""Capture still images from Blink cameras via the unofficial blinkpy API."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import ClientSession
from blinkpy import api as blink_api
from blinkpy.auth import (
    Auth,
    BlinkTwoFARequiredError,
    LoginError,
    TokenRefreshFailed,
    UnauthorizedError,
)
from blinkpy.blinkpy import Blink
from blinkpy.helpers.constants import OAUTH_SIGNIN_URL, OAUTH_USER_AGENT
from blinkpy.helpers.util import json_load
from blinkpy.livestream import BlinkLiveStream
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "captures"
DEFAULT_SESSION = SCRIPT_DIR / "blink_session.json"
MIN_INTERVAL_SECONDS = 60
LOGGER = logging.getLogger("blink-snap")
AUTH_LOGGER = logging.getLogger("blink-snap.auth")

_JSON_EXTRA_KEYS = (
    "event",
    "capture",
    "camera",
    "mode",
    "path",
    "error_type",
    "refresh_path",
    "expires_in_s",
    "has_refresh_token",
    "has_hardware_id",
)

AUTH_ERRORS = (
    TokenRefreshFailed,
    LoginError,
    UnauthorizedError,
    BlinkTwoFARequiredError,
)


class BlinkConnectError(RuntimeError):
    """Raised when Blink login/setup fails without exiting the process."""


class CameraNotFoundError(ValueError):
    """Raised when the requested camera name cannot be resolved."""


WATCH_RECOVERABLE_ERRORS = AUTH_ERRORS + (BlinkConnectError, CameraNotFoundError)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line for machine-friendly Docker logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _JSON_EXTRA_KEYS:
            value = record.__dict__.get(key)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info).strip()
        return json.dumps(payload, default=str)


def resolve_log_level(verbose: bool) -> int:
    if verbose:
        return logging.DEBUG
    name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    return getattr(logging, name, logging.INFO)


def configure_logging(level: int) -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    for name in ("blinkpy", "blinkpy.auth", "blinkpy.api", "blinkpy.blinkpy"):
        logging.getLogger(name).setLevel(level)


def _patch_blinkpy_oauth_signin() -> None:
    """Treat Blink's HTTP 202 TSV response as 2FA (blinkpy 0.25.5 only checks 412)."""

    async def oauth_signin(auth, email, password, csrf_token):
        headers = {
            "User-Agent": OAUTH_USER_AGENT,
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://api.oauth.blink.com",
            "Referer": OAUTH_SIGNIN_URL,
        }
        data = {
            "username": email,
            "password": password,
            "csrf-token": csrf_token,
        }
        response = await auth.session.post(
            OAUTH_SIGNIN_URL,
            headers=headers,
            data=data,
            allow_redirects=False,
        )

        if response.status in (412, 202):
            return "2FA_REQUIRED"
        if response.status in (301, 302, 303, 307, 308):
            return "SUCCESS"

        body = await response.text()
        AUTH_LOGGER.error(
            "Blink sign-in failed (HTTP %s): %s",
            response.status,
            body[:200],
            extra={"event": "token_refresh_fail", "error_type": f"HTTP_{response.status}"},
        )
        return None

    blink_api.oauth_signin = oauth_signin


def _patch_blinkpy_refresh_tokens() -> None:
    """Use blinkpy OAuth v2 refresh mid-session instead of legacy Android login."""

    async def refresh_tokens(self, refresh=False):
        self.is_errored = True
        expires_in_s = (
            None
            if self.expiration_date is None
            else round(self.expiration_date - time.time(), 3)
        )
        context = {
            "expires_in_s": expires_in_s,
            "has_refresh_token": bool(self.refresh_token),
            "has_hardware_id": bool(self.hardware_id),
        }

        try:
            if self.refresh_token and self.hardware_id:
                AUTH_LOGGER.info(
                    "Attempting OAuth v2 token refresh",
                    extra={"event": "token_refresh", "refresh_path": "oauth_v2", **context},
                )
                token_data = await blink_api.oauth_refresh_token(
                    self, self.refresh_token, self.hardware_id
                )
                if token_data:
                    await self._process_token_data(token_data)
                    self.is_errored = False
                    AUTH_LOGGER.info(
                        "OAuth v2 token refresh successful",
                        extra={
                            "event": "token_refresh",
                            "refresh_path": "oauth_v2",
                            **context,
                            "expires_in_s": self.expires_in,
                        },
                    )
                    return True
                AUTH_LOGGER.warning(
                    "OAuth v2 token refresh returned no data; trying startup fallback",
                    extra={
                        "event": "token_refresh_fail",
                        "refresh_path": "oauth_v2",
                        "error_type": "EmptyTokenResponse",
                        **context,
                    },
                )
            else:
                AUTH_LOGGER.warning(
                    "Missing refresh_token or hardware_id; trying startup fallback",
                    extra={
                        "event": "token_refresh_fail",
                        "refresh_path": "oauth_v2",
                        "error_type": "MissingRefreshMaterial",
                        **context,
                    },
                )

            AUTH_LOGGER.info(
                "Falling back to Auth.startup() for token refresh",
                extra={
                    "event": "token_refresh",
                    "refresh_path": "startup_fallback",
                    **context,
                },
            )
            try:
                await self.startup()
            except BlinkTwoFARequiredError as error:
                AUTH_LOGGER.error(
                    "Token refresh requires interactive 2FA",
                    extra={
                        "event": "token_refresh_fail",
                        "refresh_path": "startup_fallback",
                        "error_type": "BlinkTwoFARequiredError",
                        **context,
                    },
                )
                raise TokenRefreshFailed from error

            self.is_errored = False
            AUTH_LOGGER.info(
                "Token refresh via Auth.startup() successful",
                extra={
                    "event": "token_refresh",
                    "refresh_path": "startup_fallback",
                    **context,
                    "expires_in_s": self.expires_in,
                },
            )
            return True
        except TokenRefreshFailed:
            raise
        except BlinkTwoFARequiredError as error:
            AUTH_LOGGER.error(
                "Token refresh requires interactive 2FA",
                extra={
                    "event": "token_refresh_fail",
                    "refresh_path": "startup_fallback",
                    "error_type": "BlinkTwoFARequiredError",
                    **context,
                },
            )
            raise TokenRefreshFailed from error
        except Exception as error:
            AUTH_LOGGER.error(
                "Token refresh failed: %s",
                error,
                extra={
                    "event": "token_refresh_fail",
                    "refresh_path": "startup_fallback",
                    "error_type": type(error).__name__,
                    **context,
                },
            )
            raise TokenRefreshFailed from error

    Auth.refresh_tokens = refresh_tokens


_patch_blinkpy_oauth_signin()
_patch_blinkpy_refresh_tokens()


def env_value(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def env_int(*names: str) -> int | None:
    value = env_value(*names)
    if value is None:
        return None
    return int(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture a still image from a Blink camera and save it locally.",
    )
    parser.add_argument(
        "--camera",
        default=env_value("BLINK_CAMERA", "CAMERA"),
        help=(
            "Camera name as shown in the Blink app (required unless --list). "
            "Env: BLINK_CAMERA or CAMERA."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available cameras and exit.",
    )
    parser.add_argument(
        "--mode",
        choices=("thumbnail", "live", "clip"),
        default="thumbnail",
        help=(
            "Capture method: thumbnail (fast, low-res), live (1080p via stream, "
            "needs ffmpeg), clip (1080p frame from recorded clip, slower)."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(env_value("BLINK_OUTPUT") or DEFAULT_OUTPUT),
        help=(
            f"Output directory (default: {DEFAULT_OUTPUT}). "
            "Env: BLINK_OUTPUT."
        ),
    )
    parser.add_argument(
        "--session",
        type=Path,
        default=Path(env_value("BLINK_SESSION") or DEFAULT_SESSION),
        help=(
            f"Saved auth session file (default: {DEFAULT_SESSION}). "
            "Env: BLINK_SESSION."
        ),
    )
    parser.add_argument(
        "--keep-video",
        action="store_true",
        help="In clip mode, also keep the full MP4 next to the extracted frame.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=env_int("BLINK_INTERVAL", "INTERVAL"),
        metavar="SECONDS",
        help=(
            f"Capture repeatedly every SECONDS (min {MIN_INTERVAL_SECONDS}). "
            "Runs until Ctrl+C. Use with --camera. "
            "Env: BLINK_INTERVAL or INTERVAL."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging (overrides LOG_LEVEL).",
    )
    args = parser.parse_args()

    if args.interval is not None:
        if args.interval < MIN_INTERVAL_SECONDS:
            parser.error(
                f"--interval must be at least {MIN_INTERVAL_SECONDS} seconds "
                "(Blink recommends no faster than one request per minute)."
            )
        if args.list:
            parser.error("--interval cannot be used with --list.")
        if args.mode != "thumbnail":
            parser.error("--interval watch mode only supports --mode thumbnail.")

    return args


def slugify(name: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug or "camera"


def jpeg_dimensions(path: Path) -> tuple[int, int] | None:
    """Read width/height from a JPEG without extra dependencies."""
    try:
        with path.open("rb") as handle:
            data = handle.read()
    except OSError:
        return None

    index = 0
    if data[:2] != b"\xff\xd8":
        return None

    no_length_markers = set(range(0xD0, 0xD8)) | {0x01, 0xD8, 0xD9}

    while index < len(data) - 9:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            height = struct.unpack(">H", data[index + 5 : index + 7])[0]
            width = struct.unpack(">H", data[index + 7 : index + 9])[0]
            return width, height
        if marker in no_length_markers or marker == 0x00:
            index += 2
            continue
        if index + 3 >= len(data):
            break
        segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if segment_length < 2:
            break
        index += 2 + segment_length
    return None


async def load_login_data(session_path: Path) -> dict:
    load_dotenv(SCRIPT_DIR / ".env", interpolate=False)

    username = os.getenv("BLINK_USERNAME")
    password = os.getenv("BLINK_PASSWORD")
    if not username or not password:
        print(
            "Missing BLINK_USERNAME or BLINK_PASSWORD.\n"
            f"Copy {SCRIPT_DIR / '.env.example'} to {SCRIPT_DIR / '.env'} and fill in credentials.",
            file=sys.stderr,
        )
        sys.exit(1)

    login_data = {"username": username, "password": password}
    if session_path.exists():
        try:
            saved = await json_load(str(session_path))
            if isinstance(saved, dict):
                login_data.update(saved)
        except Exception as exc:
            LOGGER.warning(
                "Could not load session file %s: %s",
                session_path,
                exc,
                extra={"event": "startup", "error_type": type(exc).__name__},
            )
    return login_data


def attach_session_saver(blink: Blink, session_path: Path) -> None:
    """Persist rotated tokens immediately after blinkpy refreshes mid-request."""

    async def _save() -> None:
        try:
            await blink.save(str(session_path))
            AUTH_LOGGER.info(
                "Session saved after token refresh",
                extra={"event": "token_refresh", "path": str(session_path)},
            )
        except Exception as exc:
            AUTH_LOGGER.error(
                "Failed to save session after token refresh: %s",
                exc,
                extra={
                    "event": "token_refresh_fail",
                    "error_type": type(exc).__name__,
                    "path": str(session_path),
                },
            )

    def _callback() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            AUTH_LOGGER.warning(
                "No running event loop; cannot schedule session save",
                extra={"event": "token_refresh_fail", "error_type": "RuntimeError"},
            )
            return
        loop.create_task(_save())

    blink.auth.callback = _callback


async def connect_blink(
    blink: Blink,
    session_path: Path,
    http_session: ClientSession,
    *,
    allow_2fa: bool = True,
) -> None:
    login_data = await load_login_data(session_path)
    blink.auth = Auth(login_data, no_prompt=True, session=http_session)

    try:
        await blink.start()
    except BlinkTwoFARequiredError:
        if not allow_2fa:
            raise BlinkConnectError(
                "Blink requires two-step verification; re-auth interactively on the host."
            ) from None
        print(
            "Blink requires two-step verification. "
            "Check your email or phone (SMS/WhatsApp) for a code.",
            file=sys.stderr,
        )
        code = input("Enter verification code: ").strip()
        if not code:
            raise BlinkConnectError("No verification code provided.") from None
        if not await blink.send_2fa_code(code):
            raise BlinkConnectError("Verification failed. Try again.") from None
    except (LoginError, TokenRefreshFailed) as exc:
        raise BlinkConnectError(
            "Blink login failed. Check BLINK_USERNAME and BLINK_PASSWORD."
        ) from exc

    if not blink.available:
        raise BlinkConnectError(
            "Failed to connect to Blink after login. Run with -v / LOG_LEVEL=DEBUG."
        )

    await blink.refresh(force_cache=True)
    await blink.save(str(session_path))
    attach_session_saver(blink, session_path)
    LOGGER.info(
        "Connected to Blink",
        extra={"event": "startup", "path": str(session_path)},
    )


def resolve_camera(blink: Blink, camera_name: str):
    if camera_name in blink.cameras:
        return blink.cameras[camera_name]

    lowered = camera_name.casefold()
    matches = [
        (name, cam)
        for name, cam in blink.cameras.items()
        if name.casefold() == lowered
    ]
    if len(matches) == 1:
        return matches[0][1]

    partial = [
        name for name in blink.cameras if lowered in name.casefold()
    ]
    if len(partial) == 1:
        return blink.cameras[partial[0]]

    available = ", ".join(sorted(blink.cameras)) or "(none)"
    raise CameraNotFoundError(
        f"Camera '{camera_name}' not found. Available cameras: {available}"
    )


def output_path(output_dir: Path, camera_name: str, mode: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    filename = f"{timestamp}_{slugify(camera_name)}_{mode}.jpg"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir / filename


def report_saved(path: Path, mode: str) -> None:
    size_kb = path.stat().st_size / 1024
    dims = jpeg_dimensions(path)
    dim_text = f"{dims[0]}x{dims[1]}" if dims else "unknown"
    print(f"Saved ({mode}): {path}")
    print(f"  size: {size_kb:.1f} KB, resolution: {dim_text}")


async def capture_thumbnail(blink: Blink, camera, dest: Path) -> None:
    await camera.snap_picture()
    await blink.refresh(force_cache=True)
    await camera.image_to_file(str(dest))


async def capture_live(camera, dest: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(
            "live mode requires ffmpeg on PATH. Install ffmpeg or use --mode thumbnail.",
            file=sys.stderr,
        )
        sys.exit(1)

    response = await blink_api.request_camera_liveview(
        camera.sync.blink,
        camera.network_id,
        camera.camera_id,
        camera_type=camera.camera_type,
    )
    if not isinstance(response, dict) or "server" not in response:
        message = (response or {}).get("message", "Blink did not return a stream URL")
        print(f"Live view unavailable: {message}", file=sys.stderr)
        print(
            "Use --mode thumbnail instead. On Blink Mini, thumbnails are often full "
            "1080p via the current API.",
            file=sys.stderr,
        )
        sys.exit(1)

    server_url = response["server"]
    if not server_url.startswith("immis://"):
        print(
            f"Unsupported live stream protocol: {server_url.split('://', 1)[0]}://",
            file=sys.stderr,
        )
        print("Use --mode thumbnail instead.", file=sys.stderr)
        sys.exit(1)

    stream = BlinkLiveStream(camera, response)
    server = await stream.start(host="127.0.0.1", port=0)
    feed_task = asyncio.create_task(stream.feed())

    try:
        await asyncio.sleep(2)
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            stream.url,
            "-frames:v",
            "1",
            str(dest),
        ]
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "ffmpeg failed"
            print(f"Live capture failed: {stderr}", file=sys.stderr)
            sys.exit(1)
    finally:
        stream.stop()
        server.close()
        feed_task.cancel()
        try:
            await feed_task
        except asyncio.CancelledError:
            pass


async def capture_clip(blink: Blink, camera, dest: Path, keep_video: bool) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print(
            "clip mode needs ffmpeg to extract a frame from the recorded MP4.",
            file=sys.stderr,
        )
        sys.exit(1)

    await camera.record()
    await blink.refresh(force_cache=True)

    with tempfile.TemporaryDirectory(prefix="blink-clip-") as tmp:
        video_path = Path(tmp) / "clip.mp4"
        await camera.video_to_file(str(video_path))

        if not video_path.exists() or video_path.stat().st_size == 0:
            print(
                "No clip was available after record(). "
                "Clip download may require a Blink subscription, or the camera was busy.",
                file=sys.stderr,
            )
            print(
                "Use --mode thumbnail instead. On Blink Mini, thumbnails are often full "
                "1080p via the current API.",
                file=sys.stderr,
            )
            sys.exit(1)

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            str(dest),
        ]
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "ffmpeg failed"
            print(f"Frame extraction failed: {stderr}", file=sys.stderr)
            sys.exit(1)

        if keep_video:
            video_dest = dest.with_suffix(".mp4")
            shutil.copy2(video_path, video_dest)
            print(f"Saved video: {video_dest}")


async def take_snapshot(
    blink: Blink, camera, args: argparse.Namespace
) -> Path:
    dest = output_path(args.output, args.camera, args.mode)

    if args.mode == "thumbnail":
        await capture_thumbnail(blink, camera, dest)
    elif args.mode == "live":
        await capture_live(camera, dest)
    elif args.mode == "clip":
        await capture_clip(blink, camera, dest, args.keep_video)

    return dest


async def reconnect_blink(
    blink: Blink,
    camera_name: str,
    session_path: Path,
    http_session: ClientSession,
    *,
    capture: int,
):
    LOGGER.info(
        "Attempting Blink reconnect",
        extra={
            "event": "reconnect",
            "capture": capture,
            "camera": camera_name,
        },
    )
    await connect_blink(blink, session_path, http_session, allow_2fa=False)
    camera = resolve_camera(blink, camera_name)
    LOGGER.info(
        "Blink reconnect succeeded",
        extra={
            "event": "reconnect",
            "capture": capture,
            "camera": camera_name,
        },
    )
    return camera


async def watch_loop(
    blink: Blink,
    camera,
    args: argparse.Namespace,
    session_path: Path,
    http_session: ClientSession,
) -> None:
    LOGGER.info(
        "Capturing '%s' every %ss (mode: %s)",
        args.camera,
        args.interval,
        args.mode,
        extra={
            "event": "startup",
            "camera": args.camera,
            "mode": args.mode,
        },
    )

    capture_count = 0
    try:
        while True:
            started = asyncio.get_running_loop().time()
            capture_count += 1
            try:
                dest = await take_snapshot(blink, camera, args)
                report_saved(dest, args.mode)
                await blink.save(str(session_path))
                LOGGER.info(
                    "Capture #%d succeeded",
                    capture_count,
                    extra={
                        "event": "capture_ok",
                        "capture": capture_count,
                        "camera": args.camera,
                        "mode": args.mode,
                        "path": str(dest),
                    },
                )
            except WATCH_RECOVERABLE_ERRORS as exc:
                LOGGER.error(
                    "Capture #%d failed: %s",
                    capture_count,
                    exc,
                    extra={
                        "event": "capture_fail",
                        "capture": capture_count,
                        "camera": args.camera,
                        "mode": args.mode,
                        "error_type": type(exc).__name__,
                    },
                    exc_info=LOGGER.isEnabledFor(logging.DEBUG),
                )
                try:
                    camera = await reconnect_blink(
                        blink,
                        args.camera,
                        session_path,
                        http_session,
                        capture=capture_count,
                    )
                except Exception as reconnect_exc:
                    LOGGER.error(
                        "Reconnect after capture #%d failed: %s",
                        capture_count,
                        reconnect_exc,
                        extra={
                            "event": "reconnect_fail",
                            "capture": capture_count,
                            "camera": args.camera,
                            "error_type": type(reconnect_exc).__name__,
                        },
                        exc_info=LOGGER.isEnabledFor(logging.DEBUG),
                    )
            except Exception as exc:
                LOGGER.error(
                    "Capture #%d failed: %s",
                    capture_count,
                    exc,
                    extra={
                        "event": "capture_fail",
                        "capture": capture_count,
                        "camera": args.camera,
                        "mode": args.mode,
                        "error_type": type(exc).__name__,
                    },
                    exc_info=LOGGER.isEnabledFor(logging.DEBUG),
                )

            elapsed = asyncio.get_running_loop().time() - started
            wait = max(0, args.interval - elapsed)
            if wait:
                await asyncio.sleep(wait)
    except asyncio.CancelledError:
        raise
    finally:
        LOGGER.info(
            "Stopped after %d capture(s)",
            capture_count,
            extra={"event": "startup", "capture": capture_count, "camera": args.camera},
        )


async def list_cameras(blink: Blink) -> None:
    if not blink.cameras:
        print("No cameras found on this account.")
        return

    print("Available cameras:")
    for name, camera in sorted(blink.cameras.items(), key=lambda item: item[0].lower()):
        camera_type = getattr(camera, "camera_type", "unknown") or "unknown"
        print(f"  - {name} ({camera_type})")


async def run(args: argparse.Namespace) -> None:
    async with ClientSession() as session:
        blink = Blink(session=session)
        try:
            await connect_blink(blink, args.session, session, allow_2fa=True)
        except BlinkConnectError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

        if args.list:
            await list_cameras(blink)
            return

        if not args.camera:
            print("error: --camera is required (or use --list).", file=sys.stderr)
            sys.exit(2)

        try:
            camera = resolve_camera(blink, args.camera)
        except CameraNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)

        if args.interval:
            await watch_loop(blink, camera, args, args.session, session)
            return

        dest = await take_snapshot(blink, camera, args)
        report_saved(dest, args.mode)
        await blink.save(str(args.session))


def main() -> None:
    args = parse_args()
    configure_logging(resolve_log_level(args.verbose))
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
