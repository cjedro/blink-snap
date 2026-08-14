# Tests + Pull-Request CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pytest suite for CLI, export, and job logic (with fake Blink objects) and a GitHub Action that runs it on every pull request.

**Architecture:** Tests live in `tests/` and import existing modules (`snap`, `export`, `export_jobs`) with no production refactor. Blink/camera collaborators are in-memory fakes. CI is a second workflow next to the existing tag-only image publish job.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, GitHub Actions

## Global Constraints

- Prefer fakes and monkeypatches over production refactors. Do not add dependency-injection layers unless a behavior cannot be exercised otherwise.
- Never log or assert on passwords, access tokens, or refresh tokens.
- Do not hit Blink’s cloud API (`immedia-semi.com` or Blink OAuth hosts). Do not call `Blink.start()` / `Auth` login.
- Do not test `--mode live` / `--mode clip` ffmpeg pipelines or `serve.py` HTTP handlers.
- Runtime `requirements.txt` stays production-only (no pytest). Docker image contents unchanged.
- Do not change `.github/workflows/publish-image.yml`.
- Python 3.12 (matches `Dockerfile`).
- These tests characterize **existing** behavior. They should pass against current code. If a test fails, fix the **test** unless you have found a real production bug — then stop and report it; do not silently change production behavior.
- Spec: `docs/superpowers/specs/2026-08-14-tests-and-pr-ci-design.md`

## File structure

| File | Responsibility |
|------|----------------|
| `pytest.ini` | pytest config: `asyncio_mode`, `pythonpath`, testpaths |
| `requirements-dev.txt` | pinned pytest + pytest-asyncio |
| `.gitignore` | ignore `.pytest_cache/` |
| `tests/conftest.py` | autouse env isolation so developer `.env` / `CAMERA` cannot leak into tests |
| `tests/fakes.py` | `FakeCamera` / `FakeBlink` used by snap tests |
| `tests/test_export.py` | timestamp parse, hour skip, image filter, fps, burn-in label |
| `tests/test_export_jobs.py` | job create/load/update/list against a temp `EXPORTS_DIR` |
| `tests/test_snap.py` | CLI helpers, JPEG parse, capture_thumbnail, watch_loop reconnect |
| `.github/workflows/test.yml` | run pytest on `pull_request` |

Do not modify `snap.py`, `export.py`, `export_jobs.py`, `serve.py`, `requirements.txt`, or `publish-image.yml`.

---

### Task 1: Pytest scaffolding + export tests

**Files:**
- Create: `pytest.ini`
- Create: `requirements-dev.txt`
- Create: `tests/conftest.py`
- Create: `tests/test_export.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `export.parse_capture_timestamp`, `export.hour_excluded`, `export.filter_images`, `export.default_fps_from_interval`, `export.format_burnin_timestamp`, `export.list_capture_images`, `export.ExportOptions`
- Produces: a pytest project that can `import export` from the repo root

- [ ] **Step 1: Add pytest config, dev deps, gitignore, and env isolation**

Create `pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
testpaths = tests
pythonpath = .
```

Create `requirements-dev.txt`:

```text
pytest==8.4.1
pytest-asyncio==1.1.0
```

Append to `.gitignore` (keep existing entries):

```gitignore
.pytest_cache/
```

Create `tests/conftest.py`:

```python
"""Shared fixtures. Isolate process env so local CAMERA / BLINK_* values cannot leak."""

from __future__ import annotations

import os

import pytest

_CLEAR_ENV = (
    "CAMERA",
    "INTERVAL",
    "LOG_LEVEL",
    "EXPORTS_DIR",
)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("BLINK_") or key in _CLEAR_ENV:
            monkeypatch.delenv(key, raising=False)
```

- [ ] **Step 2: Write export tests**

Create `tests/test_export.py`:

```python
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
```

- [ ] **Step 3: Install dev deps and run export tests**

Run:

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
pytest tests/test_export.py -v
```

Expected: all tests PASS. If pytest/pytest-asyncio pins are not on PyPI, install the latest compatible pair (`pytest>=8,<10` and a pytest-asyncio that supports `asyncio_mode = auto`) and update `requirements-dev.txt` to those exact versions.

- [ ] **Step 4: Commit**

```bash
git add pytest.ini requirements-dev.txt .gitignore tests/conftest.py tests/test_export.py
git commit -m "$(cat <<'EOF'
Add pytest scaffolding and export unit tests.

EOF
)"
```

---

### Task 2: Export job persistence tests

**Files:**
- Create: `tests/test_export_jobs.py`

**Interfaces:**
- Consumes: `export_jobs.EXPORTS_DIR`, `create_job`, `load_job`, `update_job`, `list_jobs`
- Produces: job tests that never write to `/data/exports`

- [ ] **Step 1: Write job tests**

