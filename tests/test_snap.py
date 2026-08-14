from __future__ import annotations

import asyncio
import json
import logging
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from blinkpy.auth import TokenRefreshFailed

import snap
from tests.fakes import FakeBlink, FakeCamera, minimal_jpeg


def test_slugify_spaces_and_punctuation() -> None:
    assert snap.slugify("Living Room") == "living-room"
    assert snap.slugify("Foo's Cam!") == "foos-cam"


def test_slugify_empty_after_strip() -> None:
    assert snap.slugify("---") == "camera"
    assert snap.slugify("!!!") == "camera"


def test_jpeg_dimensions_reads_sof0(tmp_path: Path) -> None:
    path = tmp_path / "frame.jpg"
    path.write_bytes(minimal_jpeg(32, 16))
    assert snap.jpeg_dimensions(path) == (32, 16)


def test_jpeg_dimensions_non_jpeg_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "nope.txt"
    path.write_text("hello", encoding="utf-8")
    assert snap.jpeg_dimensions(path) is None


def test_jpeg_dimensions_missing_file_returns_none(tmp_path: Path) -> None:
    assert snap.jpeg_dimensions(tmp_path / "missing.jpg") is None


def test_output_path_creates_dir_and_names_file(tmp_path: Path) -> None:
    dest_dir = tmp_path / "out"
    path = snap.output_path(dest_dir, "Living Room", "thumbnail")
    assert dest_dir.is_dir()
    assert path.parent == dest_dir
    assert path.suffix == ".jpg"
    assert path.name.endswith("_living-room_thumbnail.jpg")
    # Prefix is UTC `{YYYY-MM-DDTHH-MM-SSZ}_` — do not assert the clock value.
    assert path.name[10] == "T"
    assert path.name[19] == "Z"


def test_resolve_camera_exact_name() -> None:
    camera = FakeCamera()
    blink = FakeBlink({"Living Room": camera})
    assert snap.resolve_camera(blink, "Living Room") is camera


def test_resolve_camera_case_insensitive() -> None:
    camera = FakeCamera()
    blink = FakeBlink({"Living Room": camera})
    assert snap.resolve_camera(blink, "living room") is camera


def test_resolve_camera_unique_substring() -> None:
    camera = FakeCamera()
    blink = FakeBlink({"Living Room Mini": camera})
    assert snap.resolve_camera(blink, "living") is camera


def test_resolve_camera_missing() -> None:
    blink = FakeBlink({"Kitchen": FakeCamera()})
    with pytest.raises(snap.CameraNotFoundError, match="Porch"):
        snap.resolve_camera(blink, "Porch")


def test_resolve_camera_ambiguous_substring() -> None:
    blink = FakeBlink(
        {
            "Living Room": FakeCamera(),
            "Living Room 2": FakeCamera(),
        }
    )
    with pytest.raises(snap.CameraNotFoundError):
        snap.resolve_camera(blink, "Living")


def test_resolve_log_level_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    assert snap.resolve_log_level(True) == logging.DEBUG


def test_resolve_log_level_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    assert snap.resolve_log_level(False) == logging.ERROR


def test_resolve_log_level_default() -> None:
    assert snap.resolve_log_level(False) == logging.INFO


def test_json_formatter_emits_one_object_with_event() -> None:
    record = logging.LogRecord(
        name="blink-snap",
        level=logging.INFO,
        pathname="snap.py",
        lineno=1,
        msg="Capture succeeded",
        args=(),
        exc_info=None,
    )
    record.event = "capture_ok"
    record.camera = "Living Room"
    line = snap.JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "blink-snap"
    assert payload["msg"] == "Capture succeeded"
    assert payload["event"] == "capture_ok"
    assert payload["camera"] == "Living Room"
    assert "ts" in payload
    assert "password" not in payload
    assert "token" not in payload
    assert "refresh_token" not in json.dumps(payload)


def test_parse_args_interval_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["snap.py", "--camera", "X", "--interval", "30"]
    )
    with pytest.raises(SystemExit):
        snap.parse_args()


def test_parse_args_interval_with_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys, "argv", ["snap.py", "--list", "--interval", "60"]
    )
    with pytest.raises(SystemExit):
        snap.parse_args()


