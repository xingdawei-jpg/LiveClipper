"""Local ASR engines normalized to LiveClipper's SRT + word-timing contract.

The rest of the product must not need to know whether a transcript came from
SenseVoice, Whisper, or a cloud provider.  In particular, consumers continue
to read ``<subtitle>.words.json`` through ``volcengine_asr``.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from typing import Any, Callable


_SENSEVOICE_MODEL: Any | None = None
_SENSEVOICE_PUNCTUATION = False
_MODEL_LOCK = threading.Lock()
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z]+(?:['-][A-Za-z]+)*|\d+(?:[.,:]\d+)*")


class LocalASRUnavailable(RuntimeError):
    """The requested optional local ASR runtime or its model is unavailable."""


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


def _sensevoice_model_dir(log_fn: Callable[[str], None] | None = None) -> str:
    """Download once and return a SentencePiece-safe model path.

    Some Windows SentencePiece builds cannot open a path containing Chinese
    characters, although Python can. A local NTFS junction gives the native
    library an ASCII-only view without copying the 936 MB model.
    """
    configured = os.environ.get("SENSEVOICE_MODEL_DIR", "").strip()
    if configured and os.path.isfile(os.path.join(configured, "model.pt")):
        model_dir = os.path.abspath(configured)
    else:
        try:
            from modelscope import snapshot_download
            model_dir = snapshot_download("iic/SenseVoiceSmall")
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
            from funasr import AutoModel
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
            _SENSEVOICE_MODEL = AutoModel(punc_model="ct-punc", **model_kwargs)
            _SENSEVOICE_PUNCTUATION = True
            if log_fn:
                log_fn("SenseVoice 标点恢复已启用")
        except Exception as punctuation_error:
            if log_fn:
                log_fn(f"标点恢复模型不可用，继续使用停顿断句: {punctuation_error}")
            try:
                _SENSEVOICE_MODEL = AutoModel(**model_kwargs)
                _SENSEVOICE_PUNCTUATION = False
            except Exception as exc:
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


def _sensevoice_segments(result: object) -> list[dict[str, Any]]:
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
                            "start": round(start_value, 3),
                            "end": round(end_value, 3),
                        })
            else:
                words = _words_from_timestamp(text, raw_timestamp)
            start = _as_seconds(sentence.get("start"))
            end = _as_seconds(sentence.get("end"))
            if start is None and words:
                start = words[0]["start"]
            if end is None and words:
                end = words[-1]["end"]
            if not text or start is None or end is None or end <= start:
                continue
            segments.append({"text": text, "start": round(start, 3), "end": round(end, 3), "words": words})
    return segments


def sensevoice_to_srt(audio_path: str, srt_output: str, log_fn: Callable[[str], None] | None = None) -> bool:
    """Run SenseVoice CPU ASR and persist SRT plus CTC-aligned word timings."""
    model = _load_sensevoice(log_fn)
    if log_fn:
        log_fn("启动本地 SenseVoice 高质量识别（标点恢复 + CTC 时间对齐）...")
    try:
        result = model.generate(
            input=audio_path,
            cache={},
            language="zh",
            use_itn=True,
            batch_size_s=120,
            merge_vad=False,
        )
    except Exception as exc:
        raise LocalASRUnavailable(f"SenseVoice inference failed: {exc}") from exc
    segments = _sensevoice_segments(result)
    if not segments:
        raise LocalASRUnavailable("SenseVoice returned no timestamped speech segments")

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
            f"SenseVoice 识别完成：{len(segments)} 条语音段 -> "
            f"{visible_count} 条字幕（{punctuation}）{suffix}"
        )
    return True