Create `tests/test_export_jobs.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

import export_jobs


@pytest.fixture
def exports_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "exports"
    directory.mkdir()
    monkeypatch.setattr(export_jobs, "EXPORTS_DIR", directory)
    return directory


def test_create_job_writes_queued_metadata(exports_dir: Path) -> None:
    job = export_jobs.create_job("living-room 2 images", 2)
    assert job.status == "queued"
    assert job.percent == 0.0
    assert job.label == "living-room 2 images"
    assert job.image_count == 2
    assert job.filename == f"{job.export_id}.mp4"
    meta = exports_dir / f"{job.export_id}.json"
    assert meta.is_file()


def test_load_job_round_trip(exports_dir: Path) -> None:
    created = export_jobs.create_job("round-trip", 1)
    loaded = export_jobs.load_job(created.export_id)
    assert loaded is not None
    assert loaded.export_id == created.export_id
    assert loaded.label == "round-trip"
    assert loaded.image_count == 1


def test_load_job_unknown_id_returns_none(exports_dir: Path) -> None:
    assert export_jobs.load_job("does-not-exist") is None


def test_load_job_corrupt_json_returns_none(exports_dir: Path) -> None:
    (exports_dir / "broken.json").write_text("{", encoding="utf-8")
    assert export_jobs.load_job("broken") is None


def test_update_job_changes_status(exports_dir: Path) -> None:
    job = export_jobs.create_job("update-me", 3)
    export_jobs.update_job(
        job.export_id,
        status="running",
        percent=40.0,
        message="Encoding video…",
    )
    loaded = export_jobs.load_job(job.export_id)
    assert loaded is not None
    assert loaded.status == "running"
    assert loaded.percent == 40.0
    assert loaded.message == "Encoding video…"


def test_list_jobs_newest_first_skips_unreadable(exports_dir: Path) -> None:
    older = export_jobs.create_job("older", 1)
    newer = export_jobs.create_job("newer", 2)
    export_jobs.update_job(older.export_id, created_at="2026-01-01T00:00:00")
    export_jobs.update_job(newer.export_id, created_at="2026-01-02T00:00:00")
    (exports_dir / "garbage.json").write_text("not-json", encoding="utf-8")
    jobs = export_jobs.list_jobs()
    assert [job.label for job in jobs] == ["newer", "older"]
    assert {job.export_id for job in jobs} == {older.export_id, newer.export_id}
```

- [ ] **Step 2: Run job tests**

Run:

```bash
pytest tests/test_export_jobs.py tests/test_export.py -v
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_export_jobs.py
git commit -m "$(cat <<'EOF'
Add export job persistence tests.

EOF
)"
```

---

### Task 3: Snap CLI helper tests + Blink fakes

**Files:**
- Create: `tests/fakes.py`
- Create: `tests/test_snap.py`

**Interfaces:**
- Consumes: `snap.slugify`, `jpeg_dimensions`, `output_path`, `resolve_camera`, `resolve_log_level`, `JsonFormatter`, `parse_args`, `CameraNotFoundError`, `FakeBlink`, `FakeCamera`
- Produces: `minimal_jpeg(width, height)` helper and fakes used by Task 4

- [ ] **Step 1: Write fakes**

Create `tests/fakes.py`:

```python
"""In-memory Blink/camera stand-ins. No network, no secrets."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def minimal_jpeg(width: int, height: int) -> bytes:
    """SOI + SOF0 so snap.jpeg_dimensions can read width/height."""
    return (
        b"\xff\xd8\xff\xc0\x00\x0b\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x00"
    )


class FakeCamera:
    def __init__(
        self,
        jpeg_bytes: bytes | None = None,
        camera_type: str = "mini",
        fail_snaps: int = 0,
        snap_error: BaseException | None = None,
    ) -> None:
        self.camera_type = camera_type
        self.jpeg_bytes = jpeg_bytes or minimal_jpeg(32, 16)
        self.fail_snaps = fail_snaps
        self.snap_error = snap_error
        self.snap_calls = 0
        self.image_paths: list[str] = []

    async def snap_picture(self) -> None:
        self.snap_calls += 1
        if self.snap_calls <= self.fail_snaps and self.snap_error is not None:
            raise self.snap_error

    async def image_to_file(self, path: str) -> None:
        self.image_paths.append(path)
        Path(path).write_bytes(self.jpeg_bytes)


class FakeBlink:
    def __init__(self, cameras: dict[str, FakeCamera] | None = None) -> None:
        self.cameras = cameras or {}
        self.available = True
        self.refresh_calls = 0
        self.save_calls: list[str] = []
        self.auth = SimpleNamespace(callback=None)

    async def refresh(self, force_cache: bool = False) -> None:
        self.refresh_calls += 1

    async def save(self, path: str) -> None:
        self.save_calls.append(path)
```

