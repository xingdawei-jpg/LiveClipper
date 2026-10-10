"""SenseVoice local ASR normalized to LiveClipper's SRT + timing contract.

Consumers read ``<subtitle>.words.json`` through ``volcengine_asr`` without
depending on the local model implementation.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
import threading
from typing import Any, Callable

from local_asr_chunking import (
    AudioChunk,
    AudioChunkWriter,
    SHORT_FORM_MAX_CHUNK_SECONDS,
    SHORT_FORM_TARGET_CHUNK_SECONDS,
    build_pause_aware_audio_chunks,
)


_SENSEVOICE_MODEL: Any | None = None
_SENSEVOICE_PUNCTUATION = False
_SENSEVOICE_DEVICE = "cpu"
_SENSEVOICE_DEVICE_REQUEST = "auto"
_SENSEVOICE_CPU_THREADS = 4
_MODEL_LOCK = threading.RLock()
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]+(?:['-][A-Za-z]+)*|\d+(?:[.,:]\d+)*")

# FunASR discovers registered components dynamically in a normal Python
# installation. PyInstaller does not expose every package file to that scan,
# so package the exact components used by SenseVoice and VAD and import them
# explicitly before AutoModel reads a model configuration.  Do not import the
# optional ct-punc stack here: it pulls in jieba and additional Torch modules,
# and a native crash in that optional path cannot be recovered by the worker.
_SENSEVOICE_REGISTRATION_MODULES = (
    "funasr.tokenizer.sentencepiece_tokenizer",
    "funasr.tokenizer.char_tokenizer",
    "funasr.frontends.wav_frontend",
    "funasr.models.ctc.ctc",
    "funasr.models.paraformer.search",
    "funasr.models.sense_voice.model",
    "funasr.models.fsmn_vad_streaming.encoder",
    "funasr.models.fsmn_vad_streaming.model",
    "funasr.models.sanm.encoder",
    "funasr.models.specaug.specaug",
)
_SENSEVOICE_REQUIRED_COMPONENTS = (
    ("tokenizer_classes", "SentencepiecesTokenizer"),
    ("tokenizer_classes", "CharTokenizer"),
    ("frontend_classes", "WavFrontend"),
    ("frontend_classes", "WavFrontendOnline"),
    ("encoder_classes", "SenseVoiceEncoderSmall"),
    ("encoder_classes", "FSMN"),
    ("encoder_classes", "SANMEncoder"),
    ("model_classes", "SenseVoiceSmall"),
    ("model_classes", "FsmnVADStreaming"),
    ("specaug_classes", "SpecAugLFR"),
)


class LocalASRUnavailable(RuntimeError):
    """The requested optional local ASR runtime or its model is unavailable."""


def _local_asr_runtime_settings() -> dict[str, Any]:
    """Read only the small ASR preferences without importing the AI pipeline.

    The isolated worker can call this while a test or a running task has a
    partially imported ``ai_clipper`` module.  Importing that full pipeline
    here is both unnecessary and unsafe: it creates a large second import
    graph while native ASR objects are being collected or initialized.
    """
    try:
        from config import SETTINGS_PATH

        with Path(SETTINGS_PATH).open("r", encoding="utf-8-sig") as handle:
            settings = json.load(handle)
        return dict(settings) if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _cuda_is_available() -> bool:
    """Return whether this bundled Torch runtime can actually use CUDA."""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _resolve_sensevoice_device() -> tuple[str, str]:
    """Choose the safe user-facing preference and its currently usable device."""
    preference = str(_local_asr_runtime_settings().get("local_asr_device") or "auto").strip().lower()
    if preference not in {"auto", "cpu"}:
        preference = "auto"
    if preference == "auto" and _cuda_is_available():
        return preference, "cuda:0"
    return preference, "cpu"


def _sensevoice_cpu_threads() -> int:
    """Bound FunASR's CPU pool so it remains fast without starving the desktop."""
    configured = _local_asr_runtime_settings().get("local_asr_cpu_threads", 0)
    try:
        configured_threads = int(configured)
    except (TypeError, ValueError):
        configured_threads = 0
    if configured_threads > 0:
        return min(8, max(1, configured_threads))
    logical_cpus = max(1, int(os.cpu_count() or 4))
    return min(8, max(4, logical_cpus // 2))


def _is_cuda_runtime_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in (
        "cuda", "cudnn", "cublas", "gpu", "device-side", "out of memory",
    ))


def _release_sensevoice_model() -> None:
    """Forget the cached model before a one-time GPU-to-CPU recovery."""
    global _SENSEVOICE_MODEL, _SENSEVOICE_PUNCTUATION, _SENSEVOICE_DEVICE
    _SENSEVOICE_MODEL = None
    _SENSEVOICE_PUNCTUATION = False
    _SENSEVOICE_DEVICE = "cpu"
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def _run_modelscope_quietly(operation: Callable[[], Any]) -> Any:
    """Run ModelScope work without tqdm writing to a windowed EXE handle."""
    with open(os.devnull, "w", encoding="utf-8") as null_stream:
        with redirect_stdout(null_stream), redirect_stderr(null_stream):
            return operation()


def _register_sensevoice_components() -> None:
    """Load FunASR registry modules required by the three local models."""
    original_getsourcelines = inspect.getsourcelines

    def _frozen_safe_getsourcelines(target: object):
        try:
            return original_getsourcelines(target)
        except OSError:
            # FunASR records source locations as registry metadata. PyInstaller
            # keeps these modules in a bytecode archive, where source text is
            # intentionally unavailable; the metadata is not used for ASR.
            return ([""], 0)

    try:
        inspect.getsourcelines = _frozen_safe_getsourcelines
        try:
            for module_name in _SENSEVOICE_REGISTRATION_MODULES:
                importlib.import_module(module_name)
            from funasr.register import tables
        finally:
            inspect.getsourcelines = original_getsourcelines
    except Exception as exc:
        raise LocalASRUnavailable(f"SenseVoice runtime component import failed: {exc}") from exc

    missing = [
        name
        for table_name, name in _SENSEVOICE_REQUIRED_COMPONENTS
        if not getattr(tables, table_name, {}).get(name)
    ]
    if missing:
        raise LocalASRUnavailable(
            "SenseVoice runtime is incomplete; missing registered components: "
            + ", ".join(missing)
        )


def _normalize_punctuation_cluster(match: re.Match[str]) -> str:
    cluster = match.group(0)
    if "？" in cluster or "?" in cluster:
        return "？"
    if "！" in cluster or "!" in cluster:
        return "！"
    if "。" in cluster:
        return "。"
    if "；" in cluster or ";" in cluster:
        return "；"
    if "：" in cluster or ":" in cluster:
        return "："
    return "，"


def _clean_text(text: object) -> str:
    """Remove rich tags and normalize punctuation without changing words."""
    value = re.sub(r"<\s*\|\s*[^<>]*?\s*\|\s*>", "", str(text or ""))
    # ct-punc can insert a space inside decimal numbers (for example 1. 7).
    value = re.sub(r"(?<=\d)\.\s+(?=\d)", ".", value)
    value = re.sub(r"(?<!\d)\.(?!\d)|(?<=\d)\.(?!\d)", "。", value)
    value = re.sub(
        r"[。！？!?，,；;：:、](?:\s*[。！？!?，,；;：:、])+",
        _normalize_punctuation_cluster,
        value,
    )
    return value.strip()


def _as_seconds(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # FunASR/SenseVoice exposes timestamps in milliseconds, including short
    # values such as 750 ms. The LiveClipper provider contract is seconds.
    return number / 1000.0


def _format_srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def _cached_sensevoice_model_dir(configured: str = "") -> str | None:
    """Return a complete ModelScope cache path without starting a download."""
    candidates = [Path(configured)] if configured else []
    cache_roots = [
        os.environ.get("MODELSCOPE_CACHE", "").strip(),
        os.environ.get("MODELSCOPE_HOME", "").strip(),
        str(Path.home() / ".cache" / "modelscope"),
    ]
    for raw_root in cache_roots:
        if not raw_root:
            continue
        root = Path(raw_root)
        model_root = root / "models" / "iic--SenseVoiceSmall"
        candidates.extend((model_root, root / "iic--SenseVoiceSmall"))
        if model_root.is_dir():
            candidates.extend(path for path in model_root.glob("snapshots/*") if path.is_dir())

    for candidate in candidates:
        if candidate.is_file():
            candidate = candidate.parent
        if (candidate / "model.pt").is_file():
            return str(candidate.resolve())
    return None


def _modelscope_cache_roots() -> list[Path]:
    """ModelScope cache roots that may already hold a downloaded snapshot."""
    roots: list[str] = [
        os.environ.get("MODELSCOPE_CACHE", "").strip(),
        os.environ.get("MODELSCOPE_HOME", "").strip(),
        str(Path.home() / ".cache" / "modelscope"),
    ]
    return [Path(root) for root in roots if root]


def _cached_modelscope_model_dir(repo_id: str, marker: str = "model.pt") -> str | None:
    """Return a complete local snapshot path for a ModelScope repo id.

    FunASR asks ModelScope to resolve a bare model name even when the snapshot
    is already on disk.  That request fails whenever the machine is offline or
    the Windows system proxy points at a dead port, and it then surfaces as a
    misleading "model ... is not registered" error.  Resolving to the local
    folder keeps loading fully offline; callers fall back to the original name
    when nothing is cached yet, so a first run still downloads normally.
    """
    repo = str(repo_id or "").strip()
    if "/" not in repo:
        return None
    folder = repo.replace("/", "--")
    for root in _modelscope_cache_roots():
        model_root = root / "models" / folder
        candidates = [model_root, root / folder]
        if model_root.is_dir():
            candidates.extend(path for path in model_root.glob("snapshots/*") if path.is_dir())
        for candidate in candidates:
            if candidate.is_file():
                candidate = candidate.parent
            if (candidate / marker).is_file():
                return str(candidate.resolve())
    return None


_LOCAL_ASR_MODEL_IDS = {
    "fsmn-vad": "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    "ct-punc": "iic/punc_ct-transformer_cn-en-common-vocab471067-large",
}


def _prefer_cached_model(model: str) -> str:
    """Prefer an on-disk snapshot so loading never depends on the network."""
    repo_id = _LOCAL_ASR_MODEL_IDS.get(model, model)
    return _cached_modelscope_model_dir(repo_id) or model


def _sensevoice_model_dir(log_fn: Callable[[str], None] | None = None) -> str:
    """Download once and return a SentencePiece-safe model path.

    Some Windows SentencePiece builds cannot open a path containing Chinese
    characters, although Python can. A local NTFS junction gives the native
    library an ASCII-only view without copying the 936 MB model.
    """
    configured = os.environ.get("SENSEVOICE_MODEL_DIR", "").strip()
    model_dir = _cached_sensevoice_model_dir(configured)
    if not model_dir:
        try:
            from modelscope import snapshot_download

            # The frozen desktop executable has no valid stderr handle. Newer
            # ModelScope emits a tqdm progress bar during first download, which
            # otherwise raises WinError 22 before the model is cached.
            model_dir = _run_modelscope_quietly(
                lambda: snapshot_download("iic/SenseVoiceSmall")
            )
        except Exception as exc:
            raise LocalASRUnavailable(f"SenseVoice model download failed: {exc}") from exc
    if model_dir.isascii():
        return model_dir

    alias = os.path.join(os.environ.get("SENSEVOICE_ASCII_CACHE", "C:" + chr(92) + "tmp"), "LiveClipperSenseVoice")
    expected = os.path.join(alias, "chn_jpn_yue_eng_ko_spectok.bpe.model")
    if os.path.isfile(expected):
        return alias
    try:
        os.makedirs(os.path.dirname(alias), exist_ok=True)
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", alias, model_dir],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0 and os.path.isfile(expected):
            if log_fn:
                log_fn("SenseVoice model cache mapped to an ASCII-safe local path")
            return alias
    except Exception:
        pass
    raise LocalASRUnavailable(
        "SenseVoice cache path contains non-ASCII characters and could not be mapped. "
        "Set SENSEVOICE_MODEL_DIR to an ASCII-only model folder."
    )


def _load_sensevoice(
    log_fn: Callable[[str], None] | None = None,
    *,
    force_device: str | None = None,
):
    global _SENSEVOICE_MODEL, _SENSEVOICE_PUNCTUATION, _SENSEVOICE_DEVICE
    global _SENSEVOICE_DEVICE_REQUEST, _SENSEVOICE_CPU_THREADS
    # A loaded model is already bound to its execution device. Do not load
    # Torch merely to rediscover that device for every audio file.
    if _SENSEVOICE_MODEL is not None and force_device is None:
        return _SENSEVOICE_MODEL
    with _MODEL_LOCK:
        if _SENSEVOICE_MODEL is not None and force_device is None:
            return _SENSEVOICE_MODEL
        if force_device is not None:
            selected_device = force_device
            preference = "recovery"
        else:
            preference, selected_device = _resolve_sensevoice_device()
        if _SENSEVOICE_MODEL is not None and _SENSEVOICE_DEVICE == selected_device:
            return _SENSEVOICE_MODEL
        if _SENSEVOICE_MODEL is not None:
            _release_sensevoice_model()
        try:
            _register_sensevoice_components()
            from funasr.auto.auto_model import AutoModel
        except ImportError as exc:
            raise LocalASRUnavailable("SenseVoice runtime is not installed") from exc
        if log_fn:
            log_fn("正在加载本地 SenseVoice（首次使用会下载模型）...")
        cpu_threads = _sensevoice_cpu_threads()
        model_kwargs = {
            "model": _sensevoice_model_dir(log_fn),
            "vad_model": _prefer_cached_model("fsmn-vad"),
            "vad_kwargs": {"max_single_segment_time": 15000},
            "device": selected_device,
            "ncpu": cpu_threads,
            "output_timestamp": True,
            "disable_update": True,
        }
        try:
            _SENSEVOICE_MODEL = _run_modelscope_quietly(lambda: AutoModel(**model_kwargs))
            _SENSEVOICE_PUNCTUATION = False
            _SENSEVOICE_DEVICE = selected_device
            _SENSEVOICE_DEVICE_REQUEST = preference
            _SENSEVOICE_CPU_THREADS = cpu_threads
            if log_fn:
                log_fn("SenseVoice 已跳过可选标点模型，使用停顿断句")
        except Exception as exc:
            if selected_device.startswith("cuda") and preference == "auto":
                _release_sensevoice_model()
                if log_fn:
                    log_fn("SenseVoice CUDA 初始化失败，已自动回退 CPU：" + str(exc))
                return _load_sensevoice(log_fn, force_device="cpu")
            try:
                debug_log = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "LiveClipper", "sensevoice_error.log")
                os.makedirs(os.path.dirname(debug_log), exist_ok=True)
                with open(debug_log, "a", encoding="utf-8") as _df:
                    _df.write(f"--- SenseVoice AutoModel FAILED at {datetime.now()} ---\n")
                    _df.write("Error: {}\n".format(exc))
                    _df.write("cwd: {}\n".format(os.getcwd()))
                    _df.write("sys.path: {}\n".format(sys.path))
                    traceback.print_exc(file=_df)
            except Exception:
                pass
            raise LocalASRUnavailable(f"SenseVoice model unavailable: {exc}") from exc
    return _SENSEVOICE_MODEL


def _words_from_timestamp(text: str, raw_timestamp: object) -> list[dict[str, Any]]:
    """Turn FunASR CTC alignments into the provider-neutral word schema.

    SenseVoice returns CTC-aligned text units.  Chinese units are deliberately
    stored as individual characters: inventing a word split would make a cut
    look more precise than it is.  English/alphanumeric units are kept whole.
    """
    if not isinstance(raw_timestamp, (list, tuple)):
        return []
    units = _TOKEN_RE.findall(text)
    pairs: list[tuple[float, float]] = []
    for item in raw_timestamp:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        start, end = _as_seconds(item[0]), _as_seconds(item[1])
        if start is not None and end is not None and end > start:
            pairs.append((start, end))
    # Never fabricate timings. A mismatch means this FunASR/runtime version
    # did not return alignments in the expected token granularity.
    if not units or len(units) != len(pairs):
        return []
    return [
        {"text": unit, "start": round(start, 3), "end": round(end, 3)}
        for unit, (start, end) in zip(units, pairs)
    ]


def _sensevoice_is_decimal_point(words: list[object] | tuple[object, ...], index: int) -> bool:
    """A standalone dot is speech only when it joins two Arabic digits."""
    if index <= 0 or index + 1 >= len(words):
        return False
    previous_text = str(words[index - 1] or "").strip()
    following_text = str(words[index + 1] or "").strip()
    return bool(previous_text[-1:].isdigit() and following_text[:1].isdigit())


def _sensevoice_segments(result: object, time_offset: float = 0.0) -> list[dict[str, Any]]:
    records = result if isinstance(result, list) else [result]
    segments: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        sentences = record.get("sentence_info") or [record]
        for sentence in sentences:
            if not isinstance(sentence, dict):
                continue
            text = _clean_text(sentence.get("text"))
            raw_timestamp = sentence.get("timestamp")
            pairs: list[tuple[float, float]] = []
            if isinstance(raw_timestamp, (list, tuple)):
                for item in raw_timestamp:
                    if isinstance(item, (list, tuple)) and len(item) >= 2:
                        start_value, end_value = _as_seconds(item[0]), _as_seconds(item[1])
                        if start_value is not None and end_value is not None and end_value > start_value:
                            pairs.append((start_value, end_value))
            raw_words = sentence.get("words")
            if isinstance(raw_words, (list, tuple)) and len(raw_words) == len(pairs):
                words = []
                for word_index, (token, (start_value, end_value)) in enumerate(zip(raw_words, pairs)):
                    raw_token_text = str(token or "").strip()
                    if raw_token_text == "." and not _sensevoice_is_decimal_point(raw_words, word_index):
                        continue
                    # ct-punc returns punctuation as separately timed tokens.
                    # Keep it in sentence text, not in the spoken-word sidecar.
                    spoken_text = str(token or "").strip(" \t\r\n。！？!?，,；;：:、“”’\"'（）()【】[]《》<>…—·_/\\{}")
                    if spoken_text:
                        words.append({
                            "text": spoken_text,
                            "start": round(start_value + time_offset, 3),
                            "end": round(end_value + time_offset, 3),
                        })
            else:
                words = _words_from_timestamp(text, raw_timestamp)
                if time_offset and words:
                    words = [
                        {
                            **word,
                            "start": round(float(word["start"]) + time_offset, 3),
                            "end": round(float(word["end"]) + time_offset, 3),
                        }
                        for word in words
                    ]
            start = _as_seconds(sentence.get("start"))
            end = _as_seconds(sentence.get("end"))
            if start is None and words:
                start = words[0]["start"]
            elif start is not None:
                start += time_offset
            if end is None and words:
                end = words[-1]["end"]
            elif end is not None:
                end += time_offset
            if not text or start is None or end is None or end <= start:
                continue
            segments.append({"text": text, "start": round(start, 3), "end": round(end, 3), "words": words})
    return segments


def _generate_sensevoice(model: Any, audio_input: str | list[str]) -> object:
    """Keep all SenseVoice inference arguments identical for every chunk."""
    return model.generate(
        input=audio_input,
        cache={},
        language="zh",
        use_itn=True,
        # FunASR reads this at inference time. Keeping it only on AutoModel
        # construction is not reliable across bundled runtime versions, and
        # then an SRT is produced without CTC alignments.
        output_timestamp=True,
        batch_size_s=120,
        merge_vad=False,
    )


def _sensevoice_uses_cuda() -> bool:
    return _SENSEVOICE_DEVICE.startswith("cuda")


def _append_sensevoice_chunk_result(
    segments: list[dict[str, Any]],
    result: object,
    chunk: AudioChunk,
    index: int,
    total: int,
    log_fn: Callable[[str], None] | None,
) -> None:
    chunk_segments = _sensevoice_segments(result, time_offset=float(chunk.start))
    if not chunk_segments:
        # A pause-only tail may legitimately yield no tokens. Do not turn that
        # into a failed job or fabricate a transcript.
        if log_fn:
            log_fn(f"SenseVoice 音频分段 {index}/{total} 未检测到语音，已跳过")
        return
    segments.extend(chunk_segments)


def _sensevoice_pause_aware_segments(
    model: Any,
    audio_path: str,
    log_fn: Callable[[str], None] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Run local ASR on pause-aware WAV chunks and restore global timestamps."""
    chunks = build_pause_aware_audio_chunks(audio_path)
    if not chunks:
        return _sensevoice_segments(_generate_sensevoice(model, audio_path)), 1, 0
    if len(chunks) == 1:
        return _sensevoice_segments(_generate_sensevoice(model, audio_path)), 1, 0

    hard_boundaries = sum(1 for chunk in chunks if chunk.boundary_reason == "hard_limit")
    if log_fn:
        suffix = f"，其中 {hard_boundaries} 处无静音按时长切分" if hard_boundaries else ""
        log_fn(
            "SenseVoice 短视频停顿分段: "
            f"{len(chunks)} 段（目标{SHORT_FORM_TARGET_CHUNK_SECONDS:.0f}s，"
            f"最长{SHORT_FORM_MAX_CHUNK_SECONDS:.0f}s）{suffix}"
        )

    segments: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="liveclipper_sensevoice_") as temp_dir:
        with AudioChunkWriter(audio_path) as writer:
            chunk_paths: list[str] = []
            for index, chunk in enumerate(chunks, 1):
                chunk_path = str(Path(temp_dir) / f"chunk_{index:04d}.wav")
                writer.write(chunk_path, chunk)
                chunk_paths.append(chunk_path)

            if _sensevoice_uses_cuda():
                # FunASR accepts a list of inputs. Keeping these batches small
                # limits VRAM pressure while reducing CUDA/VAD dispatch
                # overhead. Every input remains the original 7–10 second
                # interval, so CTC time restoration is unchanged.
                batch_size = 8
                for batch_start in range(0, len(chunks), batch_size):
                    batch_chunks = chunks[batch_start:batch_start + batch_size]
                    batch_paths = chunk_paths[batch_start:batch_start + batch_size]
                    batch_result = _generate_sensevoice(model, batch_paths)
                    if not isinstance(batch_result, list) or len(batch_result) != len(batch_chunks):
                        if log_fn:
                            log_fn("SenseVoice GPU 批量结果不完整，当前批次已改为逐段识别")
                        batch_results = [_generate_sensevoice(model, path) for path in batch_paths]
                    else:
                        batch_results = batch_result
                    for offset, (chunk, result) in enumerate(zip(batch_chunks, batch_results), 1):
                        index = batch_start + offset
                        _append_sensevoice_chunk_result(
                            segments, result, chunk, index, len(chunks), log_fn,
                        )
            else:
                for index, (chunk, chunk_path) in enumerate(zip(chunks, chunk_paths), 1):
                    _append_sensevoice_chunk_result(
                        segments,
                        _generate_sensevoice(model, chunk_path),
                        chunk,
                        index,
                        len(chunks),
                        log_fn,
                    )

    segments.sort(key=lambda segment: (float(segment["start"]), float(segment["end"])))
    return segments, len(chunks), hard_boundaries


def _local_asr_review_settings() -> dict[str, Any]:
    """Read review preferences through the same lightweight settings reader."""
    return _local_asr_runtime_settings()


def _offset_cloud_retry_segments(
    raw_segments: object,
    finding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Translate a retry response back to its original audio window."""
    offset = float(finding["start"])
    window_end = float(finding["end"])
    restored: list[dict[str, Any]] = []
    for raw_segment in list(raw_segments or []):
        if not isinstance(raw_segment, dict):
            continue
        try:
            start = offset + float(raw_segment.get("start"))
            end = offset + float(raw_segment.get("end"))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        words = []
        for raw_word in list(raw_segment.get("words") or []):
            if not isinstance(raw_word, dict):
                continue
            try:
                word_start = offset + float(raw_word.get("start"))
                word_end = offset + float(raw_word.get("end"))
            except (TypeError, ValueError):
                continue
            if word_end <= word_start:
                continue
            words.append({
                **raw_word,
                "start": round(max(offset, word_start), 3),
                "end": round(min(window_end, word_end), 3),
            })
        restored.append({
            **raw_segment,
            "start": round(max(offset, start), 3),
            "end": round(min(window_end, end), 3),
            "words": words,
        })
    return restored


def _retry_sensevoice_window_with_volcengine(
    audio_path: str,
    finding: dict[str, Any],
    settings: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Retry one exact local-audio window with the configured word-timed cloud ASR."""
    start = float(finding["start"])
    end = float(finding["end"])
    if end - start < 1.0:
        return None
    with tempfile.TemporaryDirectory(prefix="liveclipper_asr_review_") as temp_dir:
        retry_audio = str(Path(temp_dir) / "review.wav")
        write_audio_chunk(audio_path, retry_audio, AudioChunk(start, end, "quality_retry"))
        from volcengine_asr import volcengine_asr

        retried = volcengine_asr(
            retry_audio,
            str(settings.get("volc_app_id") or ""),
            str(settings.get("volc_access_token") or ""),
            str(settings.get("volc_tos_ak") or ""),
            str(settings.get("volc_tos_sk") or ""),
            bucket=str(settings.get("volc_bucket") or "livec"),
            region=str(settings.get("volc_region") or "cn-beijing"),
            timeout=120,
            api_key=str(settings.get("volc_api_key") or "") or None,
        )
    return _offset_cloud_retry_segments(retried, finding)


def _review_sensevoice_segments(
    segments: list[dict[str, Any]],
    audio_path: str,
    srt_output: str,
    log_fn: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Record ASR quality and safely replace only successful cloud retry windows."""
    from local_asr_review import cloud_retry_eligibility, review_segments, write_quality_report

    settings = _local_asr_review_settings()
    retry_enabled, retry_reason = cloud_retry_eligibility(settings)
    initial_segments, initial_report = review_segments(segments, retry_enabled=False)
    flagged_count = int((initial_report.get("initial") or {}).get("flagged_count") or 0)
    flagged_seconds = float((initial_report.get("initial") or {}).get("flagged_seconds") or 0.0)
    if log_fn and flagged_count:
        log_fn(f"本地 ASR 质量扫描: 发现 {flagged_count} 段可疑文本（约{flagged_seconds:.1f}s）")
    if retry_enabled and flagged_count and log_fn:
        log_fn("本地 ASR 局部复核: 使用已启用的云端词级识别，仅复核可疑时间段")

    retry_callback = None
    if retry_enabled:
        retry_callback = lambda finding: _retry_sensevoice_window_with_volcengine(
            audio_path,
            finding,
            settings,
        )
    reviewed, report = review_segments(
        initial_segments,
        retry_enabled=retry_enabled,
        retry_callback=retry_callback,
    )
    report["retry"]["eligibility"] = retry_reason
    report_path = write_quality_report(srt_output, report)
    retry = dict(report.get("retry") or {})
    if log_fn and int(retry.get("replaced_count") or 0):
        log_fn(
            "本地 ASR 局部复核完成: "
            f"已替换 {int(retry.get('replaced_count') or 0)} 段，"
            "全局词级时间保持在原音频范围内"
        )
    elif log_fn and flagged_count and not retry_enabled:
        log_fn(f"本地 ASR 局部复核未启用: {retry_reason}，保留原始识别结果")
    if log_fn and report_path is not None:
        log_fn(f"本地 ASR 质量报告已保存: {report_path.name}")
    return reviewed


# ---------------------------------------------------------------------------
# 单独语气词过滤（口径A）
# ---------------------------------------------------------------------------
# “嗯，”“呃，”“啊，”这类整条只有语气词的单元不是内容：丢了它，选片候选池
# 和成片字幕都不会再出现 0.2 秒的空话片段。只处理“整条”，以语气词开头但
# 内容完整的句子（如“嗯然后今天的新款羽绒…”）不受影响。

_FILLER_ONLY_CHARS = frozenset("嗯呃啊哦噢喔唉诶欸唔")
_FILLER_ONLY_MAX_CHARS = 3
_FILLER_ONLY_IGNORED = "，。！？、；：,.!?;:…~～—－-— \u3000\"'“”‘’()（）《》【】[]"


def _is_filler_only_text(text: object) -> bool:
    """整条只有语气词（可带标点/空白）时返回 True。"""
    compact = "".join(ch for ch in str(text or "") if ch not in _FILLER_ONLY_IGNORED)
    if not compact or len(compact) > _FILLER_ONLY_MAX_CHARS:
        return False
    return all(ch in _FILLER_ONLY_CHARS for ch in compact)


def _drop_filler_only_segments(
    segments: object,
    log_fn: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """丢弃整条只有语气词的单元，返回保留的单元（顺序不变）。"""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for segment in list(segments or []):
        if not isinstance(segment, dict):
            continue
        if _is_filler_only_text(segment.get("text")):
            dropped += 1
            continue
        kept.append(segment)
    if dropped and log_fn:
        log_fn(f"已剔除 {dropped} 条单独语气词单元（嗯/呃/啊等无实质内容）")
    return kept


def _apply_visible_resegmentation(segments, *, log_fn=None):
    """[默认关闭] 把可见字幕重分段为「可独立播放的完整语句」。

    背景：可见字幕来自 ASR 的声学分段，实测 172 条里有 53% 无法作为可独立播放的
    单元（59 条以逗号结尾、24 条以承接词开头、19 条以语气词结尾）。而
    ``local_asr.py`` 上方注释已声明「visible SRT 可以自由重分段，不影响切割精度」。

    两种模式互斥，用环境变量开启（都未开启时**原样返回**，行为与改动前一致）：

      LIVECLIPPER_DETERMINISTIC_CLOSURE=1   纯确定性闭合，**无 AI 调用**（推荐）
          起点是每条条目各自成组，只在播放闸门报违规处按规则合并。
          实测 172 -> 82 条，边界通过率 47.1% -> 100%。

      LIVECLIPPER_AI_SEGMENTATION=1         AI 语义分组 + 确定性闭合（需 2 次 AI 调用）
          实测 172 -> 62 条。**注意**：AI 分组粒度过粗会把下游可用候选从 48 降到 30，
          并触发 ``candidate_reused_by_plan``（见 ai_sentence_segmentation 模块注释）。

    安全性：任何失败/输出非法/未改善，都**原样返回入参**，绝不影响识别结果。
    """
    try:
        from ai_sentence_segmentation import (
            apply_ai_sentence_segmentation,
            apply_deterministic_closure,
            deterministic_closure_enabled,
            segmentation_ai_enabled,
        )

        ai_on = segmentation_ai_enabled()
        det_on = deterministic_closure_enabled()
        if not ai_on and not det_on:
            return segments

        if not ai_on:
            # 纯确定性：不需要 api_key / 设置，失败即原样返回。
            merged, report = apply_deterministic_closure(segments, log_fn=log_fn)
            if log_fn:
                log_fn(
                    "可见字幕重分段(确定性): "
                    f"applied={report.get('applied')} reason={report.get('reason')} "
                    f"{report.get('before', {}).get('unit_count')} -> "
                    f"{(report.get('after') or {}).get('unit_count')}"
                )
            return list(merged) if report.get("applied") else list(segments)

        try:
            from ai_clipper import load_settings

            settings = load_settings() or {}
        except Exception:
            return segments
        api_key = str(settings.get("api_key") or "").strip()
        if not api_key:
            if log_fn:
                log_fn("AI 语义重分段: 未配置 api_key，跳过（保留原分段）")
            return segments
        merged, report = apply_ai_sentence_segmentation(
            segments,
            api_key=api_key,
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            log_fn=log_fn,
        )
        if log_fn:
            log_fn(
                "可见字幕重分段(AI): "
                f"applied={report.get('applied')} reason={report.get('reason')} "
                f"{report.get('before', {}).get('unit_count')} -> "
                f"{(report.get('after') or {}).get('unit_count')}"
            )
        return list(merged) if report.get("applied") else list(segments)
    except Exception as exc:  # noqa: BLE001 - 重分段失败绝不能影响识别结果
        if log_fn:
            log_fn(f"可见字幕重分段异常，保留原分段: {type(exc).__name__}: {exc}")
        return segments


def sensevoice_to_srt(audio_path: str, srt_output: str, log_fn: Callable[[str], None] | None = None) -> bool:
    """Run local SenseVoice and persist SRT plus CTC-aligned word timings."""
    model = _load_sensevoice(log_fn)
    if log_fn:
        segmentation = "标点恢复" if _SENSEVOICE_PUNCTUATION else "停顿断句"
        execution = (
            "NVIDIA CUDA GPU"
            if _sensevoice_uses_cuda()
            else f"CPU {int(_SENSEVOICE_CPU_THREADS)} 线程"
        )
        log_fn(f"启动本地 SenseVoice 高质量识别（{segmentation} + CTC 时间对齐 + {execution}）...")
    try:
        segments, input_chunk_count, _hard_boundaries = _sensevoice_pause_aware_segments(
            model,
            audio_path,
            log_fn=log_fn,
        )
    except Exception as exc:
        if _sensevoice_uses_cuda() and _is_cuda_runtime_error(exc):
            if log_fn:
                log_fn("SenseVoice CUDA 识别异常，已自动回退 CPU 并重试本次音频：" + str(exc))
            _release_sensevoice_model()
            try:
                model = _load_sensevoice(log_fn, force_device="cpu")
                segments, input_chunk_count, _hard_boundaries = _sensevoice_pause_aware_segments(
                    model,
                    audio_path,
                    log_fn=log_fn,
                )
            except Exception as cpu_exc:
                raise LocalASRUnavailable(f"SenseVoice CPU recovery failed: {cpu_exc}") from cpu_exc
        else:
            raise LocalASRUnavailable(f"SenseVoice inference failed: {exc}") from exc
    if not segments:
        raise LocalASRUnavailable("SenseVoice returned no timestamped speech segments")

    # Apply only deterministic, context-bound transcript repairs before the
    # quality scan.  Otherwise a known repaired token such as ``下0天40度``
    # would unnecessarily consume one of the very limited retry windows.
    from local_asr_quality import apply_domain_corrections, improve_sensevoice_segments

    segments, _ = apply_domain_corrections(segments, log_fn=log_fn)
    segments = _review_sensevoice_segments(segments, audio_path, srt_output, log_fn=log_fn)

    from volcengine_asr import semantic_segments_to_srt, write_word_timing_sidecar

    segments, semantic_segments = improve_sensevoice_segments(segments, log_fn=log_fn)
    # 单独语气词不是内容：整条丢弃（SRT 与词级 sidecar 同步过滤，保持两者一致）
    semantic_segments = _drop_filler_only_segments(semantic_segments, log_fn)
    srt_segments = _drop_filler_only_segments(segments, None)
    if semantic_segments:
        # [默认关闭] AI 语义重分段：把声学分段重组成「可独立播放的完整语句」。
        # 失败/未改善时原样返回，可见 SRT 与词级 sidecar 的契约不变。
        semantic_segments = _apply_visible_resegmentation(semantic_segments, log_fn=log_fn)
        srt_content = semantic_segments_to_srt(semantic_segments)
    else:
        srt_lines: list[str] = []
        for index, segment in enumerate(srt_segments, 1):
            srt_lines.extend((
                str(index),
                f"{_format_srt_time(segment['start'])} --> {_format_srt_time(segment['end'])}",
                segment["text"],
                "",
            ))
        srt_content = "\n".join(srt_lines)
    with open(srt_output, "w", encoding="utf-8") as handle:
        handle.write(srt_content)

    # Preserve corrected raw tokens in one provider-neutral sidecar. The
    # visible SRT can be resegmented freely without changing cutting precision.
    sidecar = write_word_timing_sidecar(
        srt_output, srt_segments, provider="sensevoice", log_fn=log_fn,
    )
    word_count = sum(len(segment["words"]) for segment in srt_segments)
    if log_fn:
        suffix = f"，{word_count} 个 CTC 对齐单元" if sidecar else "，未得到可用的词级对齐"
        visible_count = len(semantic_segments) if semantic_segments else len(srt_segments)
        punctuation = "标点恢复" if _SENSEVOICE_PUNCTUATION else "停顿断句"
        log_fn(
            f"SenseVoice 识别完成：{len(segments)} 条原始语音段"
            + (f"（音频输入{input_chunk_count}段）" if input_chunk_count > 1 else "")
            + " -> "
            f"{visible_count} 条字幕（{punctuation}）{suffix}"
        )
    return True


# ---------------------------------------------------------------------------
# 方案A（STEP 1 可选旁路）：Paraformer-large 模型级分段
# ---------------------------------------------------------------------------
# 默认不启用：只有显式选择 paraformer（参数或环境变量
# LIVECLIPPER_LOCAL_ASR_ENGINE）时才加载；SenseVoice 仍是默认引擎。
# 输出契约与 SenseVoice 一致：SRT + <srt>.words.json。

SENSEVOICE_ENGINE = "sensevoice"
PARAFORMER_ENGINE = "paraformer"

_ENV_LOCAL_ASR_ENGINE = "LIVECLIPPER_LOCAL_ASR_ENGINE"
_PARAFORMER_ASR_MODEL = (
    "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
)
_PARAFORMER_VAD_MODEL = "fsmn-vad"
_PARAFORMER_PUNC_MODEL = "ct-punc"
_PARAFORMER_EXTRA_MODULES = (
    "funasr.models.paraformer.model",
    "funasr.models.transformer.model",
    "funasr.models.ct_transformer.model",
)
_PARAFORMER_MODEL: Any | None = None


def resolve_local_asr_engine(engine: str | None = None) -> str:
    """Resolve the local engine name; saved setting first, then env, then SenseVoice."""
    requested = str(engine or "").strip().lower()
    if not requested or requested == "auto":
        try:
            from stt import saved_local_asr_engine

            requested = saved_local_asr_engine()
        except Exception:
            requested = ""
        if not requested:
            requested = str(os.environ.get(_ENV_LOCAL_ASR_ENGINE, "") or "").strip().lower()
    if requested in {"paraformer", "paraformer-large", "plan-a", "plan_a"}:
        return PARAFORMER_ENGINE
    return SENSEVOICE_ENGINE


def _register_paraformer_components() -> None:
    original_getsourcelines = inspect.getsourcelines

    def _frozen_safe_getsourcelines(target: object):
        try:
            return original_getsourcelines(target)
        except OSError:
            return ([""], 0)

    try:
        inspect.getsourcelines = _frozen_safe_getsourcelines
        try:
            for module_name in _SENSEVOICE_REGISTRATION_MODULES + _PARAFORMER_EXTRA_MODULES:
                importlib.import_module(module_name)
        finally:
            inspect.getsourcelines = original_getsourcelines
    except Exception as exc:
        raise LocalASRUnavailable(f"Paraformer runtime component import failed: {exc}") from exc


def _release_paraformer_model() -> None:
    global _PARAFORMER_MODEL
    with _MODEL_LOCK:
        _PARAFORMER_MODEL = None
    try:
        import gc

        gc.collect()
    except Exception:
        pass


def _load_paraformer(
    log_fn: Callable[[str], None] | None = None,
    *,
    force_device: str | None = None,
):
    global _PARAFORMER_MODEL
    _install_funasr_frozen_shims(log_fn)
    if _PARAFORMER_MODEL is not None and force_device is None:
        return _PARAFORMER_MODEL
    with _MODEL_LOCK:
        if _PARAFORMER_MODEL is not None and force_device is None:
            return _PARAFORMER_MODEL
        if force_device is not None:
            selected_device = force_device
            preference = "recovery"
        else:
            preference, selected_device = _resolve_sensevoice_device()
        try:
            _register_paraformer_components()
            from funasr.auto.auto_model import AutoModel
        except ImportError as exc:
            raise LocalASRUnavailable("Paraformer runtime is not installed") from exc
        if log_fn:
            log_fn("正在加载本地 Paraformer（首次使用会下载模型）...")
        configured_dir = os.environ.get("LIVECLIPPER_PARAFORMER_MODEL_DIR", "").strip()
        model_kwargs = {
            "model": configured_dir or _prefer_cached_model(_PARAFORMER_ASR_MODEL),
            "vad_model": _prefer_cached_model(_PARAFORMER_VAD_MODEL),
            "vad_kwargs": {"max_single_segment_time": 15000},
            "punc_model": _prefer_cached_model(_PARAFORMER_PUNC_MODEL),
            "device": selected_device,
            "ncpu": int(_sensevoice_cpu_threads()),
            "output_timestamp": True,
            "disable_update": True,
        }
        try:
            _PARAFORMER_MODEL = _run_modelscope_quietly(lambda: AutoModel(**model_kwargs))
            if log_fn:
                log_fn("Paraformer 已就绪（模型级分段 + 标点恢复）")
        except Exception as exc:
            if selected_device.startswith("cuda") and preference == "auto":
                _release_paraformer_model()
                if log_fn:
                    log_fn("Paraformer CUDA 初始化失败，已自动回退 CPU：" + str(exc))
                return _load_paraformer(log_fn, force_device="cpu")
            raise LocalASRUnavailable(f"Paraformer model unavailable: {exc}") from exc
    return _PARAFORMER_MODEL


def _paraformer_sentence_segments(payload: object) -> list[dict[str, Any]]:
    """Map one Paraformer result to sentence-level segments (方案A 原样分段)."""
    if not isinstance(payload, dict):
        return []
    segments: list[dict[str, Any]] = []
    sentences = payload.get("sentence_info")
    if isinstance(sentences, (list, tuple)) and sentences:
        for item in sentences:
            if not isinstance(item, dict):
                continue
            text = _clean_text(item.get("text"))
            start = _as_seconds(item.get("start"))
            end = _as_seconds(item.get("end"))
            if not text or start is None or end is None or end <= start:
                continue
            words = _words_from_timestamp(text, item.get("timestamp"))
            if words:
                start = words[0]["start"]
                end = words[-1]["end"]
            else:
                # 段级时间戳可用但字级对齐缺失：保留模型自己的句子跨度，
                # 否则该句在下游词级 sidecar 里会整条消失。
                words = [{"text": text, "start": round(start, 3), "end": round(end, 3)}]
            segments.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": text,
                "words": words,
            })
        if segments:
            return segments
    text = _clean_text(payload.get("text"))
    words = _words_from_timestamp(text, payload.get("timestamp"))
    if text and words:
        return [{
            "start": words[0]["start"],
            "end": words[-1]["end"],
            "text": text,
            "words": words,
        }]
    return []


# --- FunASR 句级后处理越界保护（Paraformer 专用）--------------------------
# funasr 的 sentence_postprocess() 拼句时直接按编号取时间戳
# （postprocess_utils.py 的 begin = time_stamp[i][0]），而它的时间戳来自 CIF
# 峰值触发，偶尔会比 token 数少一条（实测 900 秒素材约 0.5%/段）。少一条就是
# IndexError，整条本地识别直接失败；funasr 1.3.16 / 1.4.16 都没有长度检查。
# 这里在本地识别入口装一层保护：先走 funasr 原实现，只有真的越界时才切到等价
# 的安全实现继续跑，其余情况行为完全不变（实测 196 次调用仅 1 次接管）。
# 置为 False 即恢复原状。
PARAFORMER_SENTENCE_POSTPROCESS_GUARD = True
# Paraformer 万一仍然失败（模型加载失败 / 推理异常 / 无可用分段），
# 带日志自动改用本地 SenseVoice 重跑，最坏只是慢一点，不至于整条任务失败。
PARAFORMER_FALLBACK_TO_SENSEVOICE = True
_PARAFORMER_POSTPROCESS_GUARD_INSTALLED = False
_PARAFORMER_POSTPROCESS_GUARD_TAKEOVERS = 0
# 冻结核心（PyInstaller 打包的 LiveClipperHost.exe）只把 SenseVoice 那一支的
# funasr 模块收进了 PYZ，Paraformer 那一支的模块不在包内。下列模块随业务包一起
# 分发（app/funasr_compat/），本地识别启动时按标准模块名补挂进 sys.modules，
# 再登记组件；环境自带这些模块时（源码模式）完全不干预。置 False 即关闭。
PARAFORMER_FROZEN_MODULE_SHIM = True
_FUNASR_FROZEN_SHIM_INSTALLED = False
_FUNASR_FROZEN_SHIM_MODULES = (
    ("funasr.utils.postprocess_utils", "utils/postprocess_utils.py"),
    ("funasr.losses.cr_ctc", "losses/cr_ctc.py"),
    ("funasr.models.transformer.utils.add_sos_eos",
     "models/transformer/utils/add_sos_eos.py"),
    ("funasr.models.transformer.scorers.ctc_prefix_score",
     "models/transformer/scorers/ctc_prefix_score.py"),
    ("funasr.models.transformer.scorers.ctc", "models/transformer/scorers/ctc.py"),
    ("funasr.models.transformer.scorers.length_bonus",
     "models/transformer/scorers/length_bonus.py"),
    ("funasr.models.transformer.search", "models/transformer/search.py"),
    ("funasr.models.transformer.model", "models/transformer/model.py"),
    ("funasr.models.paraformer.export_meta", "models/paraformer/export_meta.py"),
    ("funasr.models.paraformer.cif_predictor", "models/paraformer/cif_predictor.py"),
    ("funasr.models.paraformer.model", "models/paraformer/model.py"),
)


def _paraformer_safe_sentence_postprocess(words: Any, time_stamp: Any = None):
    """funasr sentence_postprocess 的等价实现 + 时间戳越界保护。"""
    import funasr.utils.postprocess_utils as postprocess_utils

    is_all_chinese = getattr(postprocess_utils, "isAllChinese", None)
    is_all_alpha = getattr(postprocess_utils, "isAllAlpha", None)
    abbr_dispose = getattr(postprocess_utils, "abbr_dispose", None)
    specials = ("<s>", "</s>", "<unk>", "<OOV>")

    middle_lists = []
    for item in words:
        word = item if isinstance(item, str) else item.decode("utf-8")
        if word not in specials:
            middle_lists.append(word)

    word_lists: list[str] = []
    ts_lists: list[Any] = []
    word_item = ""
    begin: Any = 0
    end: Any = 0

    def stamp_at(index: int):
        """取第 index 个 token 的时间戳；越界时沿用上一条（通常只差一两条）。"""
        if time_stamp is None:
            return None
        if index < len(time_stamp):
            return time_stamp[index][0], time_stamp[index][1]
        if len(time_stamp) > 0:
            return time_stamp[-1][0], time_stamp[-1][1]
        return None

    if is_all_chinese and is_all_chinese(middle_lists):
        for char in middle_lists:
            word_lists.append(char.replace(" ", ""))
        if time_stamp is not None:
            ts_lists = list(time_stamp)
    elif is_all_alpha and is_all_alpha(middle_lists):
        ts_flag = True
        for index, char in enumerate(middle_lists):
            if ts_flag and time_stamp is not None:
                stamp = stamp_at(index)
                if stamp is not None:
                    begin, end = stamp
            if "@@" in char:
                word_item += char.replace("@@", "")
                if time_stamp is not None:
                    ts_flag = False
                    stamp = stamp_at(index)
                    if stamp is not None:
                        end = stamp[1]
            else:
                word_item += char
                word_lists.append(word_item)
                word_lists.append(" ")
                word_item = ""
                if time_stamp is not None:
                    ts_flag = True
                    stamp = stamp_at(index)
                    if stamp is not None:
                        end = stamp[1]
                    ts_lists.append([begin, end])
                    begin = end
    else:
        alpha_blank = False
        for index, char in enumerate(middle_lists):
            if time_stamp is not None:
                stamp = stamp_at(index)
                if stamp is not None:
                    begin, end = stamp
            if is_all_chinese and is_all_chinese(char):
                if alpha_blank is True and word_lists:
                    word_lists.pop()
                word_lists.append(char)
                alpha_blank = False
                if time_stamp is not None:
                    ts_lists.append([begin, end])
                    begin = end
            elif "@@" in char:
                word_item += char.replace("@@", "")
                alpha_blank = False
                if time_stamp is not None:
                    stamp = stamp_at(index)
                    if stamp is not None:
                        end = stamp[1]
            elif is_all_alpha and is_all_alpha(char):
                word_item += char
                word_lists.append(word_item)
                word_lists.append(" ")
                word_item = ""
                alpha_blank = True
                if time_stamp is not None:
                    stamp = stamp_at(index)
                    if stamp is not None:
                        end = stamp[1]
                    ts_lists.append([begin, end])
                    begin = end
            else:
                word_lists.append(char)

    if abbr_dispose is not None:
        if time_stamp is not None:
            word_lists, ts_lists = abbr_dispose(word_lists, ts_lists)
        else:
            word_lists = abbr_dispose(word_lists)
    if time_stamp is not None:
        real_word_lists = [item for item in word_lists if item != " "]
        return " ".join(real_word_lists).strip(), ts_lists, real_word_lists
    real_word_lists = [item for item in word_lists if item != " "]
    return "".join(word_lists).strip(), real_word_lists


def _paraformer_postprocess_guard(words: Any, time_stamp: Any = None):
    """原实现优先；只有它抛 IndexError 时才接管，可用场景的行为完全不变。"""
    global _PARAFORMER_POSTPROCESS_GUARD_TAKEOVERS
    import funasr.utils.postprocess_utils as postprocess_utils

    original = getattr(postprocess_utils, "_liveclipper_original_sentence_postprocess", None)
    if original is None:
        return _paraformer_safe_sentence_postprocess(words, time_stamp)
    try:
        return original(words, time_stamp)
    except IndexError:
        _PARAFORMER_POSTPROCESS_GUARD_TAKEOVERS += 1
        log_fn = getattr(postprocess_utils, "_liveclipper_guard_log_fn", None)
        if log_fn is not None:
            try:
                log_fn(
                    "Paraformer 句级后处理越界（第 %d 次），已用保护逻辑继续"
                    % _PARAFORMER_POSTPROCESS_GUARD_TAKEOVERS
                )
            except Exception:
                pass
        return _paraformer_safe_sentence_postprocess(words, time_stamp)


def _install_paraformer_postprocess_guard(log_fn: Callable[[str], None] | None = None) -> bool:
    """幂等安装 funasr 句级后处理保护；重复调用无副作用。"""
    global _PARAFORMER_POSTPROCESS_GUARD_INSTALLED
    if not PARAFORMER_SENTENCE_POSTPROCESS_GUARD:
        return False
    try:
        import funasr.utils.postprocess_utils as postprocess_utils
    except Exception:
        return False
    try:
        postprocess_utils._liveclipper_guard_log_fn = log_fn
    except Exception:
        pass
    if _PARAFORMER_POSTPROCESS_GUARD_INSTALLED:
        return True
    current = getattr(postprocess_utils, "sentence_postprocess", None)
    if current is None:
        return False
    if getattr(current, "__name__", "") == "_paraformer_postprocess_guard":
        _PARAFORMER_POSTPROCESS_GUARD_INSTALLED = True
        return True
    postprocess_utils._liveclipper_original_sentence_postprocess = current
    postprocess_utils.sentence_postprocess = _paraformer_postprocess_guard
    _PARAFORMER_POSTPROCESS_GUARD_INSTALLED = True
    return True


def _install_funasr_frozen_shims(log_fn: Callable[[str], None] | None = None) -> int:
    """补齐冻结核心缺少的 funasr 模块（幂等；源码模式什么都不做）。"""
    global _FUNASR_FROZEN_SHIM_INSTALLED
    if not PARAFORMER_FROZEN_MODULE_SHIM or _FUNASR_FROZEN_SHIM_INSTALLED:
        return 0
    try:
        root = Path(__file__).resolve().with_name("funasr_compat")
    except Exception:
        return 0
    if not root.is_dir():
        _FUNASR_FROZEN_SHIM_INSTALLED = True
        return 0
    installed: list[str] = []
    pending = list(_FUNASR_FROZEN_SHIM_MODULES)
    failures: dict[str, str] = {}
    # 多轮重试：被依赖的模块先挂上，后面的才可能成功（最多 3 轮）
    for _round in range(3):
        still: list[tuple[str, str]] = []
        for module_name, relative in pending:
            if module_name in sys.modules:
                failures.pop(module_name, None)
                continue
            try:
                importlib.import_module(module_name)
                failures.pop(module_name, None)
                continue
            except Exception:
                pass
            path = root / relative
            if not path.is_file():
                continue
            try:
                spec = importlib.util.spec_from_file_location(module_name, str(path))
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                installed.append(module_name)
                failures.pop(module_name, None)
            except Exception as exc:
                sys.modules.pop(module_name, None)
                failures[module_name] = str(exc)
                still.append((module_name, relative))
        if not still:
            break
        pending = still
    if log_fn:
        for failed_name, failed_err in failures.items():
            log_fn(f"funasr 模块补挂失败（{failed_name}）：{failed_err}")
    _FUNASR_FROZEN_SHIM_INSTALLED = True
    if installed and log_fn:
        log_fn(f"已为冻结环境补挂 funasr 模块 {len(installed)} 个（Paraformer 引擎可加载）")
    return len(installed)


def paraformer_to_srt(
    audio_path: str,
    srt_output: str,
    log_fn: Callable[[str], None] | None = None,
) -> bool:
    """Run local Paraformer-large and persist sentence-level SRT + word sidecar."""
    model = _load_paraformer(log_fn)
    if log_fn:
        log_fn("启动本地 Paraformer 识别（模型级分段 + 标点恢复 + 字级时间戳）...")
    _install_paraformer_postprocess_guard(log_fn)
    try:
        results = model.generate(
            input=audio_path,
            cache={},
            language="zh",
            use_itn=True,
            batch_size_s=120,
            merge_vad=False,
            output_timestamp=True,
            pred_timestamp=True,
            sentence_timestamp=True,
        )
    except Exception as exc:
        raise LocalASRUnavailable(f"Paraformer inference failed: {exc}") from exc
    payload = results[0] if isinstance(results, (list, tuple)) and results else results
    segments = _paraformer_sentence_segments(payload)
    if not segments:
        raise LocalASRUnavailable("Paraformer returned no timestamped speech segments")

    from volcengine_asr import semantic_segments_to_srt, write_word_timing_sidecar

    # 单独语气词不是内容：整条丢弃（SRT 与词级 sidecar 同步过滤，保持两者一致）
    srt_segments = _drop_filler_only_segments(segments, log_fn)
    # [默认关闭] 与 SenseVoice 路径同一逻辑：AI 语义重分段，失败原样返回。
    srt_segments = _apply_visible_resegmentation(srt_segments, log_fn=log_fn)
    with open(srt_output, "w", encoding="utf-8") as handle:
        handle.write(semantic_segments_to_srt(srt_segments))
    sidecar = write_word_timing_sidecar(
        srt_output, srt_segments, provider=PARAFORMER_ENGINE, log_fn=log_fn,
    )
    if log_fn:
        word_count = sum(len(segment["words"]) for segment in srt_segments)
        suffix = f"，{word_count} 个字级对齐单元" if sidecar else "，未得到可用的字级对齐"
        log_fn(f"Paraformer 识别完成：{len(srt_segments)} 条句子级字幕（模型级分段）{suffix}")
    return True


def transcribe_audio_to_srt(
    audio_path: str,
    srt_output: str,
    log_fn: Callable[[str], None] | None = None,
    engine: str | None = None,
) -> bool:
    """Transcribe with the selected local engine; SenseVoice remains the default."""
    selected = resolve_local_asr_engine(engine)
    if selected == PARAFORMER_ENGINE:
        try:
            return paraformer_to_srt(audio_path, srt_output, log_fn=log_fn)
        except LocalASRUnavailable as exc:
            if not PARAFORMER_FALLBACK_TO_SENSEVOICE:
                raise
            if log_fn:
                log_fn(f"Paraformer 识别失败，已自动回退本地 SenseVoice 重跑：{exc}")
            _release_paraformer_model()
            return sensevoice_to_srt(audio_path, srt_output, log_fn=log_fn)
    return sensevoice_to_srt(audio_path, srt_output, log_fn=log_fn)
