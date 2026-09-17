"""Process-I/O compatibility helpers for the signed Runtime V4 core."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


FALLBACK_SINK_NAME = "devnull-v4.bin"


@dataclass(frozen=True)
class DevNullResolution:
    """The output sink used by ``subprocess.DEVNULL`` in this process."""

    path: str
    used_fallback: bool


def _data_root() -> Path:
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or tempfile.gettempdir()
    )
    return base / "LiveClipper"


def ensure_subprocess_devnull() -> DevNullResolution:
    """Keep ``subprocess.DEVNULL`` usable when the Windows ``NUL`` device fails.

    ``subprocess`` opens :data:`os.devnull` when a child process is given
    ``subprocess.DEVNULL``. A few Windows environments report ``NUL`` as a
    missing path instead of exposing the device. Probe that exact operation
    before starting children and, only when it fails, replace ``os.devnull``
    for this process with a private regular-file sink.
    """

    candidate = os.devnull
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as original_error:
        fallback = _data_root() / "process_sinks" / FALLBACK_SINK_NAME
        try:
            fallback.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                str(fallback),
                flags | os.O_CREAT,
                0o600,
            )
        except OSError as fallback_error:
            raise fallback_error from original_error
        else:
            os.close(descriptor)
            os.devnull = str(fallback)
            return DevNullResolution(path=str(fallback), used_fallback=True)
    else:
        os.close(descriptor)
        return DevNullResolution(path=candidate, used_fallback=False)
