"""Short-lived SenseVoice worker used by source and frozen desktop runtimes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, OSError):
            pass


def _configure_runtime_path() -> None:
    script = Path(__file__).resolve()
    candidates = []
    bundle_dir = os.environ.get("LIVECLIPPER_BUNDLE_DIR", "").strip()
    if bundle_dir:
        candidates.append(Path(bundle_dir) / "app")
    if len(script.parents) >= 3:
        candidates.append(script.parents[2] / "app")
    if len(script.parents) >= 2:
        candidates.append(script.parents[1] / "app")
    for candidate in candidates:
        text = str(candidate)
        if candidate.is_dir() and text not in sys.path:
            sys.path.insert(0, text)


def _emit(message_type: str, **payload: object) -> None:
    record = {"type": message_type, **payload}
    print(json.dumps(record, ensure_ascii=False), flush=True)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        _emit("log", message="SenseVoice worker 参数不完整。")
        _emit("result", ok=False)
        return 2

    audio_path, srt_output = args
    _configure_runtime_path()
    try:
        from local_asr import sensevoice_to_srt

        ok = bool(
            sensevoice_to_srt(
                audio_path,
                srt_output,
                log_fn=lambda message: _emit("log", message=str(message)),
            )
        )
        _emit("result", ok=ok)
        return 0 if ok else 1
    except Exception as exc:
        _emit("log", message=f"SenseVoice 本地识别异常: {type(exc).__name__}: {exc}")
        _emit("result", ok=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