def test_parse_args_interval_with_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["snap.py", "--camera", "X", "--interval", "60", "--mode", "live"],
    )
    with pytest.raises(SystemExit):
        snap.parse_args()


def test_parse_args_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLINK_CAMERA", "Kitchen")
    monkeypatch.setenv("BLINK_INTERVAL", "120")
    monkeypatch.setattr(sys, "argv", ["snap.py"])
    args = snap.parse_args()
    assert args.camera == "Kitchen"
    assert args.interval == 120


def _thumbnail_args(output: Path, camera: str = "Living Room") -> Namespace:
    return Namespace(
        camera=camera,
        mode="thumbnail",
        output=output,
        interval=0,
        keep_video=False,
    )


@pytest.mark.asyncio
async def test_capture_thumbnail_writes_jpeg(tmp_path: Path) -> None:
    camera = FakeCamera(jpeg_bytes=minimal_jpeg(32, 16))
    blink = FakeBlink({"Living Room": camera})
    dest = tmp_path / "shot.jpg"
    await snap.capture_thumbnail(blink, camera, dest)
    assert camera.snap_calls == 1
    assert blink.refresh_calls == 1
    assert camera.image_paths == [str(dest)]
    assert dest.is_file()
    assert snap.jpeg_dimensions(dest) == (32, 16)


@pytest.mark.asyncio
async def test_take_snapshot_thumbnail_returns_path(tmp_path: Path) -> None:
    camera = FakeCamera()
    blink = FakeBlink({"Living Room": camera})
    args = _thumbnail_args(tmp_path)
    dest = await snap.take_snapshot(blink, camera, args)
    assert dest.parent == tmp_path
    assert dest.name.endswith("_living-room_thumbnail.jpg")
    assert dest.is_file()


@pytest.mark.asyncio
async def test_watch_loop_reconnects_on_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = FakeCamera(
        fail_snaps=1,
        snap_error=TokenRefreshFailed("expired"),
    )
    blink = FakeBlink({"Living Room": camera})
    args = _thumbnail_args(tmp_path)
    args.interval = 60
    session_path = tmp_path / "blink_session.json"
    connect_calls = {"n": 0}

    async def fake_connect(*_args, **_kwargs) -> None:
        connect_calls["n"] += 1

    monkeypatch.setattr(snap, "connect_blink", fake_connect)

    async def sleep_until_captured(_delay: float) -> None:
        if camera.snap_calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(snap.asyncio, "sleep", sleep_until_captured)

    with pytest.raises(asyncio.CancelledError):
        await snap.watch_loop(
            blink,
            camera,
            args,
            session_path,
            http_session=object(),
        )

    assert camera.snap_calls == 2
    assert connect_calls["n"] == 1
    assert blink.save_calls == [str(session_path)]
    assert any(tmp_path.glob("*_living-room_thumbnail.jpg"))


@pytest.mark.asyncio
async def test_watch_loop_survives_failed_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    camera = FakeCamera(
        fail_snaps=2,
        snap_error=TokenRefreshFailed("expired"),
    )
    blink = FakeBlink({"Living Room": camera})
    args = _thumbnail_args(tmp_path)
    args.interval = 60
    session_path = tmp_path / "blink_session.json"
    connect_calls = {"n": 0}

    async def fake_connect(*_args, **_kwargs) -> None:
        connect_calls["n"] += 1
        if connect_calls["n"] == 1:
            raise snap.BlinkConnectError("temporary reconnect failure")

    async def sleep_until_captured(_delay: float) -> None:
        if blink.save_calls:
            raise asyncio.CancelledError

    monkeypatch.setattr(snap, "connect_blink", fake_connect)
    monkeypatch.setattr(snap.asyncio, "sleep", sleep_until_captured)

    with pytest.raises(asyncio.CancelledError):
        await snap.watch_loop(
            blink,
            camera,
            args,
            session_path,
            http_session=object(),
        )

    assert camera.snap_calls == 3
    assert connect_calls["n"] == 2
    assert blink.save_calls == [str(session_path)]
    assert any(tmp_path.glob("*_living-room_thumbnail.jpg"))
