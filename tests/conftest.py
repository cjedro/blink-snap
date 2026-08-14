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
