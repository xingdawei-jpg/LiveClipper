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
from typing import Callable, Protocol

from config import FFMPEG_PATH


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOCAL_ASR_TIMEOUT_SECONDS = 4 * 60 * 60
_LOCAL_ASR_TOOL_FLAG = "--liveclipper-run-tool"


class _CancelEvent(Protocol):
    def is_set(self) -> bool: ...


def get_ffmpeg_cmd() -> str:
    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        return FFMPEG_PATH
    return "ffmpeg"


def extract_audio(
    video_path: str,
    output_wav: str,
    log_fn: Callable[[str], None] | None = None,
    cancel_event: _CancelEvent | None = None,
) -> bool:
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
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    stderr = ""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            proc.kill()
            proc.communicate(timeout=10)
            _log("音频提取已停止。")
            return False
        try:
            _stdout, stderr = proc.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue
    if proc.returncode != 0 or not os.path.exists(output_wav):
        _log(f"音频提取失败: {str(stderr or '')[:200]}")
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


def _local_asr_worker_serve_command(worker: Path) -> list[str]:
    """Start the local worker in its task-scoped reusable mode."""
    if getattr(sys, "frozen", False):
        return [
            sys.executable,
            _LOCAL_ASR_TOOL_FLAG,
            str(worker),
            "--serve",
        ]
    return [sys.executable, str(worker), "--serve"]


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


def _local_asr_exit_diagnostic(return_code: int | None) -> str:
    """Describe native worker crashes without retrying an unstable process."""
    if return_code is None:
        return "SenseVoice 本地识别进程意外退出，未取得退出码。"
    try:
        unsigned = int(return_code) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return f"SenseVoice 本地识别进程失败（返回码 {return_code}）。"
    if unsigned == 0xC0000005:
        return (
            "SenseVoice 本地识别进程发生原生访问冲突（0xC0000005）。"
            "已停止重试并释放模型内存；请改用云端识别或检查本机 Python/Torch/显卡驱动环境。"
        )
    return f"SenseVoice 本地识别进程失败（返回码 {return_code} / 0x{unsigned:08X}）。"


class LocalASRWorkerSession:
    """A task-scoped SenseVoice worker that keeps its model warm between jobs.

    The worker is never global: callers own the session and must close it when
    their batch finishes.  This avoids a repeated model load for each final
    subtitle while preserving the process boundary used to release model memory
    deterministically after the task.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._request_index = 0
        self._closed = False

    @staticmethod
    def _timeout_seconds() -> float:
        try:
            return max(
                60.0,
                float(os.environ.get("LIVECLIPPER_LOCAL_ASR_TIMEOUT_SECONDS", _LOCAL_ASR_TIMEOUT_SECONDS)),
            )
        except (TypeError, ValueError):
            return float(_LOCAL_ASR_TIMEOUT_SECONDS)

    def _start(self, log_fn: Callable[[str], None] | None) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        worker = _local_asr_worker_script()
        if worker is None:
            if log_fn:
                log_fn("SenseVoice 独立识别进程缺少 worker 文件，请重新安装完整版本。")
            return False
        try:
            worker_env = os.environ.copy()
            worker_env["PYTHONIOENCODING"] = "utf-8"
            if log_fn:
                log_fn("正在启动可复用 SenseVoice 批量识别进程...")
            self._lines = queue.Queue()
            self._proc = subprocess.Popen(
                _local_asr_worker_serve_command(worker),
                stdin=subprocess.PIPE,
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
                    if self._proc and self._proc.stdout:
                        for line in self._proc.stdout:
                            self._lines.put(line)
                finally:
                    self._lines.put(None)

            self._reader = threading.Thread(
                target=_read_output,
                name="liveclipper-local-asr-batch-log",
                daemon=True,
            )
            self._reader.start()
            return True
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            if log_fn:
                log_fn(f"SenseVoice 批量识别进程启动失败: {exc}")
            self._terminate()
            return False

    def _terminate(self) -> None:
        proc = self._proc
        reader = self._reader
        self._proc = None
        self._reader = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
                proc.wait(timeout=10)
            except (OSError, subprocess.SubprocessError):
                pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        if reader is not None:
            reader.join(timeout=2)

    def transcribe(
        self,
        audio_path: str,
        srt_output: str,
        *,
        log_fn: Callable[[str], None] | None = None,
        cancel_event: _CancelEvent | None = None,
    ) -> bool:
        """Run one request, keeping the already-loaded model for the next one."""
        with self._lock:
            if self._closed:
                if log_fn:
                    log_fn("SenseVoice 批量识别进程已关闭，无法继续识别。")
                return False
            if cancel_event is not None and cancel_event.is_set():
                return False
            if not self._start(log_fn):
                _remove_local_asr_outputs(srt_output)
                return False
            proc = self._proc
            if proc is None or proc.stdin is None:
                _remove_local_asr_outputs(srt_output)
                return False

            self._request_index += 1
            request_id = str(self._request_index)
            _remove_local_asr_outputs(srt_output)
            try:
                proc.stdin.write(json.dumps({
                    "id": request_id,
                    "audio_path": audio_path,
                    "srt_output": srt_output,
                }, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                if log_fn:
                    log_fn(f"SenseVoice 批量识别请求发送失败: {exc}")
                self._terminate()
                _remove_local_asr_outputs(srt_output)
                return False

            deadline = time.monotonic() + self._timeout_seconds()
            diagnostics: list[str] = []
            result_ok: bool | None = None
            while result_ok is None:
                if cancel_event is not None and cancel_event.is_set():
                    if log_fn:
                        log_fn("SenseVoice 本地识别已停止。")
                    self._terminate()
                    _remove_local_asr_outputs(srt_output)
                    return False
                if time.monotonic() >= deadline:
                    if log_fn:
                        log_fn("SenseVoice 本地识别超时，识别进程已结束。")
                    self._terminate()
                    _remove_local_asr_outputs(srt_output)
                    return False
                try:
                    raw_line = self._lines.get(timeout=0.2)
                except queue.Empty:
                    raw_line = ""
                if raw_line is None:
                    if log_fn:
                        log_fn(_local_asr_exit_diagnostic(proc.poll()))
                    self._terminate()
                    _remove_local_asr_outputs(srt_output)
                    return False
                if not raw_line:
                    if proc.poll() is not None:
                        if log_fn:
                            log_fn(_local_asr_exit_diagnostic(proc.returncode))
                        self._terminate()
                        _remove_local_asr_outputs(srt_output)
                        return False
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    diagnostics.append(str(raw_line).strip())
                    diagnostics = diagnostics[-5:]
                    continue
                if not isinstance(payload, dict):
                    continue
                message_type = str(payload.get("type") or "").strip().lower()
                response_id = str(payload.get("id") or "")
                if message_type == "log" and (not response_id or response_id == request_id):
                    message = str(payload.get("message") or "").strip()
                    if message and log_fn:
                        log_fn(message)
                elif message_type == "result" and response_id == request_id:
                    result_ok = bool(payload.get("ok"))

            srt_path = Path(srt_output)
            succeeded = bool(result_ok) and srt_path.is_file() and srt_path.stat().st_size > 0
            if not succeeded:
                if log_fn:
                    for diagnostic in diagnostics:
                        log_fn(f"SenseVoice 诊断: {diagnostic[:300]}")
                    log_fn("SenseVoice 批量识别未生成可用字幕。")
                _remove_local_asr_outputs(srt_output)
            return succeeded

    def close(self, log_fn: Callable[[str], None] | None = None) -> None:
        """Stop the process even after a failed/cancelled batch."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._proc
            if proc is not None and proc.poll() is None and proc.stdin is not None:
                try:
                    proc.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
                    proc.stdin.flush()
                    proc.wait(timeout=8)
                except (OSError, subprocess.SubprocessError, ValueError):
                    pass
            self._terminate()
            if log_fn:
                log_fn("SenseVoice 批量识别进程已退出，本地模型内存已释放。")