- [ ] **Step 2: Write snap helper tests**

Create `tests/test_snap.py`:

```python
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

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
```

- [ ] **Step 3: Run helper tests**

Run:

```bash
pytest tests/test_snap.py tests/test_export.py tests/test_export_jobs.py -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/fakes.py tests/test_snap.py
git commit -m "$(cat <<'EOF'
Add snap CLI helper tests and Blink fakes.

EOF
)"
```

---

### Task 4: Thumbnail capture and watch-loop reconnect tests

**Files:**
- Modify: `tests/test_snap.py` (append the tests below)

**Interfaces:**
- Consumes: `snap.capture_thumbnail`, `snap.take_snapshot`, `snap.watch_loop`, `FakeBlink`, `FakeCamera`; monkeypatch `snap.connect_blink`
- Produces: coverage of thumbnail capture and recoverable-auth reconnect without network

- [ ] **Step 1: Append capture/watch tests to `tests/test_snap.py`**

Add imports at the top (keep existing imports):

```python
import asyncio
from argparse import Namespace

from blinkpy.auth import TokenRefreshFailed
```

Append:

```python
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
    session_path = tmp_path / "blink_session.json"
    connect_calls = {"n": 0}

    async def fake_connect(*_args, **_kwargs) -> None:
        connect_calls["n"] += 1

    monkeypatch.setattr(snap, "connect_blink", fake_connect)

    original_take = snap.take_snapshot

    async def take_until_cancelled(*a, **k):
        result = await original_take(*a, **k)
        if camera.snap_calls >= 2:
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(snap, "take_snapshot", take_until_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await snap.watch_loop(
            blink,
            camera,
            args,
            session_path,
            http_session=object(),
        )

    assert camera.snap_calls >= 2
    assert connect_calls["n"] >= 1
    assert any(tmp_path.glob("*_living-room_thumbnail.jpg"))
```

First `original_take` raises `TokenRefreshFailed` (`snap_calls == 1`) → reconnect. Second `original_take` succeeds (`snap_calls == 2`) then `CancelledError` stops the loop. `CancelledError` is a `BaseException`, so `watch_loop`'s `except Exception` will not swallow it. `interval=0` means the loop does not `asyncio.sleep`.

- [ ] **Step 2: Run all tests**

Run:

```bash
pytest -v
```

Expected: all tests PASS, including the three new async tests. If `watch_loop` hangs, Ctrl-C and fix the cancel condition — do not leave an unbounded loop.

- [ ] **Step 3: Commit**

```bash
git add tests/test_snap.py
git commit -m "$(cat <<'EOF'
Add thumbnail capture and watch-loop reconnect tests.

EOF
)"
```

---

### Task 5: GitHub Action on pull_request

**Files:**
- Create: `.github/workflows/test.yml`

**Interfaces:**
- Consumes: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `tests/`
- Produces: a `test` check on every PR

- [ ] **Step 1: Write the workflow**

Create `.github/workflows/test.yml`:

```yaml
name: Tests

on:
  pull_request:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install dependencies
        run: pip install -r requirements.txt -r requirements-dev.txt

      - name: Run tests
        run: pytest
```

Do not edit `.github/workflows/publish-image.yml`.

- [ ] **Step 2: Confirm the suite still passes locally (same command CI will run)**

Run:

```bash
pytest
```

Expected: exit code 0. Confirm `requirements.txt` still has only `blinkpy`, `python-dotenv`, and `aiohttp`.

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/test.yml
git commit -m "$(cat <<'EOF'
Run pytest on every pull request.

EOF
)"
```

---

## Self-review

**Spec coverage**

| Spec section | Task |
|--------------|------|
| pytest layout, `requirements-dev.txt`, `.pytest_cache/` | Task 1 |
| `test_export.py` behaviors | Task 1 |
| `test_export_jobs.py` behaviors | Task 2 |
| `tests/fakes.py` | Task 3 |
| snap helpers / parse_args / JsonFormatter | Task 3 |
| capture_thumbnail, take_snapshot, watch_loop reconnect | Task 4 |
| `.github/workflows/test.yml` on `pull_request`, Python 3.12 | Task 5 |
| No live Blink, no ffmpeg, no serve.py, no publish-image change | All tasks |

**Placeholder scan:** none remaining. Task 3 omits `Namespace`; Task 4 adds `asyncio`, `Namespace`, and `TokenRefreshFailed`. Watch-loop cancel runs after the successful second snap.

**Type consistency:** `FakeCamera.snap_picture` / `image_to_file` and `FakeBlink.refresh` / `save` / `cameras` match what `snap.py` calls. `ExportOptions` fields match `export.py`. `EXPORTS_DIR` is monkeypatched on the `export_jobs` module after import.
