"""Filter capture images and build timelapse MP4 exports."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
FILENAME_TS_FORMAT = "%Y-%m-%dT%H-%M-%SZ"
SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class ExportOptions:
    scope: str  # "whole" or "range"
    from_date: date | None
    to_date: date | None
    skip_hours: bool
    skip_from: int
    skip_to: int
    fps: float


def capture_interval_seconds() -> int | None:
    for name in ("INTERVAL", "BLINK_INTERVAL"):
        value = os.getenv(name)
        if value:
            return int(value)
    return None


def default_fps_from_interval(interval_seconds: int) -> float:
    return SECONDS_PER_DAY / interval_seconds


def parse_capture_timestamp(path: Path) -> datetime | None:
    ts_str = path.name.split("_", 1)[0]
    try:
        return datetime.strptime(ts_str, FILENAME_TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hour_excluded(hour: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end


def list_capture_images(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    images = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    ]
    return sorted(
        images,
        key=lambda path: parse_capture_timestamp(path) or datetime.min.replace(tzinfo=timezone.utc),
    )


def filter_images(
    directory: Path,
    options: ExportOptions,
    export_timezone: str,
) -> list[Path]:
    tz = ZoneInfo(export_timezone)
    selected: list[Path] = []

    for path in list_capture_images(directory):
        dt_utc = parse_capture_timestamp(path)
        if dt_utc is None:
            continue

        dt_local = dt_utc.astimezone(tz)

        if options.scope == "range":
            if options.from_date is None or options.to_date is None:
                continue
            if not (options.from_date <= dt_local.date() <= options.to_date):
                continue

        if options.skip_hours and hour_excluded(
            dt_local.hour, options.skip_from, options.skip_to
        ):
            continue

        selected.append(path)

    return selected


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def build_timelapse_mp4(images: list[Path], fps: float) -> Path:
    if not images:
        raise ValueError("No images to export")
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or not on PATH")

    work_dir = Path(tempfile.mkdtemp(prefix="blink-snap-export-"))
    try:
        for index, image in enumerate(images, start=1):
            link = work_dir / f"{index:06d}.jpg"
            link.symlink_to(image.resolve())

        output = work_dir / "output.mp4"
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-framerate",
                str(fps),
                "-i",
                "%06d.jpg",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "ffmpeg failed"
            raise RuntimeError(stderr)

        data = output.read_bytes()
        fd, final_name = tempfile.mkstemp(suffix=".mp4", prefix="blink-snap-timelapse-")
        os.close(fd)
        final = Path(final_name)
        final.write_bytes(data)
        return final
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
