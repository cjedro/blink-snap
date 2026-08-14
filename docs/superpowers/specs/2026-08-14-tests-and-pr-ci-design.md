# Design: Application tests + pull-request CI

Date: 2026-08-14  
Status: approved for implementation planning  
Scope: `tests/`, `requirements-dev.txt`, `.github/workflows/test.yml`, `.gitignore` (pytest cache); tiny production seams only if a test cannot be written against the current API  
Out of scope: live Blink accounts, ffmpeg live/clip paths, `serve.py` HTTP handlers, Docker image builds on PRs, lint/typecheck in CI, coverage reports

## Problem

blink-snap has no automated tests and no GitHub Action that runs on pull requests. The only workflow (`.github/workflows/publish-image.yml`) builds and pushes a container image on tags. Regressions in CLI parsing, export filtering, job persistence, and watch-loop reconnect can ship unnoticed.

## Goals

1. Unit-test application logic without a Blink account or ffmpeg.
2. Run those tests on every pull request.
3. Keep the Docker runtime image unchanged (`requirements.txt` stays production-only).

## Non-goals

- Hitting Blink’s cloud API in CI
- Testing `--mode live` / `--mode clip` ffmpeg pipelines
- HTTP handler tests for `serve.py`
- ruff, mypy, or coverage artifacts
- Changing the tag-triggered image publish workflow

## Constraint

**Prefer fakes and monkeypatches over production refactors.** `capture_thumbnail`, `resolve_camera`, `watch_loop`, and export helpers already accept objects/paths that tests can supply. Do not add dependency-injection layers unless a behavior cannot be exercised otherwise.

Never log or assert on passwords, access tokens, or refresh tokens.

## Approach

**pytest + pytest-asyncio, fake Blink/camera objects, PR-only GitHub Action.**

| Option | Summary | Why not |
|--------|---------|---------|
| stdlib unittest | No extra test packages | Verbose async setup; no fixtures for temp dirs/env |
| pytest + coverage upload | Same tests plus coverage.xml | Extra CI noise; not requested |

## Design

### 1. Test layout

```text
tests/
  conftest.py          # tmp dirs, env isolation, argparse helpers
  fakes.py             # in-memory Blink and camera stand-ins
  test_snap.py         # CLI, JPEG helper, capture/watch with fakes
  test_export.py       # timestamps, hour skip, date filter, fps
  test_export_jobs.py  # job CRUD against a temp EXPORTS_DIR
requirements-dev.txt   # pytest, pytest-asyncio (pinned)
.github/workflows/test.yml
pytest.ini             # asyncio_mode = auto
```

Runtime `requirements.txt` is unchanged. Local run:

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Add `.pytest_cache/` to `.gitignore`.

### 2. Fakes (`tests/fakes.py`)

Minimal objects that implement only the methods production code calls:

**`FakeCamera`**

- Attributes: `camera_type` (optional)
- `async snap_picture()` — records a call
- `async image_to_file(path)` — writes JPEG bytes to `path` (valid SOI + SOF0 so `jpeg_dimensions` can parse them)

**`FakeBlink`**

- `cameras: dict[str, FakeCamera]`
- `available: bool`
- `async refresh(force_cache=True)` — records a call
- `async save(path)` — records a call (does not write secrets)
- `auth` — simple namespace with a `callback` attribute so `attach_session_saver` can be exercised if needed

No `unittest.mock.MagicMock` for Blink/camera unless a one-off side effect (raise on Nth call) is cleaner as a mock wrapper around a fake.

### 3. `test_export.py`

Exercise `export.py` with a temp directory of dummy `.jpg` files named like production (`{YYYY-MM-DDTHH-MM-SSZ}_{slug}_{mode}.jpg`).

Must cover:

- `parse_capture_timestamp` — valid name → UTC datetime; junk name → `None`
- `hour_excluded` — same-day range (`22–23`) and overnight wrap (`22–6`)
- `filter_images` — whole-library vs date range; skip-hours applied in a named timezone
- `default_fps_from_interval` — `300` → `288.0` (`86400 / 300`)
- `format_burnin_timestamp` — UTC file → local `dd/mm/YYYY HH:00`
- `list_capture_images` — missing directory → `[]`; ignores non-jpeg files

Do not invoke ffmpeg (`build_timelapse_mp4`, `stamp_image_with_timestamp`).

### 4. `test_export_jobs.py`

Monkeypatch `export_jobs.EXPORTS_DIR` to a temp path after import.

Must cover:

- `create_job` writes JSON and returns `status="queued"`
- `load_job` round-trip; unknown id → `None`; corrupt JSON → `None`
- `update_job` changes status/percent/message
- `list_jobs` newest-first; skips unreadable JSON files

### 5. `test_snap.py`

**Pure helpers**

- `slugify` — spaces/punctuation; empty-after-strip → `"camera"`
- `jpeg_dimensions` — crafted JPEG bytes → width/height; non-JPEG / missing file → `None`
- `output_path` — filename contains UTC timestamp, slug, and mode; creates the directory
- `resolve_camera` — exact name; case-insensitive unique match; unique substring; missing / ambiguous → `CameraNotFoundError`
- `resolve_log_level` — `-v` → DEBUG; `LOG_LEVEL`; default INFO
- `JsonFormatter` — one JSON object per line with `ts`, `level`, `logger`, `msg`; copies known extras such as `event`. Tests must not put passwords or tokens into log records.
- `parse_args` via `sys.argv` monkeypatch:
  - `--interval` below 60 → `SystemExit`
  - `--interval` with `--list` → `SystemExit`
  - `--interval` with `--mode live` → `SystemExit`
  - env `BLINK_CAMERA` / `BLINK_INTERVAL` supply defaults

**Capture / watch (fakes)**

- `capture_thumbnail` calls `snap_picture`, `refresh`, `image_to_file` and leaves a JPEG at `dest`
- `take_snapshot` in thumbnail mode returns that path
- `watch_loop`: on a recoverable auth error (`TokenRefreshFailed` or `BlinkConnectError`), calls reconnect and continues (does not raise / `sys.exit`)
  - Monkeypatch `snap.connect_blink` so reconnect does not hit the network
  - Monkeypatch `asyncio.sleep` to no-op or tiny delay
  - Stop the loop with `CancelledError` after a bounded number of iterations (side effect on `take_snapshot` or sleep)

Do not test `connect_blink` against a real `Auth`/`Blink.start()`. Do not test live/clip capture.

### 6. GitHub Actions

New file `.github/workflows/test.yml`:

- **on:** `pull_request` (all branches)
- **permissions:** `contents: read`
- **job:** `test` on `ubuntu-latest`
- **steps:**
  1. `actions/checkout@v4`
  2. `actions/setup-python@v5` with `python-version: "3.12"` (matches `Dockerfile`)
  3. `pip install -r requirements.txt -r requirements-dev.txt`
  4. `pytest`

The existing `publish-image.yml` is unchanged. Dependabot already watches the pip ecosystem at `/` and will see `requirements-dev.txt`.

## Success criteria

- `pytest` exits 0 locally with no Blink credentials and no ffmpeg.
- Opening a pull request runs the new workflow; a failing test fails the check.
- Docker image contents are unchanged (no pytest in `requirements.txt`).
- No test talks to `immedia-semi.com` or Blink OAuth hosts.

## Risks

| Risk | Mitigation |
|------|------------|
| blinkpy import requires network-unrelated but heavy deps | Install `requirements.txt` in CI; tests never call `Blink.start()` |
| Infinite `watch_loop` hangs CI | Bound iterations; cancel the task; timeout via pytest if needed |
| Flaky time-based filenames | Freeze or assert prefix/suffix patterns, not exact clock values |
| Accidental production refactor | Spec forbids DI unless a test cannot be written |
