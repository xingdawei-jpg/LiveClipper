# -*- coding: utf-8 -*-
"""
火山引擎 ASR 封装 — 通过 TOS 上传音频后调用大模型语音识别
"""

import os
import sys
import time
import json
import uuid
import subprocess
import re

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_WORD_TIMING_SCHEMA = "liveclipper.word-timings.v1"
_SEMANTIC_STRONG_PUNCTUATION = set("。！？!?")
_SEMANTIC_CLAUSE_PUNCTUATION = set("，,；;：:、")
_SEMANTIC_BOUNDARY_PUNCTUATION = _SEMANTIC_STRONG_PUNCTUATION | _SEMANTIC_CLAUSE_PUNCTUATION
_SEMANTIC_IGNORED_PUNCTUATION = set("“”‘’\"'（）()【】[]《》<>…—·-_/\\{}")


def _semantic_plain_text(value):
    text = re.sub(r"^\s*\[V\d+\]\s*", "", str(value or ""), flags=re.IGNORECASE)
    return "".join(
        char.lower()
        for char in text
        if not char.isspace()
        and char not in _SEMANTIC_BOUNDARY_PUNCTUATION
        and char not in _SEMANTIC_IGNORED_PUNCTUATION
    )


def _semantic_punctuation_offsets(text, expected_plain):
    """Map provider punctuation to spoken-character offsets when alignment is exact."""
    source = re.sub(r"^\s*\[V\d+\]\s*", "", str(text or ""), flags=re.IGNORECASE)
    punctuation = {}
    plain = []
    offset = 0
    for char in source:
        if char.isspace() or char in _SEMANTIC_IGNORED_PUNCTUATION:
            continue
        if char in _SEMANTIC_BOUNDARY_PUNCTUATION:
            if offset > 0:
                punctuation[offset] = punctuation.get(offset, "") + char
            continue
        plain.append(char.lower())
        offset += 1
    if "".join(plain) != expected_plain:
        return {}
    return punctuation


def _semantic_group_key(segment):
    marker = str(segment.get("source_marker") or "").strip().upper()
    source = str(segment.get("source") or "").strip()
    return marker, source


def _semantic_tokens_for_group(segments):
    tokens = []
    for segment_order, segment in enumerate(segments or []):
        if not isinstance(segment, dict):
            continue
        clean_words = []
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            text = str(word.get("text") or "").strip()
            try:
                start = float(word.get("start") or 0)
                end = float(word.get("end") or start)
            except (TypeError, ValueError):
                continue
            if not text or end <= start:
                continue
            item = {"text": text, "start": start, "end": end}
            if word.get("confidence") is not None:
                item["confidence"] = word.get("confidence")
            clean_words.append(item)
        if not clean_words:
            continue

        expected_plain = "".join(_semantic_plain_text(word["text"]) for word in clean_words)
        punctuation = _semantic_punctuation_offsets(segment.get("text") or "", expected_plain)
        spoken_offset = 0
        for word_order, word in enumerate(clean_words):
            spoken_offset += len(_semantic_plain_text(word["text"]))
            token = dict(word)
            token["_punct_after"] = punctuation.get(spoken_offset, "")
            token["_segment_order"] = segment_order
            token["_word_order"] = word_order
            tokens.append(token)

    tokens.sort(key=lambda item: (float(item["start"]), float(item["end"]), item["_segment_order"], item["_word_order"]))
    monotonic = []
    for token in tokens:
        if monotonic and token["start"] < monotonic[-1]["start"]:
            continue
        monotonic.append(token)
    return monotonic


def _semantic_boundary_score(token, next_token, unit_start):
    punctuation = str(token.get("_punct_after") or "")
    raw_gap = float(next_token["start"]) - float(token["end"]) if next_token else 10.0
    gap = max(0.0, raw_gap)
    duration = float(token["end"]) - float(unit_start)
    if next_token is not None and raw_gap < 0:
        return 0, "overlapping_words", gap, duration
    if any(char in _SEMANTIC_STRONG_PUNCTUATION for char in punctuation):
        return 100, "strong_punctuation", gap, duration
    if gap >= 0.65:
        return 85, "long_pause", gap, duration
    if any(char in _SEMANTIC_CLAUSE_PUNCTUATION for char in punctuation):
        return 70 if gap >= 0.18 else 58, "clause_punctuation", gap, duration
    if gap >= 0.45:
        return 62, "pause", gap, duration
    if gap >= 0.28:
        return 42, "short_pause", gap, duration
    return 0, "", gap, duration


def _semantic_render_words(words):
    return "".join(str(word.get("text") or "") + str(word.get("_punct_after") or "") for word in words).strip()


