"""Filter capture images and build timelapse MP4 exports."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
FILENAME_TS_FORMAT = "%Y-%m-%dT%H-%M-%SZ"
SECONDS_PER_DAY = 86400

ProgressCallback = Callable[[float, str], None]


@dataclass(frozen=True)
class ExportOptions:
    scope: str  # "whole" or "range"
    from_date: date | None
    to_date: date | None
    skip_hours: bool
    skip_from: int
    skip_to: int
    fps: float
    burn_in_timestamp: bool


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


def format_burnin_timestamp(path: Path, export_timezone: str) -> str:
    dt_utc = parse_capture_timestamp(path)
    if dt_utc is None:
        return ""
    return dt_utc.astimezone(ZoneInfo(export_timezone)).strftime("%d/%m/%Y %H:00")


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def stamp_image_with_timestamp(source: Path, dest: Path, label: str) -> None:
    escaped = _escape_drawtext(label)
    vf = (
        f"drawtext=text='{escaped}':fontsize=28:fontcolor=white:"
        "box=1:boxcolor=black@0.5:boxborderw=8:x=16:y=h-th-16"
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-vf",
            vf,
            str(dest),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or "ffmpeg stamp failed"
        raise RuntimeError(stderr)


def build_timelapse_mp4(
    images: list[Path],
    output_path: Path,
    fps: float,
    export_timezone: str = "UTC",
    burn_in_timestamp: bool = True,
    progress: ProgressCallback | None = None,
) -> None:
    if not images:
        raise ValueError("No images to export")
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed or not on PATH")

    total_images = len(images)
    total_steps = total_images + 1

    def report(step: int, message: str) -> None:
        if progress is not None:
            percent = min(100.0, (step / total_steps) * 100)
            progress(percent, message)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(tempfile.mkdtemp(prefix="blink-snap-export-"))
    try:
        for index, image in enumerate(images, start=1):
            dest = work_dir / f"{index:06d}.jpg"
            if burn_in_timestamp:
                report(
                    index - 1,
                    f"Adding burn-in on picture {index}/{total_images}…",
                )
                label = format_burnin_timestamp(image, export_timezone)
                stamp_image_with_timestamp(image, dest, label)
            else:
                report(
                    index - 1,
                    f"Preparing picture {index}/{total_images}…",
                )
                dest.symlink_to(image.resolve())

        report(total_images, "Encoding video…")
        temp_output = work_dir / "output.mp4"
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
                str(temp_output),
            ],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip() or "ffmpeg failed"
            raise RuntimeError(stderr)

        report(total_steps, "Export complete")
        shutil.move(str(temp_output), str(output_path))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
