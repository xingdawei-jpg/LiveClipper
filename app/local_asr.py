"""SenseVoice local ASR normalized to LiveClipper's SRT + timing contract.

Consumers read ``<subtitle>.words.json`` through ``volcengine_asr`` without
depending on the local model implementation.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib
import inspect
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

from local_asr_chunking import AudioChunk, build_pause_aware_audio_chunks, write_audio_chunk


_SENSEVOICE_MODEL: Any | None = None
_SENSEVOICE_PUNCTUATION = False
_MODEL_LOCK = threading.Lock()
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


def _load_sensevoice(log_fn: Callable[[str], None] | None = None):
    global _SENSEVOICE_MODEL, _SENSEVOICE_PUNCTUATION
    if _SENSEVOICE_MODEL is not None:
        return _SENSEVOICE_MODEL
    with _MODEL_LOCK:
        if _SENSEVOICE_MODEL is not None:
            return _SENSEVOICE_MODEL
        try:
            _register_sensevoice_components()
            from funasr.auto.auto_model import AutoModel
        except ImportError as exc:
            raise LocalASRUnavailable("SenseVoice runtime is not installed") from exc
        if log_fn:
            log_fn("正在加载本地 SenseVoice（首次使用会下载模型）...")
        model_kwargs = {
            "model": _sensevoice_model_dir(log_fn),
            "vad_model": "fsmn-vad",
            "vad_kwargs": {"max_single_segment_time": 15000},
            "device": "cpu",
            "output_timestamp": True,
            "disable_update": True,
        }
        try:
            _SENSEVOICE_MODEL = _run_modelscope_quietly(lambda: AutoModel(**model_kwargs))
            _SENSEVOICE_PUNCTUATION = False
            if log_fn:
                log_fn("SenseVoice 已跳过可选标点模型，使用停顿断句")
        except Exception as exc:
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


def _generate_sensevoice(model: Any, audio_path: str) -> object:
    """Keep all SenseVoice inference arguments identical for every chunk."""
    return model.generate(
        input=audio_path,
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
        log_fn(f"SenseVoice 音频停顿分段: {len(chunks)} 段（目标9s，最长12s）{suffix}")

    segments: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="liveclipper_sensevoice_") as temp_dir:
        for index, chunk in enumerate(chunks, 1):
            chunk_path = str(Path(temp_dir) / f"chunk_{index:04d}.wav")
            write_audio_chunk(audio_path, chunk_path, chunk)
            result = _generate_sensevoice(model, chunk_path)
            chunk_segments = _sensevoice_segments(result, time_offset=float(chunk.start))
            if not chunk_segments:
                # A pause-only tail may legitimately yield no tokens. Do not
                # turn that into a failed job or fabricate a transcript.
                if log_fn:
                    log_fn(f"SenseVoice 音频分段 {index}/{len(chunks)} 未检测到语音，已跳过")
                continue
            segments.extend(chunk_segments)

    segments.sort(key=lambda segment: (float(segment["start"]), float(segment["end"])))
    return segments, len(chunks), hard_boundaries


def _local_asr_review_settings() -> dict[str, Any]:
    """Load existing ASR settings only after local recognition has succeeded."""
    try:
        from ai_clipper import load_settings

        settings = load_settings()
        return dict(settings) if isinstance(settings, dict) else {}
    except Exception:
        return {}


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


def sensevoice_to_srt(audio_path: str, srt_output: str, log_fn: Callable[[str], None] | None = None) -> bool:
    """Run SenseVoice CPU ASR and persist SRT plus CTC-aligned word timings."""
    model = _load_sensevoice(log_fn)
    if log_fn:
        segmentation = "标点恢复" if _SENSEVOICE_PUNCTUATION else "停顿断句"
        log_fn(f"启动本地 SenseVoice 高质量识别（{segmentation} + CTC 时间对齐）...")
    try:
        segments, input_chunk_count, _hard_boundaries = _sensevoice_pause_aware_segments(
            model,
            audio_path,
            log_fn=log_fn,
        )
    except Exception as exc:
        raise LocalASRUnavailable(f"SenseVoice inference failed: {exc}") from exc
    if not segments:
        raise LocalASRUnavailable("SenseVoice returned no timestamped speech segments")

    segments = _review_sensevoice_segments(segments, audio_path, srt_output, log_fn=log_fn)

    from local_asr_quality import improve_sensevoice_segments
    from volcengine_asr import semantic_segments_to_srt, write_word_timing_sidecar

    segments, semantic_segments = improve_sensevoice_segments(segments, log_fn=log_fn)
    if semantic_segments:
        srt_content = semantic_segments_to_srt(semantic_segments)
    else:
        srt_lines: list[str] = []
        for index, segment in enumerate(segments, 1):
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
    sidecar = write_word_timing_sidecar(srt_output, segments, provider="sensevoice", log_fn=log_fn)
    word_count = sum(len(segment["words"]) for segment in segments)
    if log_fn:
        suffix = f"，{word_count} 个 CTC 对齐单元" if sidecar else "，未得到可用的词级对齐"
        visible_count = len(semantic_segments) if semantic_segments else len(segments)
        punctuation = "标点恢复" if _SENSEVOICE_PUNCTUATION else "停顿断句"
        log_fn(
            f"SenseVoice 识别完成：{len(segments)} 条原始语音段"
            + (f"（音频输入{input_chunk_count}段）" if input_chunk_count > 1 else "")
            + " -> "
            f"{visible_count} 条字幕（{punctuation}）{suffix}"
        )
    return True