def _semantic_trim_weak_prefix(words):
    """Trim only disposable leading connectors, keeping timestamps word-exact."""
    current = list(words or [])
    removed = []
    prefixes = (
        "然后", "而且", "但是", "因为", "就是", "没错", "对的", "是的",
        "嗯", "啊", "对",
    )
    while current:
        compact = "".join(_semantic_plain_text(word.get("text") or "") for word in current)
        matched = next((prefix for prefix in prefixes if compact.startswith(prefix)), "")
        if not matched or len(compact) - len(matched) < 5:
            break
        consumed = 0
        cut_count = 0
        for word in current:
            consumed += len(_semantic_plain_text(word.get("text") or ""))
            cut_count += 1
            if consumed >= len(matched):
                break
        if consumed != len(matched) or cut_count >= len(current):
            break
        removed.append(matched)
        current = current[cut_count:]
    return current, removed


def _semantic_trim_weak_suffix(words):
    """Remove short dangling tails without estimating a replacement end time."""
    current = list(words or [])
    removed = []
    suffixes = ("但是我", "所以我", "然后我", "但是", "然后", "而且", "因为", "就是", "所以")
    while current:
        compact = "".join(_semantic_plain_text(word.get("text") or "") for word in current)
        matched = next((suffix for suffix in suffixes if compact.endswith(suffix)), "")
        if not matched or len(compact) - len(matched) < 5:
            break
        consumed = 0
        cut_count = 0
        for word in reversed(current):
            consumed += len(_semantic_plain_text(word.get("text") or ""))
            cut_count += 1
            if consumed >= len(matched):
                break
        if consumed != len(matched) or cut_count >= len(current):
            break
        removed.insert(0, matched)
        current = current[:-cut_count]
    return current, removed


def _semantic_segments_for_group(segments, marker="", source=""):
    tokens = _semantic_tokens_for_group(segments)
    if not tokens:
        return []

    result = []
    start_index = 0
    index = 0
    candidates = []
    while index < len(tokens):
        token = tokens[index]
        next_token = tokens[index + 1] if index + 1 < len(tokens) else None
        score, reason, gap, duration = _semantic_boundary_score(token, next_token, tokens[start_index]["start"])
        if score and duration >= 1.2:
            candidates.append((index, score, reason, duration))

        cut_index = None
        cut_reason = ""
        is_last = next_token is None
        strong_boundary = reason == "strong_punctuation" and duration >= 1.5
        clear_pause = reason == "long_pause" and duration >= 1.5
        useful_clause = reason == "clause_punctuation" and duration >= 3.5

        if is_last:
            cut_index, cut_reason = index, reason or "source_end"
        elif strong_boundary or clear_pause or useful_clause:
            cut_index, cut_reason = index, reason
        elif duration >= 6.2 and candidates:
            eligible = [candidate for candidate in candidates if candidate[3] >= 1.5]
            if eligible:
                cut_index, _, cut_reason, _ = max(
                    eligible,
                    key=lambda candidate: candidate[1] - abs(candidate[3] - 4.6) * 5.0,
                )
        elif duration >= 8.8:
            eligible = [candidate for candidate in candidates if candidate[3] >= 1.2]
            if eligible:
                cut_index, _, cut_reason, _ = max(
                    eligible,
                    key=lambda candidate: candidate[1] - abs(candidate[3] - 5.5) * 3.0,
                )
            else:
                safe_positions = [
                    probe
                    for probe in range(start_index, index)
                    if float(tokens[probe + 1]["start"]) >= float(tokens[probe]["end"])
                    and float(tokens[probe]["end"]) - float(tokens[start_index]["start"]) >= 3.0
                ]
                if safe_positions:
                    cut_index = min(
                        safe_positions,
                        key=lambda probe: abs(
                            (float(tokens[probe]["end"]) - float(tokens[start_index]["start"])) - 5.5
                        ),
                    )
                    cut_reason = "hard_limit_word_boundary"

        if cut_index is None:
            index += 1
            continue

        unit_words = tokens[start_index:cut_index + 1]
        if unit_words:
            unit_words, trimmed_prefixes = _semantic_trim_weak_prefix(unit_words)
            unit_words, trimmed_suffixes = _semantic_trim_weak_suffix(unit_words)
        if unit_words:
            public_words = []
            for word in unit_words:
                public_word = {key: value for key, value in word.items() if not key.startswith("_")}
                public_words.append(public_word)
            rendered_text = _semantic_render_words(unit_words)
            if trimmed_suffixes:
                rendered_text = rendered_text.rstrip("，,；;：:、")
            item = {
                "start": round(float(unit_words[0]["start"]), 3),
                "end": round(float(unit_words[-1]["end"]), 3),
                "text": rendered_text,
                "words": public_words,
                "semantic_unit": True,
                "boundary_reason": cut_reason,
            }
            if trimmed_prefixes:
                item["trimmed_prefix"] = "".join(trimmed_prefixes)
            if trimmed_suffixes:
                item["trimmed_suffix"] = "".join(trimmed_suffixes)
            if marker:
                item["source_marker"] = marker
            if source:
                item["source"] = source
            result.append(item)

        start_index = cut_index + 1
        index = start_index
        candidates = []

    return result


