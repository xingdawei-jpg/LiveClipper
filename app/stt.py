"""Local SenseVoice speech recognition and subtitle generation."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from typing import Callable

from config import FFMPEG_PATH


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


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
    try:
        from local_asr import LocalASRUnavailable, sensevoice_to_srt

        return bool(sensevoice_to_srt(audio_path, srt_output, log_fn=log_fn))
    except LocalASRUnavailable as exc:
        _log(f"SenseVoice 本地识别不可用: {exc}")
    except Exception as exc:
        _log(f"SenseVoice 本地识别异常: {type(exc).__name__}: {exc}")
    return False


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
