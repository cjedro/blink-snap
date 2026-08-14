from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import export


def _touch(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_bytes(b"")
    return path


def _opts(**overrides) -> export.ExportOptions:
    values = dict(
        scope="whole",
        from_date=None,
        to_date=None,
        skip_hours=False,
        skip_from=0,
        skip_to=0,
        fps=1.0,
        burn_in_timestamp=False,
    )
    values.update(overrides)
    return export.ExportOptions(**values)


def test_parse_capture_timestamp_valid() -> None:
    path = Path("2026-08-14T15-30-00Z_living-room_thumbnail.jpg")
    parsed = export.parse_capture_timestamp(path)
    assert parsed == datetime(2026, 8, 14, 15, 30, 0, tzinfo=timezone.utc)


def test_parse_capture_timestamp_junk_returns_none() -> None:
    assert export.parse_capture_timestamp(Path("not-a-capture.jpg")) is None


def test_hour_excluded_same_day_range() -> None:
    assert export.hour_excluded(22, 22, 23) is True
    assert export.hour_excluded(23, 22, 23) is False
    assert export.hour_excluded(21, 22, 23) is False


def test_hour_excluded_overnight_wrap() -> None:
    assert export.hour_excluded(22, 22, 6) is True
    assert export.hour_excluded(3, 22, 6) is True
    assert export.hour_excluded(6, 22, 6) is False
    assert export.hour_excluded(12, 22, 6) is False


def test_default_fps_from_interval() -> None:
    assert export.default_fps_from_interval(300) == 288.0


def test_format_burnin_timestamp_converts_to_local() -> None:
    path = Path("2026-08-14T15-30-00Z_living-room_thumbnail.jpg")
    assert export.format_burnin_timestamp(path, "UTC") == "14/08/2026 15:00"
    assert export.format_burnin_timestamp(path, "America/New_York") == "14/08/2026 11:00"


def test_list_capture_images_missing_directory(tmp_path: Path) -> None:
    assert export.list_capture_images(tmp_path / "missing") == []


def test_list_capture_images_ignores_non_jpeg(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    jpg = _touch(captures, "2026-08-14T10-00-00Z_cam_thumbnail.jpg")
    _touch(captures, "notes.txt")
    _touch(captures, "2026-08-14T11-00-00Z_cam_thumbnail.mp4")
    assert export.list_capture_images(captures) == [jpg]


def test_filter_images_whole_library(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    first = _touch(captures, "2026-08-13T10-00-00Z_cam_thumbnail.jpg")
    second = _touch(captures, "2026-08-14T10-00-00Z_cam_thumbnail.jpg")
    selected = export.filter_images(captures, _opts(scope="whole"), "UTC")
    assert selected == [first, second]


def test_filter_images_date_range(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    _touch(captures, "2026-08-13T10-00-00Z_cam_thumbnail.jpg")
    kept = _touch(captures, "2026-08-14T10-00-00Z_cam_thumbnail.jpg")
    _touch(captures, "2026-08-15T10-00-00Z_cam_thumbnail.jpg")
    selected = export.filter_images(
        captures,
        _opts(
            scope="range",
            from_date=date(2026, 8, 14),
            to_date=date(2026, 8, 14),
        ),
        "UTC",
    )
    assert selected == [kept]


def test_filter_images_skip_hours_in_named_timezone(tmp_path: Path) -> None:
    captures = tmp_path / "captures"
    captures.mkdir()
    # 22:00 UTC is 18:00 in America/New_York (EDT, UTC-4 in August).
    evening = _touch(captures, "2026-08-14T22-00-00Z_cam_thumbnail.jpg")
    later = _touch(captures, "2026-08-14T23-30-00Z_cam_thumbnail.jpg")
    selected = export.filter_images(
        captures,
        _opts(skip_hours=True, skip_from=18, skip_to=19),
        "America/New_York",
    )
    assert evening not in selected
    assert selected == [later]
