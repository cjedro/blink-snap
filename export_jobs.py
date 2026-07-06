"""Persistent export job storage and background job metadata."""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

EXPORTS_DIR = Path(os.getenv("EXPORTS_DIR", "/data/exports"))

_jobs_lock = threading.Lock()


@dataclass
class ExportJob:
    export_id: str
    status: str  # queued, running, done, error
    percent: float
    message: str
    label: str
    image_count: int
    created_at: str
    error: str | None = None
    filename: str | None = None

    @property
    def mp4_path(self) -> Path:
        return EXPORTS_DIR / f"{self.export_id}.mp4"

    @property
    def meta_path(self) -> Path:
        return EXPORTS_DIR / f"{self.export_id}.json"


def ensure_exports_dir() -> None:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _job_from_dict(data: dict) -> ExportJob:
    return ExportJob(
        export_id=data["export_id"],
        status=data["status"],
        percent=float(data.get("percent", 0)),
        message=data.get("message", ""),
        label=data.get("label", ""),
        image_count=int(data.get("image_count", 0)),
        created_at=data.get("created_at", ""),
        error=data.get("error"),
        filename=data.get("filename"),
    )


def save_job(job: ExportJob) -> None:
    ensure_exports_dir()
    with _jobs_lock:
        job.meta_path.write_text(
            json.dumps(asdict(job), indent=2),
            encoding="utf-8",
        )


def load_job(export_id: str) -> ExportJob | None:
    meta_path = EXPORTS_DIR / f"{export_id}.json"
    if not meta_path.is_file():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return _job_from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def list_jobs() -> list[ExportJob]:
    ensure_exports_dir()
    jobs: list[ExportJob] = []
    for meta_path in EXPORTS_DIR.glob("*.json"):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            jobs.append(_job_from_dict(data))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    jobs.sort(key=lambda job: job.created_at, reverse=True)
    return jobs


def create_job(label: str, image_count: int) -> ExportJob:
    now = datetime.now()
    export_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}"
    job = ExportJob(
        export_id=export_id,
        status="queued",
        percent=0.0,
        message="Export queued",
        label=label,
        image_count=image_count,
        created_at=now.isoformat(timespec="seconds"),
        filename=f"{export_id}.mp4",
    )
    save_job(job)
    return job


def update_job(export_id: str, **kwargs: object) -> None:
    with _jobs_lock:
        job = load_job(export_id)
        if job is None:
            return
        for key, value in kwargs.items():
            setattr(job, key, value)
        job.meta_path.write_text(
            json.dumps(asdict(job), indent=2),
            encoding="utf-8",
        )
