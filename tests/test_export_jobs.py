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