def build_semantic_segments(segments, log_fn=None):
    """Create source-local semantic units using only provider word timestamps."""
    valid = [segment for segment in (segments or []) if isinstance(segment, dict)]
    if not valid:
        return []
    if all(segment.get("semantic_unit") for segment in valid):
        return [dict(segment) for segment in valid]

    groups = {}
    group_order = []
    for segment in valid:
        key = _semantic_group_key(segment)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(segment)

    semantic = []
    for marker, source in group_order:
        semantic.extend(_semantic_segments_for_group(groups[(marker, source)], marker=marker, source=source))
    if not semantic:
        return []
    if log_fn:
        long_count = sum(1 for segment in semantic if float(segment["end"]) - float(segment["start"]) > 8.8)
        log_fn(f"语义断句: {len(valid)} 个原始语音段 -> {len(semantic)} 个词级语义段" + (f"，{long_count} 段保留长句" if long_count else ""))
    return semantic


def _semantic_srt_time(seconds):
    value = max(0.0, float(seconds or 0.0))
    total_ms = int(round(value * 1000.0))
    hours, remainder = divmod(total_ms, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def semantic_segments_to_srt(segments):
    blocks = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or start)
        except (TypeError, ValueError):
            continue
        text = str(segment.get("text") or "").strip()
        marker = str(segment.get("source_marker") or "").strip().upper()
        if marker and not re.match(r"^\[V\d+\]", text, flags=re.IGNORECASE):
            text = f"[{marker}] {text}"
        if not text or end <= start:
            continue
        blocks.append(
            f"{len(blocks) + 1}\n{_semantic_srt_time(start)} --> {_semantic_srt_time(end)}\n{text}\n"
        )
    return "\n".join(blocks)


def word_timing_sidecar_path(srt_path):
    """Return the word-timing sidecar path for an SRT file."""
    base, _ = os.path.splitext(os.fspath(srt_path))
    return base + ".words.json"


def write_word_timing_sidecar(srt_path, segments, provider="volcengine", log_fn=None):
    """Persist normalized word timings without changing the compatible SRT."""
    clean_segments = []
    word_count = 0
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        clean_words = []
        for word in segment.get("words") or []:
            if not isinstance(word, dict):
                continue
            text = str(word.get("text") or "").strip()
            try:
                start = float(word.get("start") or 0)
                end = float(word.get("end") or start)
            except (TypeError, ValueError):
                continue
            if not text or end <= start:
                continue
            item = {"text": text, "start": round(start, 3), "end": round(end, 3)}
            try:
                confidence = word.get("confidence")
                if confidence is not None:
                    item["confidence"] = round(float(confidence), 4)
            except (TypeError, ValueError):
                pass
            clean_words.append(item)
        if not clean_words:
            continue
        clean_segments.append({
            "start": round(float(segment.get("start") or clean_words[0]["start"]), 3),
            "end": round(float(segment.get("end") or clean_words[-1]["end"]), 3),
            "text": str(segment.get("text") or "").strip(),
            "words": clean_words,
        })
        word_count += len(clean_words)
    if not clean_segments:
        return None

    sidecar = word_timing_sidecar_path(srt_path)
    payload = {
        "schema": _WORD_TIMING_SCHEMA,
        "provider": provider,
        "word_count": word_count,
        "segments": clean_segments,
    }
    temp_path = sidecar + ".tmp"
    os.makedirs(os.path.dirname(os.path.abspath(sidecar)), exist_ok=True)
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(temp_path, sidecar)
    if log_fn:
        log_fn(f"词级时间已保存: {word_count} 词 ({os.path.basename(sidecar)})")
    return sidecar


def load_word_timing_sidecar(srt_path, semantic=False, log_fn=None):
    """Load a sidecar defensively; missing/legacy subtitles simply return []."""
    sidecar = word_timing_sidecar_path(srt_path)
    try:
        with open(sidecar, "r", encoding="utf-8-sig") as handle:
            payload = json.load(handle)
        if payload.get("schema") != _WORD_TIMING_SCHEMA:
            return []
        segments = list(payload.get("segments") or [])
        if semantic:
            return build_semantic_segments(segments, log_fn=log_fn) or segments
        return segments
    except (OSError, ValueError, TypeError, AttributeError):
        return []