def _run_local_asr_worker(
    audio_path: str,
    srt_output: str,
    log_fn: Callable[[str], None] | None = None,
    cancel_event: _CancelEvent | None = None,
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
            if cancel_event is not None and cancel_event.is_set():
                proc.kill()
                proc.wait(timeout=10)
                _log("SenseVoice 本地识别已停止。")
                _remove_local_asr_outputs(srt_output)
                return False
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
            _log(_local_asr_exit_diagnostic(return_code))
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
    cancel_event: _CancelEvent | None = None,
    worker_session: LocalASRWorkerSession | None = None,
) -> bool:
    """Transcribe through SenseVoice only; never silently switch engines."""
    def _log(message: str) -> None:
        if log_fn:
            log_fn(message)

    selected_engine = str(asr_engine or "sensevoice").strip().lower()
    if selected_engine not in {"", "auto", "sensevoice"}:
        _log("本地识别仅支持 SenseVoice，已忽略过期的本地模型设置。")
    if worker_session is not None:
        return worker_session.transcribe(
            audio_path,
            srt_output,
            log_fn=log_fn,
            cancel_event=cancel_event,
        )
    return _run_local_asr_worker(
        audio_path,
        srt_output,
        log_fn=log_fn,
        cancel_event=cancel_event,
    )


def generate_srt(
    video_path: str,
    log_fn: Callable[[str], None] | None = None,
    asr_engine: str = "sensevoice",
    cancel_event: _CancelEvent | None = None,
) -> str | None:
    """Generate an SRT sidecar with SenseVoice, or return ``None`` on failure."""
    temp_dir = os.path.join(tempfile.gettempdir(), "live_cutter_stt")
    os.makedirs(temp_dir, exist_ok=True)
    video_hash = hashlib.md5(video_path.encode("utf-8")).hexdigest()[:8]
    wav_path = os.path.join(temp_dir, f"audio_{video_hash}.wav")
    srt_path = os.path.join(temp_dir, f"sub_{video_hash}.srt")

    try:
        if not extract_audio(video_path, wav_path, log_fn, cancel_event=cancel_event):
            return None
        if not transcribe_local_audio_to_srt(
            wav_path,
            srt_path,
            log_fn=log_fn,
            asr_engine=asr_engine,
            cancel_event=cancel_event,
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
    if not srt_path:
        return
    path = Path(srt_path)
    for target in (
        path,
        path.with_suffix(".words.json"),
        path.with_suffix(".asr-cache.json"),
        path.with_suffix(".asr_quality.json"),
    ):
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
