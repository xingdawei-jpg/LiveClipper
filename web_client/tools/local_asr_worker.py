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


def _configure_ffmpeg_path() -> str:
    """Expose the Core-bundled FFmpeg to FunASR's command-name probe."""
    candidates = []
    configured = os.environ.get("LIVECLIPPER_FFMPEG_PATH", "").strip()
    if configured:
        path = Path(configured)
        candidates.append(path.parent if path.suffix else path)
    frozen_root = str(getattr(sys, "_MEIPASS", "") or "").strip()
    if frozen_root:
        candidates.append(Path(frozen_root) / "ffmpeg")
    executable_root = Path(sys.executable).resolve().parent
    candidates.extend((
        executable_root / "_internal" / "ffmpeg",
        executable_root / "ffmpeg",
    ))
    for candidate in candidates:
        ffmpeg = candidate / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not ffmpeg.is_file():
            continue
        current = os.environ.get("PATH", "")
        entries = [entry for entry in current.split(os.pathsep) if entry]
        candidate_text = str(candidate.resolve())
        if all(os.path.normcase(entry) != os.path.normcase(candidate_text) for entry in entries):
            os.environ["PATH"] = candidate_text + (os.pathsep + current if current else "")
        return str(ffmpeg.resolve())
    return ""


def _emit(message_type: str, **payload: object) -> None:
    record = {"type": message_type, **payload}
    print(json.dumps(record, ensure_ascii=False), flush=True)

def _engine_label(engine: str | None) -> str:
    """显示名与 stt.local_asr_engine_label 同源；不读设置项，未指定时按 SenseVoice 兜底。"""
    try:
        from stt import LOCAL_ASR_ENGINE_LABELS

        labels = LOCAL_ASR_ENGINE_LABELS
    except Exception:
        labels = {}
    key = str(engine or "").strip().lower()
    return labels.get(key) or labels.get("sensevoice") or "本地识别"


def _serve() -> int:
    """Keep one SenseVoice model alive for a caller-owned batch session."""
    _configure_runtime_path()
    try:
        from local_asr import transcribe_audio_to_srt
    except Exception as exc:
        _emit("log", message=f"本地语音识别异常: {type(exc).__name__}: {exc}")
        return 1

    for raw_line in sys.stdin:
        try:
            request = json.loads(raw_line)
        except json.JSONDecodeError:
            _emit("log", message="本地识别 worker 收到无法解析的批量请求。")
            continue
        if not isinstance(request, dict):
            continue
        if request.get("command") == "shutdown":
            return 0
        request_id = str(request.get("id") or "")
        audio_path = str(request.get("audio_path") or "")
        srt_output = str(request.get("srt_output") or "")
        if not request_id or not audio_path or not srt_output:
            _emit("result", id=request_id, ok=False)
            continue
        engine = str(request.get("engine") or "") or None
        label = _engine_label(engine)
        try:
            ok = bool(
                transcribe_audio_to_srt(
                    audio_path,
                    srt_output,
                    log_fn=lambda message, rid=request_id: _emit("log", id=rid, message=str(message)),
                    engine=engine,
                )
            )
            _emit("result", id=request_id, ok=ok)
        except Exception as exc:
            _emit("log", id=request_id, message=f"{label} 本地识别异常: {type(exc).__name__}: {exc}")
            _emit("result", id=request_id, ok=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    _configure_runtime_path()
    _configure_ffmpeg_path()
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--serve"]:
        return _serve()
    if len(args) != 2:
        _emit("log", message="本地识别 worker 参数不完整。")
        _emit("result", ok=False)
        return 2

    audio_path, srt_output = args
    _configure_runtime_path()
    engine = None
    label = "本地识别"
    try:
        from local_asr import _ENV_LOCAL_ASR_ENGINE, transcribe_audio_to_srt

        # 单发模式同样要尊重调用方指定的引擎：_run_local_asr_worker() 通过环境变量传入，
        # 这里必须显式透传给 transcribe_audio_to_srt()。否则 resolve_local_asr_engine()
        # 会优先读「设置项」，调用方的指定会被静默忽略。
        engine = str(os.environ.get(_ENV_LOCAL_ASR_ENGINE, "")).strip() or None
        label = _engine_label(engine)
        ok = bool(
            transcribe_audio_to_srt(
                audio_path,
                srt_output,
                log_fn=lambda message: _emit("log", message=str(message)),
                engine=engine,
            )
        )
        _emit("result", ok=ok)
        return 0 if ok else 1
    except Exception as exc:
        _emit("log", message=f"{label} 本地识别异常: {type(exc).__name__}: {exc}")
        _emit("result", ok=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