# PyInstaller 打包后 certifi 路径可能指向已删除的临时目录，
# 尝试多个可能的位置找到 cacert.pem
if hasattr(sys, '_MEIPASS'):
    _cert_candidates = [
        os.path.join(sys._MEIPASS, 'certifi', 'cacert.pem'),
        os.path.join(os.path.dirname(sys._MEIPASS), 'certifi', 'cacert.pem'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'certifi', 'cacert.pem'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'certifi', 'cacert.pem'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'certifi', 'cacert.pem'),
    ]
    for _cp in _cert_candidates:
        if os.path.exists(_cp):
            os.environ.setdefault('SSL_CERT_FILE', _cp)
            os.environ.setdefault('REQUESTS_CA_BUNDLE', _cp)
            break
    else:
        # 都找不到，干脆跳过 SSL 验证
        try:
            import ssl as _ssl
            _ssl._create_default_https_context = _ssl._create_unverified_context
        except Exception:
            pass

# 预导入 tos SDK（避免首次调用时卡顿）
try:
    import tos
    _TOS_AVAILABLE = True
except ImportError as _e:
    import sys as _sys
    print(f"[VOLC_DEBUG] tos import failed: {_e}", file=_sys.stderr, flush=True)
    _TOS_AVAILABLE = False


_TOS_REGIONS = [
    ("tos-cn-beijing.volces.com", "cn-beijing"),
    ("tos-cn-shanghai.volces.com", "cn-shanghai"),
    ("tos-cn-guangzhou.volces.com", "cn-guangzhou"),
    ("tos-ap-southeast-1.volces.com", "ap-southeast-1"),
]

_VOLC_REGION_ALIASES = {
    "": "cn-beijing",
    "beijing": "cn-beijing",
    "bj": "cn-beijing",
    "cn-beijing": "cn-beijing",
    "\u5317\u4eac": "cn-beijing",
    "\u4e2d\u56fd\u5317\u4eac": "cn-beijing",
    "shanghai": "cn-shanghai",
    "sh": "cn-shanghai",
    "cn-shanghai": "cn-shanghai",
    "\u4e0a\u6d77": "cn-shanghai",
    "\u4e2d\u56fd\u4e0a\u6d77": "cn-shanghai",
    "guangzhou": "cn-guangzhou",
    "gz": "cn-guangzhou",
    "cn-guangzhou": "cn-guangzhou",
    "\u5e7f\u5dde": "cn-guangzhou",
    "\u4e2d\u56fd\u5e7f\u5dde": "cn-guangzhou",
    "singapore": "ap-southeast-1",
    "ap-southeast-1": "ap-southeast-1",
    "\u65b0\u52a0\u5761": "ap-southeast-1",
}


