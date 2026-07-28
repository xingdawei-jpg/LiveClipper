"""Local SenseVoice speech recognition and subtitle generation."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from config import FFMPEG_PATH


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOCAL_ASR_TIMEOUT_SECONDS = 4 * 60 * 60
_LOCAL_ASR_TOOL_FLAG = "--liveclipper-run-tool"


def get_ffmpeg_cmd() -> str:
    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        return FFMPEG_PATH
    return "ffmpeg"


def extract_audio(video_path: str, output_wav: str, log_fn: Callable[[str], None] | None = None) -> bool:
    """Extract 16 kHz mono WAV audio for the local SenseVoice engine."""
    def _log(message: str) -> None:
        if log_fn:
            log_fn(message)

    cmd = [
        get_ffmpeg_cmd(),
        "-y",
        "-i",
        video_path,
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        output_wav,
    ]
    _log("正在提取音频...")
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    if result.returncode != 0 or not os.path.exists(output_wav):
        _log(f"音频提取失败: {result.stderr[:200]}")
        return False

    size_mb = os.path.getsize(output_wav) / (1024 * 1024)
    _log(f"音频提取完成 ({size_mb:.1f}MB)")
    return True


def _local_asr_worker_script() -> Path | None:
    configured = os.environ.get("LIVECLIPPER_LOCAL_ASR_WORKER", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(os.path.expandvars(configured)).expanduser())

    bundle_dir = os.environ.get("LIVECLIPPER_BUNDLE_DIR", "").strip()
    if bundle_dir:
        candidates.append(Path(bundle_dir) / "tools" / "local_asr_worker.py")

    root = Path(__file__).resolve().parents[1]
    candidates.extend((
        root / "web_client" / "tools" / "local_asr_worker.py",
        root / "tools" / "local_asr_worker.py",
    ))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _local_asr_worker_command(worker: Path, audio_path: str, srt_output: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            _LOCAL_ASR_TOOL_FLAG,
            str(worker),
            audio_path,
            srt_output,
        ]
    return [sys.executable, str(worker), audio_path, srt_output]


def _remove_local_asr_outputs(srt_output: str) -> None:
    srt_path = Path(srt_output)
    for path in (srt_path, srt_path.with_suffix(".words.json")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _decode_worker_message(raw_line: str) -> tuple[str, str | bool] | None:
    line = str(raw_line or "").strip()
    if not line:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return "raw", line
    if not isinstance(payload, dict):
        return "raw", line
    message_type = str(payload.get("type") or "").strip().lower()
    if message_type == "result":
        return "result", bool(payload.get("ok"))
    if message_type == "log":
        return "log", str(payload.get("message") or "").strip()
    return None


def _run_local_asr_worker(
    audio_path: str,
    srt_output: str,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    def _log(message: str) -> None:
        if log_fn and message:
            log_fn(message)

    worker = _local_asr_worker_script()
    if worker is None:
        _log("SenseVoice 独立识别进程缺少 worker 文件，请重新安装完整版本。")
        return False

    _remove_local_asr_outputs(srt_output)
    command = _local_asr_worker_command(worker, audio_path, srt_output)
    try:
        timeout_seconds = max(
            60.0,
            float(os.environ.get("LIVECLIPPER_LOCAL_ASR_TIMEOUT_SECONDS", _LOCAL_ASR_TIMEOUT_SECONDS)),
        )
    except (TypeError, ValueError):
        timeout_seconds = float(_LOCAL_ASR_TIMEOUT_SECONDS)
    line_queue: queue.Queue[str | None] = queue.Queue()
    result_ok = False
    reader_finished = False
    raw_diagnostics: list[str] = []
    proc: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None

    try:
        _log("正在启动独立 SenseVoice 识别进程...")
        worker_env = os.environ.copy()
        worker_env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_NO_WINDOW,
            env=worker_env,
        )

        def _read_output() -> None:
            try:
                if proc and proc.stdout:
                    for line in proc.stdout:
                        line_queue.put(line)
            finally:
                line_queue.put(None)

        reader = threading.Thread(target=_read_output, name="liveclipper-local-asr-log", daemon=True)
        reader.start()
        deadline = time.monotonic() + timeout_seconds

        while True:
            if time.monotonic() >= deadline:
                proc.kill()
                proc.wait(timeout=10)
                _log("SenseVoice 本地识别超时，识别进程已结束。")
                return False
            try:
                raw_line = line_queue.get(timeout=0.2)
            except queue.Empty:
                raw_line = ""
            if raw_line is None:
                reader_finished = True
            elif raw_line:
                decoded = _decode_worker_message(raw_line)
                if decoded:
                    message_type, value = decoded
                    if message_type == "result":
                        result_ok = bool(value)
                    elif message_type == "raw":
                        raw_diagnostics.append(str(value))
                        raw_diagnostics = raw_diagnostics[-5:]
                    elif value:
                        _log(str(value))

            if proc.poll() is not None and reader_finished and line_queue.empty():
                break

        return_code = proc.wait(timeout=10)
        srt_path = Path(srt_output)
        succeeded = result_ok and return_code == 0 and srt_path.is_file() and srt_path.stat().st_size > 0
        if not succeeded:
            for diagnostic in raw_diagnostics:
                _log(f"SenseVoice 诊断: {diagnostic[:300]}")
            _log(f"SenseVoice 独立识别进程失败（返回码 {return_code}）。")
            _remove_local_asr_outputs(srt_output)
        return succeeded
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        _log(f"SenseVoice 独立识别进程启动失败: {exc}")
        _remove_local_asr_outputs(srt_output)
        return False
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.SubprocessError:
                pass
        if reader is not None:
            reader.join(timeout=2)
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()
        if proc is not None:
            _log("SenseVoice 识别进程已退出，本地模型内存已释放。")


def transcribe_local_audio_to_srt(
    audio_path: str,
    srt_output: str,
    log_fn: Callable[[str], None] | None = None,
    asr_engine: str = "sensevoice",
) -> bool:
    """Transcribe through SenseVoice only; never silently switch engines."""
    def _log(message: str) -> None:
        if log_fn:
            log_fn(message)

    selected_engine = str(asr_engine or "sensevoice").strip().lower()
    if selected_engine not in {"", "auto", "sensevoice"}:
        _log("本地识别仅支持 SenseVoice，已忽略过期的本地模型设置。")
    return _run_local_asr_worker(audio_path, srt_output, log_fn=log_fn)


def generate_srt(
    video_path: str,
    log_fn: Callable[[str], None] | None = None,
    asr_engine: str = "sensevoice",
) -> str | None:
    """Generate an SRT sidecar with SenseVoice, or return ``None`` on failure."""
    temp_dir = os.path.join(tempfile.gettempdir(), "live_cutter_stt")
    os.makedirs(temp_dir, exist_ok=True)
    video_hash = hashlib.md5(video_path.encode("utf-8")).hexdigest()[:8]
    wav_path = os.path.join(temp_dir, f"audio_{video_hash}.wav")
    srt_path = os.path.join(temp_dir, f"sub_{video_hash}.srt")

    try:
        if not extract_audio(video_path, wav_path, log_fn):
            return None
        if not transcribe_local_audio_to_srt(
            wav_path,
            srt_path,
            log_fn=log_fn,
            asr_engine=asr_engine,
        ):
            return None
        return srt_path if os.path.exists(srt_path) else None
    finally:
        try:
            if os.path.exists(wav_path):
                os.remove(wav_path)
        except OSError:
            pass


def cleanup_srt(srt_path: str | None) -> None:
    try:
        if srt_path and os.path.exists(srt_path):
            os.remove(srt_path)
    except OSError:
        pass
