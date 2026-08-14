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