def prepare_volcengine_audio(source_path, output_dir, prefix=None, ffmpeg="ffmpeg", log_fn=None, timeout=300):
    """Prepare a compact 16k mono audio file for Volcengine ASR upload."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    os.makedirs(output_dir, exist_ok=True)
    safe_prefix = prefix or f"volc_{uuid.uuid4().hex}"
    candidates = [
        (
            ".mp3",
            ["-map", "0:a:0?", "-vn", "-af", "aresample=async=1:first_pts=0", "-acodec", "libmp3lame", "-b:a", "64k", "-ar", "16000", "-ac", "1"],
            "MP3 64kbps",
        ),
        (
            ".wav",
            ["-map", "0:a:0?", "-vn", "-af", "aresample=async=1:first_pts=0", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1"],
            "WAV PCM",
        ),
    ]

    last_error = ""
    for ext, audio_args, label in candidates:
        audio_path = os.path.join(output_dir, f"{safe_prefix}{ext}")
        try:
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            pass

        cmd = [ffmpeg, "-y", "-fflags", "+genpts", "-i", source_path, *audio_args, audio_path]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout,
                creationflags=_NO_WINDOW,
            )
            if result.returncode == 0 and os.path.exists(audio_path) and os.path.getsize(audio_path) > 0:
                size_mb = os.path.getsize(audio_path) / (1024 * 1024)
                _log(f"volcengine_asr: 音频准备完成 ({label}, {size_mb:.1f}MB)")
                return audio_path
            stderr = (result.stderr or b"").decode("utf-8", errors="ignore").strip()
            last_error = stderr.splitlines()[-1] if stderr else f"ffmpeg exit {result.returncode}"
            _log(f"volcengine_asr: {label} 音频准备失败，尝试下一个格式")
        except Exception as exc:
            last_error = str(exc)
            _log(f"volcengine_asr: {label} 音频准备异常，尝试下一个格式: {exc}")
        try:
            if os.path.exists(audio_path):
                os.remove(audio_path)
        except Exception:
            pass

    _log(f"volcengine_asr: 音频准备失败: {last_error}")
    return None


def _normalize_region(value):
    text = str(value or "").strip()
    compact = text.replace(" ", "").replace("_", "-").lower()
    return _VOLC_REGION_ALIASES.get(text) or _VOLC_REGION_ALIASES.get(compact) or compact or "cn-beijing"


def _ordered_tos_regions(preferred_region=""):
    preferred = _normalize_region(preferred_region)
    ordered = []
    for endpoint, region in _TOS_REGIONS:
        if region == preferred:
            ordered.append((endpoint, region))
            break
    for item in _TOS_REGIONS:
        if item not in ordered:
            ordered.append(item)
    return ordered


def _volc_ssl_context():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _volc_headers(task_id, app_id="", access_token="", api_key=None):
    if api_key:
        return {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": "volc.seedasr.auc",
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
        }
    return {
        "Content-Type": "application/json",
        "X-Api-App-Key": str(app_id),
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": "volc.seedasr.auc",
        "X-Api-Request-Id": task_id,
        "X-Api-Sequence": "-1",
    }


def _make_diagnostic_wav(path):
    import math
    import struct
    import wave

    sample_rate = 16000
    duration = 1.0
    frames = int(sample_rate * duration)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        for i in range(frames):
            sample = int(4000 * math.sin(2 * math.pi * 440 * i / sample_rate))
            wf.writeframes(struct.pack("<h", sample))


def _explain_tos_error(error_text):
    text = str(error_text)
    low = text.lower()
    if "ssl" in low or "eof" in low or "certificate" in low:
        return "TOS HTTPS/SSL failed. Ask the user to try another network, disable proxy/VPN/HTTPS inspection, or use a China-region bucket."
    if "timeout" in low or "timed out" in low or "max retries" in low:
        return "TOS upload timed out. This is usually network/firewall/DNS or a far-away bucket region."
    if "nosuchbucket" in text or "not found" in low:
        return "Bucket was not found in the tested region. Check bucket name and region."
    if "accessdenied" in text or "access denied" in low:
        return "TOS denied access. Check AK/SK permissions for the bucket."
    if "signature" in low:
        return "TOS signature mismatch. Check AK/SK and bucket region."
    return "TOS upload failed. Check bucket name, region, AK/SK permissions, and user network."


def diagnose_volcengine(app_id="", access_token="", tos_ak="", tos_sk="",
                        bucket="", region="", api_key=None, log_fn=None, timeout=45):
    """Run an end-to-end Volcengine ASR diagnostic without using user media."""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    bucket = (bucket or "livec").strip()
    api_key = (api_key or "").strip()
    app_id = (app_id or "").strip()
    access_token = (access_token or "").strip()
    tos_ak = (tos_ak or "").strip()
    tos_sk = (tos_sk or "").strip()
    region = _normalize_region(region)

    if not _TOS_AVAILABLE:
        return {"ok": False, "stage": "sdk", "message": "tos SDK is not available in this build."}
    if not tos_ak or not tos_sk:
        return {"ok": False, "stage": "config", "message": "TOS AK/SK is required for the full diagnostic."}
    if not bucket:
        return {"ok": False, "stage": "config", "message": "TOS bucket name is required."}
    if not api_key and not (app_id and access_token):
        return {"ok": False, "stage": "config", "message": "Volcengine API Key, or App ID + Access Token, is required."}

    import tempfile
    import shutil
    import urllib.request

    temp_dir = tempfile.mkdtemp(prefix="liveclipper_volc_diag_")
    wav_path = os.path.join(temp_dir, "diagnostic.wav")
    obj_key = f"asr_diag/{uuid.uuid4().hex}.wav"
    client = None
    uploaded = False
    selected_region = ""
    last_error = ""

    try:
        _make_diagnostic_wav(wav_path)
        _log("1/4 Created 1-second diagnostic WAV.")

        for endpoint, test_region in _ordered_tos_regions(region):
            _log(f"2/4 Testing TOS upload: bucket={bucket}, region={test_region}, endpoint={endpoint}")
            try:
                client = tos.TosClientV2(
                    ak=tos_ak,
                    sk=tos_sk,
                    endpoint=endpoint,
                    region=test_region,
                )
                client.put_object_from_file(bucket, obj_key, wav_path)
                uploaded = True
                selected_region = test_region
                _log(f"2/4 TOS upload OK: region={test_region}")
                break
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                _log(f"2/4 TOS upload failed on {test_region}: {last_error[:600]}")
                if "AccessDenied" in last_error or "access denied" in last_error.lower():
                    break
                if "Signature" in last_error or "signature" in last_error.lower():
                    break

        if not uploaded:
            return {
                "ok": False,
                "stage": "tos_upload",
                "message": _explain_tos_error(last_error),
                "detail": last_error,
            }

        try:
            url_resp = client.pre_signed_url(
                tos.HttpMethodType.Http_Method_Get, bucket, obj_key, 3600
            )
            audio_url = url_resp.signed_url
            _log("3/4 TOS pre-signed URL OK.")
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            return {"ok": False, "stage": "tos_signed_url", "message": "Failed to generate TOS pre-signed URL.", "detail": detail}

        task_id = str(uuid.uuid4())
        headers = _volc_headers(task_id, app_id=app_id, access_token=access_token, api_key=api_key)
        submit_body = {
            "user": {"uid": "live_cutter_diagnostic"},
            "audio": {"format": "wav", "url": audio_url},
            "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "show_utterances": True},
        }

        try:
            req = urllib.request.Request(
                "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit",
                data=json.dumps(submit_body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout, context=_volc_ssl_context()) as resp:
                submit_body_text = resp.read().decode("utf-8", errors="ignore")
                status_code = resp.headers.get("X-Api-Status-Code", "")
                message = resp.headers.get("X-Api-Message", "")
            if status_code != "20000000":
                return {
                    "ok": False,
                    "stage": "asr_submit",
                    "message": f"ASR submit failed: status={status_code} message={message}",
                    "detail": submit_body_text[:800],
                }
            _log(f"4/4 ASR submit OK: task_id={task_id}")
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            return {"ok": False, "stage": "asr_submit", "message": "ASR submit request failed.", "detail": detail}

        try:
            time.sleep(3)
            query_headers = dict(headers)
            query_headers["X-Api-Sequence"] = "-1"
            req = urllib.request.Request(
                "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query",
                data=json.dumps({}).encode("utf-8"),
                headers=query_headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout, context=_volc_ssl_context()) as resp:
                query_text = resp.read().decode("utf-8", errors="ignore")
                query_status = resp.headers.get("X-Api-Status-Code", "")
                query_message = resp.headers.get("X-Api-Message", "")
            if query_status in ("20000000", "20000001", "20000002"):
                return {
                    "ok": True,
                    "stage": "ok",
                    "message": f"Full diagnostic passed. TOS region={selected_region}; ASR status={query_status or 'empty'} {query_message}".strip(),
                    "detail": query_text[:800],
                }
            if query_status == "20000003" and "no valid speech" in query_message.lower():
                return {
                    "ok": True,
                    "stage": "submit_ok_no_speech",
                    "message": f"TOS upload and ASR submit passed. Diagnostic audio has no valid speech, which is expected. TOS region={selected_region}",
                    "detail": query_text[:800],
                }
            return {
                "ok": False,
                "stage": "asr_query",
                "message": f"ASR query failed: status={query_status} message={query_message}",
                "detail": query_text[:800],
            }
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
            return {
                "ok": True,
                "stage": "submit_ok_query_warn",
                "message": f"TOS upload and ASR submit passed, but query check failed: {detail}",
                "detail": detail,
            }
    finally:
        if uploaded and client:
            _cleanup_tos(client, bucket, obj_key, _log)
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def volcengine_asr(audio_path, app_id, access_token, tos_ak, tos_sk,
                   bucket="", region="", timeout=300, log_fn=None, api_key=None):
    """
    调用火山引擎大模型 ASR 识别音频文件，返回 segments 列表。
    
    Args:
        audio_path: 音频文件路径
        app_id: 火山引擎 APP ID（旧版控制台）
        access_token: 火山引擎 Access Token（旧版控制台）
        tos_ak: TOS Access Key ID
        tos_sk: TOS Secret Access Key
        bucket: TOS bucket 名 (默认 livec)
        timeout: 最大等待秒数 (默认 300)
        log_fn: 日志回调函数
        api_key: 新版控制台 API Key（优先级高于 app_id+token，用于豆包2.0）
    
    Returns:
        list[dict] 格式 [{"start": float, "end": float, "text": str}, ...]
        失败返回 None
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not _TOS_AVAILABLE:
        _log("volcengine_asr: tos SDK 未安装，跳过")
        return None

    if not api_key and not all([app_id, access_token, tos_ak, tos_sk]):
        _log("volcengine_asr: 配置不完整，跳过")
        return None
    if not api_key and not all([tos_ak, tos_sk]):
        _log("volcengine_asr: 配置不完整，跳过")
        return None

    # 生成 TOS 上的临时对象 key
    region = _normalize_region(region)
    ext = os.path.splitext(audio_path)[1].lower()
    if not ext:
        ext = ".wav"
    obj_key = f"asr_temp/{uuid.uuid4().hex}{ext}"

    # --- 1. 上传音频到 TOS（自动检测区域） ---
    _log(f"volcengine_asr: 上传音频到 TOS ({bucket}/{obj_key})...")
    _upload_ok = False
    _last_error = ""
    _tos_client = None
    for _ep, _region in _ordered_tos_regions(region):
        try:
            _tos_client = tos.TosClientV2(
                ak=tos_ak,
                sk=tos_sk,
                endpoint=_ep,
                region=_region,
            )
            _tos_client.put_object_from_file(bucket, obj_key, audio_path)
            _log(f"volcengine_asr: TOS 上传完成 (region={_region})")
            _upload_ok = True
            break
        except Exception as e:
            _last_error = str(e)
            # "not found" 可能是区域不对，继续试下一个
            if "not found" in _last_error.lower() or "NoSuchBucket" in _last_error:
                continue
            # "ACCESS DENIED" 是认证问题，再试也没用
            if "access denied" in _last_error.lower() or "AccessDenied" in _last_error:
                break
            continue
    if not _upload_ok:
        _log(f"volcengine_asr: TOS 上传失败: {_last_error}")
        _log("volcengine_asr: 提示：请检查桶名和区域是否正确，AK/SK是否有TOS权限")
        return None

    # 获取 pre_signed_url
    try:
        url_resp = _tos_client.pre_signed_url(
            tos.HttpMethodType.Http_Method_Get, bucket, obj_key, 3600
        )
        audio_url = url_resp.signed_url
        _log(f"volcengine_asr: 获取 pre_signed_url 成功")
    except Exception as e:
        _log(f"volcengine_asr: 获取 pre_signed_url 失败: {e}")
        _cleanup_tos(_tos_client, bucket, obj_key, _log)
        return None

    # --- 2. 提交 ASR 任务 ---
    import uuid as _uuid
    task_id = str(_uuid.uuid4())
    submit_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
    if api_key:
        # 新版控制台鉴权（豆包2.0）
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": "volc.seedasr.auc",
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
        }
    else:
        # 旧版控制台鉴权（豆包1.0）
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": str(app_id),
            "X-Api-Access-Key": access_token,
            "X-Api-Resource-Id": "volc.seedasr.auc",
            "X-Api-Request-Id": task_id,
            "X-Api-Sequence": "-1",
        }
    submit_body = {
        "user": {"uid": "live_cutter"},
        "audio": {
            "format": ext.lstrip("."),
            "url": audio_url,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": True,
            "show_utterances": True,
        },
    }

    _log("volcengine_asr: 提交 ASR 任务...")
    try:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req_data = json.dumps(submit_body).encode("utf-8")
        req = urllib.request.Request(submit_url, data=req_data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            resp_body = resp.read().decode("utf-8")
        
        status_code = resp.headers.get("X-Api-Status-Code", "")
        if status_code != "20000000":
            _log(f"volcengine_asr: 提交失败: status={status_code} body={resp_body[:200]}")
            _cleanup_tos(_tos_client, bucket, obj_key, _log)
            return None
        
        _log(f"volcengine_asr: 任务已提交, id={task_id}")
    except Exception as e:
        _log(f"volcengine_asr: 提交异常: {e}")
        if "429" in str(e):
            _log("⚠️ 火山引擎请求频率超限(429)，请稍后再试或联系火山引擎提升配额")
        elif "401" in str(e):
            _log("⚠️ 401认证失败！请检查语音识别控制台的 App ID 和 Access Token 是否正确，教程：https://www.feishu.cn/docx/QdJDdGpzGofSSuxmPDjc4lrxnVb")
        elif "403" in str(e) or "Forbidden" in str(e):
            _log("⚠️ 403鉴权失败/欠费！火山引擎账号可能已欠费，自动切换到本地识别")
        _cleanup_tos(_tos_client, bucket, obj_key, _log)
        return None

    # --- 3. 轮询结果 ---
    query_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
    start_time = time.time()
    poll_schedule = [2, 4, 6, 10]
    poll_index = 0
    poll_interval = poll_schedule[poll_index]
    _empty_count = 0

    while time.time() - start_time < timeout:
        time.sleep(poll_interval)
        elapsed = time.time() - start_time
        _log(f"volcengine_asr: 轮询中 ({elapsed:.0f}s)...")

        try:
            query_body = json.dumps({}).encode("utf-8")
            req = urllib.request.Request(query_url, data=query_body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                qr = resp.read().decode("utf-8")
            
            status_code = resp.headers.get("X-Api-Status-Code", "")
            message = resp.headers.get("X-Api-Message", "")

            # 先判断处理中状态（20000001=处理中，20000000=完成）
            if status_code in ("20000001", "20000002") or "Processing" in message or "PENDING" in str(message).upper():
                poll_index = min(poll_index + 1, len(poll_schedule) - 1)
                poll_interval = poll_schedule[poll_index]
                continue
            # 429限流：指数退避而不是立即放弃
            elif "429" in str(status_code) or "429" in message or "rate" in message.lower() or "limit" in message.lower():
                poll_interval = min(poll_interval * 2, 30)
                _log(f"volcengine_asr: 请求频率超限(429)，退避到{poll_interval}s后重试...")
                continue
            elif status_code and status_code != "20000000":
                _log(f"volcengine_asr: 查询失败: status={status_code} msg={message}")
                _cleanup_tos(_tos_client, bucket, obj_key, _log)
                return None
            elif "silence" in message.lower() or "no valid speech" in message.lower():
                _log(f"volcengine_asr: 音频无有效语音: {message}")
                _cleanup_tos(_tos_client, bucket, obj_key, _log)
                return None
            elif "error" in message.lower() or "fail" in message.lower():
                # 防止"Error processing"等含Processing的错误消息被误判为继续轮询
                _log(f"volcengine_asr: 查询返回错误: status={status_code} msg={message}")
                _cleanup_tos(_tos_client, bucket, obj_key, _log)
                return None
            elif message and ("Processing" in message or "PENDING" in str(message).upper()):
                poll_index = min(poll_index + 1, len(poll_schedule) - 1)
                poll_interval = poll_schedule[poll_index]
                continue
            # status_code为空且message为空：可能是异常响应，最多等3轮
            elif not status_code and not message:
                _empty_count += 1
                if _empty_count >= 3:
                    _log("volcengine_asr: 连续3次空响应，终止轮询")
                    _cleanup_tos(_tos_client, bucket, obj_key, _log)
                    return None
                poll_index = min(poll_index + 1, len(poll_schedule) - 1)
                poll_interval = poll_schedule[poll_index]
                continue
            
            # --- 4. 解析结果 ---
            _log("volcengine_asr: 识别完成，解析结果...")
            data = json.loads(qr)
            result = data.get("result", {})
            utterances = result.get("utterances", [])

            segments = []
            word_count = 0
            for utt in utterances:
                text = utt.get("text", "").strip()
                if not text:
                    continue
                utt_start = utt.get("start_time", 0) / 1000.0  # ms -> s
                utt_end = utt.get("end_time", 0) / 1000.0
                if utt_end <= utt_start:
                    continue
                words = []
                for raw_word in utt.get("words") or []:
                    word_text = str(raw_word.get("text") or "").strip()
                    try:
                        word_start = float(raw_word.get("start_time", 0)) / 1000.0
                        word_end = float(raw_word.get("end_time", 0)) / 1000.0
                    except (TypeError, ValueError):
                        continue
                    if not word_text or word_end <= word_start:
                        continue
                    word = {"text": word_text, "start": word_start, "end": word_end}
                    if raw_word.get("confidence") is not None:
                        word["confidence"] = raw_word.get("confidence")
                    words.append(word)
                word_count += len(words)
                segments.append({"start": utt_start, "end": utt_end, "text": text, "words": words})

            _log(f"volcengine_asr: 解析得到 {len(segments)} 条语音段, {word_count} 个词级时间")
            _cleanup_tos(_tos_client, bucket, obj_key, _log)
            return segments if segments else None

        except Exception as e:
            if "429" in str(e):
                poll_interval = min(poll_interval * 2, 30)
                _log(f"volcengine_asr: 轮询被限流(429)，退避到{poll_interval}s...")
            elif "403" in str(e) or "Forbidden" in str(e):
                _log("volcengine_asr: 轮询被拒(403)，账号欠费或Token过期，终止重试")
                _cleanup_tos(_tos_client, bucket, obj_key, _log)
                return None
            else:
                _log(f"volcengine_asr: 轮询异常: {e}")
            continue

    _log(f"volcengine_asr: 超时 ({timeout}s)")
    _cleanup_tos(_tos_client, bucket, obj_key, _log)
    return None


def _cleanup_tos(client, bucket, obj_key, _log):
    """删除 TOS 上的临时文件"""
    try:
        client.delete_object(bucket, obj_key)
        _log("volcengine_asr: 已清理 TOS 临时文件")
    except Exception:
        pass
