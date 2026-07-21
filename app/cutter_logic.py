"""
剪辑核心逻辑 v3.0（GUI 和 CLI 共用）
- 自动语音识别（无 SRT 时自动生成）
- 字幕叠加
- 文案逻辑优化（每类最多2个、模糊匹配、语气词过滤）
"""

import logging
_LOG = logging.getLogger("liveclipper.cutter_logic")

import os
import sys
import re
import time
from ssl_context import create_ssl_context
import shutil

# 多版本缓存：process_video 写入，process_video_multi 读取
_multi_result_cache = {}
import subprocess
import json
import glob
import random
import math
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from srt_parser import open_srt, _time_to_seconds
from selection_contracts import DurationContract, SHORTAGE_GRACE_SECONDS

# 编码器辅助：优先硬件加速，回退软件编码
_hw_encoder_checked = False
_hw_encoder = None
_hw_fallback = False  # 硬件编码回退标志
_sw_encoder_checked = False
_sw_encoder_args = None
_ACTIVE_PROCS = {}
_ACTIVE_PROCS_LOCK = threading.Lock()
OUTPUT_CLIP_MIRROR_PROBABILITY = 0.25
CLIP_AUDIO_TAIL_GUARD_SECONDS = 0.18
LAST_CLIP_AUDIO_TAIL_GUARD_SECONDS = 0.22
CLIP_AUDIO_FADE_SECONDS = 0.015
CLIP_VIDEO_TRANSITION_SECONDS = 0.12


def _register_process(proc, cancel_event=None):
    if not proc:
        return proc
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS[proc] = cancel_event
    return proc


def _unregister_process(proc):
    if not proc:
        return
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_PROCS.pop(proc, None)


def _associate_process_cancel_event(proc, cancel_event=None):
    if not proc or cancel_event is None:
        return
    with _ACTIVE_PROCS_LOCK:
        if proc in _ACTIVE_PROCS:
            _ACTIVE_PROCS[proc] = cancel_event


def _terminate_process(proc):
    if not proc or proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                creationflags=globals().get("_NO_WINDOW", 0),
            )
        else:
            proc.terminate()
    except Exception:
        try:
            proc.kill()
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass


def cancel_active_processes(cancel_event=None):
    with _ACTIVE_PROCS_LOCK:
        items = list(_ACTIVE_PROCS.items())
    stopped = 0
    for proc, proc_cancel_event in items:
        if cancel_event is not None and proc_cancel_event is not cancel_event:
            continue
        if proc and proc.poll() is None:
            _terminate_process(proc)
            stopped += 1
    return stopped


def _wait_process(proc, timeout=None, cancel_event=None, poll_interval=0.2):
    _associate_process_cancel_event(proc, cancel_event)
    deadline = time.time() + timeout if timeout else None
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                _terminate_process(proc)
                raise RuntimeError("cancelled")
            try:
                rc = proc.wait(timeout=poll_interval)
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("cancelled")
                return rc
            except subprocess.TimeoutExpired:
                if deadline and time.time() >= deadline:
                    raise
    finally:
        _unregister_process(proc)


def _communicate_process(proc, timeout=None, cancel_event=None, poll_interval=0.2):
    _associate_process_cancel_event(proc, cancel_event)
    deadline = time.time() + timeout if timeout else None
    try:
        while True:
            if cancel_event and cancel_event.is_set():
                _terminate_process(proc)
                raise RuntimeError("cancelled")
            try:
                result = proc.communicate(timeout=poll_interval)
                if cancel_event and cancel_event.is_set():
                    raise RuntimeError("cancelled")
                return result
            except subprocess.TimeoutExpired:
                if deadline and time.time() >= deadline:
                    raise
    finally:
        _unregister_process(proc)


def _clip_preview_exact(clip):
    if not isinstance(clip, (list, tuple)):
        return False
    return any(isinstance(item, dict) and item.get("preview_exact") for item in clip)


def _infer_mix_source_idx_from_srt(text, start, end, srt_entries):
    """Resolve a markerless mix clip only when its words identify one source."""
    import difflib

    def _normalized(value):
        value = re.sub(r"\[V\d+\]\s*", "", str(value or ""), flags=re.I).lower()
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)

    clip_text = _normalized(text)
    if not clip_text or not srt_entries:
        return -1

    clip_start = float(start)
    clip_end = float(end)
    clip_mid = (clip_start + clip_end) / 2.0
    best_by_source = {}
    for entry in srt_entries:
        entry_text = _normalized(entry.get("text", ""))
        if not entry_text:
            continue
        ratio = difflib.SequenceMatcher(None, clip_text, entry_text).ratio()
        contained = entry_text in clip_text or clip_text in entry_text
        if contained:
            ratio = max(ratio, 0.96)
        # Every source starts at zero, so matching timestamps cannot identify a
        # source by themselves. Require meaningful text evidence first.
        if ratio < 0.45:
            continue

        entry_start = float(entry.get("start", 0))
        entry_end = float(entry.get("end", entry_start))
        overlap = max(0.0, min(clip_end, entry_end) - max(clip_start, entry_start))
        shorter_duration = max(0.2, min(clip_end - clip_start, entry_end - entry_start))
        time_score = min(0.08, overlap / shorter_duration * 0.08) if overlap > 0 else 0.0
        mid_diff = abs(clip_mid - ((entry_start + entry_end) / 2.0))
        if mid_diff <= 1.5:
            time_score += 0.04
        score = ratio + time_score
        source_idx = int(entry.get("source_idx", -1))
        if source_idx < 0:
            continue
        previous = best_by_source.get(source_idx)
        candidate = (score, ratio, contained)
        if previous is None or candidate[0] > previous[0]:
            best_by_source[source_idx] = candidate

    ranked = sorted(
        ((score, ratio, contained, source_idx) for source_idx, (score, ratio, contained) in best_by_source.items()),
        reverse=True,
    )
    if not ranked:
        return -1
    best_score, best_ratio, best_contained, best_idx = ranked[0]
    if best_ratio < 0.45:
        return -1
    if len(ranked) > 1:
        next_score, _next_ratio, next_contained, _next_idx = ranked[1]
        clearly_stronger_containment = best_contained and not next_contained
        if best_score - next_score < 0.08 and not clearly_stronger_containment:
            return -1
    return best_idx


def _mix_semantic_segments_for_source(srt_entries, word_segments, marker, source):
    """Keep every mix source visible when only some sources have word timing sidecars."""
    marker = str(marker or "").strip().upper()
    source = str(source or "").strip()
    result = []
    for segment in word_segments or []:
        if not isinstance(segment, dict):
            continue
        item = dict(segment)
        item["source_marker"] = marker
        item["source"] = source
        item["timing_precision"] = "word"
        result.append(item)
    if result:
        return result

    for segment in srt_entries or []:
        if not isinstance(segment, dict):
            continue
        try:
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or start)
        except (TypeError, ValueError):
            continue
        text = str(segment.get("text") or "").strip()
        if not text or end <= start:
            continue
        result.append({
            "start": start,
            "end": end,
            "text": text,
            "words": [],
            "semantic_unit": True,
            "source_marker": marker,
            "source": source,
            "timing_precision": "srt",
        })
    return result


def _get_video_encoder():
    global _hw_encoder_checked, _hw_encoder
    if not _hw_encoder_checked:
        _hw_encoder_checked = True
        try:
            import importlib
            import platform_config as _pc
            _pc = importlib.reload(_pc)
            _hw_encoder = _pc.HARDWARE_ENCODER  # "h264_qsv" or None
        except Exception:
            _hw_encoder = None
    return _hw_encoder


def _software_vcodec_args():
    """Return a software encoder supported by the current FFmpeg."""
    global _sw_encoder_checked, _sw_encoder_args
    if _sw_encoder_checked and _sw_encoder_args:
        return list(_sw_encoder_args)
    _sw_encoder_checked = True
    try:
        ffmpeg = get_ffmpeg_cmd()
        ret = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            creationflags=_NO_WINDOW,
        )
        encoders = (ret.stdout or "") + (ret.stderr or "")
        if "libx264" in encoders:
            _sw_encoder_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]
        else:
            _sw_encoder_args = ["-c:v", "mpeg4", "-q:v", "2"]
    except Exception:
        _sw_encoder_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]
    return list(_sw_encoder_args)


def _software_encoder_name():
    args = _software_vcodec_args()
    try:
        return args[args.index("-c:v") + 1]
    except Exception:
        return "libx264"


def _intermediate_software_vcodec_args():
    """Higher-quality software encoding for temporary clip files."""
    args = _software_vcodec_args()
    if "libx264" in args:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "12"]
    return args


def _final_software_vcodec_args():
    """Higher-quality software encoding for final filtered outputs."""
    args = _software_vcodec_args()
    if "libx264" in args:
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14"]
    return args


def _final_vcodec_args():
    """Video encoder args for final filtered outputs such as subtitles/PIP."""
    global _hw_fallback
    enc = _get_video_encoder()
    if _hw_fallback or enc is None:
        return _final_software_vcodec_args()
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "fast", "-global_quality", "16"]
    elif enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-qp", "16"]
    elif enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "fast", "-cq", "16", "-b:v", "0"]
    return _final_software_vcodec_args()


def _final_audio_sync_args():
    """Re-encode final audio so video filters and AAC timestamps stay aligned."""
    return [
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        "-af", "aresample=async=1:first_pts=0",
        "-shortest",
    ]


def _intermediate_vcodec_args():
    """Video encoder args for temporary clips; keep final-output encoder settings separate."""
    global _hw_fallback
    enc = _get_video_encoder()
    if _hw_fallback or enc is None:
        return _intermediate_software_vcodec_args()
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "fast", "-global_quality", "16"]
    elif enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-qp", "16"]
    elif enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "fast", "-cq", "16", "-b:v", "0"]
    return _intermediate_software_vcodec_args()


def _hardware_encoder_requested():
    try:
        import platform_config as _pc
        return bool(getattr(_pc, "ENABLE_HARDWARE_ENCODER", False))
    except Exception:
        return False


def _with_software_encoder(cmd, software_args=None):
    """Replace hardware video encoder options while keeping output options before the output file."""
    if not cmd:
        return cmd
    output = cmd[-1]
    body = cmd[:-1]
    cleaned = []
    skip_next = False
    skip_opts = {"-c:v", "-vcodec", "-qp", "-global_quality", "-quality", "-preset", "-cq", "-b:v", "-crf"}
    for arg in body:
        if skip_next:
            skip_next = False
            continue
        if arg in skip_opts:
            skip_next = True
            continue
        if isinstance(arg, str) and arg.startswith("h264_"):
            continue
        cleaned.append(arg)
    return cleaned + (software_args or _software_vcodec_args()) + [output]


def _command_uses_hardware_encoder(cmd):
    return any(isinstance(arg, str) and arg.startswith("h264_") for arg in (cmd or []))


def _run_ffmpeg_with_hw_fallback(cmd, popen_kw, timeout, _log, stage_name, output_path, software_args=None, cancel_event=None):
    """Run FFmpeg once, then retry with software encoding if the hardware encoder fails."""
    global _hw_fallback
    try:
        if output_path and os.path.exists(output_path):
            os.remove(output_path)
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass
    proc = _register_process(subprocess.Popen(cmd, **popen_kw, creationflags=_NO_WINDOW))
    try:
        _, stderr_data = _communicate_process(proc, timeout=timeout, cancel_event=cancel_event)
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        proc.communicate()
        raise
    ok = proc.returncode == 0 and (not output_path or (os.path.exists(output_path) and os.path.getsize(output_path) > 1000))
    if ok:
        return True, proc.returncode, stderr_data

    if _command_uses_hardware_encoder(cmd):
        sw_args = software_args or _final_software_vcodec_args()
        sw_name = sw_args[sw_args.index("-c:v") + 1] if "-c:v" in sw_args else _software_encoder_name()
        _log(f"{stage_name}硬件编码失败，切换到 {sw_name} 软件编码重试。")
        _hw_fallback = True
        try:
            if output_path and os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
        retry_cmd = _with_software_encoder(cmd, sw_args)
        proc2 = _register_process(subprocess.Popen(retry_cmd, **popen_kw, creationflags=_NO_WINDOW))
        try:
            _, stderr_retry = _communicate_process(proc2, timeout=timeout, cancel_event=cancel_event)
        except subprocess.TimeoutExpired:
            _terminate_process(proc2)
            proc2.communicate()
            raise
        ok2 = proc2.returncode == 0 and (not output_path or (os.path.exists(output_path) and os.path.getsize(output_path) > 1000))
        if ok2:
            _log(f"{stage_name}软件编码重试成功。")
        return ok2, proc2.returncode, stderr_retry

    return False, proc.returncode, stderr_data


def _vcodec_args():
    """返回视频编码参数，优先硬件编码（如果硬件编码不行自动回退 libx264）"""
    global _hw_fallback, _hw_encoder_checked, _hw_encoder
    enc = _get_video_encoder()
    if _hw_fallback or enc is None:
        return _final_software_vcodec_args()
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "fast", "-global_quality", "16"]
    elif enc == "h264_amf":
        return ["-c:v", "h264_amf", "-quality", "speed", "-qp", "16"]
    elif enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "fast", "-cq", "16", "-b:v", "0"]
    else:
        return _final_software_vcodec_args()


def _stable_output_fps(fps=None):
    try:
        value = float(fps if fps is not None else VIDEO_CONFIG.get("fps", 30))
    except Exception:
        value = 30.0
    return max(1.0, min(120.0, value))


def _format_fps_value(fps=None):
    value = _stable_output_fps(fps)
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _stable_video_tail_filter(fps=None):
    value = _stable_output_fps(fps)
    # Rebuild video PTS from frame index so joins cannot inherit uneven packet timing.
    return f"fps={value:.3f}:round=near,settb=AVTB,setpts=N/({value:.6f}*TB),format=yuv420p"


def _append_stable_video_tail_filter(vf, fps=None):
    return _append_filter(vf, _stable_video_tail_filter(fps))


def _stable_audio_tail_filter():
    return "asetpts=PTS-STARTPTS,aresample=async=1:first_pts=0,asetpts=N/SR/TB"


def _stable_cfr_output_args(fps=None):
    value = _stable_output_fps(fps)
    timescale = max(1000, int(round(value * 1000)))
    return ["-r", _format_fps_value(value), "-vsync", "cfr", "-video_track_timescale", str(timescale)]


def _smart_crop_no_crop_vf(src_w, src_h, out_w, out_h):
    return "scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,crop=%d:%d" % (out_w, out_h, out_w, out_h)


def _parse_resolution_pair(resolution, fallback=(1080, 1920)):
    try:
        parts = re.split(r"[:xX]", str(resolution or ""))
        if len(parts) >= 2:
            w, h = int(parts[0]), int(parts[1])
            if w > 0 and h > 0:
                return w, h
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass
    return fallback


def _even_source_dim(value):
    value = int(value or 0)
    if value <= 0:
        return 2
    return value if value % 2 == 0 else value - 1


def _standard_vertical_tiers(default_w, default_h):
    tiers = [(720, 1280), (1080, 1920), (1440, 2560), (2160, 3840)]
    default_pair = (int(default_w), int(default_h))
    if default_pair not in tiers:
        tiers.append(default_pair)
    return sorted(tiers, key=lambda item: item[0] * item[1])


def _output_resolution_for_source(src_w, src_h, default_resolution="1080:1920"):
    default_w, default_h = _parse_resolution_pair(default_resolution)
    src_w, src_h = int(src_w or 0), int(src_h or 0)
    if src_w <= 0 or src_h <= 0:
        return default_w, default_h, "默认竖屏尺寸"

    target_ratio = default_w / max(default_h, 1)
    source_ratio = src_w / max(src_h, 1)
    ratio_delta = abs(source_ratio - target_ratio) / max(target_ratio, 0.001)

    if src_h >= src_w and ratio_delta <= 0.03:
        best_tier = None
        best_delta = None
        for tier_w, tier_h in _standard_vertical_tiers(default_w, default_h):
            size_delta = max(
                abs(src_w - tier_w) / max(tier_w, 1),
                abs(src_h - tier_h) / max(tier_h, 1),
            )
            if size_delta <= 0.06 and (best_delta is None or size_delta < best_delta):
                best_tier = (tier_w, tier_h)
                best_delta = size_delta
        if best_tier:
            tier_w, tier_h = best_tier
            tier_label = f"{tier_w}x{tier_h}档"
            return tier_w, tier_h, f"标准9:16规整({tier_label})，避免发布黑边"

    return _even_source_dim(src_w), _even_source_dim(src_h), "保持源分辨率"


def _probe_video_size(ffmpeg, video_path, timeout=30):
    try:
        probe = subprocess.run(
            [ffmpeg, "-i", video_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        stderr = probe.stderr.decode("utf-8", errors="replace") if isinstance(probe.stderr, bytes) else (probe.stderr or "")
        best_w = best_h = best_area = 0
        for match in re.finditer(r"(\d+)x(\d+)", stderr):
            test_w, test_h = int(match.group(1)), int(match.group(2))
            area = test_w * test_h
            if area > best_area:
                best_w, best_h, best_area = test_w, test_h, area
        if best_w > 100 and best_h > 100:
            return best_w, best_h
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass
    return 0, 0


def _is_ts_like_video(video_path):
    return os.path.splitext(str(video_path or ""))[1].lower() in {".ts", ".mts", ".m2ts"}


def _append_seek_input_args(cmd, video_path, start, accurate=False):
    try:
        start_value = max(0.0, float(start))
    except Exception:
        start_value = 0.0
    if _is_ts_like_video(video_path):
        cmd += ["-fflags", "+genpts"]
    if accurate and start_value > 0.001:
        # Preview-selected sub-sentences need frame-accurate cutting. Use a small
        # preroll for speed, then trim precisely after decoding.
        pre_seek = max(0.0, start_value - 2.0)
        fine_seek = start_value - pre_seek
        cmd += ["-ss", f"{pre_seek:.3f}", "-i", video_path]
        if fine_seek > 0.001:
            cmd += ["-ss", f"{fine_seek:.3f}"]
        return cmd
    cmd += ["-ss", f"{start_value:.3f}", "-i", video_path]
    return cmd


def _preview_exact_seek_window(start, duration):
    try:
        start_value = max(0.0, float(start))
    except Exception:
        start_value = 0.0
    try:
        duration_value = max(0.2, float(duration))
    except Exception:
        duration_value = 0.2
    input_seek = max(0.0, start_value - 2.0)
    filter_start = max(0.0, start_value - input_seek)
    input_duration = max(0.3, duration_value + filter_start + 0.25)
    return input_seek, filter_start, input_duration


def _preview_exact_cut_cmd(ffmpeg, video_path, start, duration, video_filter, audio_filter, output_path, vcodec_args, fps=None):
    input_seek, filter_start, input_duration = _preview_exact_seek_window(start, duration)
    duration_value = max(0.2, float(duration))
    vf = (
        f"trim=start={filter_start:.3f}:duration={duration_value:.3f},"
        f"setpts=PTS-STARTPTS,{video_filter}"
    )
    af = (
        f"atrim=start={filter_start:.3f}:duration={duration_value:.3f},"
        f"{audio_filter}"
    )
    cmd = [ffmpeg, "-y"]
    if _is_ts_like_video(video_path):
        cmd += ["-fflags", "+genpts"]
    cmd += ["-ss", f"{input_seek:.3f}", "-i", video_path, "-t", f"{input_duration:.3f}"]
    cmd += _stable_cfr_output_args(fps)
    cmd += list(vcodec_args or _intermediate_software_vcodec_args())
    cmd += [
        "-filter_complex", f"[0:v]{vf}[v];[0:a]{af}[a]",
        "-map", "[v]", "-map", "[a]",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-shortest", "-movflags", "+faststart", output_path,
    ]
    return cmd


def _remux_ts_for_editing(video_path, temp_dir, ffmpeg, log_fn=None):
    """Normalize TS-family files to an edit-friendly MP4 before ASR/cutting."""
    if not _is_ts_like_video(video_path):
        return video_path

    def _log(msg):
        if log_fn:
            log_fn(msg)

    try:
        cache_dir = temp_dir or os.path.join(tempfile.gettempdir(), "liveclipper_ts_normalized")
        os.makedirs(cache_dir, exist_ok=True)
        now = time.time()
        for name in os.listdir(cache_dir):
            if not name.startswith("ts_normalized_") or not name.endswith(".mp4"):
                continue
            path = os.path.join(cache_dir, name)
            try:
                if now - os.path.getmtime(path) > 2 * 24 * 3600:
                    os.remove(path)
            except Exception:
                _LOG.warning("unexpected error", exc_info=True)
                pass

        base = os.path.splitext(os.path.basename(video_path))[0]
        safe_base = re.sub(r"[^0-9A-Za-z._-]+", "_", base).strip("._") or "source"
        import hashlib
        stat = os.stat(video_path)
        sig_src = f"{os.path.abspath(video_path)}|{stat.st_size}|{int(stat.st_mtime)}"
        digest = hashlib.md5(sig_src.encode("utf-8", errors="ignore")).hexdigest()[:10]
        out_path = os.path.join(cache_dir, f"ts_normalized_{safe_base}_{digest}.mp4")
        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            _log(f"TS normalize: reusing cached CFR 30fps MP4 ({os.path.basename(out_path)}).")
            return out_path

        _log("TS normalize: transcoding to edit-safe MP4 (PTS=0, CFR 30fps, AAC 44.1k).")
        started = time.time()
        cmd = [
            ffmpeg, "-y",
            "-fflags", "+genpts",
            "-i", video_path,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-vf", "setpts=PTS-STARTPTS,fps=30,format=yuv420p",
            "-af", "aresample=async=1:first_pts=0",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "16",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-ac", "2",
            "-movflags", "+faststart",
            out_path,
        ]
        ret = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=7200,
            creationflags=_NO_WINDOW,
        )
        if ret.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            _log(f"TS normalize: done in {time.time() - started:.1f}s.")
            return out_path
        _log(f"TS normalize: failed, falling back to original TS (rc={ret.returncode}).")
        if ret.stderr:
            for line in ret.stderr.strip().splitlines()[-5:]:
                if line.strip():
                    _log(f"  ffmpeg: {line.strip()[:160]}")
    except subprocess.TimeoutExpired:
        _log("TS normalize: timeout, falling back to original TS.")
    except Exception as exc:
        _log(f"TS normalize: error, falling back to original TS: {exc}")
    return video_path


def _smart_crop_zoom(sc_crop):
    crop = sc_crop or {}
    try:
        if crop.get("method") == "smart":
            return 1.0 / max(float(crop.get("crop_w", 1.0) or 1.0), 0.01)
        return float(crop.get("zoom", 1.0) or 1.0)
    except Exception:
        return 1.0


def _kb_quality_cap_for_zoom(smart_zoom):
    try:
        zoom = float(smart_zoom or 1.0)
    except Exception:
        return None
    if zoom >= 1.12:
        return 0.05
    if zoom >= 1.08:
        return 0.08
    if zoom >= 1.04:
        return 0.10
    return None


def _final_sharpen_vf():
    return "unsharp=5:5:0.22:3:3:0.00"


def _smart_crop_vf(sc_crop, src_w, src_h, out_w, out_h, even_fn, log_fn=None):
    """Build SmartCrop filter; skip tiny zooms that only cause soft upsampling."""
    crop = sc_crop or {}
    min_visible_zoom = 1.04
    if crop.get("method") == "smart":
        zoom = 1.0 / max(float(crop.get("crop_w", 1.0) or 1.0), 0.01)
        if zoom < min_visible_zoom:
            if log_fn:
                log_fn(f"SmartCrop: zoom={zoom:.2f}x 小幅构图，为保清晰度跳过上采样")
            return _smart_crop_no_crop_vf(src_w, src_h, out_w, out_h)
        cw = even_fn(int(src_w * crop["crop_w"]))
        ch = even_fn(int(src_h * crop["crop_h"]))
        cx = even_fn(int(src_w * crop["crop_x"]))
        cy = even_fn(int(src_h * crop["crop_y"]))
        cw = max(2, min(cw, src_w))
        ch = max(2, min(ch, src_h))
        cx = max(0, min(cx, max(0, src_w - cw)))
        cy = max(0, min(cy, max(0, src_h - ch)))
        if log_fn:
            log_fn(f"SmartCrop: 应用封面构图 zoom={zoom:.2f}x crop={cw}x{ch}+{cx}+{cy}")
        return "crop=%d:%d:%d:%d,scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,crop=%d:%d" % (cw, ch, cx, cy, out_w, out_h, out_w, out_h)

    zoom = float(crop.get("zoom", 1.08) or 1.08)
    if zoom < min_visible_zoom:
        if log_fn:
            log_fn(f"SmartCrop: zoom={zoom:.2f}x 兜底构图变化过小，为保清晰度跳过")
        return _smart_crop_no_crop_vf(src_w, src_h, out_w, out_h)
    rcw = even_fn(int(src_w / zoom))
    rch = even_fn(int(src_h / zoom))
    rcx = even_fn(int((src_w - rcw) / 2))
    rcy = even_fn(int(src_h - rch))
    rcw = max(2, min(rcw, src_w))
    rch = max(2, min(rch, src_h))
    rcx = max(0, min(rcx, max(0, src_w - rcw)))
    rcy = max(0, min(rcy, max(0, src_h - rch)))
    if log_fn:
        log_fn(f"SmartCrop: 应用兜底构图 zoom={zoom:.2f}x crop={rcw}x{rch}+{rcx}+{rcy}")
    return "crop=%d:%d:%d:%d,scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,crop=%d:%d" % (rcw, rch, rcx, rcy, out_w, out_h, out_w, out_h)
from config import (
    CLIP_KEYWORDS, CLIP_ORDER, VIDEO_CONFIG, FFMPEG_PATH,
    DEDUP_CONFIG, DEDUP_PRESET, SUBTITLE_OVERLAY,
    FILLER_WORDS, CLIP_DURATION_RANGE,
    NEGATIVE_SIGNALS, NEGATION_WORDS, TEXT_OPTIMIZATION,
    TARGET_DURATION, TARGET_DURATION_TOLERANCE, REQUIRED_CLIP_TYPES,
    TIME_WINDOW_MINUTES,
)



_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
def get_ffmpeg_cmd():
    from platform_config import FFMPEG_CMD
    if FFMPEG_CMD and os.path.exists(FFMPEG_CMD):
        return FFMPEG_CMD
    # 回退：打包目录中查找
    import sys
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    for name in ["ffmpeg", "ffmpeg.exe"]:
        p = os.path.join(base, "ffmpeg", name)
        if os.path.exists(p):
            return p
    return "ffmpeg"


# ============================================================
# 去重逻辑（保持不变）
# ============================================================

def rand_range(min_val, max_val, decimals=2):
    return round(random.uniform(min_val, max_val), decimals)


def apply_preset(preset):
    """应用去重预设。返回 (方法数量, 配置字典)"""
    config = dict(DEDUP_CONFIG)
    methods = dict(config.get("methods", {}))
    strategy = config.get("strategy", "classic")

    if preset == "none":
        for m in methods: methods[m]["enabled"] = False
        return 0, methods, strategy
    elif preset == "light":
        # 轻度：只镜像 + 轻微变速
        for m in methods: methods[m]["enabled"] = False
        return 0, methods, strategy
    elif preset == "medium":
        return 0, methods, strategy
    elif preset == "heavy":
        return 0, methods, strategy
    else:
        return 0, methods, strategy


def _generate_random_dedup_params(clip_index, preset=None):
    """
    生成随机去重参数（每段唯一）
    返回: dict with speed, crop_w, crop_h, crop_x, crop_y, audio_pitch
    """
    cfg = DEDUP_CONFIG
    preset = str(preset or DEDUP_PRESET or "medium").strip().lower()
    rng = random.Random(clip_index * 7919 + random.randint(0, 99999))

    params = {}

    # 1. 加权随机变速
    if cfg.get("variable_speed", {}).get("enabled"):
        sc = cfg["variable_speed"]
        if preset == "light":
            speed = round(rng.uniform(1.03, 1.08), sc["decimal_precision"])
        elif preset == "heavy":
            speed = round(rng.uniform(1.18, min(1.30, sc["max_rate"])), sc["decimal_precision"])
        elif rng.random() <= sc["weight_low"]:
            speed = round(rng.uniform(sc["min_rate"], 1.20), sc["decimal_precision"])
        else:
            speed = round(rng.uniform(1.20, sc["max_rate"]), sc["decimal_precision"])
        speed = min(speed, sc["max_rate"])
        params["speed"] = speed
    else:
        params["speed"] = 1.0

    # 2. 随机微裁剪
    if cfg.get("random_crop", {}).get("enabled"):
        rc = cfg["random_crop"]
        if preset == "light":
            crop_min, crop_max = 0.990, 0.996
            offset_min, offset_max = 0.002, 0.008
        elif preset == "heavy":
            crop_min, crop_max = 0.955, 0.982
            offset_min, offset_max = 0.006, 0.022
        else:
            crop_min, crop_max = rc["crop_min"], rc["crop_max"]
            offset_min, offset_max = rc["offset_min"], rc["offset_max"]
        params["crop_w"] = round(rng.uniform(crop_min, crop_max), 3)
        params["crop_h"] = round(rng.uniform(crop_min, crop_max), 3)
        params["crop_x"] = round(rng.uniform(offset_min, offset_max), 3)
        params["crop_y"] = round(rng.uniform(offset_min, offset_max), 3)
    else:
        params["crop_w"] = 1.0
        params["crop_h"] = 1.0
        params["crop_x"] = 0.0
        params["crop_y"] = 0.0

    # 3. 音频微pitch
    if cfg.get("audio_pitch", {}).get("enabled"):
        ap = cfg["audio_pitch"]
        params["audio_pitch"] = round(rng.uniform(ap["min_pitch"], ap["max_pitch"]), 2)
    else:
        params["audio_pitch"] = 0.0

    # 4. 伽马微调
    if cfg.get("gamma_shift", {}).get("enabled"):
        if preset == "light":
            g = (-0.008, 0.008)
        elif preset == "heavy":
            g = (-0.03, 0.03)
        else:
            g = cfg["gamma_shift"]["range"]
        params["gamma"] = round(rng.uniform(g[0], g[1]), 3)
    else:
        params["gamma"] = 0.0

    # 5. 新增方法开关
    if preset == "light":
        params["corner_mask"] = False
        params["audio_reverb"] = False
        params["noise_fusion"] = False
    else:
        reverb_probability = 0.85 if preset == "heavy" else cfg.get("audio_reverb", {}).get("probability", 0.5)
        noise_probability = 0.75 if preset == "heavy" else cfg.get("noise_fusion", {}).get("probability", 0.4)
        params["corner_mask"] = cfg.get("corner_mask", {}).get("enabled", False)
        params["audio_reverb"] = cfg.get("audio_reverb", {}).get("enabled", False) and rng.random() < reverb_probability
        params["noise_fusion"] = cfg.get("noise_fusion", {}).get("enabled", False) and rng.random() < noise_probability
    params["frame_interp"] = cfg.get("frame_interpolation", {}).get("enabled", False) and rng.random() < cfg.get("frame_interpolation", {}).get("probability", 0.3)

    return params


def _dedup_mirror_enabled(mirror_enabled=None):
    if mirror_enabled is None:
        return bool(DEDUP_CONFIG.get("mirror", {}).get("enabled", True))
    return bool(mirror_enabled)


def _normalize_dedup_preset(preset):
    value = str(preset or "medium").strip().lower()
    if value in {"", "off", "close", "closed", "disable", "disabled"}:
        return "none"
    return value


def _bool_option(data, key):
    return bool((data or {}).get(key))


def _num_option(data, key, default=0):
    try:
        return float((data or {}).get(key, default))
    except Exception:
        return float(default)


def _planned_output_speed_factor(dedup_preset="medium", video_options=None):
    preset = _normalize_dedup_preset(dedup_preset)
    if preset == "none":
        return 1.0
    if preset == "custom":
        if _bool_option(video_options, "speed"):
            return max(0.5, min(2.0, _num_option(video_options, "speed_value", 100) / 100))
        return 1.0
    try:
        speed_cfg = DEDUP_CONFIG.get("variable_speed", {})
        if speed_cfg.get("enabled"):
            return max(0.5, min(2.0, float(speed_cfg.get("fallback_speed") or 1.15)))
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass
    return 1.0


def _selection_duration_contract(
    target_duration,
    dedup_preset="medium",
    video_options=None,
    duration_tolerance=None,
):
    speed = _planned_output_speed_factor(dedup_preset, video_options)
    return DurationContract.create(target_duration, speed, tolerance=duration_tolerance)


def _ai_target_duration_for_final_duration(
    target_duration,
    dedup_preset="medium",
    video_options=None,
    duration_tolerance=None,
):
    contract = _selection_duration_contract(
        target_duration,
        dedup_preset,
        video_options,
        duration_tolerance,
    )
    return max(10, contract.ai_target_seconds), contract.speed_factor


def _final_duration_contract(target_duration, duration_tolerance=None):
    contract = DurationContract.create(target_duration, 1.0, tolerance=duration_tolerance)
    return contract.final_target, contract.final_min, contract.final_max


def _selection_duration_total(clips):
    total = 0.0
    for clip in clips or []:
        try:
            if isinstance(clip, dict):
                duration = clip.get("duration")
                if duration is None:
                    duration = float(clip.get("end", 0)) - float(clip.get("start", 0))
            elif len(clip) >= 6:
                duration = clip[5]
            else:
                duration = float(clip[3]) - float(clip[2])
            total += max(0.0, float(duration or 0))
        except Exception:
            continue
    return total


def _selection_shortage_grace_seconds(analysis_metadata):
    relaxation = dict((analysis_metadata or {}).get("duration_relaxation") or {})
    if not relaxation.get("applied"):
        return 0.0
    try:
        return min(
            float(SHORTAGE_GRACE_SECONDS),
            max(0.0, float(relaxation.get("grace_seconds") or 0.0)),
        )
    except (TypeError, ValueError):
        return 0.0


def _validate_selected_duration_contract(
    clips,
    target_duration,
    speed_factor=1.0,
    log_fn=None,
    shortage_grace_seconds=0.0,
    user_confirmed=False,
    duration_tolerance=None,
):
    source_total = _selection_duration_total(clips)
    contract = DurationContract.create(
        target_duration,
        speed_factor,
        tolerance=duration_tolerance,
    )
    status = contract.status(
        source_total,
        shortage_grace_seconds=shortage_grace_seconds,
    )
    speed = contract.speed_factor
    projected = status["projected_final"]
    target, high = contract.final_target, contract.final_max
    standard_low = contract.final_min
    low = float(status["relaxed_low"])
    accepted = bool(status["accepted"] or (user_confirmed and source_total > 0))
    if log_fn:
        log_fn(
            f"时长合同: 片单原时长{source_total:.1f}s，按{speed:.2f}x预计成片{projected:.1f}s，"
            f"要求{low:.0f}-{high:.0f}s"
        )
        if user_confirmed and not status["accepted"]:
            log_fn(
                f"手动片单时长确认: 预计成片{projected:.1f}s低于自动下限{low:.0f}s，"
                "按用户在预览中的最终选择继续成片"
            )
        elif status.get("used_shortage_grace"):
            log_fn(
                f"内容不足时长弹性: 标准下限{standard_low:.0f}s，"
                f"本次宽限{float(shortage_grace_seconds):.0f}s后下限{low:.0f}s"
            )
    if not accepted:
        raise RuntimeError(
            f"AI未满足时长：片单原时长{source_total:.1f}秒，预计成片{projected:.1f}秒，"
            f"目标{target:.0f}秒（允许{low:.0f}-{high:.0f}秒）"
        )
    return {
        "source_total": source_total,
        "projected_final": projected,
        "target": target,
        "low": low,
        "standard_low": standard_low,
        "high": high,
        "shortage_grace_seconds": float(shortage_grace_seconds or 0.0),
        "used_shortage_grace": bool(status.get("used_shortage_grace")),
        "user_confirmed": bool(user_confirmed),
        "duration_contract": contract.to_dict(),
    }


def _validate_actual_duration_contract(
    actual_duration,
    target_duration,
    margin=1.0,
    shortage_grace_seconds=0.0,
    user_confirmed=False,
    duration_tolerance=None,
):
    contract = DurationContract.create(
        target_duration,
        1.0,
        tolerance=duration_tolerance,
        acceptance_margin=margin,
    )
    actual = max(0.0, float(actual_duration or 0.0))
    status = contract.status(
        actual,
        shortage_grace_seconds=shortage_grace_seconds,
    )
    ok = actual > 0 and bool(status["accepted"] or user_confirmed)
    return ok, {
        "actual": actual,
        "target": contract.final_target,
        "low": float(status["relaxed_low"]),
        "standard_low": contract.final_min,
        "high": contract.final_max,
        "shortage_grace_seconds": float(shortage_grace_seconds or 0.0),
        "used_shortage_grace": bool(status.get("used_shortage_grace")),
        "user_confirmed": bool(user_confirmed),
    }


def _append_filter(base, extra):
    base = (base or "").strip()
    extra = (extra or "").strip()
    if not extra:
        return base
    if not base or base == "null":
        return extra
    return f"{base},{extra}"


def _video_options_without_mirror(video_options=None):
    options = dict(video_options or {})
    options["mirror"] = False
    return options


def _manual_dedup_filters(video_options=None, audio_options=None):
    video_options = video_options or {}
    audio_options = audio_options or {}
    vf_list = []
    af_list = []
    applied = []
    if _bool_option(video_options, "mirror"):
        vf_list.append("hflip")
        applied.append("mirror")
    if _bool_option(video_options, "crop"):
        ratio = max(0.8, min(1.0, 1 - _num_option(video_options, "crop_value", 0) / 100))
        vf_list.append(f"crop=iw*{ratio}:ih*{ratio},scale=iw:ih")
        applied.append(f"crop({ratio:.3f})")
    if _bool_option(video_options, "speed"):
        speed = max(0.5, min(2.0, _num_option(video_options, "speed_value", 100) / 100))
        vf_list.append(f"setpts=PTS/{speed:.6f}")
        af_list.append(f"atempo={speed:.3f}")
        applied.append(f"speed({speed:.2f}x)")
    if _bool_option(video_options, "blur"):
        vf_list.append(f"gblur=sigma={max(0.1, _num_option(video_options, 'blur_value', 2))}")
        applied.append("blur")
    if _bool_option(video_options, "sharpen"):
        vf_list.append(f"unsharp=luma_amount={_num_option(video_options, 'sharpen_value', 30) / 50:.2f}")
        applied.append("sharpen")
    if _bool_option(video_options, "gamma_shift"):
        vf_list.append("eq=gamma=1.03:saturation=1.04:contrast=1.02")
        applied.append("gamma")
    if _bool_option(video_options, "corner_mask"):
        vf_list.append("drawbox=x=0:y=0:w=42:h=42:color=black@0.18:t=fill")
        applied.append("corner_mask")
    if _bool_option(video_options, "bg_fill"):
        vf_list.append("pad=iw+40:ih+40:20:20:color=black")
        applied.append("bg_fill")
    if _bool_option(audio_options, "pitch"):
        af_list.append("asetrate=44100*1.015,aresample=44100,atempo=0.985222")
        applied.append("audio_pitch")
    if _bool_option(audio_options, "reverb"):
        af_list.append("aecho=0.8:0.7:60:0.25")
        applied.append("reverb")
    if _bool_option(audio_options, "noise_fusion"):
        af_list.append("volume=1.0")
        applied.append("audio_fusion")
    return {"video_filters": ",".join(vf_list), "audio_filters": ",".join(af_list), "applied": applied}


def _custom_frame_structure_filter(video_options=None, source_fps=30.0):
    if not _bool_option(video_options, "frame_structure"):
        return "", ""
    level = str((video_options or {}).get("frame_structure_level") or "medium").strip().lower()
    if level in {"light", "轻度"}:
        level = "light"
    elif level in {"heavy", "强力"}:
        level = "heavy"
    else:
        level = "medium"
    try:
        fps = float(source_fps or 30.0)
    except Exception:
        fps = 30.0
    # The final encoder already emits CFR at the configured source rate. Lowering
    # FPS here and forcing it back later duplicates frames and causes visible
    # periodic stalls, especially around clip boundaries.
    target_fps = max(15.0, min(60.0, fps))
    return f"fps=fps={target_fps:.3f}:round=near", f"frame_structure({level},stable-{target_fps:.2f}fps)"


def build_dedup_filters(width, height, clip_index=0, mirror_enabled=None):
    """
    构建去重滤镜链
    - enhanced模式: 镜像 + 随机变速 + 随机微裁剪（pitch已移除）
    - classic模式: 原有随机方法（兼容）
    """
    preset = _normalize_dedup_preset(DEDUP_PRESET)
    if preset == "none":
        return {"video_filters": "", "audio_filters": "", "applied": []}
    _, methods, strategy = apply_preset(preset)
    mirror_enabled = _dedup_mirror_enabled(mirror_enabled)

    if strategy == "enhanced":
        return _build_enhanced_dedup(width, height, clip_index, mirror_enabled=mirror_enabled, preset=preset)
    else:
        return _build_classic_dedup(width, height, clip_index, methods, mirror_enabled=mirror_enabled)


def _build_enhanced_dedup(width, height, clip_index, mirror_enabled=True, preset="medium"):
    """增强版去重：镜像 + 随机变速 + 随机微裁剪（pitch已移除，修复音画不同步）"""
    cfg = DEDUP_CONFIG
    params = _generate_random_dedup_params(clip_index, preset=preset)

    vf_list = []
    af_list = []
    applied = []

    # 1. 水平镜像（80%概率开启，增加随机性）
    mirror_probability = {"light": 0.45, "medium": 0.8, "heavy": 1.0, "custom": 0.8}.get(preset, 0.8)
    if mirror_enabled and random.random() < mirror_probability:
        vf_list.append("hflip")
        applied.append("mirror")

    # 2. 随机微裁剪（先裁再缩放，保证输出分辨率不变）
    cw, ch = params["crop_w"], params["crop_h"]
    cx, cy = params["crop_x"], params["crop_y"]
    if cw < 1.0 or ch < 1.0:
        crop_w = int(width * cw) + (int(width * cw) % 2)
        crop_h = int(height * ch) + (int(height * ch) % 2)
        crop_x = int(width * cx)
        crop_y = int(height * cy)
        # 确保裁剪区域不超出画面
        if crop_x + crop_w > width: crop_x = width - crop_w
        if crop_y + crop_h > height: crop_y = height - crop_h
        vf_list.append(f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y}")
        vf_list.append(f"scale={width}:{height}:flags=lanczos")
        applied.append(f"crop({cw:.3f}x{ch:.3f})")

    # [v9.1] 变速：setpts和atempo使用同一个speed值，确保音画绝对同步
    # pitch已移除——之前视频用speed变速、音频用speed*pitch变速，速率不一致是音画不同步的根因
    speed = params["speed"]

    if speed != 1.0:
        vf_list.append(f"setpts=PTS/{speed}")   # 视频变速
        af_list.append(f"atempo={speed}")         # 音频变速（同值，保证同步）
        applied.append(f"speed({speed}x)")

    # 5. 伽马微调（等级1，肉眼不可见）
    gamma = params.get("gamma", 0.0)
    if gamma != 0.0:
        vf_list.append(f"eq=gamma={1.0 + gamma:.3f}")
        applied.append(f"gamma({gamma:+.3f})")

    # 6. 四角微遮罩（等级2）
    if params.get("corner_mask"):
        s = cfg.get("corner_mask", {})
        pct = s.get("size_pct", 0.005)
        clr = s.get("color", "0x000000")
        bw = max(int(width * pct), 2)
        bh = max(int(height * pct), 2)
        # 四个角各画一个小矩形
        corners = [
            f"drawbox=x=0:y=0:w={bw}:h={bh}:c={clr}:t=fill",
            f"drawbox=x=iw-{bw}:y=0:w={bw}:h={bh}:c={clr}:t=fill",
            f"drawbox=x=0:y=ih-{bh}:w={bw}:h={bh}:c={clr}:t=fill",
            f"drawbox=x=iw-{bw}:y=ih-{bh}:w={bw}:h={bh}:c={clr}:t=fill",
        ]
        vf_list.extend(corners)
        applied.append("corner_mask")

    # 7. 音频极轻微混响（等级2）
    if params.get("audio_reverb"):
        af_list.append("aecho=0.8:0.88:60:0.4")
        applied.append("reverb")

    # 8. 双音轨融合 - 原音+极轻白噪音（等级3）
    if params.get("noise_fusion"):
        nv = cfg.get("noise_fusion", {}).get("noise_volume", 0.001)
        af_list.append(f"aevalsrc=-{nv}*random(0):c=stereo:s=44100")
        af_list.append("amix=inputs=2:duration=first:dropout_transition=0")
        applied.append("noise_fusion")

    # 9. 单帧插值（等级3，默认关闭）
    if params.get("frame_interp"):
        vf_list.append("minterpolate=mi_mode=blend:fps=30.5")
        applied.append("frame_interp")

    # 日志输出参数
    _log_dedup_params(params)

    return {
        "video_filters": ",".join(vf_list),
        "audio_filters": ",".join(af_list),
        "applied": applied,
    }


def _build_classic_dedup(width, height, clip_index, methods, mirror_enabled=True):
    """经典去重模式（兼容原有逻辑）"""
    enabled_methods = [name for name, c in methods.items() if c.get("enabled")]
    if not mirror_enabled:
        enabled_methods = [name for name in enabled_methods if name != "mirror"]
    if not enabled_methods:
        return {"video_filters": "", "audio_filters": "", "applied": []}

    count = min(2, len(enabled_methods))
    chosen = random.sample(enabled_methods, count)
    vf_list, af_list = [], []
    rng = random.Random(clip_index * 1000 + random.randint(0, 9999))

    if "speed_change" in chosen:
        s = round(rng.uniform(methods["speed_change"]["min_speed"], methods["speed_change"]["max_speed"]), 3)
        vf_list.append(f"setpts=PTS/{s}"); af_list.append(f"atempo={s}")
    if "zoom_crop" in chosen:
        sc = round(rng.uniform(methods["zoom_crop"]["min_scale"], methods["zoom_crop"]["max_scale"]), 3)
        nw = int(width*sc) + int(width*sc)%2; nh = int(height*sc) + int(height*sc)%2
        vf_list.append(f"scale={nw}:{nh}:flags=lanczos"); vf_list.append(f"crop={width}:{height}")
    if "mirror" in chosen: vf_list.append("hflip")

    return {"video_filters": ",".join(vf_list), "audio_filters": ",".join(af_list), "applied": chosen}


def _log_dedup_params(params):
    """输出本次去重参数（供追溯）"""
    import sys
    _log = lambda msg: None  # 会在 process_video 里通过 _log 函数使用
    # 这里用 print 输出到标准输出，process_video 的 _log 会捕获
    pass


# ============================================================
# v3.0: 文案逻辑优化
# ============================================================

def _clean_text(text):
    """清理语气词和冗余 + Whisper 识别纠错"""
    t = text
    for w in FILLER_WORDS:
        t = t.replace(w, "")
    # 去掉多余空格
    t = re.sub(r"\s+", "", t).strip()
    # 去掉纯标点
    t = re.sub(r"^[，。！？、\s]+", "", t)
    t = re.sub(r"[，。！？、\s]+$", "", t)

    # Whisper 常见误识别修复（服装直播场景）
    whisper_fixes = {
        "30米": "30元", "100单": "100单", "1,000单": "1000单",
        "米的优惠券": "元的优惠券", "米的券": "元的券",
        "到手架隔": "到手价", "到手只要1": "到手只要",
        "给到我们到手": "到手",
        "还给你们": "",
        "是的": "",
        "对首批": "首批",
        "应该是7": "应该是7天",
    }
    for wrong, right in whisper_fixes.items():
        t = t.replace(wrong, right)

    t = re.sub(r"\s+", "", t).strip()
    return t


def _score_block(block):
    """
    语义块综合评分（0-100）
    - 关键词命中 (0-30)
    - 文案长度适中 (0-20): 5-25字最佳
    - 时长适中 (0-20): 3-8秒最佳
    - 无问号/纯疑问 (0-15)
    - 含数字/价格 (0-15): 带货文案加分
    """
    score = 0
    text = block["text"]
    dur = block["duration"]

    # 关键词命中
    score += min(block["kw_count"] * 8, 30)

    # 文案长度
    text_len = len(text)
    if 10 <= text_len <= 35:
        score += 20
    elif 5 <= text_len <= 40:
        score += 12
    elif 3 <= text_len <= 45:
        score += 6

    # 时长适中
    if 3 <= dur <= 8:
        score += 20
    elif 2 <= dur <= 10:
        score += 10
    elif 1.5 <= dur <= 12:
        score += 4

    # 无问号加分（问号多的片段通常不适合做短视频文案）
    question_marks = text.count("？") + text.count("?")
    if question_marks == 0:
        score += 15
    elif question_marks == 1:
        score += 5

    # 含数字/价格加分（"119""199""30元"等具体数字更有说服力）
    has_numbers = bool(re.search(r"\d+", text))
    if has_numbers:
        score += 15

    return min(score, 100)


def _match_keyword(text, keywords):
    """模糊匹配：去掉标点后匹配"""
    clean = re.sub(r"[，。！？、\s,.\-!?]", "", text)
    for key in keywords:
        if re.sub(r"[，。！？、\s,.\-!?]", "", key) in clean:
            return True
    return False


def _clip_transition_config(options):
    opts = options or {}
    mode = str(opts.get("mode") or "off").strip().lower()
    if mode not in ("fade", "淡入淡出"):
        return "off", 0.0
    try:
        duration = float(opts.get("duration", CLIP_VIDEO_TRANSITION_SECONDS))
    except Exception:
        duration = CLIP_VIDEO_TRANSITION_SECONDS
    duration = max(0.08, min(0.20, duration))
    return "fade", duration


def _apply_clip_video_transition_fade(vf, clip_duration, clip_index, total_clips, mode, duration):
    return vf


def _probe_media_duration(media_path, ffprobe_cmd=None):
    ffprobe = ffprobe_cmd
    if not ffprobe:
        try:
            _ff_dir = os.path.dirname(get_ffmpeg_cmd())
            ffprobe = os.path.join(_ff_dir, "ffprobe" + (".exe" if sys.platform == "win32" else ""))
        except Exception:
            ffprobe = "ffprobe"
    try:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", media_path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=globals().get("_NO_WINDOW", 0),
        )
        return max(0.0, float(str(proc.stdout).strip()))
    except Exception:
        return 0.0


def _transition_boundary_mask(cut_maps, clip_count, continuous_gap=1.2):
    """Return which clip boundaries should use dissolve instead of a hard join."""
    if clip_count < 2:
        return []
    items = list(cut_maps or [])
    if len(items) < clip_count:
        return [True] * (clip_count - 1)
    mask = []
    for idx in range(clip_count - 1):
        cur = items[idx] or {}
        nxt = items[idx + 1] or {}
        try:
            cur_end = float(cur.get("end", 0))
            nxt_start = float(nxt.get("start", 0))
        except Exception:
            mask.append(True)
            continue
        cur_source = cur.get("source")
        nxt_source = nxt.get("source")
        same_source = not cur_source or not nxt_source or cur_source == nxt_source
        gap = nxt_start - cur_end
        is_continuous = same_source and -0.25 <= gap <= continuous_gap
        mask.append(not is_continuous)
    return mask


def _concat_clips_with_light_dissolve(
    ffmpeg, temp_files, raw_file, duration, _log, cancel_event=None,
    transition_mask=None, video_filter=None, audio_filter=None, video_codec_args=None,
):
    if len(temp_files) < 2:
        return False, []
    durations = [_probe_media_duration(path) for path in temp_files]
    if any(d <= 0.2 for d in durations):
        _log("片段转场: 有片段过短，回退普通拼接")
        return False, []

    if transition_mask is None:
        transition_mask = [True] * (len(temp_files) - 1)
    else:
        transition_mask = list(transition_mask)
        if len(transition_mask) < len(temp_files) - 1:
            transition_mask += [True] * (len(temp_files) - 1 - len(transition_mask))
        transition_mask = transition_mask[:len(temp_files) - 1]
    if not any(transition_mask):
        _log("片段转场: 片段时间连续，跳过轻叠化")
        return False, [0.0] * (len(temp_files) - 1)

    groups = []
    current_group = [0]
    for boundary_idx, use_transition in enumerate(transition_mask):
        if use_transition:
            groups.append(current_group)
            current_group = [boundary_idx + 1]
        else:
            current_group.append(boundary_idx + 1)
    groups.append(current_group)
    if len(groups) < 2:
        _log("片段转场: 片段时间连续，跳过轻叠化")
        return False, [0.0] * (len(temp_files) - 1)

    overlaps = [0.0] * (len(temp_files) - 1)
    work_files = []
    work_durations = []
    temp_dir = os.path.dirname(os.path.abspath(raw_file)) or tempfile.gettempdir()
    for group_idx, group in enumerate(groups):
        if len(group) == 1:
            work_files.append(temp_files[group[0]])
            work_durations.append(durations[group[0]])
            continue
        group_file = os.path.join(temp_dir, f"transition_group_{group_idx:02d}.mp4")
        list_file = os.path.join(temp_dir, f"transition_group_{group_idx:02d}.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for clip_idx in group:
                f.write(f"file '{os.path.abspath(temp_files[clip_idx]).replace(chr(92), '/')}'\n")
        group_cmd = [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c:v", "copy", "-c:a", "copy", group_file]
        try:
            proc = _register_process(subprocess.Popen(
                group_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=globals().get("_NO_WINDOW", 0),
            ))
            _, group_stderr = _communicate_process(proc, timeout=120, cancel_event=cancel_event)
        except Exception as exc:
            if cancel_event and cancel_event.is_set():
                raise
            _log(f"片段转场: 连续片段预拼接失败，回退普通拼接: {exc}")
            return False, []
        if proc.returncode != 0 or not os.path.exists(group_file) or os.path.getsize(group_file) <= 1000:
            _log(f"片段转场: 连续片段预拼接失败 exit={proc.returncode}，回退普通拼接")
            if group_stderr:
                for line in group_stderr.strip().split("\n")[-5:]:
                    if line.strip():
                        _log(f"  ffmpeg: {line.strip()}")
            return False, []
        work_files.append(group_file)
        work_durations.append(sum(durations[i] for i in group))

    inputs = []
    filters = []
    for idx, path in enumerate(work_files):
        inputs += ["-i", path]
        filters.append(f"[{idx}:v]{_stable_video_tail_filter(VIDEO_CONFIG['fps'])},setsar=1[v{idx}]")
        filters.append(f"[{idx}:a]{_stable_audio_tail_filter()},aformat=sample_fmts=fltp:channel_layouts=stereo[a{idx}]")

    v_label = "v0"
    a_label = "a0"
    acc_duration = work_durations[0]
    for group_idx in range(1, len(work_files)):
        first_clip_idx = groups[group_idx][0]
        boundary_idx = first_clip_idx - 1
        overlap = min(float(duration), work_durations[group_idx - 1] / 3, work_durations[group_idx] / 3, acc_duration / 3)
        overlap = max(0.04, overlap)
        overlaps[boundary_idx] = overlap
        offset = max(0.0, acc_duration - overlap)
        v_out = f"vxg{group_idx}"
        a_out = f"axg{group_idx}"
        filters.append(
            f"[{v_label}][v{group_idx}]xfade=transition=fade:duration={overlap:.3f}:offset={offset:.3f},format=yuv420p[{v_out}]"
        )
        filters.append(f"[{a_label}][a{group_idx}]acrossfade=d={overlap:.3f}:c1=tri:c2=tri[{a_out}]")
        v_label = v_out
        a_label = a_out
        acc_duration = acc_duration + work_durations[group_idx] - overlap

    map_v = v_label
    map_a = a_label
    if video_filter:
        filters.append(f"[{map_v}]{video_filter}[vfinal]")
        map_v = "vfinal"
    if audio_filter:
        filters.append(f"[{map_a}]{audio_filter}[afinal]")
        map_a = "afinal"
    filters.append(f"[{map_v}]{_stable_video_tail_filter(VIDEO_CONFIG['fps'])},setsar=1[vstable]")
    map_v = "vstable"
    filters.append(f"[{map_a}]{_stable_audio_tail_filter()},aformat=sample_fmts=fltp:channel_layouts=stereo[astable]")
    map_a = "astable"

    cmd = [ffmpeg, "-y"] + inputs + [
        "-filter_complex", ";".join(filters),
        "-map", f"[{map_v}]", "-map", f"[{map_a}]",
    ]
    cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
    cmd += list(video_codec_args or _final_vcodec_args())
    cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart", raw_file]

    try:
        proc = _register_process(subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=globals().get("_NO_WINDOW", 0),
        ))
        _, stderr_data = _communicate_process(proc, timeout=240, cancel_event=cancel_event)
    except subprocess.TimeoutExpired:
        _terminate_process(proc)
        _log("片段转场: 轻叠化拼接超时，回退普通拼接")
        return False, []
    except Exception as exc:
        if cancel_event and cancel_event.is_set():
            raise
        _log(f"片段转场: 轻叠化拼接失败，回退普通拼接: {exc}")
        return False, []

    if proc.returncode == 0 and os.path.exists(raw_file) and os.path.getsize(raw_file) > 1000:
        _log(f"片段转场: 轻叠化拼接完成，{sum(1 for value in overlaps if value > 0)}/{len(overlaps)} 处，每处约 {duration:.2f}s")
        return True, overlaps

    _log(f"片段转场: 轻叠化拼接失败 exit={proc.returncode}，回退普通拼接")
    if stderr_data:
        for line in stderr_data.strip().split("\n")[-5:]:
            if line.strip():
                _log(f"  ffmpeg: {line.strip()}")
    return False, []


def parse_srt_clips(srt_path, log_fn=None):
    """
    智能片段提取 v5.0 - 基于 AYOBE/小贤实际爆款视频分析
    - 6个核心类型（hook/selling_point/price/size/urgency/cta）
    - 时间窗口聚类：优先在5分钟窗口内选片段，不跳来跳去
    - selling_point允许重复（实际话术反复讲卖点）
    - Whisper纠错 + 语义块合并
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    _log(f"解析字幕: {os.path.basename(srt_path)}")
    try:
        subs, encoding = open_srt(srt_path)
        _log(f"字幕编码: {encoding}，共 {len(subs)} 条")
    except Exception as e:
        _log(f"解析 SRT 失败: {e}")
        return []

    _log("第一步：逐句分类，合并语义块...")
    sentences = []
    for sub in subs:
        text = sub.text.strip()
        if not text: continue
        start = _time_to_seconds(sub.start)
        end = _time_to_seconds(sub.end)
        duration = end - start
        if duration < 0.5 or duration > 15: continue
        if any(neg in text for neg in NEGATIVE_SIGNALS): continue

        best_type, best_kw = None, 0
        for ct, cfg in CLIP_KEYWORDS.items():
            kw = sum(1 for k in cfg["keywords"] if k in text)
            if kw > best_kw: best_kw, best_type = kw, ct
        if not best_type or best_kw == 0: continue

        positive = False
        for kw in CLIP_KEYWORDS[best_type]["keywords"]:
            if kw in text:
                idx = text.find(kw)
                if text[max(0, idx-1):idx] not in NEGATION_WORDS:
                    positive = True; break
        if not positive: continue
        sentences.append((best_type, text, start, end, best_kw))

    if not sentences:
        _log("未找到有效句子！"); return []

    # 语义块合并
    blocks, cur = [], None
    for st, txt, ss, se, kw in sentences:
        if cur:
            gap, md = ss - cur["end"], se - cur["start"]
            if cur["type"] == st and gap < 8 and md <= 20:
                cur["text"] += " " + txt; cur["end"] = se
                cur["kw_count"] += kw; continue
            blocks.append(cur)
        cur = {"type": st, "text": txt, "start": ss, "end": se, "kw_count": kw}
    if cur: blocks.append(cur)

    for b in blocks:
        b["text"] = _clean_text(b["text"])
        if not b["text"]: continue
        if b["text"][-1] not in "!?!?.": b["text"] += "！"
        b["duration"] = b["end"] - b["start"]
        b["score"] = _score_block(b)

    fb = [b for b in blocks if b["text"] and 2 <= b["duration"] <= 20 and 5 <= len(b["text"]) <= 45]
    if not fb:
        _log("无有效片段！"); return []
    _log(f"  有效语义块: {len(fb)} 个")

    # 时间窗口聚类：找最密集的5分钟窗口
    _log("第二步：时间窗口聚类...")
    ws = TIME_WINDOW_MINUTES * 60
    mt = min(b["start"] for b in fb)
    xt = max(b["start"] for b in fb)
    best_t, best_s = mt, 0
    t = mt
    while t <= xt:
        we = t + ws
        wb = [b for b in fb if t <= b["start"] <= we]
        types = set(b["type"] for b in wb)
        score = len(types) * 20 + sum(b["score"] for b in wb)
        for rt in REQUIRED_CLIP_TYPES:
            if rt in types: score += 50
        if score > best_s: best_s, best_t = score, t
        t += 30

    we = best_t + ws
    wb = [b for b in fb if best_t <= b["start"] <= we]
    if len(wb) < 5:
        wb = fb
        _log("  窗口片段不足，使用全部")
    else:
        _log(f"  最佳窗口: {int(best_t//60)}分{int(best_t%60):02d}秒起，{len(wb)}个片段")

    # 分组评分
    _log("第三步：黄金链路编排...")
    tp = {}
    for b in wb:
        tp.setdefault(b["type"], []).append(b)
    for bt in tp:
        tp[bt].sort(key=lambda x: -x["score"])
        tp[bt] = tp[bt][:1]  # 每类型仅1个

    tgt = TARGET_DURATION
    mn = tgt - TARGET_DURATION_TOLERANCE
    mx = tgt + TARGET_DURATION_TOLERANCE
    oc, td, ut = [], 0.0, {}

    # 第一轮：各类型取1个
    for ct in CLIP_ORDER:
        if td >= tgt: break
        if ct in tp and tp[ct]:
            b = tp[ct].pop(0); oc.append(b)
            ut[ct] = ut.get(ct, 0) + 1; td += b["duration"]

    # 第二轮：补充 selling_point（允许重复到3个）
    if "selling_point" in tp:
        for b in list(tp["selling_point"]):
            if td >= mn or ut.get("selling_point", 0) >= 5: break
            oc.append(b); ut["selling_point"] = ut.get("selling_point", 0) + 1
            td += b["duration"]

    # 第三轮：时长不足，从其他类型补充
    if td < mn:
        rem = sorted([b for bs in tp.values() for b in bs], key=lambda x: -x["score"])
        for b in rem:
            if td >= tgt: break
            if oc and oc[-1]["type"] == b["type"]: continue
            oc.append(b); td += b["duration"]

    # 时长过长裁剪
    while td > mx and len(oc) > 3:
        wi, ws2 = None, 999
        for i, b in enumerate(oc):
            if b["type"] not in REQUIRED_CLIP_TYPES and b["score"] < ws2:
                ws2, wi = b["score"], i
        if wi is None: break
        td -= oc.pop(wi)["duration"]

    # 输出
    fc = [(b["type"], b["text"], b["start"], b["end"], b["score"], b["duration"]) for b in oc]
    _log(f"{'='*65}")
    _log(f"最终片段（{len(fc)} 个，总时长 {sum(d for _,_,_,_,_,d in fc):.1f}s）")
    _log(f"{'='*65}")
    for i, (ct, txt, s, e, sc, d) in enumerate(fc):
        _log(f"  [{i+1:02d}] {ct:<14s} | {s:7.2f}s-{e:7.2f}s ({d:.1f}s) | {sc:3.0f}分 | {txt}")
    _log("-" * 65)
    return fc

# ============================================================
# v3.0: 字幕叠加 - 生成 ASS 文件
# ============================================================


_OUTPUT_SUBTITLE_PUNCTUATION = '，。！？、；：“”‘’（）《》【】…—·,.!?;:\'"()[]{}<>~～・'


def _strip_output_subtitle_punctuation(text):
    return str(text or "").translate(
        str.maketrans("", "", _OUTPUT_SUBTITLE_PUNCTUATION)
    ).strip()


def _split_subtitle_text(text, max_chars=12):
    """将长文案拆分为短句，按标点和语义停顿点分割"""
    import re
    # 按标点分割
    parts = re.split(r'([，。！？、；：,])', text)
    # 重新组合：标点跟前面的文字
    segments = []
    current = ""
    for p in parts:
        if re.match(r'^[，。！？、；：,]$', p):
            current += p
            if current.strip():
                segments.append(current.strip())
                current = ""
        else:
            current += p
    if current.strip():
        segments.append(current.strip())

    # 如果某个 segment 还是太长，按 max_chars 强制拆
    result = []
    for seg in segments:
        if len(seg) <= max_chars:
            result.append(seg)
        else:
            # 尝试在词边界拆，避免截断词语
            i = 0
            while i < len(seg):
                end = min(i + max_chars, len(seg))
                # 如果不是切到末尾，尝试微调到词边界
                if end < len(seg):
                    # 向前找助词/连词位置
                    adjusted = end
                    for offset in range(0, 3):
                        pos = end - offset
                        if pos <= i:
                            break
                        if seg[pos-1] in '的了着过是在也都还很最把被让给和与但而':
                            adjusted = pos
                            break
                    end = adjusted
                chunk = seg[i:end]
                if chunk:
                    result.append(chunk)
                i = end
    return result


def _highlight_text(text, keywords, sc):
    """对文字中的关键词进行高亮处理（黄色+放大）"""
    kw_size = sc.get("keyword_font_size", sc["font_size"] + 4)
    kw_color = sc.get("keyword_font_color", "&H0000FFFF")
    kw_bold = "-1" if sc.get("keyword_bold") else "0"
    base_color = sc["font_color"]
    base_bold = "-1" if sc.get("bold", True) else "0"

    # 构建正则：按关键词长度降序匹配（优先匹配长词）
    import re
    sorted_kw = sorted(keywords, key=len, reverse=True)
    pattern = "|".join(re.escape(k) for k in sorted_kw)
    if not pattern:
        return text

    result = []
    last_end = 0
    for m in re.finditer(pattern, text):
        # 前面的普通文字
        if m.start() > last_end:
            normal = text[last_end:m.start()]
            result.append(f"{{\\c&H{base_color[2:]}&\\b{base_bold}\\fs{sc['font_size']}}}{normal}")
        # 关键词
        kw_text = m.group()
        result.append(f"{{\\c&H{kw_color[2:]}&\\b{kw_bold}\\fs{kw_size}}}{kw_text}")
        last_end = m.end()

    # 剩余文字
    if last_end < len(text):
        normal = text[last_end:]
        result.append(f"{{\\c&H{base_color[2:]}&\\b{base_bold}\\fs{sc['font_size']}}}{normal}")

    return "".join(result)


def generate_ass(clips, width, height, output_path):
    """为片段生成 ASS 字幕文件（支持关键词高亮）"""
    from config import SUBTITLE_KEYWORDS
    from platform_config import FONT_BOLD_NAME
    sc = dict(SUBTITLE_OVERLAY)  # 复制避免修改原配置
    sc["font_name"] = FONT_BOLD_NAME
    margin_v = sc["margin_v"]
    outline_w = sc.get("outline_width", 3)
    # 底部对齐：用 PlayResY - margin_v
    if sc["position"] == "top":
        margin_v = sc["margin_v"]
        alignment = 8  # top center
    elif sc["position"] == "center":
        margin_v = 0
        alignment = 5  # center
    else:
        alignment = 2  # bottom center

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{sc['font_name']},{sc['font_size']},{sc['font_color']},&H000000FF,{sc['outline_color']},&H80000000,-1,0,0,0,100,100,0,0,1,{outline_w},1,{alignment},10,10,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    current_time = 0.0
    for c_type, text, start, end, score, dur, *_ in clips:
        duration = end - start
        # 去除AI标注的【】标记，提取重点词
        ai_keywords = re.findall(r'【(.*?)】', text)
        clean_text = re.sub(r'【|】', '', text)
        # 拆分为短句
        segments = _split_subtitle_text(clean_text, max_chars=12)
        if not segments:
            segments = [clean_text]
        segments = [
            cleaned for cleaned in (_strip_output_subtitle_punctuation(seg) for seg in segments)
            if cleaned
        ]
        # 合并关键词列表（AI标注 + 静态配置）
        all_keywords = list(set(ai_keywords + SUBTITLE_KEYWORDS))
        # 按短句数分配时间
        seg_dur = duration / len(segments)
        for i, seg in enumerate(segments):
            seg_start = current_time + i * seg_dur
            seg_end = current_time + (i + 1) * seg_dur
            ass_s = _sec_to_ass_time(seg_start)
            ass_e = _sec_to_ass_time(seg_end)
            highlighted = _highlight_text(seg, all_keywords, sc)
            lines.append(f"Dialogue: 0,{ass_s},{ass_e},Default,,0,0,0,,{highlighted}")
        current_time += duration

    with open(output_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines))


def _sec_to_ass_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _get_clip_duration(filepath):
    """读取 MP4 文件的精确时长（解析 moov/mvhd 原子，无需 ffprobe）"""
    try:
        fsize = os.path.getsize(filepath)
        with open(filepath, 'rb') as f:
            head = f.read(min(fsize, 1 * 1024 * 1024))
            idx = head.find(b'moov')
            if idx < 0:
                f.seek(max(0, fsize - 1 * 1024 * 1024))
                tail = f.read()
                idx = tail.find(b'moov')
                if idx < 0:
                    return None
                head = tail
            search_end = min(idx + 100000, len(head))
            mvhd_idx = head.find(b'mvhd', idx, search_end)
            if mvhd_idx < 0:
                return None
            version = head[mvhd_idx + 8] if mvhd_idx + 8 < len(head) else 0
            if version == 0:
                if mvhd_idx + 28 > len(head):
                    return None
                timescale = int.from_bytes(head[mvhd_idx+20:mvhd_idx+24], 'big')
                duration = int.from_bytes(head[mvhd_idx+24:mvhd_idx+28], 'big')
            else:
                if mvhd_idx + 40 > len(head):
                    return None
                timescale = int.from_bytes(head[mvhd_idx+28:mvhd_idx+32], 'big')
                duration = int.from_bytes(head[mvhd_idx+32:mvhd_idx+40], 'big')
            if timescale <= 0:
                return None
            return duration / timescale
    except Exception:
        return None




# ============================================================
# v3.0: 核心流程
# ============================================================



def _build_cut_report(ordered_clips, success_count, total_clips, output_path, size_mb):
    """构建切割评分报告"""
    import os
    report = {
        "ok": True,
        "clips_count": success_count,
        "clips_total": total_clips,
        "size_mb": round(size_mb, 1),
        "duration": 0.0,
        "has_hook": False,
        "hook_type": "",
        "category": "",
        "score": 0,
        "warnings": [],
    }
    if not ordered_clips:
        report["warnings"].append("没有选中任何片段")
        report["score"] = 0
        return report

    # 计算总时长
    total_dur = 0.0
    types_seen = []
    hook_found = None
    for clip in ordered_clips:
        if isinstance(clip, (list, tuple)) and len(clip) >= 6:
            c_type, text, start, end, score, dur = clip[0], clip[1], clip[2], clip[3], clip[4], clip[5]  # clip[6]=focus ignored here
        elif isinstance(clip, dict):
            c_type = clip.get("type", "")
            start = clip.get("start", 0)
            end = clip.get("end", 0)
            dur = end - start
        else:
            continue
        total_dur += dur
        if c_type not in types_seen:
            types_seen.append(c_type)
        if hook_found is None and "hook" in c_type.lower():
            hook_found = c_type

    report["duration"] = round(total_dur, 1)
    if hook_found:
        report["has_hook"] = True
        report["hook_type"] = hook_found

    # 品类（从片段类型中推断）
    cat_types = [t for t in types_seen if t not in ("hook", "bridge", "close", "cta", "transition")]
    if cat_types:
        report["category"] = cat_types[0]

    # ---- 评分 ----
    score = 0

    # 时长分 (0-30)
    _dur_tgt = TARGET_DURATION
    _dur_low = _dur_tgt - _dur_tgt // 4
    _dur_high = _dur_tgt + _dur_tgt // 4
    if _dur_low <= total_dur <= _dur_high:
        score += 30
    elif _dur_low - 10 <= total_dur <= _dur_high + 10:
        score += 22
    elif _dur_low - 20 <= total_dur <= _dur_high + 20:
        score += 15
        if total_dur < _dur_low:
            report["warnings"].append(f"时长偏短({total_dur:.0f}s，建议{_dur_low}s+)")
        else:
            report["warnings"].append(f"时长偏长({total_dur:.0f}s，建议{_dur_high}s以内)")
    else:
        score += 5
        report["warnings"].append(f"时长异常({total_dur:.0f}s)")

    # Hook分 (0-25)
    if report["has_hook"]:
        score += 25
    else:
        score += 5
        report["warnings"].append("缺少Hook开头，建议保留吸引眼球的片段")

    # 片段数分 (0-20)
    if 5 <= success_count <= 8:
        score += 20
    elif 3 <= success_count <= 10:
        score += 14
    elif success_count >= 2:
        score += 8
    else:
        score += 2
        report["warnings"].append("片段太少，成品信息密度不足")

    # 类型多样性 (0-15)
    type_count = len(types_seen)
    if type_count >= 4:
        score += 15
    elif type_count >= 3:
        score += 11
    elif type_count >= 2:
        score += 7
    else:
        score += 3
        report["warnings"].append("片段类型单一，建议混搭不同类型")

    # 有收尾 (0-10)
    close_types = [t for t in types_seen if t in ("close", "cta", "urgency")]
    if close_types:
        score += 10
    else:
        score += 3
        report["warnings"].append("缺少自然收尾，建议补充尺码建议、场景总结或信任背书片段，避免价格/链接/强促单内容")

    report["score"] = min(score, 100)
    return report


def _clip_log_fields(clip):
    """Normalize tuple/dict clip records for human-readable logs."""
    if isinstance(clip, dict):
        c_type = clip.get("type") or clip.get("clip_type") or "product"
        text = clip.get("text") or ""
        start = clip.get("start", 0)
        end = clip.get("end", start)
        dur = clip.get("duration", None)
        source = clip.get("source") or clip.get("video") or ""
    elif isinstance(clip, (list, tuple)):
        c_type = clip[0] if len(clip) > 0 else "product"
        text = clip[1] if len(clip) > 1 else ""
        start = clip[2] if len(clip) > 2 else 0
        end = clip[3] if len(clip) > 3 else start
        dur = clip[5] if len(clip) > 5 else None
        raw_source = clip[7] if len(clip) > 7 else ""
        source = "" if isinstance(raw_source, dict) else raw_source
    else:
        c_type, text, start, end, dur, source = "product", "", 0, 0, None, ""
    try:
        start = float(start or 0)
    except Exception:
        start = 0.0
    try:
        end = float(end if end is not None else start)
    except Exception:
        end = start
    try:
        dur = float(dur if dur is not None else max(0.0, end - start))
    except Exception:
        dur = max(0.0, end - start)
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    source_name = os.path.basename(str(source)) if source else ""
    return str(c_type or "product"), text, start, end, dur, source_name


def _log_final_clip_details(clips, log_fn=None, title="最终片段明细"):
    """Print the exact retained clip list before cutting."""
    if not log_fn or not clips:
        return
    try:
        total = 0.0
        rows = []
        for idx, clip in enumerate(clips, 1):
            c_type, text, start, end, dur, source_name = _clip_log_fields(clip)
            total += max(0.0, dur)
            text = text[:120]
            source_part = f" | {source_name}" if source_name else ""
            rows.append(
                f"片段{idx:02d} | {c_type} | {start:.1f}-{end:.1f}s | {dur:.1f}s{source_part} | {text}"
            )
        log_fn(f"{title}: {len(rows)} 段, {total:.1f}s")
        for row in rows:
            log_fn(row)
    except Exception as exc:
        try:
            log_fn(f"{title}: 输出失败 {exc}")
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass


def _print_cut_report(report, _log):
    """在日志中打印切割评分报告"""
    bar_len = 20
    score = report["score"]
    filled = int(bar_len * score / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    # 评分等级
    if score >= 85:
        grade = "优秀"
        grade_icon = "🌟"
    elif score >= 70:
        grade = "良好"
        grade_icon = "👍"
    elif score >= 55:
        grade = "一般"
        grade_icon = "⚡"
    else:
        grade = "需改进"
        grade_icon = "🔧"

    _log("")
    _log("━━━ 切割报告 ━━━━━━━━━━━━━━━━━━")
    _log(f"  {grade_icon} 综合评分: {score}/100 {grade}")
    _log(f"  [{bar}]")
    _log(f"  ⏱ 总时长: {report['duration']:.0f}s | 🎬 片段: {report['clips_count']}/{report['clips_total']}段")
    hook_str = f"{report['hook_type']} ✅" if report['has_hook'] else "无 ❌"
    _log(f"  🪝 Hook: {hook_str}")
    if report['category']:
        _log(f"  🏷 品类: {report['category']}")
    for w in report['warnings']:
        _log(f"  ⚠️ {w}")
    _log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    _log("")
def process_video(video_path, srt_path=None, output_path=None,
                   dedup_preset="medium", subtitle_overlay=True,
                   log_fn=None, force_category=None, cancel_event=None,
                   pip_path=None, pip_size=0.15, pip_opacity=0.03, pip_pos="右下",
                   _clips_only=False, _asr_only=False, focus_hint="自动", smart_crop_enabled=True, crop_level="medium", ken_burns_enabled=True,
                   target_duration=60, mirror_enabled=None, kb_intensity="中", ai_controls=None,
                   dedup_video_options=None, dedup_audio_options=None, transition_options=None,
                   _user_confirmed_clips=False, duration_tolerance=None):
    """
    完整处理流程：
    1. 如果没有 SRT，自动语音识别
    2. 解析字幕提取片段
    3. 切割 + 去重
    4. 字幕叠加 + 拼接
    返回 True/False
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    def _cancelled():
        return cancel_event and cancel_event.is_set()

    original_video_path = video_path
    dedup_preset = _normalize_dedup_preset(dedup_preset)
    dedup_video_options = dedup_video_options or {}
    dedup_audio_options = dedup_audio_options or {}
    _transition_mode, _transition_duration = _clip_transition_config(transition_options)
    _dedup_is_custom = dedup_preset == "custom"
    _mirror_enabled = _dedup_mirror_enabled(mirror_enabled) and dedup_preset != "none"

    # ---- 运行日志 ----
    global TARGET_DURATION, TARGET_DURATION_TOLERANCE, _hw_fallback, _hw_encoder_checked, _hw_encoder
    old_dur, old_tol = TARGET_DURATION, TARGET_DURATION_TOLERANCE
    _duration_contract = _selection_duration_contract(
        target_duration,
        dedup_preset,
        dedup_video_options,
        duration_tolerance,
    )
    TARGET_DURATION = target_duration
    TARGET_DURATION_TOLERANCE = max(
        0.0,
        _duration_contract.final_max - _duration_contract.final_target,
    )
    _log(f"目标时长: {target_duration}秒 (容差{TARGET_DURATION_TOLERANCE:g}秒)")
    _ai_target_duration = _duration_contract.ai_target_seconds
    _planned_speed_factor = _duration_contract.speed_factor
    if abs(_planned_speed_factor - 1.0) > 0.01:
        _log(
            f"AI选片时长折算: 成片目标{target_duration}s × 预计变速{_planned_speed_factor:.2f}x "
            f"→ AI按原片约{_ai_target_duration}s选片"
        )
    if _transition_mode == "fade":
        _log(f"片段转场: 画面淡入淡出 {_transition_duration:.2f}s，音频保持短防爆音")
    import time as _time, json as _json
    _run_log = {
        "时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "视频": video_path,
        "结果": "进行中",
        "耗时": None,
        "参数": {
            "去重": dedup_preset,
            "字幕叠加": subtitle_overlay,
            "指定SRT": srt_path or "自动识别",
            "画中画": pip_path or "无",
            "主推品类": force_category or "自动",
        },
        "选片": {},
        "输出": None,
        "错误": None,
    }
    _run_start = _time.time()
    _run_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

    def _save_run_log():
        """写入运行日志 JSON"""
        try:
            _run_log["耗时"] = f"{_time.time() - _run_start:.1f}s"
            os.makedirs(_run_log_dir, exist_ok=True)
            vname = os.path.splitext(os.path.basename(original_video_path))[0][:20]
            ts = _time.strftime("%Y%m%d_%H%M%S", _time.localtime(_run_start))
            status = "成功" if _run_log["结果"] == "成功" else "失败"
            fname = f"{ts}_{vname}_{status}.json"
            with open(os.path.join(_run_log_dir, fname), "w", encoding="utf-8") as f:
                _json.dump(_run_log, f, ensure_ascii=False, indent=2)
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass

    auto_srt = False
    auto_srt = True
    temp_srt = None

    # 1. 自动语音识别（如果没给 SRT）
    if _cancelled():
        _log("已取消。"); return {"ok": False, "error": "cancelled"}

    ffmpeg = get_ffmpeg_cmd()
    normalized_video_path = _remux_ts_for_editing(video_path, None, ffmpeg, _log)
    if normalized_video_path != video_path:
        _run_log["参数"]["TS标准化输入"] = normalized_video_path
        video_path = normalized_video_path

    if not srt_path:
        # 先检查视频旁边是否有 SRT 缓存
        _srt_cache = os.path.splitext(original_video_path)[0] + ".srt"
        if os.path.exists(_srt_cache):
            _log(f"使用本地SRT: {os.path.basename(_srt_cache)}")
            srt_path = _srt_cache
            temp_srt = _srt_cache
            auto_srt = False
        
        if not srt_path:
            # 使用缓存标志，让后面的代码知道用了缓存
            auto_srt = True
            # 检查云端ASR是否启用
            _volc_asr_on = False
        
        if not srt_path:
            # 没有缓存，走 ASR 流程
            # 检查云端ASR是否启用
            _volc_asr_on = False
        _volc_used = False
        try:
            from ai_clipper import load_settings as _ld_asr
            _volc_asr_on = _ld_asr().get("asr_enabled", False)
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
        # 记录AI模型到运行日志
        try:
            from ai_clipper import load_settings as _ld_log
            from ai_model_config import DEEPSEEK_DEFAULT_MODEL
            _s = _ld_log()
            _run_log["参数"]["AI模型"] = _s.get("model", DEEPSEEK_DEFAULT_MODEL)
            _run_log["参数"]["云端ASR"] = _s.get("asr_enabled", False)
            _run_log["参数"]["ASR预设"] = _s.get("asr_preset", "自定义")
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
        if _volc_asr_on and not srt_path:
            # 使用火山引擎 ASR（断句精准），失败则降级到本地 Whisper
            _volc_used = False
            try:
                import os as _os2
                from ai_clipper import load_settings as _ld3
                _cfg2 = _ld3()
                if _cfg2:
                    # --- 阿里云 ASR ---
                    _asr_preset = _cfg2.get("asr_preset", "") or _cfg2.get("asr_provider", "")
                    if _asr_preset == "阿里云" and not _volc_used:
                        _ali_api_key = _cfg2.get("aliyun_api_key", "")
                        _ali_oss_ak = _cfg2.get("aliyun_oss_ak", "")
                        _ali_oss_sk = _cfg2.get("aliyun_oss_sk", "")
                        _ali_bucket = _cfg2.get("aliyun_bucket", "")
                        _ali_endpoint = _cfg2.get("aliyun_endpoint", "oss-cn-beijing.aliyuncs.com")
                        _ali_model = _cfg2.get("asr_model", "paraformer-v2") or "paraformer-v2"
                        if _ali_api_key and _ali_oss_ak and _ali_oss_sk and _ali_bucket:
                            _log("启动阿里云语音识别...")
                            try:
                                from aliyun_asr import aliyun_asr
                                import tempfile as _tf_ali, hashlib as _hl_ali
                                _td_ali = _os2.path.join(_tf_ali.gettempdir(), "live_cutter_stt")
                                _os2.makedirs(_td_ali, exist_ok=True)
                                _vh_ali = _hl_ali.md5(video_path.encode("utf-8")).hexdigest()[:8]
                                _wav_ali = _os2.path.join(_td_ali, f"audio_{_vh_ali}.wav")
                                _srt_ali = _os2.path.join(_td_ali, f"sub_{_vh_ali}.srt")
                                _ff_ali = get_ffmpeg_cmd()
                                _ext_ali = [_ff_ali, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", _wav_ali]
                                _pk_ali = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                _p_ali = subprocess.Popen(_ext_ali, **_pk_ali, creationflags=_NO_WINDOW)
                                _p_ali.wait(timeout=120)
                                if _p_ali.returncode == 0 and _os2.path.exists(_wav_ali):
                                    _segs_ali = aliyun_asr(_wav_ali, app_key=_ali_api_key, model=_ali_model,
                                                           oss_ak=_ali_oss_ak, oss_sk=_ali_oss_sk,
                                                           oss_bucket=_ali_bucket, oss_endpoint=_ali_endpoint,
                                                           log_fn=_log)
                                    if _segs_ali:
                                        _srt_ali_lines = []
                                        for _i_ali, _seg_ali in enumerate(_segs_ali, 1):
                                            _st_ali = float(_seg_ali.get("start", 0))
                                            _et_ali = float(_seg_ali.get("end", _st_ali + 3))
                                            _txt_ali = _seg_ali.get("text", "").strip()
                                            for _ch in "，。！？、；：“”‘’（）《》【】…—·,.!?:;'\"()[]{}<>":
                                                _txt_ali = _txt_ali.replace(_ch, "")
                                            _txt_ali = _txt_ali.strip()
                                            _txt_ali = _txt_ali.strip()
                                            if not _txt_ali:
                                                continue
                                            _srt_ali_lines.append(str(_i_ali))
                                            _srt_ali_lines.append(
                                                f"{int(_st_ali//3600):02d}:{int((_st_ali%3600)//60):02d}:{int(_st_ali%60):02d},{int((_st_ali%1)*1000):03d}"
                                                f" --> "
                                                f"{int(_et_ali//3600):02d}:{int((_et_ali%3600)//60):02d}:{int(_et_ali%60):02d},{int((_et_ali%1)*1000):03d}"
                                            )
                                            _srt_ali_lines.append(_txt_ali)
                                            _srt_ali_lines.append("")
                                        with open(_srt_ali, "w", encoding="utf-8") as _f_ali:
                                            _f_ali.write(chr(10).join(_srt_ali_lines))
                                        srt_path = _srt_ali
                                        auto_srt = True
                                        _volc_used = True
                                        temp_srt = _srt_ali
                                        _log(f"阿里云语音识别成功: {len(_segs_ali)} 条语音段")
                                    else:
                                        _log("阿里云 ASR 识别失败，将降级")
                                else:
                                    _log("音频提取失败，降级到本地 Whisper")
                            except Exception as _e_ali:
                                _log(f"阿里云 ASR 异常: {_e_ali}")
                        else:
                            _log("阿里云 ASR 配置不完整（需要 API Key + OSS AK/SK/Bucket），降级")
                    # --- 以下是火山引擎 ASR（仅在阿里云未成功时执行） ---
                    _v2_app_id = _cfg2.get("volc_app_id", "")
                    _v2_token = _cfg2.get("volc_access_token", "")
                    _v2_tos_ak = _cfg2.get("volc_tos_ak", "")
                    _v2_tos_sk = _cfg2.get("volc_tos_sk", "")
                    _v2_bucket = _cfg2.get("volc_bucket", "livec")
                    _v2_apikey = _cfg2.get("volc_api_key", "")
                    if not _volc_used and all([_v2_tos_ak, _v2_tos_sk]) and (all([_v2_app_id, _v2_token]) or _v2_apikey):
                        _log("启动火山引擎语音识别...")
                        from volcengine_asr import (
                            build_semantic_segments,
                            prepare_volcengine_audio,
                            semantic_segments_to_srt,
                            volcengine_asr,
                            write_word_timing_sidecar,
                        )
                        import tempfile as _tf2
                        import hashlib as _hl2
                        _temp_dir2 = _os2.path.join(_tf2.gettempdir(), "live_cutter_stt")
                        _os2.makedirs(_temp_dir2, exist_ok=True)
                        _vhash = _hl2.md5(video_path.encode("utf-8")).hexdigest()[:8]
                        _srt2 = _os2.path.join(_temp_dir2, f"sub_{_vhash}.srt")
                        _ff2 = get_ffmpeg_cmd()
                        _audio2 = prepare_volcengine_audio(video_path, _temp_dir2, prefix=f"audio_{_vhash}", ffmpeg=_ff2, log_fn=_log, timeout=120)
                        if _audio2 and _os2.path.exists(_audio2):
                            _segs2 = volcengine_asr(_audio2, _v2_app_id, _v2_token, _v2_tos_ak, _v2_tos_sk, bucket=_v2_bucket, region=_cfg2.get("volc_region", "cn-beijing"), log_fn=_log, api_key=_cfg2.get("volc_api_key", "") or None)
                            if _segs2:
                                _semantic2 = build_semantic_segments(_segs2, log_fn=_log) or _segs2
                                with open(_srt2, "w", encoding="utf-8") as _f2:
                                    _f2.write(semantic_segments_to_srt(_semantic2))
                                write_word_timing_sidecar(_srt2, _segs2, log_fn=_log)
                                srt_path = _srt2
                                auto_srt = True
                                _volc_used = True
                                temp_srt = _srt2
                                _log(f"火山引擎语音识别成功: {len(_segs2)} 条语音段")
                            else:
                                _log("⚠️ 云端语音识别失败，已自动切换到本地识别")
                        else:
                            _log("音频提取失败，降级到本地 Whisper")
                    elif _volc_used:
                        pass  # 阿里云已成功
                    else:
                        _log("未配置云端语音识别，使用本地识别")
            except Exception as _e2:
                _log(f"⚠️ 云端语音识别异常: {type(_e2).__name__}: {_e2}，已自动切换到本地识别")
        
        if not _volc_used and not srt_path:
            _log("[STEP] 🎬 语音识别中...")
            _log("启动本地语音识别 (Whisper)...")
            try:
                from stt import generate_srt
                # Read whisper model preference from settings
                _wmodel = "small"
                try:
                    import json as _json
                    _spath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_settings.json")
                    if os.path.exists(_spath):
                        with open(_spath, "r", encoding="utf-8-sig") as _sf:
                            _sdata = _json.load(_sf)
                        _wmodel = _sdata.get("whisper_model", "small")
                        _local_asr_engine = _sdata.get("local_asr_engine", "sensevoice")
                except Exception:
                    _LOG.warning("unexpected error", exc_info=True)
                    pass
                temp_srt = generate_srt(video_path, log_fn=_log, whisper_model=_wmodel, asr_engine=locals().get("_local_asr_engine", "sensevoice"))
            except Exception as _whisper_err:
                _err_str = str(_whisper_err).lower()
                if "huggingface" in _err_str or "hf_hub" in _err_str:
                    _log("❌ Whisper 模型下载失败（国内可能无法访问 HuggingFace）")
                    _log("💡 建议：1) 开启云端ASR（火山引擎）或 2) 手动提供 SRT 字幕文件")
                elif "winerror" in _err_str or "connection" in _err_str or "connect" in _err_str:
                    _log("❌ Whisper 模型下载失败：网络连接被中断")
                    _log("💡 建议：检查网络连接，或开启云端ASR / 提供SRT字幕文件")
                elif "cuda" in _err_str or "gpu" in _err_str:
                    _log("❌ Whisper GPU 加载失败，请尝试在设置中切换为 CPU 模式")
                else:
                    _log(f"❌ 语音识别失败: {_whisper_err}")
                    _log("💡 建议：开启云端ASR 或 手动提供 SRT 字幕文件")
                temp_srt = None
        if not temp_srt:
            _log("语音识别失败！")
            _run_log["结果"] = "失败"; _run_log["错误"] = "ASR识别失败"; _save_run_log(); return {"ok": False, "error": "asr_failed"}
        srt_path = temp_srt
        # 缓存SRT到视频旁边，下次直接复用
        if srt_path and os.path.exists(srt_path):
            try:
                _cache_path = os.path.splitext(original_video_path)[0] + ".srt"
                if _cache_path != srt_path:  # 避免自拷贝
                    import shutil as _shutil
                    _shutil.copy2(srt_path, _cache_path)
                    try:
                        from volcengine_asr import word_timing_sidecar_path as _word_sidecar_path
                        _source_words = _word_sidecar_path(srt_path)
                        if os.path.exists(_source_words):
                            _shutil.copy2(_source_words, _word_sidecar_path(_cache_path))
                    except Exception:
                        _LOG.warning("unexpected error", exc_info=True)
                        pass
                    _log(f"SRT已缓存: {os.path.basename(_cache_path)}")
            except Exception:
                _LOG.warning("unexpected error", exc_info=True)
                pass
        if auto_srt is not False:
            auto_srt = True
    # 2. 自动生成输出路径
    if not output_path:
        video_name = os.path.splitext(os.path.basename(original_video_path))[0]
        output_dir = os.path.join(os.path.dirname(original_video_path), "output")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"{video_name}_爆款切片_{time.strftime('%Y%m%d_%H%M%S')}.mp4")

    # 3. 解析字幕（AI 模式 或 关键词模式）
    # 多版本缓存（global声明，供_asr_only和_clips_only使用）
    global _multi_result_cache
    # _asr_only: 只做ASR，跳过AI选片
    if _asr_only:
        if isinstance(_multi_result_cache, dict):
            _srt_file = srt_path
            if _srt_file and os.path.exists(_srt_file):
                try:
                    with open(_srt_file, 'r', encoding='utf-8') as _f:
                        _multi_result_cache['srt_text'] = _f.read()
                except Exception:
                    _LOG.warning("unexpected error", exc_info=True)
                    pass
        _log("ASR完成，跳过AI选片（_asr_only模式）")
        return {"ok": True, "asr_only": True}

    from ai_clipper import is_enabled as ai_is_enabled, ai_analyze_clips, fallback_clips
    preference_summary = {}
    analysis_metadata = {}
    _word_timings = []
    try:
        from volcengine_asr import load_word_timing_sidecar
        _word_timings = load_word_timing_sidecar(srt_path, semantic=True, log_fn=_log)
        if _word_timings:
            _log(f"AI边界裁剪: 已载入 {sum(len(seg.get('words') or []) for seg in _word_timings)} 个词级时间")
    except Exception as _word_timing_error:
        _log(f"AI边界裁剪: 词级时间不可用 ({_word_timing_error})")
    if _cancelled():
        _log("已取消。"); return {"ok": False, "error": "cancelled"}
    if ai_is_enabled():
        _log("[STEP] 🤖 AI 选片中...")
        _log("🤖 AI 智能选片模式已启用...")
        try:
            with open(srt_path, "r", encoding="utf-8") as f:
                srt_text = f.read()
            # 单版本：focus_hint传给AI（"自动"=随机偏好，指定=用指定偏好）
            _fh = focus_hint if focus_hint and focus_hint != "自动" else None
            import ai_clipper as _ai_mod; _ai_mod._AI_TARGET_DURATION = _ai_target_duration
            # 动态控制AI输出的片段数量；统一由 ai_clipper 按目标时长推导。
            _ai_mod._AI_CLIP_COUNT = _ai_mod.target_clip_count_text(_ai_target_duration)
            _effective_force_category = force_category
            if not _effective_force_category:
                try:
                    _filename_category = _ai_mod.infer_category_from_filename(os.path.basename(str(original_video_path)))
                except Exception:
                    _filename_category = None
                if _filename_category:
                    _effective_force_category = _filename_category
                    _log(f"自动品类: 文件名强信号 → {_filename_category}")
            ordered_clips = ai_analyze_clips(
                srt_text,
                log_fn=_log,
                force_category=_effective_force_category,
                multi_version=False,
                focus_hint=_fh,
                target_duration=_ai_target_duration,
                final_target_duration=target_duration,
                duration_contract=_duration_contract,
                ai_controls=ai_controls,
                record_history=not _clips_only,
                word_timings=_word_timings,
            )
            try:
                analysis_metadata = dict(_ai_mod.get_last_analysis_metadata() or {})
                preference_summary = dict(analysis_metadata.get("preference_summary") or {})
            except Exception:
                analysis_metadata = {}
                preference_summary = {}
            if not ordered_clips:
                raise RuntimeError(_ai_mod.selection_failure_message(analysis_metadata))
        except Exception as e:
            _log(f"AI 选片失败: {e}")
            raise
    else:
        ordered_clips = parse_srt_clips(srt_path, log_fn=_log)
    if ordered_clips and ai_is_enabled():
        try:
            import ai_clipper as _topic_ai
            _topic_preference_summary = dict(
                (_topic_ai.get_last_analysis_metadata() or {}).get("preference_summary") or {}
            )
            _topic_preference = str(
                _topic_preference_summary.get("used_label")
                or _topic_preference_summary.get("label")
                or ""
            )
            if _topic_preference:
                _topic_ai._set_last_topic_coverage_summary(_topic_ai._topic_coverage_summary(
                    ordered_clips,
                    _topic_preference,
                    TARGET_DURATION,
                    _topic_preference_summary.get("requested", "自动"),
                ))
            analysis_metadata = dict(_topic_ai.get_last_analysis_metadata() or analysis_metadata or {})
            _log("AI叙事模式: 使用AI最终片单和顺序，主题统计仅用于预览展示")
        except Exception as _topic_balance_error:
            _log(f"主题统计跳过: {_topic_balance_error}")
    if not ordered_clips:
        _log("未提取到核心片段！")
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        return False

    _duration_shortage_grace = _selection_shortage_grace_seconds(analysis_metadata)
    if ai_is_enabled():
        _validate_selected_duration_contract(
            ordered_clips,
            target_duration,
            _planned_speed_factor,
            _log,
            shortage_grace_seconds=_duration_shortage_grace,
            user_confirmed=_user_confirmed_clips,
            duration_tolerance=duration_tolerance,
        )

    # 多版本缓存：保存选片结果和SRT内容，供 process_video_multi 使用    # 多版本缓存：保存选片结果和SRT内容，供 process_video_multi 使用
    if not _clips_only:
        _log_final_clip_details(ordered_clips, _log)

    try:
        if isinstance(_multi_result_cache, dict):
            _multi_result_cache['clips'] = list(ordered_clips)
            _multi_result_cache['srt_path'] = srt_path
            try:
                import ai_clipper as _ai_meta
                analysis_metadata = dict(_ai_meta.get_last_analysis_metadata() or analysis_metadata or {})
            except Exception:
                _LOG.warning("unexpected error", exc_info=True)
                pass
            _multi_result_cache['analysis_metadata'] = dict(analysis_metadata or {})
            _multi_result_cache['category_summary'] = dict(analysis_metadata.get('category_summary') or {})
            _multi_result_cache['topic_coverage_summary'] = dict(analysis_metadata.get('topic_coverage_summary') or {})
            _multi_result_cache['preference_summary'] = dict(analysis_metadata.get('preference_summary') or preference_summary or {})
            _multi_result_cache['word_timings'] = list(_word_timings or [])
            _multi_result_cache['requested_target_duration'] = target_duration
            _multi_result_cache['ai_target_duration'] = _ai_target_duration
            _multi_result_cache['duration_speed_factor'] = _planned_speed_factor
            # 保存SRT内容
            if srt_path and os.path.exists(srt_path):
                with open(srt_path, "r", encoding="utf-8") as _f:
                    _multi_result_cache['srt_text'] = _f.read()
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass

    # 选片预览/多版本模式：只做AI选片，跳过切割/去重/字幕（省30-60秒）
    if _clips_only:
        _log("AI选片完成，已生成片段预览，跳过切割处理。")
        return {"ok": True, "clips_cached": True}

    # 4. 切割 + 去重 + 字幕叠加
    ffmpeg = get_ffmpeg_cmd()
    cfg = VIDEO_CONFIG
    temp_dir = tempfile.mkdtemp(prefix="lc_temp_", dir="C:\\")
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    video_path = _remux_ts_for_editing(video_path, temp_dir, ffmpeg, _log)
    # 动态检测源视频分辨率，短边=宽，长边=宽*16/9，偶数对齐
    _ff_dir = os.path.dirname(get_ffmpeg_cmd())
    _ffprobe = os.path.join(_ff_dir, "ffprobe" + (".exe" if sys.platform == "win32" else ""))
    _probe = [_ffprobe, "-v", "quiet", "-print_format", "json", "-show_streams", "-select_streams", "v:0", "-i", video_path]
    _src_w, _src_h = 0, 0
    try:
        _pr = subprocess.run(_probe, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        _prj = json.loads(_pr.stdout)
        _vs = _prj.get("streams", [{}])[0]
        _video_fps = 30
        _rfr = _vs.get("r_frame_rate", "30/1")
        try:
            if "/" in str(_rfr):
                _n, _d = str(_rfr).split("/")
                _video_fps = int(_n) / max(int(_d), 1)
            else:
                _video_fps = float(_rfr)
        except:
            _video_fps = 30
        _sw, _sh = int(_vs.get("width", 0)), int(_vs.get("height", 0))
        if _sw > 0 and _sh > 0:
            _src_w, _src_h = _sw, _sh
            w, h, _res_note = _output_resolution_for_source(_sw, _sh, cfg["resolution"])
            _log(f"检测到源视频 {_sw}x{_sh}，输出分辨率: {w}x{h}（{_res_note}）")
        else:
            w, h = map(int, cfg["resolution"].split(":"))
            _src_w, _src_h = w, h
            _log(f"无法获取视频分辨率，使用默认 {w}x{h}")
    except Exception as _e:
        w, h = map(int, cfg["resolution"].split(":"))
        _src_w, _src_h = w, h
        _log(f"分辨率检测失败({_e})，使用默认 {w}x{h}")
    total_clips = len(ordered_clips)

    # 覆盖全局预设
    global DEDUP_PRESET
    old_preset = DEDUP_PRESET
    DEDUP_PRESET = dedup_preset

    # 每个任务使用独立临时目录。前面 TS 标准化也写入这个目录，结束时统一清理。
    will_subtitle = subtitle_overlay and SUBTITLE_OVERLAY.get("enabled")
    _log(f"去重: {dedup_preset} | 字幕叠加: {'开（后置ASR+DeepSeek修复）' if will_subtitle else '关'}")
    _log(f"镜像翻转: {'开' if _mirror_enabled else '关'}" + (f" (单片段概率 {OUTPUT_CLIP_MIRROR_PROBABILITY:.0%})" if _mirror_enabled else ""))
    if _is_ts_like_video(video_path):
        _log("TS normalize: using original TS fallback; cut stage will still use genpts.")
    # [v9.6] Parse SRT boundaries for hook tail buffer
    _srt_boundaries = []
    _srt_segments_for_cut = []
    try:
        with open(srt_path, "r", encoding="utf-8") as _sf:
            _srt_text = _sf.read()
        _srt_segments_for_cut = _parse_srt_to_segments(_srt_text)
        for _seg in _srt_segments_for_cut:
            _srt_boundaries.append((float(_seg.get("start", 0)), float(_seg.get("end", 0))))
        _srt_boundaries.sort()
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass

    def _needs_next_sentence(_text):
        _t = str(_text or "").strip().rstrip("。！？!?，,、 ")
        if not _t:
            return False
        return _t.endswith((
            "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
            "的话", "你看", "我觉得", "感觉", "给你们", "这个", "这款", "这件",
            "它是", "它会", "来讲的话", "一点", "一点点", "有没有发现",
            "你去", "去", "你想象一下", "想象一下", "七八月份你去", "你这一套"
        ))

    def _starts_as_followup(_text):
        _t = str(_text or "").strip()
        return _t.startswith(("然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实", "它", "这个", "这款", "这件", "你看"))

    _log(f"开始切割 {total_clips} 个片段...")

    # 获取视频时长
    _log("检测视频时长...")
    try:
        ffmpeg_cmd = get_ffmpeg_cmd()
        if not os.path.exists(ffmpeg_cmd):
            ffmpeg_cmd = "ffmpeg"
        _log(f"FFmpeg: {ffmpeg_cmd}")
        _hw_encoder_checked = False
        _hw_encoder = None
        _encoder = _get_video_encoder()
        if _encoder:
            _log(f"编码器: 实验硬件加速 ({_encoder})")
            try:
                import platform_config as _pc_diag
                for _diag in getattr(_pc_diag, "HARDWARE_ENCODER_DIAGNOSTICS", [])[-4:]:
                    _log(f"硬件诊断: {_diag}")
            except Exception:
                _LOG.warning("unexpected error", exc_info=True)
                pass
        elif _hardware_encoder_requested():
            _log(f"编码器: 硬件加速自检未通过，使用稳定软件编码 ({_software_encoder_name()})")
            try:
                import platform_config as _pc_diag
                for _diag in getattr(_pc_diag, "HARDWARE_ENCODER_DIAGNOSTICS", [])[-8:]:
                    _log(f"硬件诊断: {_diag}")
            except Exception:
                _LOG.warning("unexpected error", exc_info=True)
                pass
        else:
            _log(f"编码器: 软件编码 ({_software_encoder_name()})，硬件加速设置已关闭，整片处理会更慢")
        probe_cmd = [ffmpeg_cmd, "-i", video_path]
        proc = subprocess.Popen(probe_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
        _, stderr_data = proc.communicate(timeout=45)
        import re as _re
        m = _re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", stderr_data)
        if m:
            video_duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4)) / 100
            _log(f"视频时长: {video_duration:.1f}s")
        else:
            video_duration = 9999
            _log("无法检测视频时长，跳过安全检查")
    except Exception:
        video_duration = 9999
        _log("无法检测视频时长，跳过安全检查")

    # ============================================================
    # 切割：每片段独立生成 ASS + 切割时烧录字幕（零漂移）
    # ============================================================
    temp_files = []
    _clip_kb_caps = []
    success_count = 0
    _cut_stage_started = time.time()
    _log(f"[STEP] ✂ 切割片段中 ({total_clips}段)...")
    # ===== Smart Crop 批量检测 =====
    _sc_results = None
    if smart_crop_enabled:
        try:
            from smart_crop import batch_detect_clips, compute_smart_crop, _even
            _sc_results = batch_detect_clips(video_path, ordered_clips, log_fn=_log, ffmpeg_cmd=ffmpeg_cmd, frame_w=_src_w, frame_h=_src_h)
        except ImportError:
            _log("SmartCrop: smart_crop.py 不可用，使用标准裁切")
            smart_crop_enabled = False
        except Exception as _sce:
            _log(f"SmartCrop: 检测失败({_sce})，使用标准裁切")
            smart_crop_enabled = False
    else:
        try:
            from smart_crop import _even
        except ImportError:
            def _even(v): return v + (v % 2)

    # Ken Burns uses the stable OpenCV pass. FFmpeg zoompan is faster but can introduce visible jitter.
    _ken_burns_opencv = None
    _ken_burns_ffmpeg = None
    if ken_burns_enabled:
        try:
            from smart_crop import apply_ken_burns_opencv as _ken_burns_opencv
            from smart_crop import apply_ken_burns_ffmpeg as _ken_burns_ffmpeg
        except ImportError:
            _log("KenBurns: 滤镜不可用")
            ken_burns_enabled = False

    _log(f"开始切割 {total_clips} 个片段 (FFmpeg: {ffmpeg_cmd})...")
    _log(f"[T] {time.strftime('%H:%M:%S')} enter cut loop, total={total_clips}")

    # 硬件编码回退：第一次失败后自动切到 libx264
    _hw_fallback = False

    try:
        _clip_starts = []
        _clip_ends = []
        _clip_cut_maps = []
        for clip_idx, clip in enumerate(ordered_clips):
            c_type, text, start, end, score, dur = clip[0], clip[1], clip[2], clip[3], clip[4], clip[5]
            _orig_start, _orig_end = float(start), float(end)
            _preview_exact = _clip_preview_exact(clip)
            _preview_meta = next((item for item in clip if isinstance(item, dict) and item.get("preview_exact")), {}) if isinstance(clip, (list, tuple)) else {}
            _log(f"[T] [{time.strftime('%H:%M:%S')}] loop clip_idx={clip_idx}")
            if _cancelled():
                _log("已取消，跳过剩余切割。"); break
            _preview_note = ""
            if _preview_meta:
                try:
                    _parent = _preview_meta.get("preview_parent_index")
                    _group_i = int(_preview_meta.get("preview_group_index", 0) or 0) + 1
                    _group_n = int(_preview_meta.get("preview_group_count", 1) or 1)
                    _segs = [int(v) + 1 for v in (_preview_meta.get("selected_segment_indices") or [])]
                    _preview_note = f" 父片段{_parent} 子句{_segs} 组{_group_i}/{_group_n}"
                except Exception:
                    _preview_note = " 精确子句"
            _log(f"切割 [{clip_idx+1}/{total_clips}] {c_type}{_preview_note} ({start:.1f}s-{end:.1f}s)...")
            temp_file = os.path.join(temp_dir, f"clip_{clip_idx:02d}.mp4")
            # [v9.5] 尾部缓冲已禁用：会导致拖入其他片段内容产生重复
            start_buf = 0
            end_buf = 0
            # [v9.6] 所有片段SRT边界对齐：防止说半句话
            # 找到start/end所在的SRT条目→对齐到该条目边界；口播长句允许适度补尾。
            if _srt_boundaries and not _preview_exact:
                start_srt, end_srt = None, None
                for _ts, _te in _srt_boundaries:
                    if _ts <= end <= _te:
                        end_srt = _te
                    if _ts <= start <= _te:
                        start_srt = _ts
                if end_srt and end_srt - end <= 8.0:
                    end = end_srt
                if start_srt and start - start_srt <= 3.0:
                    start = start_srt
            if (not _preview_exact) and _srt_segments_for_cut and (clip_idx == total_clips - 1 or str(c_type).lower() in ("close", "cta", "call_to_action")):
                try:
                    for _si, _seg in enumerate(_srt_segments_for_cut):
                        _ts = float(_seg.get("start", 0))
                        _te = float(_seg.get("end", _ts))
                        if abs(_te - end) <= 0.6 and _si + 1 < len(_srt_segments_for_cut):
                            _next = _srt_segments_for_cut[_si + 1]
                            _next_start = float(_next.get("start", _te))
                            _next_end = float(_next.get("end", _next_start))
                            _txt = str(_seg.get("text", ""))
                            _ntxt = str(_next.get("text", ""))
                            if _next_start - end <= 1.2 and _next_end - start <= 14.0 and (_needs_next_sentence(_txt) or _starts_as_followup(_ntxt)):
                                end = _next_end
                                _log(f"结尾承接: 延伸到下一句 {end:.1f}s，避免最后一句截断")
                            break
                except Exception:
                    _LOG.warning("unexpected error", exc_info=True)
                    pass
            start = max(0, start - start_buf)
            end = min(video_duration, end + end_buf)

            if start >= video_duration:
                _log(f"SKIP [{c_type}] 起始 {start:.1f}s > 视频时长 {video_duration:.1f}s")
                continue
            if end > video_duration:
                end = video_duration - 0.1
                if end <= start:
                    continue

            # [v9.2] 切割编码模式 + Smart Crop + mirror
            if _preview_exact:
                _shared_boundary_changed = False
            else:
                start, end, _shared_boundary_changed = _apply_srt_cut_alignment(
                    c_type, start, end, _srt_segments_for_cut, clip_idx, total_clips
                )
            if end > video_duration:
                end = video_duration - 0.1
                if end <= start:
                    continue
            _tail_guard = LAST_CLIP_AUDIO_TAIL_GUARD_SECONDS if clip_idx == total_clips - 1 else CLIP_AUDIO_TAIL_GUARD_SECONDS
            if _tail_guard > 0:
                old_end = end
                end = min(video_duration, end + _tail_guard)
                if end > old_end + 0.01:
                    _log(f"Tail audio guard: clip {clip_idx+1} extended {old_end:.2f}s->{end:.2f}s")
            if False and clip_idx == total_clips - 1:
                old_end = end
                end = min(video_duration, end + 0.0)
                if end > old_end + 0.01:
                    _log(f"尾音保护: 最后一段延长 {old_end:.2f}s→{end:.2f}s，避免末字被截")
            _actual_clip_text = _srt_text_for_range(_srt_segments_for_cut, start, end) or str(text or "")
            if abs(start - _orig_start) > 0.01 or abs(end - _orig_end) > 0.01:
                _log(f"实际切割 [{clip_idx+1}/{total_clips}] {_orig_start:.1f}-{_orig_end:.1f}s -> {start:.1f}-{end:.1f}s | {_actual_clip_text[:120]}")

            mirror_vf = ""
            if _mirror_enabled and random.random() < OUTPUT_CLIP_MIRROR_PROBABILITY:
                mirror_vf = "hflip"
            clip_duration = max(0.2, end - start)

            # Smart Crop VF
            _sc_zoom = 1.0
            if smart_crop_enabled and _sc_results is not None:
                _sc_info = _sc_results.get(clip_idx, None)
                _sc_crop = compute_smart_crop(_sc_info, _src_w, _src_h, crop_level=crop_level, log_fn=_log)
                _sc_zoom = _smart_crop_zoom(_sc_crop)
                combined_vf = _smart_crop_vf(_sc_crop, _src_w, _src_h, w, h, _even, log_fn=_log)
            else:
                combined_vf = _smart_crop_no_crop_vf(_src_w, _src_h, w, h)

            if mirror_vf:
                combined_vf += "," + mirror_vf
            combined_vf += ",setpts=PTS-STARTPTS"
            combined_vf = _apply_clip_video_transition_fade(
                combined_vf, clip_duration, clip_idx, total_clips,
                _transition_mode, _transition_duration
            )
            combined_vf = _append_stable_video_tail_filter(combined_vf, VIDEO_CONFIG["fps"])
            _clip_audio_fade = min(CLIP_AUDIO_FADE_SECONDS, max(0.0, clip_duration / 3))
            _fade_out_start = max(0.0, clip_duration - _clip_audio_fade)
            _audio_filter = (
                _stable_audio_tail_filter()
                if _preview_exact
                else (
                    f"atrim=0:{clip_duration:.3f},{_stable_audio_tail_filter()},"
                    f"afade=t=in:st=0:d={_clip_audio_fade:.3f},"
                    f"afade=t=out:st={_fade_out_start:.3f}:d={_clip_audio_fade:.3f}"
                )
            )
            _log("[T] VF: " + combined_vf[:200])

            if _preview_exact:
                cmd = _preview_exact_cut_cmd(
                    ffmpeg, video_path, start, clip_duration, combined_vf,
                    _audio_filter, temp_file, _intermediate_vcodec_args(), VIDEO_CONFIG["fps"]
                )
            else:
                cmd = [ffmpeg, "-y"]
                _append_seek_input_args(cmd, video_path, start, accurate=False)
                cmd += ["-t", f"{clip_duration:.3f}"]
                cmd += ["-fflags", "+genpts"]
                cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
                cmd += _intermediate_vcodec_args()
                cmd += ["-vf", combined_vf]
                cmd += ["-pix_fmt", "yuv420p"]
                cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-async", "1",
                       "-af", _audio_filter, "-shortest"]
                cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
                cmd += [temp_file]
            _log(f"[T] [{time.strftime('%H:%M:%S')}] Popen start")

            try:
                popen_kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                proc = _register_process(subprocess.Popen(cmd, **popen_kwargs, creationflags=_NO_WINDOW))
                rc = _wait_process(proc, timeout=300, cancel_event=cancel_event)
                _log(f"[T] [{time.strftime('%H:%M:%S')}] rc={rc}")
                # 失败时重新运行一次捕获 stderr 看错误
                if rc != 0:
                    try:
                        _proc2 = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace",
                            timeout=60, creationflags=_NO_WINDOW)
                        err_tail = _proc2.stderr.strip().split("\n")[-5:]
                        for _line in err_tail:
                            _log(f"  ffmpeg: {_line}")
                    except Exception:
                        _LOG.warning("unexpected error", exc_info=True)
                        pass
            except subprocess.TimeoutExpired:
                _terminate_process(proc)
                _log(f"TIMEOUT [{c_type}] {start:.2f}s-{end:.2f}s")
                continue
            except Exception as e:
                if _cancelled():
                    _log("已取消。")
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    DEDUP_PRESET = old_preset
                    return False
                _log(f"[T] subprocess error: {type(e).__name__}: {e}")
                continue

            if rc == 0 and os.path.exists(temp_file) and os.path.getsize(temp_file) > 1000:
                size_kb = os.path.getsize(temp_file) / 1024
                _log(f"OK [{c_type}] {start:.1f}s-{end:.1f}s -> {size_kb:.0f}KB")
                temp_files.append(temp_file)
                _clip_starts.append(start)
                _clip_ends.append(end)
                _clip_cut_maps.append({"start": start, "end": end, "text": _actual_clip_text, "type": c_type})
                _clip_kb_caps.append(_kb_quality_cap_for_zoom(_sc_zoom))
                success_count += 1
            else:
                _log(f"FAIL [{c_type}] rc={rc}")
                # 硬件编码失败：回退到 libx264 并重试当前片段
                if not _hw_fallback and _get_video_encoder():
                    _sw_args = _intermediate_software_vcodec_args()
                    _sw_name = _software_encoder_name()
                    if _sw_name != "libx264":
                        _log("当前 FFmpeg 不支持 libx264，已使用 mpeg4 作为软件编码兜底。建议发布包内置完整 FFmpeg。")
                    _log(f"当前设备硬件编码不可用，已自动切换到 {_sw_name} 软件编码，后续片段不再尝试硬件编码。")
                    _hw_fallback = True
                    # 重新构建命令，用软件编码替换硬件编码
                    cmd = [ffmpeg, "-y"]
                    _append_seek_input_args(cmd, video_path, start, accurate=_preview_exact)
                    cmd += ["-t", f"{clip_duration:.3f}"]
                    cmd += ["-fflags", "+genpts"]
                    cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
                    cmd += _sw_args
                    cmd += ["-vf", combined_vf]
                    cmd += ["-pix_fmt", "yuv420p"]
                    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100",
                            "-ac", "2", "-async", "1", "-af", _audio_filter, "-shortest"]
                    cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
                    cmd += [temp_file]
                    try:
                        proc = _register_process(subprocess.Popen(cmd, **popen_kwargs, creationflags=_NO_WINDOW))
                        rc2 = _wait_process(proc, timeout=300, cancel_event=cancel_event)
                        if rc2 == 0 and os.path.exists(temp_file) and os.path.getsize(temp_file) > 1000:
                            _log(f"OK [{c_type}] {_sw_name} 软件编码成功")
                            temp_files.append(temp_file)
                            _clip_starts.append(start)
                            _clip_ends.append(end)
                            _clip_cut_maps.append({"start": start, "end": end, "text": _actual_clip_text, "type": c_type})
                            _clip_kb_caps.append(_kb_quality_cap_for_zoom(_sc_zoom))
                            success_count += 1
                        else:
                            _log(f"FAIL [{c_type}] {_sw_name} 也失败 rc={rc2}")
                    except Exception:
                        _log(f"FAIL [{c_type}] {_sw_name} 回退异常")

            _log(f"[PROGRESS] {(clip_idx + 1) / total_clips * 0.3:.2f}")

    except Exception as e:
        _log(f"[T] FATAL: {type(e).__name__}: {e}")
        import traceback
        _log(traceback.format_exc())

    _log(f"阶段耗时: 切片 {time.time() - _cut_stage_started:.1f}s")

    if not temp_files:
        _log("没有成功切割任何片段！")
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    # ============================================================
    # 第二步：拼接（stream copy → 中间文件）
    # ============================================================
    if _cancelled():
        _log("已取消。"); shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    if ken_burns_enabled and temp_files:
        _kb_stage_started = time.time()
        _log("KenBurns: 稳定模式二次处理开始")
        _kb_ok = 0
        for _kbi, _clip_file in enumerate(temp_files):
            if _cancelled():
                break
            try:
                _kb_dur = _clip_ends[_kbi] - _clip_starts[_kbi] if _kbi < len(_clip_starts) else 10.0
                _kb_out = _clip_file.replace(".mp4", "_kb.mp4")
                _kb_cap = _clip_kb_caps[_kbi] if _kbi < len(_clip_kb_caps) else None
                if _ken_burns_opencv:
                    _kb_ok_flag = _ken_burns_opencv(
                        _clip_file, _kb_out, _kb_dur, w, h, cfg["fps"],
                        ffmpeg_cmd=get_ffmpeg_cmd(), log_fn=_log,
                        intensity=kb_intensity, max_zoom_delta=_kb_cap)
                else:
                    _kb_ok_flag = False
                if not _kb_ok_flag and _ken_burns_ffmpeg:
                    _log("KenBurns: OpenCV稳定模式失败，回退FFmpeg快速模式")
                    _kb_ok_flag = _ken_burns_ffmpeg(
                        _clip_file, _kb_out, _kb_dur, w, h, cfg["fps"],
                        ffmpeg_cmd=get_ffmpeg_cmd(), log_fn=_log,
                        intensity=kb_intensity, max_zoom_delta=_kb_cap)
                if _kb_ok_flag and os.path.exists(_kb_out):
                    os.replace(_kb_out, _clip_file)
                    _kb_ok += 1
                elif os.path.exists(_kb_out):
                    os.remove(_kb_out)
            except Exception as _kbe:
                _log(f"KenBurns: 稳定模式片段{_kbi+1}失败 {_kbe}")
        _log(f"KenBurns: 稳定模式完成 {_kb_ok}/{len(temp_files)}")
        _log(f"阶段耗时: KenBurns {time.time() - _kb_stage_started:.1f}s")

    _concat_stage_started = time.time()
    _log(f"拼接 {len(temp_files)} 个片段...")
    raw_file = os.path.join(temp_dir, "raw_concat.mp4")
    # Re-encode and normalize each clip before joining. Stream-copy concat can
    # carry AAC/video duration rounding across boundaries and cause A/V drift.
    clip_durations = []
    clip_has_audio = []
    for clip_file in temp_files:
        clip_durations.append(_probe_media_duration(clip_file))
        try:
            probe = subprocess.run(
                [ffmpeg, "-i", clip_file],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=_NO_WINDOW,
                timeout=15,
            )
            clip_has_audio.append("Audio:" in (probe.stderr or ""))
        except Exception as probe_error:
            clip_has_audio.append(False)
            _log(f"拼接预检: 无法读取音频流 {os.path.basename(clip_file)}: {probe_error}")

    concat_cmd = [ffmpeg, "-y"]
    concat_filters = []
    concat_inputs = []
    concat_input_idx = 0
    for concat_idx, clip_file in enumerate(temp_files):
        clip_input_idx = concat_input_idx
        concat_cmd += ["-i", clip_file]
        v_label = f"vnorm{concat_idx}"
        a_label = f"anorm{concat_idx}"
        concat_filters.append(
            f"[{clip_input_idx}:v]{_stable_video_tail_filter(VIDEO_CONFIG['fps'])},setsar=1[{v_label}]"
        )
        if clip_has_audio[concat_idx]:
            audio_input_idx = clip_input_idx
        else:
            silent_duration = max(0.1, float(clip_durations[concat_idx] or 1.0))
            concat_cmd += [
                "-f", "lavfi", "-t", f"{silent_duration:.3f}",
                "-i", "anullsrc=r=44100:cl=stereo",
            ]
            audio_input_idx = clip_input_idx + 1
            _log(f"拼接预检: {os.path.basename(clip_file)} 无音频流，补静音轨")
        concat_filters.append(
            f"[{audio_input_idx}:a]{_stable_audio_tail_filter()},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[{a_label}]"
        )
        concat_inputs.append(f"[{v_label}][{a_label}]")
        concat_input_idx = audio_input_idx + 1

    concat_filters.append(
        f"{''.join(concat_inputs)}concat=n={len(temp_files)}:v=1:a=1[outv][outa]"
    )
    concat_filters.append(
        f"[outv]{_stable_video_tail_filter(VIDEO_CONFIG['fps'])},setsar=1[vstable]"
    )
    concat_filters.append(
        f"[outa]{_stable_audio_tail_filter()},"
        "aformat=sample_fmts=fltp:channel_layouts=stereo[astable]"
    )
    concat_cmd += [
        "-filter_complex", ";".join(concat_filters),
        "-map", "[vstable]", "-map", "[astable]",
    ]
    concat_cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
    concat_cmd += _intermediate_vcodec_args()
    concat_cmd += [
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-shortest", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
        raw_file,
    ]
    _log("拼接: 使用音视频时间轴规范化模式")
    stderr_data = ""
    try:
        concat_ok, concat_rc, stderr_data = _run_ffmpeg_with_hw_fallback(
            concat_cmd,
            dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"),
            600,
            _log,
            "智能成片拼接",
            raw_file,
            software_args=_intermediate_software_vcodec_args(),
            cancel_event=cancel_event,
        )
    except subprocess.TimeoutExpired:
        _log("拼接超时(>600s)")
        if stderr_data:
            for line in stderr_data.strip().split("\n")[-5:]:
                if line.strip(): _log(f"  ffmpeg: {line.strip()}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False
    except Exception as exc:
        if _cancelled():
            _log("已取消。")
        else:
            _log(f"拼接失败: {exc}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    if not concat_ok or not os.path.exists(raw_file):
        _log(f"拼接失败(exit={concat_rc})")
        if stderr_data:
            for line in stderr_data.strip().split("\n")[-5:]:
                if line.strip(): _log(f"  ffmpeg: {line.strip()}")
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    _transition_mask = []
    _transition_overlaps = [0.0] * max(0, len(temp_files) - 1)
    if _transition_mode == "fade" and len(temp_files) > 1:
        _transition_mask = _transition_boundary_mask(locals().get("_clip_cut_maps", []), len(temp_files))
        _transition_count = sum(1 for value in _transition_mask if value)
        _log(f"片段转场: 计划轻叠化 {_transition_count}/{len(_transition_mask)} 处，连续片段保持硬切")
        if _transition_count and dedup_preset == "none":
            transition_raw_file = os.path.join(temp_dir, "raw_transition.mp4")
            transition_ok, _transition_overlaps = _concat_clips_with_light_dissolve(
                ffmpeg, temp_files, transition_raw_file, _transition_duration, _log,
                cancel_event=cancel_event, transition_mask=_transition_mask
            )
            if transition_ok:
                raw_file = transition_raw_file

    raw_mb = os.path.getsize(raw_file) / (1024 * 1024)
    _log("[STEP] 🔗 拼接合并中...")
    _log(f"拼接完成: {raw_mb:.1f}MB")
    _log(f"阶段耗时: 拼接 {time.time() - _concat_stage_started:.1f}s")
    _log(f"[PROGRESS] 0.5")

    # ============================================================
    # 第三步：去重重编码（全程无字幕，避免镜像携带字幕）
    # ============================================================
    if _cancelled():
        _log("已取消。"); shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    _log(f"整体去重 ({dedup_preset})...")
    _dedup_stage_started = time.time()

    nosub_file = os.path.join(temp_dir, "nosub.mp4")
    _subtitle_speed_factor = 1.0

    if dedup_preset == "none":
        import shutil as _shutil
        _shutil.copy2(raw_file, nosub_file)
    else:
        _log(f"去重步骤使用分辨率: {w}x{h}，去重预设: {dedup_preset}")
        if _dedup_is_custom:
            dedup = _manual_dedup_filters(_video_options_without_mirror(dedup_video_options), dedup_audio_options)
            frame_vf, frame_applied = _custom_frame_structure_filter(dedup_video_options, cfg.get("fps", 30))
            if frame_vf:
                dedup["video_filters"] = _append_filter(dedup.get("video_filters"), frame_vf)
                dedup.setdefault("applied", []).append(frame_applied)
        else:
            dedup = build_dedup_filters(w, h, 0, mirror_enabled=False)
        _planned_subtitle_speed = _dedup_speed_factor(dedup)
        # [v9.1] 9:16裁剪+镜像+afade从切割步骤移至去重步骤
        # 字幕在去重后添加，镜像不会影响字幕
        vf = f"setpts=PTS-STARTPTS,scale=-2:{h}:force_original_aspect_ratio=decrease:flags=lanczos,crop={w}:{h},{_final_sharpen_vf()}"
        # 音频淡入淡出（消除片段间硬切感）+ 异步重采样
        af = f"{_stable_audio_tail_filter()},afade=t=in:st=0:d=0.3"
        if dedup["video_filters"]:
            vf = dedup["video_filters"] + "," + vf
        if dedup["audio_filters"]:
            af = dedup["audio_filters"] + "," + af
        vf = _append_stable_video_tail_filter(vf, cfg["fps"])

        # 输出去重参数详情
        applied = ",".join(dedup["applied"]) if dedup["applied"] else "none"
        _log(f"去重效果: {applied}")

        # 判断是否需要 filter_complex（aevalsrc 会创建额外音频流）
        needs_complex = "aevalsrc" in af or "amix" in af
        _combined_transition_dedup_done = False
        if _transition_mode == "fade" and any(_transition_mask) and not needs_complex:
            _log("片段转场: 合并轻叠化与去重编码，减少一次整片重编码")
            transition_ok, combined_overlaps = _concat_clips_with_light_dissolve(
                ffmpeg, temp_files, nosub_file, _transition_duration, _log,
                cancel_event=cancel_event,
                transition_mask=_transition_mask,
                video_filter=vf,
                audio_filter=af,
                video_codec_args=_vcodec_args(),
            )
            if transition_ok:
                _transition_overlaps = combined_overlaps
                _subtitle_speed_factor = _planned_subtitle_speed
                _combined_transition_dedup_done = True
            else:
                _log("片段转场: 合并编码失败，回退普通去重链路")
        elif _transition_mode == "fade" and any(_transition_mask) and needs_complex:
            _log("片段转场: 当前音频去重含双音轨融合，暂不合并编码，保留完整去重效果")
            transition_raw_file = os.path.join(temp_dir, "raw_transition.mp4")
            transition_ok, separate_overlaps = _concat_clips_with_light_dissolve(
                ffmpeg, temp_files, transition_raw_file, _transition_duration, _log,
                cancel_event=cancel_event,
                transition_mask=_transition_mask,
            )
            if transition_ok:
                raw_file = transition_raw_file
                _transition_overlaps = separate_overlaps
            else:
                _log("片段转场: 单独轻叠化失败，回退普通去重链路")

        if _combined_transition_dedup_done:
            dedup_cmd = None
        else:
            dedup_cmd = [ffmpeg, "-y", "-i", raw_file]

        if dedup_cmd is not None and needs_complex:
            af_parts = af.split(",")
            simple_af_parts = []
            noise_src = None
            for part in af_parts:
                if part.startswith("aevalsrc="):
                    noise_src = part
                elif part.startswith("amix="):
                    continue
                else:
                    simple_af_parts.append(part)
            simple_af = ",".join(simple_af_parts)
            if noise_src:
                complex_a = f"[0:a]{simple_af}[a1];{noise_src}[noise];[a1][noise]amix=inputs=2:duration=first:dropout_transition=0[out_a]"
            else:
                complex_a = f"[0:a]{simple_af}[out_a]" if simple_af else "[0:a]anull[out_a]"

            complex_v = f"[0:v]{vf}[out_v]"

            complex_graph = f"{complex_v};{complex_a}"
            dedup_cmd += ["-filter_complex", complex_graph]
            dedup_cmd += ["-map", "[out_v]", "-map", "[out_a]"]
        elif dedup_cmd is not None:
            dedup_cmd += ["-vf", vf]
            dedup_cmd += ["-af", af]
        if dedup_cmd is not None:
            dedup_cmd += _stable_cfr_output_args(cfg["fps"])
            _ve = _get_video_encoder() if not _hw_fallback else None
            _using_hw_encoder = bool(_ve)
            dedup_cmd += _vcodec_args()
            dedup_cmd += ["-c:a", cfg["codec_a"], "-b:a", cfg["bitrate_a"], "-shortest"]
            dedup_cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
            dedup_cmd += [nosub_file]

        try:
            if dedup_cmd is not None:
                proc = _register_process(subprocess.Popen(dedup_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW))
                _, stderr_data = _communicate_process(proc, timeout=600, cancel_event=cancel_event)
            if dedup_cmd is not None and proc.returncode != 0:
                _log(f"去重FFmpeg返回 {proc.returncode}")
                _log(f"去重stderr: {stderr_data[-300:]}")
                if _using_hw_encoder:
                    _hw_fallback = True
                    _sw_name = _software_encoder_name()
                    if _sw_name != "libx264":
                        _log("当前 FFmpeg 不支持 libx264，去重阶段使用 mpeg4 兜底。")
                    _log(f"硬件编码不可用，已自动改用 {_sw_name} 重新执行去重...")
                    cmd2 = _with_software_encoder(dedup_cmd)
                    try:
                        p2 = _register_process(subprocess.Popen(cmd2, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            creationflags=_NO_WINDOW))
                        _, _ = _communicate_process(p2, timeout=600, cancel_event=cancel_event)
                        if p2.returncode == 0 and os.path.exists(nosub_file) and os.path.getsize(nosub_file) > 1000:
                            _subtitle_speed_factor = _planned_subtitle_speed
                            _log(f"  去重已用 {_sw_name} 完成，去重效果已保留")
                        else:
                            _log(f"  {_sw_name} 去重重试失败，使用未去重拼接片继续输出")
                            import shutil as _shutil
                            _shutil.copy2(raw_file, nosub_file)
                    except:
                        import shutil as _shutil
                        _shutil.copy2(raw_file, nosub_file)
                else:
                    import shutil as _shutil
                    _shutil.copy2(raw_file, nosub_file)
            elif dedup_cmd is not None:
                _subtitle_speed_factor = _planned_subtitle_speed
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            proc.communicate()
            _log("去重超时，直接输出原始拼接...")
            import shutil as _shutil
            _shutil.copy2(raw_file, nosub_file)
        except Exception as exc:
            if _cancelled():
                _log("已取消。")
                shutil.rmtree(temp_dir, ignore_errors=True)
                if auto_srt and temp_srt:
                    from stt import cleanup_srt; cleanup_srt(temp_srt)
                DEDUP_PRESET = old_preset
                return False
            _log(f"去重异常，直接输出原始拼接: {exc}")
            import shutil as _shutil
            _shutil.copy2(raw_file, nosub_file)

        if not os.path.exists(nosub_file):
            _log(f"去重失败，直接输出原始拼接...")
            import shutil as _shutil
            _shutil.copy2(raw_file, nosub_file)

    nosub_mb = os.path.getsize(nosub_file) / (1024 * 1024)
    _log("[STEP] 📝 字幕处理中...")
    _log(f"[PROGRESS] 0.6")
    _log(f"去重完成: {nosub_mb:.1f}MB")
    _log(f"阶段耗时: 去重 {time.time() - _dedup_stage_started:.1f}s")

    _nosub_duration = _probe_media_duration(nosub_file)
    _duration_ok, _duration_contract = _validate_actual_duration_contract(
        _nosub_duration,
        target_duration,
        margin=1.0,
        shortage_grace_seconds=_duration_shortage_grace,
        user_confirmed=_user_confirmed_clips,
        duration_tolerance=duration_tolerance,
    )
    _log(
        f"成片时长预验收: {_duration_contract['actual']:.1f}s，"
        f"要求{_duration_contract['low']:.0f}-{_duration_contract['high']:.0f}s"
    )
    if not _duration_ok:
        _duration_error = (
            f"成片时长未达标：实际{_duration_contract['actual']:.1f}秒，"
            f"目标{_duration_contract['target']:.0f}秒"
            f"（允许{_duration_contract['low']:.0f}-{_duration_contract['high']:.0f}秒）"
        )
        _log(_duration_error + "，已停止字幕烧录，避免继续耗时并输出错误成片")
        _run_log["结果"] = "失败"
        _run_log["错误"] = _duration_error
        _save_run_log()
        shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt
            cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        raise RuntimeError(_duration_error)

    # ============================================================
    # 第四步：字幕后置处理（统一ASR识别最终视频 + DeepSeek修复错别字）
    # ============================================================
    if _cancelled():
        _log("已取消。"); shutil.rmtree(temp_dir, ignore_errors=True)
        if auto_srt and temp_srt:
            from stt import cleanup_srt; cleanup_srt(temp_srt)
        DEDUP_PRESET = old_preset
        return False

    # 画中画：auto模式在字幕关闭时也需要加；指定文件时总是加
    if pip_path and pip_path != "auto" and os.path.exists(pip_path):
        has_pip = True
    elif pip_path == "auto" and not will_subtitle:
        has_pip = True
    else:
        has_pip = False
    _log(f"字幕={will_subtitle}, 画中画={has_pip} ({pip_path})")
    _subtitle_stage_started = time.time()
    if will_subtitle and os.path.exists(nosub_file) and os.path.getsize(nosub_file) > 10000:
        _mapped_subtitles = _build_mapped_subtitle_segments(
            locals().get("_clip_cut_maps", []),
            _srt_segments_for_cut,
            _subtitle_speed_factor,
            transition_overlaps=_transition_overlaps,
        )
        if _mapped_subtitles:
            _log(f"字幕时间轴: 源SRT映射 {len(_mapped_subtitles)} 条，变速倍率 {_subtitle_speed_factor:.3f}x")
            _mapped_ok = _burn_mapped_subtitles_final(
                nosub_file, output_path, w, h, temp_dir, _log, _mapped_subtitles,
                pip_path, pip_size, pip_opacity, pip_pos,
            )
            if not _mapped_ok:
                _add_subtitles_final(nosub_file, output_path, w, h, temp_dir, _log, pip_path, pip_size, pip_opacity, pip_pos)
        else:
            _log("字幕时间轴: 源SRT映射不可用，回退成片语音识别。")
            _add_subtitles_final(nosub_file, output_path, w, h, temp_dir, _log, pip_path, pip_size, pip_opacity, pip_pos)
    elif has_pip and os.path.exists(nosub_file):
        # auto模式用视频本身做画中画素材
        _effective_pip = video_path if pip_path == "auto" else pip_path
        _add_pip_only(nosub_file, output_path, temp_dir, _log, _effective_pip, pip_size, pip_opacity, pip_pos)
    else:
        import shutil as _shutil
        _shutil.copy2(nosub_file, output_path)
    _log(f"阶段耗时: 字幕/画中画 {time.time() - _subtitle_stage_started:.1f}s")

    # 第五步：AI 画面质量分析与替换
    if _cancelled():
        _log("已取消，跳过画面替换。")
    # ============================================================
    try:
        from vision_replace import is_vision_enabled, vision_replace_pipeline
        if os.path.exists(output_path):
            # Auto-enable: 只要 API Key 和视觉模型配置了就运行
            try:
                from vision_replace import load_vision_settings
                vs = load_vision_settings()
                auto_vision = bool(vs.get("api_key") and vs.get("base_url"))
            except Exception:
                auto_vision = False
            if auto_vision:
                # 传入剪辑片段信息用于画面分析
                clip_info = [{"start": c.get("start", 0) if isinstance(c, dict) else c[3], "end": c.get("end", 0) if isinstance(c, dict) else c[4]} for c in ordered_clips]
                vision_replace_pipeline(output_path, clip_info, log_fn=_log)
    except ImportError:
        pass  # vision_replace.py 不存在则跳过
    except Exception as e:
        _log(f"AI画面替换出错: {e}")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    _log(f"[PROGRESS] 1.0")

    # 用 ffprobe 测成品真实时长
    _actual_dur = 0.0
    try:
        _ff = get_ffmpeg_cmd()
        _ffprobe = os.path.join(os.path.dirname(_ff), "ffprobe" + (".exe" if os.name == "nt" else ""))
        _r = subprocess.run([_ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", output_path],
                            capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=10, creationflags=_NO_WINDOW)
        if _r.returncode == 0 and _r.stdout.strip():
            _actual_dur = float(_r.stdout.strip())
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass

    if _actual_dur > 0:
        _final_duration_ok, _final_contract = _validate_actual_duration_contract(
            _actual_dur,
            target_duration,
            margin=1.0,
            shortage_grace_seconds=_duration_shortage_grace,
            user_confirmed=_user_confirmed_clips,
            duration_tolerance=duration_tolerance,
        )
        if not _final_duration_ok:
            _duration_error = (
                f"最终成片时长未达标：实际{_final_contract['actual']:.1f}秒，"
                f"目标{_final_contract['target']:.0f}秒"
                f"（允许{_final_contract['low']:.0f}-{_final_contract['high']:.0f}秒）"
            )
            _log(_duration_error)
            try:
                os.remove(output_path)
            except OSError:
                _LOG.warning("failed to remove output path", exc_info=True)
            _run_log["结果"] = "失败"
            _run_log["错误"] = _duration_error
            _save_run_log()
            shutil.rmtree(temp_dir, ignore_errors=True)
            if auto_srt and temp_srt:
                from stt import cleanup_srt
                cleanup_srt(temp_srt)
            DEDUP_PRESET = old_preset
            raise RuntimeError(_duration_error)

    # ---- 切割评分 ----
    report = _build_cut_report(ordered_clips, success_count, total_clips, output_path, size_mb)
    _print_cut_report(report, _log)
    if _actual_dur > 0:
        _log(f"  成品真实时长: {_actual_dur:.0f}s")

    _log(f"生成成功！")
    _log(f"  路径: {output_path}")
    _log(f"  大小: {size_mb:.1f} MB")
    _log(f"  片段: {success_count}/{total_clips}")

    # 清理
    shutil.rmtree(temp_dir, ignore_errors=True)
    if auto_srt and temp_srt:
        from stt import cleanup_srt; cleanup_srt(temp_srt)
    DEDUP_PRESET = old_preset
    _run_log["结果"] = "成功"
    _run_log["选片"] = report
    _run_log["输出"] = output_path
    _save_run_log()
    return {"ok": True, "report": report}


# 兼容旧接口
def cut_and_dedup(video_path, srt_path, output_path, dedup_preset="medium", log_fn=None, cancel_event=None):
    return process_video(video_path, srt_path=srt_path, output_path=output_path,
                          dedup_preset=dedup_preset, subtitle_overlay=False, log_fn=log_fn,
                          cancel_event=cancel_event)



def _parse_srt_to_segments(srt_text):
    """解析 SRT 格式文本为 segments 列表: [{"start": float, "end": float, "text": str}, ...]"""
    import re
    segments = []
    lines = srt_text.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})', line)
        if m:
            start = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3)) + int(m.group(4))/1000.0
            end = int(m.group(5))*3600 + int(m.group(6))*60 + int(m.group(7)) + int(m.group(8))/1000.0
            text = ""
            j = i + 1
            while j < len(lines) and lines[j].strip():
                text += lines[j].strip()
                j += 1
            if text.strip():
                segments.append({"start": start, "end": end, "text": text.strip()})
            i = j
        else:
            i += 1
    return segments


def _srt_text_for_range(srt_segments, start, end, min_overlap=0.04):
    """Return joined SRT text that is actually covered by a source time range."""
    pieces = []
    try:
        start = float(start)
        end = float(end)
    except Exception:
        return ""
    if end <= start:
        return ""
    for seg in srt_segments or []:
        try:
            seg_start = float(seg.get("start", 0))
            seg_end = float(seg.get("end", seg_start))
        except Exception:
            continue
        overlap_start = max(start, seg_start)
        overlap_end = min(end, seg_end)
        if overlap_end - overlap_start < min_overlap:
            continue
        text = str(seg.get("text", "")).strip()
        if text:
            pieces.append(text)
    return "".join(pieces).strip()


def _dedup_speed_factor(dedup):
    """Extract the effective final speed multiplier from dedup filters."""
    if not dedup:
        return 1.0
    haystack = " ".join([
        ",".join(str(x) for x in dedup.get("applied", []) or []),
        str(dedup.get("video_filters", "") or ""),
        str(dedup.get("audio_filters", "") or ""),
    ])
    for pattern in (r"speed\((\d+(?:\.\d+)?)x\)", r"setpts=PTS/(\d+(?:\.\d+)?)", r"atempo=(\d+(?:\.\d+)?)"):
        m = re.search(pattern, haystack)
        if not m:
            continue
        try:
            speed = float(m.group(1))
            if 0.05 <= speed <= 8.0:
                return speed
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
    return 1.0


def _build_mapped_subtitle_segments(cut_maps, srt_segments, speed_factor=1.0, transition_overlaps=None):
    """Map source SRT segments through actual cut ranges into final output time."""
    try:
        speed_factor = float(speed_factor or 1.0)
    except Exception:
        speed_factor = 1.0
    if speed_factor <= 0:
        speed_factor = 1.0

    mapped = []
    cursor = 0.0
    overlap_list = list(transition_overlaps or [])
    for clip_index, item in enumerate(cut_maps or []):
        try:
            clip_start = float(item.get("start", 0))
            clip_end = float(item.get("end", clip_start))
        except Exception:
            continue
        clip_duration = max(0.0, clip_end - clip_start)
        if clip_duration <= 0:
            continue

        added_for_clip = 0
        for seg in srt_segments or []:
            try:
                seg_start = float(seg.get("start", 0))
                seg_end = float(seg.get("end", seg_start))
            except Exception:
                continue
            overlap_start = max(clip_start, seg_start)
            overlap_end = min(clip_end, seg_end)
            if overlap_end - overlap_start < 0.04:
                continue
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            out_start = (cursor + (overlap_start - clip_start)) / speed_factor
            out_end = (cursor + (overlap_end - clip_start)) / speed_factor
            if out_end - out_start >= 0.04:
                mapped.append({"start": out_start, "end": out_end, "text": text})
                added_for_clip += 1

        if added_for_clip == 0:
            text = str(item.get("text", "")).strip()
            if text:
                mapped.append({
                    "start": cursor / speed_factor,
                    "end": (cursor + clip_duration) / speed_factor,
                    "text": text,
                })
        overlap_after = 0.0
        if clip_index < len(overlap_list):
            try:
                overlap_after = max(0.0, min(float(overlap_list[clip_index]), clip_duration / 2))
            except Exception:
                overlap_after = 0.0
        cursor += max(0.0, clip_duration - overlap_after)
    return mapped


def _cut_needs_next_sentence(text):
    text = str(text or "").strip().rstrip("。！？!?，,、；;：:")
    if not text:
        return False
    return text.endswith((
        "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
        "的话", "你看", "我觉得", "感觉", "给你们", "这个", "这款", "这件",
        "它是", "它会", "来讲的话", "一点", "一点点", "有没有发现",
        "你去", "去", "你想象一下", "想象一下"
    ))


def _cut_starts_as_followup(text):
    text = str(text or "").strip()
    return text.startswith((
        "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
        "它", "这个", "这款", "这件", "你看"
    ))


def _apply_srt_cut_alignment(c_type, start, end, srt_segments, clip_idx=0, total_clips=1):
    """Apply the same SRT boundary alignment used by final cutting."""
    try:
        start = float(start)
        end = float(end)
    except Exception:
        return start, end, False
    original = (start, end)
    boundaries = []
    for seg in srt_segments or []:
        try:
            boundaries.append((float(seg.get("start", 0)), float(seg.get("end", 0))))
        except Exception:
            continue

    if boundaries:
        start_srt, end_srt = None, None
        for seg_start, seg_end in boundaries:
            if seg_start <= end <= seg_end:
                end_srt = seg_end
            if seg_start <= start <= seg_end:
                start_srt = seg_start
        if end_srt and end_srt - end <= 8.0:
            end = end_srt
        if start_srt and start - start_srt <= 3.0:
            start = start_srt

    c_type_text = str(c_type or "").lower()
    is_tail = clip_idx == total_clips - 1 or c_type_text in ("close", "cta", "call_to_action")
    if srt_segments and is_tail:
        try:
            for seg_idx, seg in enumerate(srt_segments):
                seg_start = float(seg.get("start", 0))
                seg_end = float(seg.get("end", seg_start))
                if abs(seg_end - end) <= 0.6 and seg_idx + 1 < len(srt_segments):
                    next_seg = srt_segments[seg_idx + 1]
                    next_start = float(next_seg.get("start", seg_end))
                    next_end = float(next_seg.get("end", next_start))
                    text = str(seg.get("text", ""))
                    next_text = str(next_seg.get("text", ""))
                    if next_start - end <= 1.2 and next_end - start <= 14.0 and (
                        _cut_needs_next_sentence(text) or _cut_starts_as_followup(next_text)
                    ):
                        end = next_end
                    break
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass

    return start, end, (abs(start - original[0]) > 0.01 or abs(end - original[1]) > 0.01)


def _get_video_duration(path, ffmpeg_cmd):
    """Get video duration in seconds using ffprobe"""
    import subprocess, json
    ffprobe = ffmpeg_cmd.replace("ffmpeg", "ffprobe")
    if ffprobe == ffmpeg_cmd:
        ffprobe = "ffprobe"
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=10, creationflags=_NO_WINDOW)
        data = json.loads(proc.stdout)
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0


def _calc_pip_loop(main_path, pip_path, ffmpeg_cmd):
    """Calculate how many times pip video needs to loop to cover main video duration"""
    main_dur = _get_video_duration(main_path, ffmpeg_cmd)
    pip_dur = _get_video_duration(pip_path, ffmpeg_cmd)
    if pip_dur <= 0 or main_dur <= 0:
        return 2
    import math
    return max(2, math.ceil(main_dur / pip_dur) + 1)


def _add_pip_only(video_path, output_path, temp_dir, _log, pip_path, pip_size=0.15, pip_opacity=0.03, pip_pos="\u53f3\u4e0b"):
    """\u53ea\u53e0\u52a0\u753b\u4e2d\u753b\uff0c\u4e0d\u70e7\u5f55\u5b57\u5e55"""
    from platform_config import IS_MAC
    import subprocess, os, sys
    ffmpeg = get_ffmpeg_cmd()

    loop_n = _calc_pip_loop(video_path, pip_path, ffmpeg)
    _pos_map = {"\u5de6\u4e0a": "10:10", "\u53f3\u4e0a": "W-w-10:10", "\u5de6\u4e0b": "10:H-h-10", "\u53f3\u4e0b": "W-w-10:H-h-10"}
    _pip_pos = _pos_map.get(pip_pos, "W-w-10:H-h-10")
    _pip_fc = f"[1:v]scale=iw*{pip_size}:ih*{pip_size},format=rgba,colorchannelmixer=aa={pip_opacity}[pip];[0:v][pip]overlay={_pip_pos}[out_v]"

    _norm_output = output_path.replace("/", os.sep)
    cmd = [
        ffmpeg, "-y", "-i", video_path, "-stream_loop", str(loop_n), "-i", pip_path,
        "-filter_complex", _pip_fc,
        "-map", "[out_v]", "-map", "0:a",
    ]
    cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
    cmd += _final_vcodec_args()
    cmd += _final_audio_sync_args()
    cmd += ["-movflags", "+faststart", _norm_output]

    popen_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")

    _log(f"叠加画中画: {os.path.basename(pip_path)}")
    try:
        ok, rc, stderr = _run_ffmpeg_with_hw_fallback(
            cmd, popen_kw, 450, _log, "画中画叠加", output_path,
            software_args=_final_software_vcodec_args()
        )
        if ok:
            _log("\u753b\u4e2d\u753b\u53e0\u52a0\u6210\u529f!")
        else:
            _log(f"\u753b\u4e2d\u753b\u53e0\u52a0\u5931\u8d25: {stderr[-200:] if stderr else ''}")
            import shutil as _shutil; _shutil.copy2(video_path, output_path)
    except Exception as e:
        _log(f"\u753b\u4e2d\u753b\u53e0\u52a0\u5f02\u5e38: {e}")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)


def _split_mapped_subtitle_text(text, max_chars=14):
    """Split mapped subtitle text without changing the words."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return []
    parts = []
    while len(text) > max_chars:
        cut = -1
        for mark in "，。！？、；,.!?; ":
            pos = text.rfind(mark, 1, max_chars + 1)
            if pos > 0:
                cut = pos + (0 if mark == " " else 1)
                break
        if cut <= 0:
            cut = max_chars
        chunk = text[:cut].strip()
        if chunk:
            parts.append(chunk)
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def _prepare_mapped_subtitle_segments(raw_segments):
    fixed = []
    for seg in raw_segments or []:
        try:
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start))
        except Exception:
            continue
        text = str(seg.get("text", "")).strip()
        if not text or end <= start:
            continue
        fixed.append({"start": start, "end": end, "text": text})

    if len(fixed) > 1:
        fixed.sort(key=lambda item: (item["start"], item["end"]))
        deduped = [fixed[0].copy()]
        for seg in fixed[1:]:
            prev = deduped[-1]
            if seg["start"] < prev["end"]:
                prev["end"] = seg["start"]
            if seg["end"] > seg["start"]:
                deduped.append(seg.copy())
        fixed = deduped

    try:
        from config import ASR_CORRECTIONS as _asr_corrections
    except Exception:
        _asr_corrections = {}
    if _asr_corrections:
        for seg in fixed:
            text = seg["text"]
            for wrong, right in _asr_corrections.items():
                if wrong in text:
                    text = text.replace(wrong, right)
            seg["text"] = text

    split_segments = []
    for seg in fixed:
        parts = _split_mapped_subtitle_text(seg["text"], max_chars=14)
        if not parts:
            continue
        duration = max(0.04, seg["end"] - seg["start"])
        total_chars = sum(max(1, len(part)) for part in parts)
        cursor = seg["start"]
        for idx, part in enumerate(parts):
            if idx == len(parts) - 1:
                part_end = seg["end"]
            else:
                part_end = cursor + duration * (max(1, len(part)) / total_chars)
            if part_end > cursor:
                output_text = _strip_output_subtitle_punctuation(part)
                if output_text:
                    split_segments.append({"start": cursor, "end": part_end, "text": output_text})
            cursor = part_end
    if len(split_segments) <= 1:
        return split_segments

    # Very short mapped fragments are usually produced by a cut boundary or a
    # proportional text split. Keep their words, but merge them into the
    # nearest continuous caption so viewers do not see a 1-frame flash.
    merged_segments = []
    pending_prefix = None
    for seg in split_segments:
        current = seg.copy()
        duration = current["end"] - current["start"]
        if pending_prefix is not None:
            if current["start"] - pending_prefix["end"] <= 0.18:
                current["start"] = pending_prefix["start"]
                current["text"] = pending_prefix["text"] + current["text"]
            else:
                merged_segments.append(pending_prefix)
            pending_prefix = None
        if duration >= 0.22:
            merged_segments.append(current)
            continue
        if merged_segments and current["start"] - merged_segments[-1]["end"] <= 0.18:
            merged_segments[-1]["end"] = max(merged_segments[-1]["end"], current["end"])
            merged_segments[-1]["text"] += current["text"]
        else:
            pending_prefix = current
    if pending_prefix is not None:
        merged_segments.append(pending_prefix)
    return merged_segments


def _ass_timestamp(seconds):
    total_cs = int(round(max(0.0, float(seconds or 0.0)) * 100.0))
    hours, remainder = divmod(total_cs, 360000)
    minutes, remainder = divmod(remainder, 6000)
    secs, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def _ffmpeg_filter_path(path):
    return (
        os.path.abspath(str(path))
        .replace("\\", "/")
        .replace(":", "\\:")
        .replace("'", "\\'")
    )


def _write_mapped_subtitle_ass(path, segments, width, height, font_name, font_size, margin_v):
    sc = SUBTITLE_OVERLAY
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {max(1, int(width or 1080))}",
        f"PlayResY: {max(1, int(height or 1920))}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        (
            f"Style: Default,{font_name},{int(font_size)},"
            f"{sc.get('font_color', '&H00FFFFFF')},&H000000FF,"
            f"{sc.get('outline_color', '&H00000000')},&H80000000,"
            f"-1,0,0,0,100,100,0,0,1,{max(0, int(sc.get('outline_width', 0)))},2,2,20,20,{int(margin_v)},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for seg in segments:
        text = str(seg.get("text") or "").replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        if not text:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(seg['start'])},{_ass_timestamp(seg['end'])},Default,,0,0,0,,{text}"
        )
    with open(path, "w", encoding="utf-8-sig", newline="\n") as ass_file:
        ass_file.write("\n".join(lines) + "\n")


def _burn_mapped_subtitles_final(video_path, output_path, w, h, temp_dir, _log, subtitle_segments,
                                 pip_path=None, pip_size=0.15, pip_opacity=0.03, pip_pos="右下"):
    """Burn already-timed subtitle segments. Returns True when output is created."""
    fixed_segments = _prepare_mapped_subtitle_segments(subtitle_segments)
    if not fixed_segments:
        return False

    ffmpeg = get_ffmpeg_cmd()
    _log(f"字幕：使用源 SRT 映射 {len(fixed_segments)} 条，跳过成片语音识别。")
    _log("[PROGRESS] 0.78")

    try:
        from platform_config import DRAWTEXT_FONT_PATH, FONT_BOLD_NAME, FONT_BOLD_PATH, IS_MAC

        font_dest = os.path.join(temp_dir, "drawtext_font.ttc")
        if os.path.exists(FONT_BOLD_PATH) and not os.path.exists(font_dest):
            import shutil as _shutil_font
            _shutil_font.copy2(FONT_BOLD_PATH, font_dest)
        if os.path.exists(font_dest):
            drawtext_font = font_dest.replace(os.sep, "/").replace(":", "\\:")
        else:
            drawtext_font = DRAWTEXT_FONT_PATH

        sc = SUBTITLE_OVERLAY
        font_size = sc.get("font_size", 52)
        try:
            from ai_clipper import load_settings as _load_subtitle_settings
            subtitle_settings = _load_subtitle_settings()
            font_size = max(32, min(96, int(float(subtitle_settings.get("subtitle_font_size", font_size)))))
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
        if w and w > 0:
            font_size = max(28, int(font_size * w / 1080))
        margin_v = sc.get("margin_v", 270) + 100
        norm_output = str(output_path).replace("/", os.sep)
        has_pip = pip_path is not None
        pos_map = {"左上": "10:10", "右上": "W-w-10:10", "左下": "10:H-h-10", "右下": "W-w-10:H-h-10"}
        pip_position = pos_map.get(pip_pos, "W-w-10:H-h-10")

        def _build_subtitle_cmd(filter_chain):
            if has_pip and pip_path and pip_path != "auto" and os.path.exists(pip_path):
                filter_complex = (
                    f"[1:v]scale=iw*{pip_size}:ih*{pip_size},format=rgba,"
                    f"colorchannelmixer=aa={pip_opacity}[pip];"
                    f"[0:v][pip]overlay={pip_position}[with_pip];"
                    f"[with_pip]{filter_chain}[out_v]"
                )
                loop_n = _calc_pip_loop(video_path, pip_path, ffmpeg)
                command = [
                    ffmpeg, "-y", "-i", video_path, "-stream_loop", str(loop_n), "-i", pip_path,
                    "-filter_complex", filter_complex,
                    "-map", "[out_v]", "-map", "0:a",
                ]
            elif has_pip:
                filter_complex = (
                    f"[0:v]split[main][pip];"
                    f"[pip]scale=iw*{pip_size}:ih*{pip_size},format=rgba,"
                    f"colorchannelmixer=aa={pip_opacity}[overlay];"
                    f"[main][overlay]overlay={pip_position}[with_pip];"
                    f"[with_pip]{filter_chain}[out_v]"
                )
                command = [
                    ffmpeg, "-y", "-i", video_path,
                    "-filter_complex", filter_complex,
                    "-map", "[out_v]", "-map", "0:a",
                ]
            else:
                command = [ffmpeg, "-y", "-i", video_path, "-vf", filter_chain]
            command += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
            command += _final_vcodec_args()
            command += _final_audio_sync_args()
            command += ["-movflags", "+faststart", norm_output]
            return command

        popen_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace")
        if sys.platform == "win32":
            fc_conf = os.path.join(temp_dir, "fonts.conf")
            with open(fc_conf, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig><include ignore_missing="yes"/></fontconfig>\n')
            fc_dtd = os.path.join(temp_dir, "fonts.dtd")
            if not os.path.exists(fc_dtd):
                with open(fc_dtd, "w", encoding="utf-8") as f:
                    f.write('<!ELEMENT fontconfig (dir|cache|match)*>\n')
                    f.write('<!ELEMENT dir (#PCDATA)>\n')
                    f.write('<!ELEMENT cache (#PCDATA)>\n')
                    f.write('<!ELEMENT match (test|edit)*>\n')
                    f.write('<!ELEMENT test (#PCDATA)>\n')
                    f.write('<!ELEMENT edit (#PCDATA)>\n')
            popen_kw["env"] = dict(os.environ)
            popen_kw["env"]["FONTCONFIG_FILE"] = fc_conf

        ass_path = os.path.join(temp_dir, "mapped_subtitles.ass")
        _write_mapped_subtitle_ass(
            ass_path, fixed_segments, w, h, FONT_BOLD_NAME, font_size, margin_v,
        )
        ass_filter = (
            f"ass=filename='{_ffmpeg_filter_path(ass_path)}'"
            f":fontsdir='{_ffmpeg_filter_path(temp_dir)}'"
        )
        _log(f"字幕烧录: 单轨 ASS，共 {len(fixed_segments)} 条字幕")
        ok, rc, stderr_data = _run_ffmpeg_with_hw_fallback(
            _build_subtitle_cmd(ass_filter), popen_kw, 450, _log, "源SRT单轨字幕烧录", output_path,
            software_args=_final_software_vcodec_args()
        )
        if ok:
            _log("源 SRT 映射字幕烧录成功。")
            _log("[PROGRESS] 0.9")
            return True
        _log(f"单轨字幕烧录失败，改用兼容字幕滤镜。exit={rc}")
        if stderr_data:
            for line in stderr_data.strip().split("\n")[-3:]:
                if line.strip():
                    _log(f"  ffmpeg: {line.strip()}")

        try:
            if os.path.exists(norm_output):
                os.remove(norm_output)
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
        drawtext_filters = []
        for idx, seg in enumerate(fixed_segments):
            txt_path = os.path.join(temp_dir, f"mapped_sub_{idx:04d}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(seg["text"])
            if IS_MAC:
                text_file = txt_path.replace("'", "'\\''")
            else:
                text_file = txt_path.replace("\\", "/").replace(":", "\\:")
            drawtext_filters.append(
                f"drawtext=fontfile='{drawtext_font}':textfile='{text_file}'"
                f":fontsize={font_size}:fontcolor=white"
                f":shadowx=2:shadowy=2:shadowcolor=black@0.5"
                f":x=(w-text_w)/2:y=h-{margin_v}"
                f":enable='between(t\\,{seg['start']:.3f}\\,{seg['end']:.3f})'"
            )
        if not drawtext_filters:
            return False
        ok, rc, stderr_data = _run_ffmpeg_with_hw_fallback(
            _build_subtitle_cmd(",".join(drawtext_filters)), popen_kw, 450, _log,
            "源SRT兼容字幕烧录", output_path, software_args=_final_software_vcodec_args(),
        )
        if ok:
            _log("源 SRT 映射字幕烧录成功（兼容模式）。")
            _log("[PROGRESS] 0.9")
            return True
        _log(f"源 SRT 映射字幕烧录失败，回退成片语音识别。exit={rc}")
        return False
    except Exception as exc:
        _log(f"源 SRT 映射字幕异常，回退成片语音识别: {exc}")
        return False


def _final_subtitle_local_asr_segments(wav_path, temp_dir, settings, log_fn):
    """Use the same SenseVoice-first local ASR policy as smart-cut preview."""
    selected_engine = str(settings.get("local_asr_engine", "sensevoice") or "sensevoice").strip().lower()
    whisper_model = str(settings.get("whisper_model", "small") or "small")
    engine_label = "SenseVoice" if selected_engine in ("sensevoice", "auto") else "Whisper"
    local_srt = os.path.join(temp_dir, "final_local_asr.srt")
    log_fn(f"字幕阶段：正在使用本地 {engine_label} 识别最终视频音频...")

    try:
        from stt import transcribe_local_audio_to_srt

        recognized = transcribe_local_audio_to_srt(
            wav_path,
            local_srt,
            log_fn=log_fn,
            whisper_model=whisper_model,
            asr_engine=selected_engine,
        )
    except Exception as exc:
        log_fn(f"本地语音识别失败: {exc}")
        return []
    if not recognized or not os.path.exists(local_srt):
        log_fn("本地语音识别未生成可用字幕")
        return []

    segments = []
    try:
        from volcengine_asr import load_word_timing_sidecar

        segments = list(load_word_timing_sidecar(local_srt, semantic=True, log_fn=log_fn) or [])
    except Exception as sidecar_error:
        log_fn(f"本地词级时间不可用，改读 SRT 时间段: {sidecar_error}")
    if not segments:
        try:
            with open(local_srt, "r", encoding="utf-8-sig") as handle:
                segments = _parse_srt_to_segments(handle.read())
        except Exception as srt_error:
            log_fn(f"本地字幕读取失败: {srt_error}")
            return []

    normalized = []
    for segment in segments:
        try:
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or start)
            segment_text = str(segment.get("text") or "").strip()
        except (AttributeError, TypeError, ValueError):
            continue
        if segment_text and end > start:
            normalized.append({"start": start, "end": end, "text": segment_text})
    log_fn(f"本地语音识别完成: {len(normalized)} 条语音段")
    return normalized

def _add_subtitles_final(video_path, output_path, w, h, temp_dir, _log, pip_path=None, pip_size=0.15, pip_opacity=0.03, pip_pos="右下"):
    """
    字幕后置处理：对去重后的视频做 ASR 识别（云端优先，本地与智能成片一致）→ DeepSeek修复 → 烧录字幕。
    时间戳来自最终视频本身，100% 对齐，不受镜像/拼接/变速影响。
    """
    import json as _json

    _log("=" * 50)
    _log("第四步：字幕后置处理")
    _log("=" * 50)

    # --- 4a: 提取音频 ---
    wav_path = os.path.join(temp_dir, "final_audio.wav")
    ffmpeg = get_ffmpeg_cmd()
    extract_cmd = [ffmpeg, "-y", "-i", video_path, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", wav_path]
    try:
        popen_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = subprocess.Popen(extract_cmd, **popen_kw, creationflags=_NO_WINDOW)
        rc = proc.wait(timeout=60)
        if rc != 0 or not os.path.exists(wav_path):
            _log("音频提取失败，跳过字幕")
            import shutil as _shutil; _shutil.copy2(video_path, output_path)
            return
    except Exception as e:
        _log(f"音频提取异常: {e}，跳过字幕")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)
        return

    wav_mb = os.path.getsize(wav_path) / (1024 * 1024)
    _log(f"音频提取完成: {wav_mb:.1f}MB")
    _log("[PROGRESS] 0.65")

    # --- 4b: 云端 ASR 优先；本地路径复用智能成片的 SenseVoice 优先策略 ---
    import os as _os
    _os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

    raw_segments = None
    volcengine_success = False
    cloud_reference = ""  # 云端ASR的准确全文，用于AI修正

    def _run_aliyun_asr_subtitle():
        """阿里云 ASR：高精度字级时间戳+按标点断句"""
        try:
            with open(sp, "r", encoding="utf-8-sig") as _af:
                _acfg = _json.load(_af)
                if not _acfg.get("asr_enabled", False):
                    _log("云端ASR未启用，跳过阿里云")
                    return
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
        nonlocal raw_segments, volcengine_success
        try:
            _log("正在尝试阿里云 ASR...")
            from ai_clipper import load_settings as _ld_sub_ali
            cfg = _ld_sub_ali()
            _ali_api_key = cfg.get("aliyun_api_key", "")
            _ali_oss_ak = cfg.get("aliyun_oss_ak", "")
            _ali_oss_sk = cfg.get("aliyun_oss_sk", "")
            _ali_bucket = cfg.get("aliyun_bucket", "")
            _ali_endpoint = cfg.get("aliyun_endpoint", "oss-cn-beijing.aliyuncs.com")
            _ali_model = cfg.get("asr_model", "paraformer-v2") or "paraformer-v2"
            if not all([_ali_api_key, _ali_oss_ak, _ali_oss_sk, _ali_bucket]):
                _log("aliyun_asr: 未配置阿里云参数，跳过")
                return
            from aliyun_asr import aliyun_asr
            segs = aliyun_asr(wav_path, app_key=_ali_api_key, model=_ali_model,
                             oss_ak=_ali_oss_ak, oss_sk=_ali_oss_sk,
                             oss_bucket=_ali_bucket, oss_endpoint=_ali_endpoint,
                             log_fn=_log)
            if segs:
                raw_segments = segs
                volcengine_success = True
                _log(f"阿里云 ASR 成功: {len(raw_segments)} 条语音段")
            else:
                _log("阿里云 ASR 失败，将降级")
        except Exception as e:
            _log(f"阿里云 ASR 异常: {e}")

    def _run_volcengine_asr():
        """火山引擎大模型 ASR：高精度时间戳+断句"""
        # 仅当云端ASR启用时才执行
        try:
            with open(sp, "r", encoding="utf-8-sig") as _vf:
                if not _json.load(_vf).get("asr_enabled", False):
                    _log("云端ASR未启用，跳过火山引擎")
                    return
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
        nonlocal raw_segments, volcengine_success
        try:
            _log("正在尝试火山引擎 ASR...")
            from ai_clipper import load_settings as _ld_sub_volc
            cfg = _ld_sub_volc()
            if not cfg.get("volc_tos_ak") or not cfg.get("volc_tos_sk"):
                _log("volcengine_asr: 未配置火山引擎，跳过")
                return
            v_app_id = cfg.get("volc_app_id", "")
            v_token = cfg.get("volc_access_token", "")
            v_tos_ak = cfg.get("volc_tos_ak", "")
            v_tos_sk = cfg.get("volc_tos_sk", "")
            v_apikey = cfg.get("volc_api_key", "")
            if not all([v_tos_ak, v_tos_sk]) or not (all([v_app_id, v_token]) or v_apikey):
                _log("volcengine_asr: 未配置火山引擎参数，跳过")
                return
            from volcengine_asr import prepare_volcengine_audio, volcengine_asr
            v_bucket = cfg.get("volc_bucket", "livec")
            volc_audio = prepare_volcengine_audio(wav_path, temp_dir, prefix="volc_final_audio", ffmpeg=ffmpeg, log_fn=_log, timeout=120)
            if not volc_audio:
                _log("火山引擎 ASR 音频准备失败，将切换到本地语音识别")
                return
            segs = volcengine_asr(volc_audio, v_app_id, v_token, v_tos_ak, v_tos_sk,
                                  bucket=v_bucket, region=cfg.get("volc_region", "cn-beijing"), log_fn=_log, api_key=cfg.get("volc_api_key", "") or None)
            if segs:
                raw_segments = segs
                volcengine_success = True
                _log(f"火山引擎 ASR 成功: {len(raw_segments)} 条语音段")
            else:
                _log("火山引擎 ASR 失败，将切换到本地语音识别")
        except Exception as e:
            _log(f"火山引擎 ASR 异常: {e}")

    def _run_local_asr():
        """Reuse the selected local engine and fallback policy from smart cut."""
        nonlocal raw_segments
        try:
            from ai_clipper import load_settings as load_local_settings

            local_settings = load_local_settings()
        except Exception as exc:
            _log(f"读取本地 ASR 设置失败，使用 SenseVoice 默认值: {exc}")
            local_settings = {"local_asr_engine": "sensevoice", "whisper_model": "small"}
        raw_segments = _final_subtitle_local_asr_segments(
            wav_path,
            temp_dir,
            local_settings,
            _log,
        )

    def _build_fallback_segments(cloud_text, wav_path, _log):
        """Use cloud text across the final audio duration when local ASR is unavailable."""
        if not cloud_text or len(cloud_text) < 10:
            return None
        try:
            import wave

            with wave.open(wav_path, "rb") as wav_file:
                frame_rate = wav_file.getframerate()
                duration = wav_file.getnframes() / frame_rate if frame_rate else 0.0
            if duration <= 0:
                return None
            _log("本地ASR失败，按成片时长生成云端参考字幕...")
            return [{"start": 0.0, "end": duration, "text": cloud_text.strip()}]
        except Exception as exc:
            _log(f"备用字幕生成失败: {exc}")
            return None

    def _run_cloud_asr():
        """云端ASR: 准确全文"""
        nonlocal cloud_reference
        try:
            from asr_api import is_asr_enabled as _ce, cloud_asr as _ca
            if _ce():
                _log("正在用云端 ASR 获取准确文本(并行)...")
                import subprocess as _sp, tempfile as _tf
                mp3_p = wav_path.replace(".wav", "_ref.mp3")
                ffmpeg = get_ffmpeg_cmd()
                kw = dict(stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
                p = _sp.Popen([ffmpeg, "-y", "-i", video_path, "-vn", "-acodec",
                              "libmp3lame", "-ar", "16000", "-ac", "1", "-q:a", "4", mp3_p], creationflags=_NO_WINDOW, **kw)
                p.wait(timeout=60)
                if p.returncode == 0:
                    srt_text = _ca(mp3_p)
                    try: _os.remove(mp3_p)
                    except: pass
                    if srt_text:
                        # Extract just the text lines from SRT
                        import re
                        texts = []
                        for line in srt_text.split("\n"):
                            if not line.strip() or re.match(r"^\d+$", line.strip()) or "-->" in line:
                                continue
                            texts.append(line.strip())
                        cloud_reference = "".join(texts)
                        _log(f"云端ASR参考文本: {len(cloud_reference)}字")
                    else:
                        _log("云端ASR参考文本获取失败")
        except Exception as e:
            _log(f"云端ASR参考获取跳过: {e}")

    # 先尝试选定云端 ASR，失败则走与智能成片一致的本地 ASR
    import threading

    # 字幕阶段：跟随用户ASR选项（阿里云/火山引擎）
    _asr_preset_sub = ""
    _use_cloud_sub = False
    try:
        from ai_clipper import load_settings as _ld_sub
        _sub_cfg = _ld_sub()
        _use_cloud_sub = _sub_cfg.get("asr_enabled", False)
        _asr_preset_sub = _sub_cfg.get("asr_preset", "") or _sub_cfg.get("asr_provider", "")
    except Exception:
        _LOG.warning("failed to read asr preset", exc_info=True)
    if _use_cloud_sub:
        if _asr_preset_sub == "阿里云":
            _log("字幕阶段：云端ASR已启用，优先阿里云")
            t_ali = threading.Thread(target=_run_aliyun_asr_subtitle)
            t_ali.start()
            t_ali.join(timeout=180)
            if not volcengine_success:
                _log("阿里云ASR失败，切换到本地语音识别")
                t1 = threading.Thread(target=_run_local_asr)
                t1.start()
                t1.join(timeout=180)
        else:
            _log("字幕阶段：云端ASR已启用，优先火山引擎")
            t_volc = threading.Thread(target=_run_volcengine_asr)
            t_volc.start()
            t_volc.join(timeout=120)
            if not volcengine_success:
                _log("火山引擎ASR失败，切换到本地语音识别")
                t1 = threading.Thread(target=_run_local_asr)
                t1.start()
                t1.join(timeout=180)
    else:
        _log("字幕阶段：云端ASR未启用，使用本地语音识别（跟随设置）")
        t1 = threading.Thread(target=_run_local_asr)
        t1.start()
        t1.join(timeout=180)

    if not volcengine_success:
        if not raw_segments:
            if cloud_reference:
                _log("本地ASR失败，尝试用云端ASR文本生成字幕...")
                fallback = _build_fallback_segments(cloud_reference, wav_path, _log)
                if fallback:
                    raw_segments = fallback
                    _log(f"云端ASR备用字幕: {len(raw_segments)} 条")
                else:
                    _log("云端ASR备用也失败，跳过字幕")
                    import shutil as _shutil; _shutil.copy2(video_path, output_path)
                    return
            else:
                _log("本地语音识别失败且无云端参考，跳过字幕")
                if pip_path and pip_path != "auto" and os.path.exists(pip_path):
                    _log("尝试只叠加画中画（无字幕）...")
                    _add_pip_only(video_path, output_path, temp_dir, _log, pip_path, pip_size, pip_opacity, pip_pos)
                else:
                    import shutil as _shutil; _shutil.copy2(video_path, output_path)
                return

    _log("[PROGRESS] 0.75")

# --- 4c: DeepSeek修复错别字 + 繁简转换 + 长句切分 ---
    if False:  # 始终走DeepSeek修复
        _log("云端ASR也需要DeepSeek修复")
        # 仍然清理标点符号和语气词（不跳过）
        _punct_re = re.compile(r"[，。！？、；：“”‘’（）《》【】…—·,.!?;:\'\"()\[\]{}<>]")
        _filler_re = re.compile(r"^[啊呢嗯哦哈]+|[啊呢嗯哦哈]+$")
        for seg in raw_segments:
            seg["text"] = _punct_re.sub("", seg["text"])
            seg["text"] = _filler_re.sub("", seg["text"])
            seg["text"] = seg["text"].strip()
        fixed_segments = raw_segments
    else:
        _log("正在用DeepSeek修复字幕（错别字+繁简转换+断句）...")
        try:
            from ai_clipper import load_settings as _load_ai_settings
            from ai_model_config import ai_chat_completions_url, normalize_ai_base_url
            settings = _load_ai_settings()
            api_key = settings.get("api_key", "").strip()
            base_url = normalize_ai_base_url(settings.get("base_url"))
            model = (settings.get("model", "") or "").strip()
        except Exception as e:
            _log(f"读取 AI 设置失败: {e}")
            api_key = ""; base_url = ""; model = ""

        if not api_key:
            _log("未找到 AI API Key，跳过DeepSeek修复，直接使用 ASR 原始文本")
            fixed_segments = raw_segments
        elif not base_url or not model:
            _log("AI 设置不完整（缺少 Base URL 或模型名），跳过DeepSeek修复")
            fixed_segments = raw_segments
        else:
            _log(f"字幕修复模型: {model}")
            seg_text = "\n".join([f"[{s['start']:.2f}-{s['end']:.2f}] {s['text']}" for s in raw_segments])
            ref_note = ""
            if cloud_reference:
                ref_note = f"\n\n参考（另一个更准确的语音识别结果，用于纠错参考）：\n{cloud_reference}"

            fix_prompt = f"""你是抖音直播字幕修复专家。请修复以下 ASR 语音识别结果：
1. 修正错别字（女装术语：网纱、晴纶、锦纶、阔腿裤、罩衫、连衣裙、风衣、夹克等）
2. 繁体字转简体（褲→裤、襯→衬、風→风、夾→夹、羽絨→羽绒等）
3. 去除废话词(呕嗯然后对对对就是那个这个) + 句内重复词(已经。已经→已经) + 填充音(啊啊啊)
4. 保持时间戳不变，严格按行输出，每行格式：[start-end] 文本

原始字幕：
{seg_text}
{ref_note}

直接输出修复后的字幕，每行格式同上，不要加任何解释："""

            try:
                import urllib.request
                import ssl as _ssl
                ctx = create_ssl_context()
                req_body = _json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": fix_prompt}],
                    "temperature": 0.1,
                    "max_tokens": 4000
                }).encode("utf-8")

                req = urllib.request.Request(
                    ai_chat_completions_url(base_url),
                    data=req_body,
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                )
                with urllib.request.urlopen(req, timeout=45, context=ctx) as resp:
                    resp_data = _json.loads(resp.read().decode("utf-8"))

                fixed_text = resp_data["choices"][0]["message"]["content"].strip()
                # V4 Flash sometimes wraps in markdown code blocks
                if fixed_text.startswith("```"):
                    fixed_text = re.sub(r"^```[a-z]*\n?|\n?```$", "", fixed_text).strip()
                _log("DeepSeek修复完成")

                import re as _re
                fixed_segments = []
                for line in fixed_text.split("\n"):
                    line = line.strip()
                    if not line:
                        continue
                    m = _re.match(r'\[(\d+\.?\d*)-(\d+\.?\d*)\]\s*(.*)', line)
                    if m:
                        fixed_segments.append({
                            "start": float(m.group(1)),
                            "end": float(m.group(2)),
                            "text": m.group(3).strip()
                        })

                if len(fixed_segments) < len(raw_segments) // 2:
                    _log(f"DeepSeek返回解析异常（{len(fixed_segments)}条 vs 原始{len(raw_segments)}条），回退到 ASR 原始文本")
                    fixed_segments = raw_segments
                else:
                    _log(f"DeepSeek修复: {len(fixed_segments)} 条字幕")
            except Exception as e:
                _log(f"DeepSeek修复失败: {e}，回退到 ASR 原始文本")
                fixed_segments = raw_segments

    _log("[PROGRESS] 0.85")

    # --- 去重叠：相邻 segment 时间交叉时截断前一个的 end ---
    if len(fixed_segments) > 1:
        deduped = [fixed_segments[0].copy()]
        for seg in fixed_segments[1:]:
            prev = deduped[-1]
            if seg["start"] < prev["end"]:
                prev["end"] = seg["start"]  # 截断前一个
            if seg["end"] > seg["start"]:  # 有效段才保留
                deduped.append(seg.copy())
        if len(deduped) != len(fixed_segments):
            _log(f"字幕去重叠: {len(fixed_segments)} → {len(deduped)} 条")
        fixed_segments = deduped

        # [已移除] 字幕阶段不做文本去重：中文口语字符集重叠率高，Jaccard>60%会误杀有效字幕行
    # AI选片阶段已做语义去重，字幕阶段只需忠实显示ASR识别内容
    # 2026-04-21: 修复字幕内容错位问题（误删导致后续字幕时间戳不错但文本错位）

# --- 长句拆分：把超过 max_chars 的 segment 按标点拆成多段，时间按字符比分配 ---
    max_sub = 14
    min_sub = 4  # 最短片段不低于4字
    split_segments = []
    for seg in fixed_segments:
        text = seg["text"].strip()
        if not text:
            continue
        seg_dur = seg["end"] - seg["start"]
        if len(text) <= max_sub:
            split_segments.append(seg)
            continue
        # 按标点 + 语气词优先断句
        parts = []
        while len(text) > max_sub:
            cut = -1
            # 优先找标点（在 max_sub 范围内找最后一个标点）
            for p in ["，", "。", "！", "？", "、", "；", "：", "~", "—", ",", ".", "!", "?", ";", ":"]:
                pos = text.rfind(p, 1, max_sub + 1)
                if pos > 0 and (len(text) - pos >= min_sub or len(text) <= max_sub * 2):
                    cut = pos + 1
                    break
            # 如果找不到标点，找语气词
            if cut <= 0:
                for word in ["啊", "呢", "吧", "嘛", "哦", "呀", "哈", "哎", "嗯", "嘛", "啦", "哟", "哇", "噢"]:
                    pos = text.rfind(word, 1, max_sub + 1)
                    if pos > 0 and pos + len(word) <= max_sub + 1:
                        cut = pos + len(word)
                        break
            # 仍找不到则硬切，在词边界处切，避免截断词语
            if cut <= 0:
                cut = max_sub
                remaining = len(text) - cut
                if remaining > 0 and remaining < min_sub:
                    cut = len(text) - min_sub
                # 词边界检测：向前/向后扫描找助词/连词位置（在助词后断句）
                if 0 < cut < len(text):
                    best = cut
                    # 向前扫：找到「的了着过是在也都还很最把被让给和与但而」结尾位置
                    for offset in range(0, 6):
                        pos = cut - offset
                        if pos <= 1:
                            break
                        if text[pos-1] in '的了着过是在也都还很最把被让给和与但而':
                            if len(text) - pos >= min_sub:
                                best = pos
                                break
                    # 向后扫：如果向前没找到，向后找下一个助词位置
                    if best == cut and len(text) > cut:
                        for offset in range(1, 6):
                            pos = cut + offset
                            if pos >= len(text):
                                break
                            if text[pos-1] in '的了着过是在也都还很最把被让给和与但而':
                                if len(text) - pos >= min_sub:
                                    best = pos
                                    break
                    cut = best
                    # 检查是否切断了常见双字词（如"特点"→"特"+"点"）
                    if 0 < cut < len(text):
                        pair = text[cut-1:cut+1]
                        _common_pairs = {'特点','特色','特别','非常','相当','所以','因为','但是',
                            '然后','而且','或者','以及','已经','正在','可以','能够','应该',
                            '我们','这个','那个','整个','全部','完全','很多','很好','最好',
                            '最后','出来','起来','下来','一点','一下','一直','一切','衣服',
                            '面料','颜色','尺码','版型','款式','腰线','领口','袖子','下摆',
                            '细节','设计','汉麻','天丝','真丝','棉麻','雪纺','女装','新款',
                            '老款','补货','现货','差不多','不少','不行','不能','好的','行了'}
                        if pair in _common_pairs:
                            if cut - 1 >= 4 and len(text) - (cut - 1) >= min_sub:
                                cut = cut - 1
                            elif cut + 1 < len(text) and len(text) - (cut + 1) >= min_sub:
                                cut = cut + 1
                if cut <= 0 or cut >= len(text):
                    cut = min(max_sub, len(text))
            parts.append(text[:cut])
            text = text[cut:]
        if text.strip():
            parts.append(text)
        # 过滤掉太短的片段（合并到前一个）
        merged = []
        for part in parts:
            p = part.strip()
            if not p:
                continue
            if len(p) < min_sub and merged:
                merged[-1] = merged[-1] + p
            elif len(p) < min_sub and not merged:
                merged.append(p)  # 第一个片段保留
            else:
                merged.append(p)
        # 按字符数比例分配时间
        total_chars = sum(len(p) for p in merged)
        t = seg["start"]
        for part in merged:
            ratio = len(part) / total_chars if total_chars > 0 else 1 / len(merged)
            p_dur = seg_dur * ratio
            split_segments.append({
                "start": t,
                "end": t + p_dur,
                "text": part.strip()
            })
            t += p_dur
    if len(split_segments) != len(fixed_segments):
        _log(f"长句拆分: {len(fixed_segments)} → {len(split_segments)} 条")
    fixed_segments = split_segments

    # --- 字幕文本 ASR 修正（修正识别错误） ---
    try:
        from config import ASR_CORRECTIONS as _asr_corrections
        if _asr_corrections:
            _asr_fixed = 0
            for seg in fixed_segments:
                t = seg["text"]
                for wrong, right in _asr_corrections.items():
                    if wrong in t:
                        t = t.replace(wrong, right)
                if t != seg["text"]:
                    seg["text"] = t
                    _asr_fixed += 1
            if _asr_fixed:
                _log(f"字幕ASR修正: {len(fixed_segments)} 条中修正了 {_asr_fixed} 条")
    except ImportError:
        pass  # ASR_CORRECTIONS not defined in config

    # --- 去除字幕标点符号 ---
    for seg in fixed_segments:
        seg["text"] = _strip_output_subtitle_punctuation(seg["text"])
    _log(f"字幕标点已清除")

    # --- 4d+4e: drawtext 逐条烧录字幕 ---
    # 不用 subtitles/ass 滤镜（Windows 上 fontconfig 不可靠）
    # 直接用 drawtext + textfile + enable 逐条烧录，最可靠
    _log("正在用 drawtext 烧录字幕...")
    from platform_config import DRAWTEXT_FONT_PATH, FONT_BOLD_PATH, IS_MAC
    # Copy font to temp dir to avoid Chinese path issues with fontconfig
    _font_dest = os.path.join(temp_dir, "drawtext_font.ttc")
    if os.path.exists(FONT_BOLD_PATH) and not os.path.exists(_font_dest):
        import shutil as _shutil_font
        _shutil_font.copy2(FONT_BOLD_PATH, _font_dest)
    if os.path.exists(_font_dest):
        _drawtext_font = _font_dest.replace(os.sep, "/").replace(":", "\\:")
    else:
        _drawtext_font = DRAWTEXT_FONT_PATH  # fallback
    sc = SUBTITLE_OVERLAY
    font_size = sc.get("font_size", 52)
    try:
        from ai_clipper import load_settings as _load_subtitle_settings
        _subtitle_settings = _load_subtitle_settings()
        font_size = max(32, min(96, int(float(_subtitle_settings.get("subtitle_font_size", font_size)))))
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass
    _base_font_size = font_size
    # 根据视频宽度自适应缩放字号（基准1080px）
    if w and w > 0:
        font_size = max(28, int(font_size * w / 1080))
    _log(f"字幕字号: {_base_font_size}（输出适配后 {font_size}）")
    outline_w = sc.get("outline_width", 4)
    margin_v = sc.get("margin_v", 270) + 100  # 上移100

    try:
        drawtext_filters = []
        text_files = []
        for seg in fixed_segments:
            if not seg["text"]:
                continue
            lines = [seg["text"]]

            # 为每行创建一个 drawtext
            for li, line in enumerate(lines):
                txt_path = os.path.join(temp_dir, f"sub_{seg['start']:.2f}_{li}.txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(line)
                text_files.append(txt_path)

                # 文本文件路径转义（Windows 需要转义冒号和反斜杠）
                if IS_MAC:
                    tf = txt_path.replace("'", "'\\''")
                else:
                    tf = txt_path.replace("\\", "/").replace(":", "\\:")
                font = f"fontfile='{_drawtext_font}'"
                s_start = seg['start']
                s_end = seg['end']
                # 单行居中，y = h - margin_v
                line_offset = li * (font_size + 6)
                dt = (
                    f"drawtext={font}:textfile='{tf}'"
                    f":fontsize={font_size}:fontcolor=white"
                    f":shadowx=2:shadowy=2:shadowcolor=black@0.5"
                    f":x=(w-text_w)/2:y=h-{margin_v}-{line_offset}"
                    f":enable='between(t\\,{s_start:.3f}\\,{s_end:.3f})'"
                )
                drawtext_filters.append(dt)

        if not drawtext_filters:
            _log("无有效字幕文本，跳过烧录")
            import shutil as _shutil; _shutil.copy2(video_path, output_path)
        else:
            vf_chain = ",".join(drawtext_filters)
            _log(f"drawtext 滤镜数量: {len(drawtext_filters)}")
            _log(f"视频: {os.path.getsize(video_path)/(1024*1024):.1f}MB")

            # 画中画：在字幕步骤一起叠加，不增加编码次数
            _has_pip = pip_path is not None  # "auto" or actual file path
            # 位置映射
            _pos_map = {"左上":"10:10", "右上":"W-w-10:10", "左下":"10:H-h-10", "右下":"W-w-10:H-h-10"}
            _pip_pos = _pos_map.get(pip_pos, "W-w-10:H-h-10")
            if _has_pip:
                if pip_path and pip_path != "auto" and os.path.exists(pip_path):
                    _pip_fc = f"[1:v]scale=iw*{pip_size}:ih*{pip_size},format=rgba,colorchannelmixer=aa={pip_opacity}[pip];[0:v][pip]overlay={_pip_pos}[with_pip]"
                    _log(f"画中画: 叠加 {os.path.basename(pip_path)} (大小={pip_size:.0%}, 不透明度={pip_opacity:.0%}, 位置={pip_pos})")
                    _log(f"画中画filter: {_pip_fc}")
                    _norm_output = output_path.replace("/", os.sep)
                    # drawtext 在 [with_pip] 上，输出 [out_v]
                    _drawtext_fc = "[with_pip]" + vf_chain + ",copy[out_v]"
                    loop_n = _calc_pip_loop(video_path, pip_path, ffmpeg)
                    sub_cmd = [
                        ffmpeg, "-y", "-i", video_path, "-stream_loop", str(loop_n), "-i", pip_path,
                        "-filter_complex",
                        f"{_pip_fc};{_drawtext_fc}",
                        "-map", "[out_v]", "-map", "0:a",
                    ]
                    sub_cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
                    sub_cmd += _final_vcodec_args()
                    sub_cmd += _final_audio_sync_args()
                    sub_cmd += ["-movflags", "+faststart", _norm_output]
                else:
                    _log(f"画中画: 自动模式（自身缩小叠加，大小={pip_size:.0%}，不透明度={pip_opacity:.0%}）")
                    _pip_fc = f"[0:v]split[main][pip];[pip]scale=iw*{pip_size}:ih*{pip_size},format=rgba,colorchannelmixer=aa={pip_opacity}[overlay];[main][overlay]overlay={_pip_pos}[with_pip]"
                    _norm_output = output_path.replace("/", os.sep)
                    _drawtext_fc = "[with_pip]" + vf_chain
                    sub_cmd = [
                        ffmpeg, "-y", "-i", video_path,
                        "-filter_complex",
                        f"{_pip_fc};{_drawtext_fc}",
                        "-map", "0:a",
                    ]
                    sub_cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
                    sub_cmd += _final_vcodec_args()
                    sub_cmd += _final_audio_sync_args()
                    sub_cmd += ["-movflags", "+faststart", _norm_output]
            else:
                # 无画中画：保持原有 -vf 方式
                _norm_output = output_path.replace("/", os.sep)
                sub_cmd = [
                    ffmpeg, "-y", "-i", video_path,
                    "-vf", vf_chain,
                ]
                sub_cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
                sub_cmd += _final_vcodec_args()
                sub_cmd += _final_audio_sync_args()
                sub_cmd += ["-movflags", "+faststart", _norm_output]

            popen_kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
            # Windows 下禁用 fontconfig，避免 drawtext 初始化失败
            fc_env = None
            if sys.platform == "win32":
                fc_conf = os.path.join(temp_dir, "fonts.conf")
                with open(fc_conf, "w", encoding="utf-8") as f:
                    f.write('<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig><include ignore_missing="yes"/></fontconfig>\n')
                # FFmpeg 8.1+ 会尝试加载 DTD，写入最小有效内容避免解析失败
                fc_dtd = os.path.join(temp_dir, "fonts.dtd")
                if not os.path.exists(fc_dtd):
                    with open(fc_dtd, "w", encoding="utf-8") as f:
                        f.write('<!ELEMENT fontconfig (dir|cache|match)*>\n')
                        f.write('<!ELEMENT dir (#PCDATA)>\n')
                        f.write('<!ELEMENT cache (#PCDATA)>\n')
                        f.write('<!ELEMENT match (test|edit)*>\n')
                        f.write('<!ELEMENT test (#PCDATA)>\n')
                        f.write('<!ELEMENT edit (#PCDATA)>\n')
                popen_kw["env"] = dict(os.environ)
                popen_kw["env"]["FONTCONFIG_FILE"] = fc_conf
            ok, rc, stderr_data = _run_ffmpeg_with_hw_fallback(
                sub_cmd, popen_kw, 450, _log, "字幕烧录", output_path,
                software_args=_final_software_vcodec_args()
            )
            if not ok:
                _log("字幕烧录失败，输出无字幕版本")
                _log(f"FFmpeg exit code: {rc}")
                if stderr_data:
                    for line in stderr_data.strip().split("\n")[-10:]:
                        if line.strip(): _log(f"  ffmpeg: {line.strip()}")
                import shutil as _shutil; _shutil.copy2(video_path, output_path)
            else:
                _log("字幕烧录成功！")
    except subprocess.TimeoutExpired:
        _log("字幕烧录超时，输出无字幕版本")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)
    except Exception as e:
        _log(f"字幕烧录异常: {e}，输出无字幕版本")
        import shutil as _shutil; _shutil.copy2(video_path, output_path)

    _log("字幕处理完成")


def process_video_multi(video_path, srt_path=None, output_path=None,
                        dedup_preset="medium", subtitle_overlay=True,
                        log_fn=None, force_category=None, cancel_event=None,
                        pip_path=None, pip_size=0.15, pip_opacity=0.03, pip_pos="右下",
                        num_versions=1, focus_hint="自动", smart_crop_enabled=True, crop_level="medium", ken_burns_enabled=True,
                        target_duration=60, mirror_enabled=None, kb_intensity="中", ai_controls=None,
                        dedup_video_options=None, dedup_audio_options=None, transition_options=None,
                        duration_tolerance=None):
    """多版本输出：AI直接输出3个独立叙事方案，每个方案完整裁切
    
    策略(v2)：AI选片时直接出3个不同角度的方案，代码层只做裁切。
    比旧方案（一次选片+代码拆分）叙事更完整、版本差异化更好。
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if num_versions <= 1:
        return process_video(video_path, srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                               pip_path, pip_size, pip_opacity, pip_pos,
                                smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled, target_duration=target_duration, mirror_enabled=mirror_enabled, kb_intensity=kb_intensity, ai_controls=ai_controls, dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options, transition_options=transition_options, duration_tolerance=duration_tolerance)
    
    _log(f"🎬 多版本模式(v2): AI直接出{num_versions}个独立叙事方案")
    
    # Step 1: 检查AI模式
    from ai_clipper import is_enabled as ai_is_enabled
    if not ai_is_enabled():
        _log("多版本需要AI模式，降级为单版本")
        return process_video(video_path, srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                               pip_path, pip_size, pip_opacity, pip_pos,
                                smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled, target_duration=target_duration, mirror_enabled=mirror_enabled, kb_intensity=kb_intensity, ai_controls=ai_controls, dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options, transition_options=transition_options, duration_tolerance=duration_tolerance)
    
    # Step 2: 只跑ASR，不跑AI选片（AI留给多版本一次调用）
    global _multi_result_cache
    _multi_result_cache = {}
    
    _log("🎬 多版本: 运行ASR（跳过单版本AI选片）...")
    asr_result = process_video(video_path, srt_path, output_path,
                 dedup_preset, subtitle_overlay, log_fn,
                 force_category, cancel_event,
                  pip_path, pip_size, pip_opacity, pip_pos,
                  _asr_only=True,
                  smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled, target_duration=target_duration, mirror_enabled=mirror_enabled, kb_intensity=kb_intensity, ai_controls=ai_controls, dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options, transition_options=transition_options, duration_tolerance=duration_tolerance)
    
    _recorded_srt_text = _multi_result_cache.get('srt_text', '')
    
    # 保存SRT到固定文件，供后续版本复用
    _multi_srt_path = srt_path
    if not _recorded_srt_text:
        _log("ASR失败（无SRT文本），降级为单版本")
        return process_video(video_path, srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                               pip_path, pip_size, pip_opacity, pip_pos,
                                smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled, target_duration=target_duration, mirror_enabled=mirror_enabled, kb_intensity=kb_intensity, ai_controls=ai_controls, dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options, transition_options=transition_options, duration_tolerance=duration_tolerance)
    if not _multi_srt_path:
        _multi_srt_path = os.path.join(
            os.path.dirname(video_path),
            f"_multi_version_{os.path.splitext(os.path.basename(video_path))[0]}.srt"
        )
        with open(_multi_srt_path, "w", encoding="utf-8") as f:
            f.write(_recorded_srt_text)
        _log(f"🎬 多版本: SRT已保存")
    
    # Step 3: 用AI多版本选片（直接出3个独立方案）
    if _recorded_srt_text:
        from ai_clipper import ai_analyze_multi_versions
        _log("🎬 多版本: AI重新选片（3个独立方案）...")
        multi_result = ai_analyze_multi_versions(_recorded_srt_text, log_fn=_log, force_category=force_category, focus_hint=focus_hint, num_versions=num_versions, ai_controls=ai_controls, target_duration=target_duration, duration_tolerance=duration_tolerance)
    else:
        multi_result = {"versions": []}
    versions_data = multi_result.get("versions", [])
    
    if not versions_data:
        _log("AI多版本选片失败，降级为旧方案（代码拆分）")
        # Fallback: 输出单版本
        _log("🎬 多版本: 选片失败，降级为单版本输出")
        return process_video(video_path, _multi_srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                               pip_path, pip_size, pip_opacity, pip_pos,
                                smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled, target_duration=target_duration, mirror_enabled=mirror_enabled, kb_intensity=kb_intensity, ai_controls=ai_controls, dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options, transition_options=transition_options, duration_tolerance=duration_tolerance)
    
    if len(versions_data) < 1:
        _log("无有效版本，输出单版本")
        return process_video(video_path, _multi_srt_path, output_path,
                           dedup_preset, subtitle_overlay, log_fn,
                           force_category, cancel_event,
                               pip_path, pip_size, pip_opacity, pip_pos,
                                smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled, target_duration=target_duration, mirror_enabled=mirror_enabled, kb_intensity=kb_intensity, ai_controls=ai_controls, dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options, transition_options=transition_options, duration_tolerance=duration_tolerance)
    
    _log(f"🎬 多版本: AI输出 {len(versions_data)} 个方案")
    
    # Step 4: 每个版本单独裁切
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    if output_path:
        output_dir = os.path.dirname(output_path)
    else:
        output_dir = os.path.join(os.path.dirname(video_path), "output")
    os.makedirs(output_dir, exist_ok=True)
    
    results = []
    batch_stamp = time.strftime('%Y%m%d_%H%M%S')
    for vi, ver in enumerate(versions_data):
        if cancel_event and cancel_event.is_set():
            break
        
        angle = ver.get("angle", f"方案{vi+1}")
        ver_clips = ver.get("clips", [])
        
        _log(f"\n🎬 === 版本 {vi+1}/{len(versions_data)} [{angle}] ===")
        
        v_output = os.path.join(output_dir, f"{video_name}_切片_{batch_stamp}_v{vi+1}.mp4")
        
        result = _process_version_with_clips(
            video_path, _multi_srt_path, v_output,
            ver_clips, dedup_preset, subtitle_overlay,
            log_fn, cancel_event,
            pip_path, pip_size, pip_opacity, pip_pos,
            smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled,
            mirror_enabled=mirror_enabled, kb_intensity=kb_intensity,
            target_duration=target_duration, duration_tolerance=duration_tolerance,
            dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options,
            transition_options=transition_options
        )
        results.append(result)
    
    _log(f"\n✅ 多版本输出完成: {len(results)} 个版本")
    
    # 清理临时 SRT 文件
    if _multi_srt_path and _multi_srt_path != srt_path:
        try:
            if os.path.exists(_multi_srt_path):
                os.remove(_multi_srt_path)
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
    
    return {"ok": any(r.get("ok", False) if isinstance(r, dict) else r for r in results), "版本数": len(results)}


def _process_version_with_clips(video_path, srt_path, output_path,
                                 clips, dedup_preset="medium",
                                 subtitle_overlay=True, log_fn=None,
                                 cancel_event=None, pip_path=None,
                                 pip_size=0.15, pip_opacity=0.03, pip_pos="右下",
                                 smart_crop_enabled=True, crop_level="medium", ken_burns_enabled=True,
                                  mirror_enabled=None, kb_intensity="中", target_duration=60,
                                  dedup_video_options=None, dedup_audio_options=None, transition_options=None,
                                  duration_tolerance=None):
    """Process a single version with pre-determined clips (bypass AI selection)"""
    import time as _time
    from ai_clipper import is_enabled as ai_is_enabled
    
    def _log(msg):
        if log_fn: log_fn(msg)
    
    def _cancelled():
        return cancel_event and cancel_event.is_set()
    
    if _cancelled():
        return {"ok": False, "error": "cancelled"}
    
    # This is a simplified version of process_video that skips AI selection
    # and uses the provided clips directly
    # We need to call the internal cutting/dedup/subtitle logic
    
    # For now, we use a workaround: temporarily patch ai_analyze_clips to return our clips
    import ai_clipper as _ai
    _original_fn = _ai.ai_analyze_clips
    _original_is_enabled = _ai.is_enabled
    _prepared_clips = list(clips or [])
    try:
        _log("预览成片: 保留用户调整后的片段顺序，不再自动重排")
    except Exception as _prep_e:
        _log(f"预览片段整理异常，使用原片段: {_prep_e}")
    try:
        _log(f"最终片段明细: {len(_prepared_clips)} 段")
        for _idx, _clip in enumerate(_prepared_clips, 1):
            _ct = str(_clip[0] if len(_clip) > 0 else "")
            _text = str(_clip[1] if len(_clip) > 1 else "").strip()
            _s = float(_clip[2] if len(_clip) > 2 else 0)
            _e = float(_clip[3] if len(_clip) > 3 else _s)
            _d = max(0.0, _e - _s)
            _log(f"最终片段 [{_idx}/{len(_prepared_clips)}] {_ct} {_s:.1f}-{_e:.1f}s ({_d:.1f}s) | {_text[:90]}")
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass
    
    def _mock_analyze(*args, **kwargs):
        return _prepared_clips
    
    _ai.ai_analyze_clips = _mock_analyze
    _ai.is_enabled = lambda: True
    
    try:
        result = process_video(video_path, srt_path, output_path,
                              dedup_preset, subtitle_overlay, log_fn,
                              None, cancel_event,  # force_category=None (already filtered)
                               pip_path, pip_size, pip_opacity, pip_pos,
                                smart_crop_enabled=smart_crop_enabled, crop_level=crop_level, ken_burns_enabled=ken_burns_enabled,
                                mirror_enabled=mirror_enabled, kb_intensity=kb_intensity, target_duration=target_duration,
                                dedup_video_options=dedup_video_options, dedup_audio_options=dedup_audio_options,
                                transition_options=transition_options, _user_confirmed_clips=True,
                                duration_tolerance=duration_tolerance)
        return result
    finally:
        _ai.ai_analyze_clips = _original_fn
        _ai.is_enabled = _original_is_enabled

def process_video_mix(video_path, output_path=None, dedup_preset="medium",
                       subtitle_overlay=True, log_fn=None, cancel_event=None,
                       pip_path="auto", pip_size=0.15, pip_opacity=0.03, pip_pos="\u53f3\u4e0a",
                       smart_crop_enabled=True, crop_level="medium", ken_burns_enabled=True,
                       target_duration=60, focus_hint="\u81ea\u52a8", num_versions=1,
                       srt_path=None, force_category=None, duration_tolerance=None, **extra_kwargs):

    def _log(msg):
        if log_fn: log_fn(msg)

    def _cancelled():
        return cancel_event and cancel_event.is_set()

    _mix_run_started = time.time()
    dedup_preset = _normalize_dedup_preset(dedup_preset)
    dedup_video_options = extra_kwargs.get("dedup_video_options") or {}
    dedup_audio_options = extra_kwargs.get("dedup_audio_options") or {}
    _transition_mode, _transition_duration = _clip_transition_config(extra_kwargs.get("transition_options"))
    _dedup_is_custom = dedup_preset == "custom"
    _mirror_enabled = _dedup_mirror_enabled(extra_kwargs.get("mirror_enabled")) and dedup_preset != "none"
    ai_controls = extra_kwargs.get("ai_controls")
    kb_intensity = extra_kwargs.get("kb_intensity", "中")
    _clips_only = bool(extra_kwargs.get("_clips_only"))
    _user_confirmed_clips = bool(extra_kwargs.get("_user_confirmed_clips"))
    global TARGET_DURATION, TARGET_DURATION_TOLERANCE, _hw_fallback, _hw_encoder_checked, _hw_encoder
    _hw_fallback = False
    _hw_encoder_checked = False
    _hw_encoder = None

    _log("=== \u6df7\u526a\u6a21\u5f0f ===")
    if not isinstance(video_path, (list, tuple)) or not video_path:
        _log("\u8bf7\u6dfb\u52a0\u89c6\u9891\u6587\u4ef6"); return False

    original_video_list = list(video_path)
    video_list = list(original_video_list)
    _log(f"\u5171 {len(video_list)} \u4e2a\u89c6\u9891")
    _log(f"镜像翻转: {'开' if _mirror_enabled else '关'}" + (f" (单片段概率 {OUTPUT_CLIP_MIRROR_PROBABILITY:.0%})" if _mirror_enabled else ""))
    if _transition_mode == "fade":
        _log(f"片段转场: 画面淡入淡出 {_transition_duration:.2f}s，音频保持短防爆音")

    if any(_is_ts_like_video(_vp) for _vp in video_list):
        _log("TS normalize: TS inputs will be transcoded before ASR/AI/cutting.")

    # Get ffmpeg
    try:
        from platform_config import FFMPEG_CMD as ffmpeg
    except:
        ffmpeg = "ffmpeg"
    _encoder = _get_video_encoder()
    if _encoder:
        _log(f"编码器: 实验硬件加速 ({_encoder})")
        try:
            import platform_config as _pc_diag
            for _diag in getattr(_pc_diag, "HARDWARE_ENCODER_DIAGNOSTICS", [])[-4:]:
                _log(f"硬件诊断: {_diag}")
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
    elif _hardware_encoder_requested():
        _log(f"编码器: 硬件加速自检未通过，使用稳定软件编码 ({_software_encoder_name()})")
        try:
            import platform_config as _pc_diag
            for _diag in getattr(_pc_diag, "HARDWARE_ENCODER_DIAGNOSTICS", [])[-8:]:
                _log(f"硬件诊断: {_diag}")
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass
    else:
        _log(f"编码器: 软件编码 ({_software_encoder_name()})，硬件加速设置已关闭，整片处理会更慢")

    # Temp dir
    tmp = os.path.join("C:\\", "lc_temp_mix_" + os.urandom(4).hex())
    os.makedirs(tmp, exist_ok=True)

    out_dir = os.path.dirname(output_path) if output_path else os.path.join(os.path.dirname(original_video_list[0]), "mix_output")
    os.makedirs(out_dir, exist_ok=True)
    final = output_path or os.path.join(out_dir, f"mix_output_{time.strftime('%Y%m%d_%H%M%S')}.mp4")

    normalized_video_list = []
    for _src in video_list:
        normalized_video_list.append(_remux_ts_for_editing(_src, None, ffmpeg, _log))
    if any(_new != _old for _old, _new in zip(video_list, normalized_video_list)):
        _log("TS normalize: mix sources now use normalized MP4 files for ASR, AI, and cutting.")
    video_list = normalized_video_list

    # ============================================================
    # Phase 1: 合并SRT + AI 一步选片
    # ============================================================
    from ai_clipper import ai_analyze_clips, is_enabled as ai_is_enabled

    if not ai_is_enabled():
        _log("AI 未启用"); shutil.rmtree(tmp, ignore_errors=True); return False

    # 合并 SRT
    _log("合并语音文本...")
    merged_srt = ""
    _mix_srt_entries = []
    _mix_word_timings = []
    for _vi, vp in enumerate(video_list):
        if _cancelled(): return False
        original_vp = original_video_list[_vi] if _vi < len(original_video_list) else vp
        _sc = os.path.splitext(original_vp)[0] + ".srt"
        if not os.path.exists(_sc):
            # Fallback scan
            _srt_dir = os.path.dirname(original_vp)
            _base = os.path.splitext(os.path.basename(original_vp))[0]
            if os.path.isdir(_srt_dir):
                for _f in os.listdir(_srt_dir):
                    if _f.endswith(".srt") and _base in _f:
                        _sc = os.path.join(_srt_dir, _f)
                        _log(f"  找到匹配 SRT: {_f}")
                        break
        if not os.path.exists(_sc):
            # Cloud ASR + Whisper fallback
            try:
                from ai_clipper import load_settings as _ld_mix
                _cfg4 = _ld_mix()
                _asr_enabled = _cfg4.get("asr_enabled", False)
            except:
                _asr_enabled = False
            _asr_ok = False
            if _asr_enabled:
                try:
                    from volcengine_asr import (
                        build_semantic_segments,
                        prepare_volcengine_audio,
                        semantic_segments_to_srt,
                        volcengine_asr,
                        write_word_timing_sidecar,
                    )
                    import hashlib, tempfile, json
                    _td = os.path.join(tempfile.gettempdir(), "live_cutter_stt")
                    os.makedirs(_td, exist_ok=True)
                    _vh = hashlib.md5(vp.encode("utf-8")).hexdigest()[:8]
                    _audio = prepare_volcengine_audio(vp, _td, prefix=f"audio_{_vh}", ffmpeg=ffmpeg, log_fn=_log, timeout=120)
                    if not _audio:
                        raise RuntimeError("火山音频准备失败")
                    srts = volcengine_asr(_audio,
                                         _cfg4.get("volc_app_id", ""),
                                         _cfg4.get("volc_access_token", ""),
                                         _cfg4.get("volc_tos_ak", ""),
                                         _cfg4.get("volc_tos_sk", ""),
                                         bucket=_cfg4.get("volc_bucket", "livec"),
                                         region=_cfg4.get("volc_region", "cn-beijing"),
                                         log_fn=_log,
                                         api_key=_cfg4.get("volc_api_key", "") or None)
                    if srts:
                        _semantic_srts = build_semantic_segments(srts, log_fn=_log) or srts
                        with open(_sc, "w", encoding="utf-8") as _fw:
                            _fw.write(semantic_segments_to_srt(_semantic_srts))
                        write_word_timing_sidecar(_sc, srts, log_fn=_log)
                        _log(f"  火山引擎 ASR 成功: {len(srts)} 条")
                        _asr_ok = True
                except Exception as _ve:
                    _log(f"  火山引擎 ASR 失败: {_ve}")
                if not _asr_ok:
                    try:
                        from aliyun_asr import aliyun_asr
                        import json, hashlib, tempfile
                        _ali_api_key = _cfg4.get("aliyun_api_key", "")
                        _ali_oss_ak = _cfg4.get("aliyun_oss_ak", "")
                        _ali_oss_sk = _cfg4.get("aliyun_oss_sk", "")
                        _ali_bucket = _cfg4.get("aliyun_bucket", "")
                        if _ali_api_key and _ali_oss_ak and _ali_oss_sk and _ali_bucket:
                            _td2 = os.path.join(tempfile.gettempdir(), "live_cutter_stt")
                            os.makedirs(_td2, exist_ok=True)
                            _vh2 = hashlib.md5(vp.encode("utf-8")).hexdigest()[:8]
                            _wav2 = os.path.join(_td2, f"audio_{_vh2}.wav")
                            if not os.path.exists(_wav2):
                                subprocess.run([ffmpeg, "-y", "-i", vp, "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1", _wav2],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, creationflags=_NO_WINDOW)
                            srts = aliyun_asr(_wav2, _cfg4, log_fn=_log)
                            if srts:
                                with open(_sc, "w", encoding="utf-8") as _fw:
                                    for _i_seg, _seg in enumerate(srts):
                                        _st_seg = _seg["start"] if isinstance(_seg, dict) else _seg[1]
                                        _et_seg = _seg["end"] if isinstance(_seg, dict) else _seg[2]
                                        _tx_seg = _seg["text"] if isinstance(_seg, dict) else _seg[0]
                                        _fw.write(f"{_i_seg+1}\n{int(_st_seg//3600):02d}:{int((_st_seg%3600)//60):02d}:{int(_st_seg%60):02d},{int((_st_seg%1)*1000):03d} --> {int(_et_seg//3600):02d}:{int((_et_seg%3600)//60):02d}:{int(_et_seg%60):02d},{int((_et_seg%1)*1000):03d}\n{_tx_seg}\n\n")
                                _log(f"  阿里云 ASR 成功: {len(srts)} 条")
                                _asr_ok = True
                    except Exception as _ae:
                        _log(f"  阿里云 ASR 失败: {_ae}")
            if not _asr_ok:
                try:
                    from stt import generate_srt
                    _temp = generate_srt(vp, log_fn=_log)
                    if _temp and os.path.exists(_temp):
                        shutil.copy2(_temp, _sc)
                        _log(f"  Whisper ASR 成功")
                except Exception as e:
                    _log(f"  Whisper ASR 失败: {e}")
        if os.path.exists(_sc):
            with open(_sc, "r", encoding="utf-8", errors="replace") as f:
                _srt_text = f.read()
            _source_srt_entries = []
            try:
                _subs, _ = open_srt(_sc)
                for _sub in _subs:
                    _source_entry = {
                        "source_idx": _vi,
                        "source": vp,
                        "start": float(_time_to_seconds(_sub.start)),
                        "end": float(_time_to_seconds(_sub.end)),
                        "text": _sub.text,
                    }
                    _source_srt_entries.append(_source_entry)
                    _mix_srt_entries.append(_source_entry)
            except Exception:
                for _segment in _parse_srt_to_segments(_srt_text):
                    _source_entry = {
                        "source_idx": _vi,
                        "source": vp,
                        "start": float(_segment.get("start") or 0),
                        "end": float(_segment.get("end") or 0),
                        "text": str(_segment.get("text") or ""),
                    }
                    _source_srt_entries.append(_source_entry)
                    _mix_srt_entries.append(_source_entry)
            # Add [Vn] marker to text lines
            _marker = f"V{_vi+1}"
            _source_word_segments = []
            try:
                from volcengine_asr import load_word_timing_sidecar
                _source_word_segments = list(
                    load_word_timing_sidecar(_sc, semantic=True, log_fn=_log) or []
                )
            except Exception:
                _source_word_segments = []
            _source_semantic_segments = _mix_semantic_segments_for_source(
                _source_srt_entries,
                _source_word_segments,
                _marker,
                vp,
            )
            _mix_word_timings.extend(_source_semantic_segments)
            if _source_word_segments:
                _log(f"混剪候选源 {_marker}: 词级语义段 {len(_source_semantic_segments)} 条")
            else:
                _log(
                    f"混剪候选源 {_marker}: 无词级时间，使用 SRT 时间段 "
                    f"{len(_source_semantic_segments)} 条，素材仍参与AI选片"
                )
            _out_lines = []
            for _line in _srt_text.split("\n"):
                import re as _re_line
                if _re_line.match(r'^\d+$', _line.strip()):  # index
                    _out_lines.append(_line)
                elif _re_line.match(r'\d+:\d+:\d+', _line):  # timecode
                    _out_lines.append(_line)
                elif _line.strip() == "":
                    _out_lines.append(_line)
                else:
                    _out_lines.append(f"[{_marker}] {_line}")
            merged_srt += "\n".join(_out_lines) + "\n\n"

    if not merged_srt.strip():
        _log("所有视频均无语音文本"); shutil.rmtree(tmp, ignore_errors=True); return False

    _log(f"合并 SRT 完成，调用 AI 选片...")

    # 传递时长设置到 AI 模块
    import ai_clipper as _ai_mod
    preference_summary = {}
    analysis_metadata = {}
    _duration_contract = _selection_duration_contract(
        target_duration,
        dedup_preset,
        dedup_video_options,
        duration_tolerance,
    )
    _ai_target_duration = _duration_contract.ai_target_seconds
    _planned_speed_factor = _duration_contract.speed_factor
    if abs(_planned_speed_factor - 1.0) > 0.01:
        _log(
            f"AI选片时长折算: 成片目标{target_duration}s × 预计变速{_planned_speed_factor:.2f}x "
            f"→ AI按原片约{_ai_target_duration}s选片"
        )
    _ai_mod._AI_TARGET_DURATION = _ai_target_duration
    _ai_mod._AI_CLIP_COUNT = _ai_mod.target_clip_count_text(_ai_target_duration)

    if num_versions and num_versions > 1 and not _clips_only:
        _log(f"混剪多版本: 当前长素材多版本仍在优化，暂按单版本输出（请求 {num_versions} 版）")
        num_versions = 1

    if num_versions and num_versions > 1 and not _clips_only:
        _log(f"混剪多版本: 生成 {num_versions} 个独立方案...")
        try:
            from ai_clipper import ai_analyze_multi_versions
            multi_result = ai_analyze_multi_versions(
                merged_srt,
                log_fn=_log,
                force_category=force_category,
                focus_hint=focus_hint,
                num_versions=num_versions,
                ai_controls=ai_controls,
                target_duration=_ai_target_duration,
                duration_tolerance=duration_tolerance,
            )
            versions_data = list((multi_result or {}).get("versions") or [])
        except Exception as e:
            _log(f"混剪多版本选片失败: {e}")
            versions_data = []

        if versions_data:
            base, ext = os.path.splitext(final)
            ext = ext or ".mp4"
            results = []
            original_ai_analyze = _ai_mod.ai_analyze_clips
            original_ai_enabled = _ai_mod.is_enabled
            try:
                for vi, version in enumerate(versions_data):
                    if _cancelled():
                        break
                    version_clips = list(version.get("clips") or [])
                    if len(version_clips) < 3:
                        _log(f"混剪多版本: 版本{vi+1}片段不足，跳过")
                        continue
                    angle = str(version.get("angle") or f"v{vi+1}")
                    v_output = f"{base}_v{vi+1}{ext}"
                    _log(f"混剪多版本: 开始输出版本{vi+1}/{len(versions_data)} [{angle}]，{len(version_clips)}个片段")

                    def _version_ai_analyze(*_args, _clips=version_clips, **_kwargs):
                        _log(f"混剪多版本: 版本{vi+1}使用预生成方案，不重新选片")
                        return list(_clips)

                    _ai_mod.ai_analyze_clips = _version_ai_analyze
                    _ai_mod.is_enabled = lambda: True
                    ok = process_video_mix(
                        video_list,
                        output_path=v_output,
                        dedup_preset=dedup_preset,
                        subtitle_overlay=subtitle_overlay,
                        log_fn=log_fn,
                        cancel_event=cancel_event,
                        pip_path=pip_path,
                        pip_size=pip_size,
                        pip_opacity=pip_opacity,
                        pip_pos=pip_pos,
                        smart_crop_enabled=smart_crop_enabled,
                        crop_level=crop_level,
                        ken_burns_enabled=ken_burns_enabled,
                        target_duration=target_duration,
                        duration_tolerance=duration_tolerance,
                        focus_hint=focus_hint,
                        num_versions=1,
                        srt_path=srt_path,
                        force_category=force_category,
                        **extra_kwargs,
                    )
                    if ok:
                        results.append(v_output)
                        _log(f"混剪多版本: 版本{vi+1}完成 {v_output}")
                    else:
                        _log(f"混剪多版本: 版本{vi+1}失败")
            finally:
                _ai_mod.ai_analyze_clips = original_ai_analyze
                _ai_mod.is_enabled = original_ai_enabled
                shutil.rmtree(tmp, ignore_errors=True)
            if results:
                _log(f"混剪多版本完成: {len(results)}/{len(versions_data)} 个版本")
                return True
            _log("混剪多版本无成功输出，回退到单版本流程")

    try:
        ordered_clips = ai_analyze_clips(merged_srt, log_fn=_log,
                                          force_category=force_category,
                                          focus_hint=focus_hint,
                                          merge_mode=True,
                                          target_duration=_ai_target_duration,
                                          final_target_duration=target_duration,
                                          duration_contract=_duration_contract,
                                          ai_controls=ai_controls,
                                          word_timings=_mix_word_timings)
        try:
            analysis_metadata = dict(_ai_mod.get_last_analysis_metadata() or {})
            preference_summary = dict(analysis_metadata.get("preference_summary") or {})
        except Exception:
            analysis_metadata = {}
            preference_summary = {}
    except Exception as e:
        _log(f"AI 选片失败: {e}"); import traceback; _log(traceback.format_exc())
        shutil.rmtree(tmp, ignore_errors=True); return False

    if not ordered_clips:
        failure_message = _ai_mod.selection_failure_message(analysis_metadata)
        _log(f"AI 未选到任何片段: {failure_message}")
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(failure_message)

    _duration_shortage_grace = _selection_shortage_grace_seconds(analysis_metadata)
    _log(f"AI 选到 {len(ordered_clips)} 个片段")

    # Map clips back to source videos
    def _strip_mix_marker(txt):
        return re.sub(r"\[V\d+\]\s*", "", str(txt or ""))

    def _norm_mix_text(txt):
        txt = _strip_mix_marker(txt).lower()
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", txt)

    def _mix_source_balance_candidate_quality(txt, duration):
        raw = str(txt or "").strip()
        norm = _norm_mix_text(raw)
        if not norm:
            return False, "空文本", 0.0
        try:
            duration = float(duration or 0)
        except Exception:
            duration = 0.0
        if duration < 3.0:
            return False, "过短", 0.0
        if duration > 16.0:
            return False, "过长", 0.0

        negative_groups = [
            ("预售/工期", ("预售", "二十天", "20天", "十五天", "15天", "十天", "七天", "几天", "工期", "排单", "生产", "备货", "补货", "发货", "到货")),
            ("库存/订单", ("库存", "卖完", "卖光", "抢完", "下单", "订单", "两百件", "200件", "200单", "单子", "销量", "开播", "一分钟")),
            ("负面口播", ("别骂", "骂我", "不要骂", "怪我", "喷我", "抱歉", "不好意思", "没办法")),
            ("交易/客服", ("客服", "售后", "链接", "小黄车", "购物车", "后台", "发券", "福利", "运费险")),
        ]
        for reason, words in negative_groups:
            if any(word.lower() in norm for word in words):
                return False, reason, 0.0

        positive_groups = [
            ("版型", ("显瘦", "遮肉", "藏肉", "收腰", "显高", "比例", "版型", "剪裁", "肩线", "肩膀", "骨架", "胯宽", "盖臀", "宽松", "修身")),
            ("面料", ("面料", "材质", "手感", "柔软", "透气", "亲肤", "凉快", "不闷", "不粘", "垂感", "弹力", "纱线", "不起球")),
            ("场景", ("通勤", "上班", "出门", "旅游", "度假", "海边", "拍照", "逛街", "日常", "搭配", "好搭")),
            ("颜色", ("颜色", "显白", "白色", "蓝色", "黑色", "高级", "干净", "清爽", "复古")),
            ("品质", ("做工", "工艺", "细节", "质感", "高级感", "精致", "走线", "大牌")),
        ]
        matched_groups = []
        hit_count = 0
        for label, words in positive_groups:
            hits = sum(1 for word in words if word.lower() in norm)
            if hits:
                matched_groups.append(label)
                hit_count += hits
        if not matched_groups:
            return False, "缺少产品卖点", 0.0

        compact_len = len(norm)
        score = hit_count * 12.0 + len(set(matched_groups)) * 18.0
        if 5.0 <= duration <= 12.5:
            score += 18.0
        elif duration < 5.0:
            score += 6.0
        else:
            score += 10.0
        score += min(18.0, compact_len / 8.0)
        return True, "+".join(matched_groups[:3]), score

    def _infer_source_idx_from_srt(txt, start, end):
        return _infer_mix_source_idx_from_srt(txt, start, end, _mix_srt_entries)

    def _source_path_key(value):
        raw = str(value or "").strip().strip('"')
        if not raw:
            return ""
        try:
            return os.path.normcase(os.path.abspath(raw))
        except Exception:
            return os.path.normcase(raw)

    _source_lookup = {}
    for _vi_lookup, _vp_lookup in enumerate(video_list):
        _source_lookup[_source_path_key(_vp_lookup)] = _vi_lookup
        if _vi_lookup < len(original_video_list):
            _source_lookup[_source_path_key(original_video_list[_vi_lookup])] = _vi_lookup

    def _explicit_source_idx(clip):
        source = ""
        if isinstance(clip, dict):
            source = clip.get("source") or clip.get("video") or ""
        elif isinstance(clip, (list, tuple)) and len(clip) > 7:
            raw_source = clip[7]
            if not isinstance(raw_source, dict):
                source = raw_source
        key = _source_path_key(source)
        return _source_lookup.get(key, -1) if key else -1

    def _mix_meta_type(clip):
        return str((clip or {}).get("type") or "").lower()

    def _mix_meta_duration(clip):
        try:
            return float((clip or {}).get("duration") or 0)
        except Exception:
            try:
                return max(0.0, float(clip.get("end", 0)) - float(clip.get("start", 0)))
            except Exception:
                return 0.0

    def _mix_meta_total(clips):
        return sum(max(0.0, _mix_meta_duration(c)) for c in clips or [])

    def _mix_meta_is_hook(clip):
        return "hook" in _mix_meta_type(clip)

    def _mix_meta_is_close(clip):
        ctype = _mix_meta_type(clip)
        return "close" in ctype or ctype in ("cta", "call_to_action", "urgency")

    def _mix_final_order(clips):
        first_hook = []
        middle = []
        closes = []
        for clip in clips or []:
            if _mix_meta_is_close(clip):
                closes.append(clip)
            elif _mix_meta_is_hook(clip) and not first_hook:
                first_hook.append(clip)
            else:
                middle.append(clip)
        return first_hook + middle + closes

    def _mix_reclose_after_source_balance(clips):
        """Keep source-balance clips, then re-trim and restore hook/product/close order."""
        if not clips:
            return clips
        try:
            target_seconds = float(target_duration or 60)
        except Exception:
            target_seconds = 60.0
        target_high = target_seconds + max(5.0, target_seconds / 6.0)
        before_total = _mix_meta_total(clips)
        working = list(clips)
        removed = []

        while _mix_meta_total(working) > target_high + 0.2:
            current_total = _mix_meta_total(working)
            overshoot = current_total - target_high
            source_counts = {}
            for item in working:
                source_counts[item.get("source")] = source_counts.get(item.get("source"), 0) + 1
            candidates = []
            for idx, item in enumerate(working):
                if item.get("source_balance"):
                    continue
                if _mix_meta_is_hook(item) or _mix_meta_is_close(item):
                    continue
                if source_counts.get(item.get("source"), 0) <= 1:
                    continue
                dur = _mix_meta_duration(item)
                if dur <= 0:
                    continue
                try:
                    score = float(item.get("score") or 0)
                except Exception:
                    score = 0.0
                covers = dur >= overshoot
                candidates.append((
                    0 if covers else 1,
                    abs(dur - overshoot) if covers else -dur,
                    score,
                    idx,
                    item,
                ))
            if not candidates:
                _log(f"混剪来源均衡收口: 超出上限 {current_total:.1f}s>{target_high:.1f}s，但没有可安全回收的普通片段")
                break
            candidates.sort(key=lambda item: item[:4])
            _, _, _, remove_idx, removed_clip = candidates[0]
            working.pop(remove_idx)
            removed.append(removed_clip)
            text = re.sub(r"\s+", " ", str(removed_clip.get("text") or "")).strip()[:40]
            _log(
                f"混剪来源均衡收口: 回收 {removed_clip.get('start', 0):.1f}-{removed_clip.get('end', 0):.1f}s "
                f"({_mix_meta_duration(removed_clip):.1f}s) | {text}"
            )

        after_trim_total = _mix_meta_total(working)
        if removed:
            _log(f"混剪来源均衡收口: {len(clips)}段/{before_total:.1f}s -> {len(working)}段/{after_trim_total:.1f}s")

        ordered = _mix_final_order(working)
        if ordered != working:
            _log("混剪来源均衡收口: 已重新整理 Hook/Product/Close 顺序")
        for idx, item in enumerate(ordered):
            item["idx"] = idx
        return ordered

    all_clips_meta = []
    tc = 0
    for clip in ordered_clips:
        c_type, text, start, end, score, dur = clip[:6]
        preview_exact = _clip_preview_exact(clip)
        _src_idx = _explicit_source_idx(clip)
        _marker_idx = -1
        for _vi2 in range(len(video_list)):
            if f"[V{_vi2+1}]" in text:
                _marker_idx = _vi2
                break
        if _src_idx >= 0 and _marker_idx >= 0 and _marker_idx != _src_idx:
            _log(f"Source map: 使用片段源文件覆盖 V{_marker_idx+1} -> V{_src_idx+1} ({start:.1f}-{end:.1f}s)")
        if _src_idx < 0:
            _src_idx = _marker_idx
        if _src_idx < 0:
            _src_idx = _infer_source_idx_from_srt(text, start, end)
            if _src_idx >= 0:
                _log(f"Source map: 片段缺少[Vn]标记，已按SRT匹配到 V{_src_idx+1} ({start:.1f}-{end:.1f}s)")
        if _src_idx < 0:
            _log(
                f"Source map: 丢弃无法确认素材来源的片段，避免 V1 画面与字幕错配 "
                f"({start:.1f}-{end:.1f}s | {_strip_mix_marker(text)[:36]})"
            )
            continue
        vp = video_list[_src_idx]
        all_clips_meta.append({
            "idx": tc, "type": c_type, "text": text,
            "start": start, "end": end, "score": score,
            "duration": dur, "source": vp,
            "preview_exact": preview_exact,
        })
        tc += 1

    if len(video_list) > 1 and all_clips_meta and len({c["source"] for c in all_clips_meta}) <= 1:
        used_sources = {c["source"] for c in all_clips_meta}
        missing_sources = [
            f"V{src_idx + 1}"
            for src_idx, vp in enumerate(video_list)
            if vp not in used_sources
        ]
        suffix = f"；未命中 {'、'.join(missing_sources)}" if missing_sources else ""
        _log(
            "混剪来源均衡: AI只命中单一素材，已保留AI原叙事，"
            "不再由程序补片或重排"
            + suffix
        )

    _validate_selected_duration_contract(
        all_clips_meta,
        target_duration,
        _planned_speed_factor,
        _log,
        shortage_grace_seconds=_duration_shortage_grace,
        user_confirmed=_user_confirmed_clips,
        duration_tolerance=duration_tolerance,
    )

    _log(f"Mapped: {len(all_clips_meta)} clips from {len(set(c['source'] for c in all_clips_meta))} sources")

    # Bridge: ai_analyze_clips with merge_mode already sorts, so sorted_clips = all_clips_meta
    sorted_clips = all_clips_meta
    if not _clips_only:
        _log_final_clip_details(sorted_clips, _log, title="混剪最终片段明细")
    if _clips_only:
        global _multi_result_cache
        if isinstance(_multi_result_cache, dict):
            _multi_result_cache["clips"] = list(sorted_clips)
            _multi_result_cache["sources"] = list(video_list)
            _multi_result_cache["srt_text"] = merged_srt
            try:
                import ai_clipper as _ai_meta
                analysis_metadata = dict(_ai_meta.get_last_analysis_metadata() or analysis_metadata or {})
            except Exception:
                _LOG.warning("unexpected error", exc_info=True)
                pass
            _multi_result_cache["analysis_metadata"] = dict(analysis_metadata or {})
            _multi_result_cache["category_summary"] = dict(analysis_metadata.get("category_summary") or {})
            _multi_result_cache["topic_coverage_summary"] = dict(analysis_metadata.get("topic_coverage_summary") or {})
            _multi_result_cache["preference_summary"] = dict(analysis_metadata.get("preference_summary") or preference_summary or {})
            _multi_result_cache["word_timings"] = list(_mix_word_timings or [])
            _multi_result_cache["requested_target_duration"] = target_duration
            _multi_result_cache["ai_target_duration"] = _ai_target_duration
            _multi_result_cache["duration_speed_factor"] = _planned_speed_factor
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": True, "clips_cached": True}

    _source_remux_map = {}
    for _src in list(dict.fromkeys(video_list)):
        _source_remux_map[_src] = _remux_ts_for_editing(_src, tmp, ffmpeg, _log)
    if any(_source_remux_map.get(_src) != _src for _src in _source_remux_map):
        video_list = [_source_remux_map.get(_src, _src) for _src in video_list]
        for _clip in sorted_clips:
            _orig_src = _clip.get("source")
            if _orig_src in _source_remux_map:
                _clip["original_source"] = _orig_src
                _clip["source"] = _source_remux_map[_orig_src]
        for _entry in _mix_srt_entries:
            _orig_src = _entry.get("source")
            if _orig_src in _source_remux_map:
                _entry["source"] = _source_remux_map[_orig_src]

    # ============================================================
    # Step 3: Cut each clip in order
    # ============================================================
    # Probe first video for dimensions
    probe_cmd = [ffmpeg, "-i", video_list[0]]
    try:
        probe_out = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   creationflags=_NO_WINDOW, timeout=30)
        probe_stderr = probe_out.stderr
    except:
        probe_stderr = ""
    _source_w, _source_h = _probe_video_size(ffmpeg, video_list[0])
    if _source_w <= 0 or _source_h <= 0:
        _source_w, _source_h = 720, 1280
    w, h, _res_note = _output_resolution_for_source(_source_w, _source_h, VIDEO_CONFIG["resolution"])
    _log(f"Source: {_source_w}x{_source_h} -> 输出 {w}x{h}（{_res_note}）")

    # ===== Smart Crop batch detection =====
    _sc_results_all = {}
    _src_dims = {video_list[0]: (_source_w, _source_h)}
    for _vp in video_list[1:]:
        _pw, _ph = _probe_video_size(ffmpeg, _vp)
        if _pw > 100 and _ph > 100:
            _src_dims[_vp] = (_pw, _ph)
    if smart_crop_enabled:
        try:
            from smart_crop import batch_detect_clips, compute_smart_crop, _even
            _src_groups = {}
            for ci, clip in enumerate(sorted_clips):
                vp = clip["source"]
                _src_groups.setdefault(vp, []).append((ci, clip))
            for _vp, _group in _src_groups.items():
                if cancel_event and cancel_event.is_set():
                    break
                _ordered = [("", "", c["start"], c["end"], 0, c["duration"]) for ci, c in _group]
                # Probe per-source dimensions for accurate smart crop
                _fw, _fh = _src_dims.get(_vp, (w, h))
                _src_dims[_vp] = (_fw, _fh)
                _results = batch_detect_clips(_vp, _ordered, log_fn=_log, ffmpeg_cmd=ffmpeg, frame_w=_fw, frame_h=_fh)
                if _results:
                    for _ri, (_ci, _) in enumerate(_group):
                        if _ri in _results:
                            _sc_results_all[(_vp, _ci)] = _results[_ri]
        except Exception as e:
            _log(f"Smart crop: {e}")

    temp_dir = os.path.join(tmp, "clips")
    os.makedirs(temp_dir, exist_ok=True)
    temp_files = []
    _clip_kb_caps = []
    _clip_starts = []
    _clip_ends = []
    _mix_cut_maps = []
    _ken_burns_opencv = None
    _ken_burns_ffmpeg = None
    if ken_burns_enabled:
        try:
            from smart_crop import apply_ken_burns_opencv as _ken_burns_opencv
            from smart_crop import apply_ken_burns_ffmpeg as _ken_burns_ffmpeg
        except ImportError:
            _log("KenBurns: 滤镜不可用")
            ken_burns_enabled = False

    def _mix_needs_next_sentence(_text):
        _t = str(_text or "").strip().rstrip("。！？!?，,、 ")
        return bool(_t) and _t.endswith((
            "然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实",
            "的话", "你看", "我觉得", "感觉", "给你们", "这个", "这款", "这件",
            "它是", "它会", "来讲的话", "一点", "一点点", "有没有发现",
            "你去", "去", "你想象一下", "想象一下", "七八月份你去", "你这一套"
        ))

    def _mix_starts_as_followup(_text):
        _t = str(_text or "").strip()
        return _t.startswith(("然后", "而且", "但是", "不过", "所以", "因为", "就是", "其实", "它", "这个", "这款", "这件", "你看"))

    def _next_selected_start_same_source(index, source, current_start):
        nearest_start = None
        for next_idx, next_clip in enumerate(sorted_clips):
            if next_idx == index:
                continue
            if next_clip.get("source") != source:
                continue
            try:
                next_start = float(next_clip.get("start", 0))
            except Exception:
                continue
            if next_start <= current_start + 0.05:
                continue
            if nearest_start is None or next_start < nearest_start:
                nearest_start = next_start
        return nearest_start

    _cut_stage_started = time.time()
    for ci, clip in enumerate(sorted_clips):
        if _cancelled():
            break

        vp = clip["source"]
        c_type = clip["type"]
        start = float(clip["start"])
        _next_same_source_start = _next_selected_start_same_source(ci, vp, start)
        preview_exact = bool(clip.get("preview_exact"))
        if preview_exact:
            clip = dict(clip)
            clip["end"] = clip["end"]
        end = float(clip["end"]) + 0.1  # 缓冲避免尾部被切 + 0.1  # +0.1s缓冲避免语音尾部被切

        # 混剪每个源视频都有自己的0秒时间轴，按当前source的SRT边界补齐完整句。
        try:
            if preview_exact:
                raise RuntimeError("preview exact range")
            _src_entries = [e for e in _mix_srt_entries if e.get("source") == vp]
            _start_srt, _end_srt = None, None
            for _entry in _src_entries:
                _ts = float(_entry.get("start", 0))
                _te = float(_entry.get("end", _ts))
                if _ts <= end <= _te:
                    _end_srt = _te
                if _ts <= start <= _te:
                    _start_srt = _ts
            if _end_srt is not None and _end_srt - end <= 8.0:
                end = _end_srt
            if _start_srt is not None and start - _start_srt <= 3.0:
                start = _start_srt
            if ci == len(sorted_clips) - 1 or str(c_type).lower() in ("close", "cta", "call_to_action"):
                for _ei, _entry in enumerate(_src_entries):
                    _te = float(_entry.get("end", float(_entry.get("start", 0))))
                    if abs(_te - end) <= 0.7 and _ei + 1 < len(_src_entries):
                        _next = _src_entries[_ei + 1]
                        _next_start = float(_next.get("start", _te))
                        _next_end = float(_next.get("end", _next_start))
                        if _next_start - end <= 1.2 and _next_end - start <= 14.0 and (_mix_needs_next_sentence(_entry.get("text", "")) or _mix_starts_as_followup(_next.get("text", ""))):
                            end = _next_end
                            _log(f"结尾承接: 延伸到下一句 {end:.1f}s，避免最后一句截断")
                        break
        except Exception:
            _LOG.warning("unexpected error", exc_info=True)
            pass

        if _next_same_source_start is not None and end > _next_same_source_start:
            old_end = end
            end = max(start + 0.1, _next_same_source_start - 0.02)
            _log(f"相邻片段保护: clip {ci+1} end {old_end:.2f}s→{end:.2f}s，避免与下一段重复")

        _tail_guard = LAST_CLIP_AUDIO_TAIL_GUARD_SECONDS if ci == len(sorted_clips) - 1 else CLIP_AUDIO_TAIL_GUARD_SECONDS
        _extra_tail_guard = max(0.0, _tail_guard - 0.1)
        if _extra_tail_guard > 0:
            old_end = end
            end += _extra_tail_guard
            tail_limited = False
            if _next_same_source_start is not None and end > _next_same_source_start:
                end = max(start + 0.1, _next_same_source_start - 0.02)
                tail_limited = True
            if tail_limited:
                _log(f"Tail audio guard: clip {ci+1} limited {old_end:.2f}s->{end:.2f}s，避免与下一段重复")
            else:
                _log(f"Tail audio guard: clip {ci+1} extended {old_end:.2f}s->{end:.2f}s")

        if False and ci == len(sorted_clips) - 1:
            old_end = end
            end += 0.0
            _log(f"尾音保护: 最后一段延长 {old_end:.2f}s→{end:.2f}s，避免末字被截")

        duration = end - start

        if start < 0: start = 0
        if duration <= 0:
            continue

        _display_source = clip.get("original_source", vp)
        _log(f"Cut [{ci+1}/{len(sorted_clips)}] {c_type} ({start:.1f}s-{end:.1f}s) @ {os.path.basename(_display_source)[:20]}")

        out_clip = os.path.join(temp_dir, f"clip_{ci:03d}.mp4")
        _clip_starts.append(start)
        _clip_ends.append(end)

        mirror_vf = ""
        if _mirror_enabled and random.random() < OUTPUT_CLIP_MIRROR_PROBABILITY:
            mirror_vf = "hflip"

        # Smart crop or default 9:16
        _sc_zoom = 1.0
        _sw, _sh = _src_dims.get(vp, (w, h))
        _sc_info = _sc_results_all.get((vp, ci), None) if smart_crop_enabled and _sc_results_all else None
        if _sc_info:
            try:
                from smart_crop import compute_smart_crop, _even
                _sc_crop = compute_smart_crop(_sc_info, _sw, _sh, crop_level=crop_level, log_fn=_log)
                _sc_zoom = _smart_crop_zoom(_sc_crop)
                combined_vf = _smart_crop_vf(_sc_crop, _sw, _sh, w, h, _even, log_fn=_log)
            except Exception as sce:
                _log(f"Smart crop err: {sce}")
                combined_vf = _smart_crop_no_crop_vf(_sw, _sh, w, h)
        else:
            combined_vf = _smart_crop_no_crop_vf(_sw, _sh, w, h)
        if mirror_vf:
            combined_vf += "," + mirror_vf
        combined_vf += ",setpts=PTS-STARTPTS"
        combined_vf = _apply_clip_video_transition_fade(
            combined_vf, duration, ci, len(sorted_clips),
            _transition_mode, _transition_duration
        )
        combined_vf = _append_stable_video_tail_filter(combined_vf, VIDEO_CONFIG["fps"])
        _mix_audio_fade = min(CLIP_AUDIO_FADE_SECONDS, max(0.0, duration / 3))
        _mix_fade_out_start = max(0.0, duration - _mix_audio_fade)
        _mix_audio_filter = (
            _stable_audio_tail_filter()
            if preview_exact
            else (
                f"atrim=0:{duration:.3f},{_stable_audio_tail_filter()},"
                f"afade=t=in:st=0:d={_mix_audio_fade:.3f},"
                f"afade=t=out:st={_mix_fade_out_start:.3f}:d={_mix_audio_fade:.3f}"
            )
        )

        if preview_exact:
            cmd = _preview_exact_cut_cmd(
                ffmpeg, vp, start, duration, combined_vf,
                _mix_audio_filter, out_clip, _intermediate_vcodec_args(), VIDEO_CONFIG["fps"]
            )
        else:
            cmd = [ffmpeg, "-y"]
            _append_seek_input_args(cmd, vp, start, accurate=False)
            cmd += ["-t", f"{duration:.3f}"]
            cmd += ["-fflags", "+genpts"]
            cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
            cmd += _intermediate_vcodec_args()
            cmd += ["-vf", combined_vf]
            cmd += ["-pix_fmt", "yuv420p"]
            cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-async", "1",
                   "-af", _mix_audio_filter, "-shortest"]
            cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
            cmd += [out_clip]

        try:
            proc = _register_process(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=_NO_WINDOW))
            rc = _wait_process(proc, timeout=300, cancel_event=cancel_event)
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            _log(f"  Cut timeout: {start:.1f}s-{end:.1f}s ({duration:.1f}s)")
            continue
        except Exception as e:
            if _cancelled():
                _log("Cancelled.")
                shutil.rmtree(tmp, ignore_errors=True)
                return False
            _log(f"  Cut failed: {e}")
            continue

        if rc == 0 and os.path.exists(out_clip) and os.path.getsize(out_clip) > 1000:
            temp_files.append(out_clip)
            _clip_kb_caps.append(_kb_quality_cap_for_zoom(_sc_zoom))
            _mix_cut_maps.append({"start": start, "end": end, "source": vp, "type": c_type, "preview_exact": preview_exact})
        else:
            _log(f"  Cut failed (rc={rc})")
            if not _hw_fallback and _get_video_encoder():
                _sw_args = _intermediate_software_vcodec_args()
                _sw_name = _software_encoder_name()
                _log(f"  硬件编码切割失败，切换到 {_sw_name} 软件编码重试，后续片段不再尝试硬件编码。")
                _hw_fallback = True
                cmd = [ffmpeg, "-y"]
                _append_seek_input_args(cmd, vp, start, accurate=preview_exact)
                cmd += ["-t", f"{duration:.3f}"]
                cmd += ["-fflags", "+genpts"]
                cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
                cmd += _sw_args
                cmd += ["-vf", combined_vf]
                cmd += ["-pix_fmt", "yuv420p"]
                cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-async", "1",
                       "-af", _mix_audio_filter, "-shortest"]
                cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
                cmd += [out_clip]
                try:
                    proc = _register_process(subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            creationflags=_NO_WINDOW))
                    rc2 = _wait_process(proc, timeout=300, cancel_event=cancel_event)
                    if rc2 == 0 and os.path.exists(out_clip) and os.path.getsize(out_clip) > 1000:
                        temp_files.append(out_clip)
                        _clip_kb_caps.append(_kb_quality_cap_for_zoom(_sc_zoom))
                        _mix_cut_maps.append({"start": start, "end": end, "source": vp, "type": c_type, "preview_exact": preview_exact})
                        _log(f"  Cut retry OK ({_sw_name})")
                    else:
                        _log(f"  Cut retry failed ({_sw_name}, rc={rc2})")
                except subprocess.TimeoutExpired:
                    _terminate_process(proc)
                    _log(f"  Cut retry timeout ({_sw_name})")
                except Exception as e:
                    _log(f"  Cut retry failed ({_sw_name}): {e}")

    if not temp_files:
        _log("No clips cut successfully!")
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    _log(f"Cut {len(temp_files)}/{len(sorted_clips)} clips")
    _log(f"阶段耗时: 切片 {time.time() - _cut_stage_started:.1f}s")

    if ken_burns_enabled and temp_files:
        _kb_stage_started = time.time()
        _log("KenBurns: 稳定模式二次处理开始")
        _kb_ok = 0
        for _kbi, _clip_file in enumerate(temp_files):
            if _cancelled():
                break
            try:
                _kb_dur = _clip_ends[_kbi] - _clip_starts[_kbi] if _kbi < len(_clip_starts) else 10.0
                _kb_out = _clip_file.replace(".mp4", "_kb.mp4")
                _kb_cap = _clip_kb_caps[_kbi] if _kbi < len(_clip_kb_caps) else None
                if _ken_burns_opencv:
                    _kb_ok_flag = _ken_burns_opencv(
                        _clip_file, _kb_out, _kb_dur, w, h, 30,
                        ffmpeg_cmd=ffmpeg, log_fn=_log,
                        intensity=kb_intensity, max_zoom_delta=_kb_cap)
                else:
                    _kb_ok_flag = False
                if not _kb_ok_flag and _ken_burns_ffmpeg:
                    _log("KenBurns: OpenCV稳定模式失败，回退FFmpeg快速模式")
                    _kb_ok_flag = _ken_burns_ffmpeg(
                        _clip_file, _kb_out, _kb_dur, w, h, 30,
                        ffmpeg_cmd=ffmpeg, log_fn=_log,
                        intensity=kb_intensity, max_zoom_delta=_kb_cap)
                if _kb_ok_flag and os.path.exists(_kb_out):
                    os.replace(_kb_out, _clip_file)
                    _kb_ok += 1
                elif os.path.exists(_kb_out):
                    os.remove(_kb_out)
            except Exception as _kbe:
                _log(f"KenBurns: 稳定模式片段{_kbi+1}失败 {_kbe}")
        _log(f"KenBurns: 稳定模式完成 {_kb_ok}/{len(temp_files)}")
        _log(f"阶段耗时: KenBurns {time.time() - _kb_stage_started:.1f}s")

    # ============================================================
    # Step 4: Concat with filter_complex (handles format differences)
    # ============================================================
    if _cancelled():
        _log("Cancelled.")
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    _concat_stage_started = time.time()
    _log(f"Concatenating {len(temp_files)} clips...")

    # Probe clip stream info
    clip_has_video = []
    clip_has_audio = []
    clip_durations = []
    clip_probe_summaries = []
    for tf in temp_files:
        clip_durations.append(_probe_media_duration(tf))
        p = subprocess.run([ffmpeg, "-i", tf], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace",
                          creationflags=_NO_WINDOW, timeout=15)
        stderr = p.stderr or ""
        clip_has_video.append("Video:" in stderr)
        clip_has_audio.append("Audio:" in stderr)
        summary_lines = []
        for line in stderr.splitlines():
            stripped = line.strip()
            if "Duration:" in stripped or "Video:" in stripped or "Audio:" in stripped:
                summary_lines.append(stripped)
        clip_probe_summaries.append(" | ".join(summary_lines[-4:])[:500])
        if not clip_has_video[-1]:
            _log(f"  Warning: {os.path.basename(tf)} no video stream")
        if not clip_has_audio[-1]:
            _log(f"  Warning: {os.path.basename(tf)} no audio stream, adding silent track")

    raw_concat = os.path.join(tmp, "raw_concat.mp4")
    concat_copy_ok = False
    stderr_data = ""
    has_preview_exact_clips = any(bool(item.get("preview_exact")) for item in _mix_cut_maps)
    fast_copy_concat_enabled = False

    if has_preview_exact_clips:
        _log("Concat copy: preview-exact clips detected, using timestamp-normalized concat")
    elif all(clip_has_video) and all(clip_has_audio) and not fast_copy_concat_enabled:
        _log("Concat copy: stable mode disabled fast copy, using timestamp-normalized concat")

    if fast_copy_concat_enabled and all(clip_has_video) and all(clip_has_audio) and not has_preview_exact_clips:
        list_file = os.path.join(tmp, "concat_list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for tf in temp_files:
                f.write(f"file '{os.path.abspath(tf).replace(chr(92), '/')}'\n")
        copy_cmd = [
            ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "copy", "-c:a", "copy",
            raw_concat,
        ]
        try:
            proc = _register_process(subprocess.Popen(copy_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=_NO_WINDOW))
            _, stderr_data = _communicate_process(proc, timeout=180, cancel_event=cancel_event)
            concat_copy_ok = proc.returncode == 0 and os.path.exists(raw_concat) and os.path.getsize(raw_concat) > 1000
            if concat_copy_ok:
                _log("Concat copy: 无损拼接成功，跳过拼接重编码")
            else:
                _log("Concat copy: 无损拼接失败，回退兼容拼接")
        except subprocess.TimeoutExpired:
            _terminate_process(proc)
            _log("Concat copy: 超时，回退兼容拼接")
        except Exception as e:
            if _cancelled():
                _log("Cancelled.")
                shutil.rmtree(tmp, ignore_errors=True)
                return False
            _log(f"Concat copy: {e}，回退兼容拼接")

    if not concat_copy_ok:
        n = len(temp_files)
        inputs = []
        filter_normalizers = []
        filter_parts = []
        in_idx = 0

        for i in range(n):
            inputs.extend(["-i", temp_files[i]])
            clip_input_idx = in_idx
            streams = []
            if clip_has_video[i]:
                v_label = f"vnorm{i}"
                filter_normalizers.append(
                    f"[{clip_input_idx}:v]{_stable_video_tail_filter(VIDEO_CONFIG['fps'])},setsar=1[{v_label}]"
                )
                streams.append(f"[{v_label}]")
            if clip_has_audio[i]:
                a_label = f"anorm{i}"
                filter_normalizers.append(
                    f"[{clip_input_idx}:a]{_stable_audio_tail_filter()},aformat=sample_fmts=fltp:channel_layouts=stereo[{a_label}]"
                )
                streams.append(f"[{a_label}]")
            else:
                silent_duration = clip_durations[i] if i < len(clip_durations) else 1.0
                silent_duration = max(0.1, float(silent_duration or 1.0))
                inputs.extend(["-f", "lavfi", "-t", f"{silent_duration:.3f}", "-i", "anullsrc=r=44100:cl=stereo"])
                in_idx += 1
                a_label = f"anorm{i}"
                filter_normalizers.append(
                    f"[{in_idx}:a]{_stable_audio_tail_filter()},aformat=sample_fmts=fltp:channel_layouts=stereo[{a_label}]"
                )
                streams.append(f"[{a_label}]")
            filter_parts.append(streams)
            in_idx += 1

        concat_input_str = ""
        for streams in filter_parts:
            for s in streams:
                concat_input_str += s

        has_v_out = any(clip_has_video)
        has_a_out = any(clip_has_audio) or not all(clip_has_audio)
        v_count = 1 if has_v_out else 0
        a_count = 1 if has_a_out else 0
        concat_filter = f"{concat_input_str}concat=n={n}:v={v_count}:a={a_count}[outv]"
        if has_a_out:
            concat_filter += "[outa]"
        else:
            concat_filter = concat_filter.replace("[outv]", "[outv][outa]")
        if filter_normalizers:
            concat_filter = ";".join(filter_normalizers + [concat_filter])

        cmd = [ffmpeg, "-y"] + inputs + ["-filter_complex", concat_filter]
        if has_v_out:
            cmd += ["-map", "[outv]"]
        if has_a_out or not any(clip_has_audio):
            cmd += ["-map", "[outa]"]
        if has_v_out:
            cmd += _stable_cfr_output_args(VIDEO_CONFIG["fps"])
        cmd += _intermediate_vcodec_args()
        cmd += ["-c:a", "aac", raw_concat]

        try:
            ok, rc, stderr_data = _run_ffmpeg_with_hw_fallback(
                cmd,
                dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"),
                600,
                _log,
                "混剪拼接",
                raw_concat,
                software_args=_intermediate_software_vcodec_args(),
                cancel_event=cancel_event,
            )
        except subprocess.TimeoutExpired:
            _log("Concat timeout!")
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        except Exception as e:
            _log(f"Concat error: {e}")
            shutil.rmtree(tmp, ignore_errors=True)
            return False

        if not ok:
            _log(f"Concat failed (rc={rc})")
            _log(f"  concat filter: {concat_filter[:1000]}")
            _log(f"  concat streams: video={clip_has_video.count(True)}/{len(temp_files)}, audio={clip_has_audio.count(True)}/{len(temp_files)}")
            for idx, tf in enumerate(temp_files):
                size = os.path.getsize(tf) if os.path.exists(tf) else 0
                probe_summary = clip_probe_summaries[idx] if idx < len(clip_probe_summaries) else ""
                _log(f"  input[{idx+1:02d}] {os.path.basename(tf)} size={size} video={clip_has_video[idx]} audio={clip_has_audio[idx]} {probe_summary}")
            if stderr_data:
                for line in stderr_data.strip().split(chr(10))[-40:]:
                    if line.strip(): _log(f"  ffmpeg: {line.strip()[:120]}")
            shutil.rmtree(tmp, ignore_errors=True)
            return False

    _transition_mask = []
    if _transition_mode == "fade" and len(temp_files) > 1:
        _transition_mask = _transition_boundary_mask(_mix_cut_maps, len(temp_files))
        _transition_count = sum(1 for value in _transition_mask if value)
        _log(f"片段转场: 计划轻叠化 {_transition_count}/{len(_transition_mask)} 处，连续片段保持硬切")

    raw_mb = os.path.getsize(raw_concat) / (1024 * 1024)
    _log(f"Concat done: {raw_mb:.1f}MB")
    _log(f"阶段耗时: 拼接 {time.time() - _concat_stage_started:.1f}s")

    # ============================================================
    # Step 5: Dedup
    # ============================================================
    if _cancelled():
        shutil.rmtree(tmp, ignore_errors=True)
        return False

    nosub_file = os.path.join(tmp, "nosub.mp4")
    cfg = VIDEO_CONFIG
    _dedup_stage_started = time.time()

    if dedup_preset == "none":
        if any(_transition_mask):
            transition_ok, _ = _concat_clips_with_light_dissolve(
                ffmpeg, temp_files, nosub_file, _transition_duration, _log,
                cancel_event=cancel_event, transition_mask=_transition_mask
            )
            if not transition_ok:
                shutil.copy2(raw_concat, nosub_file)
        else:
            shutil.copy2(raw_concat, nosub_file)
    else:
        _log(f"Dedup ({dedup_preset})...")
        if _dedup_is_custom:
            dedup = _manual_dedup_filters(_video_options_without_mirror(dedup_video_options), dedup_audio_options)
            frame_vf, frame_applied = _custom_frame_structure_filter(dedup_video_options, cfg.get("fps", 30))
            if frame_vf:
                dedup["video_filters"] = _append_filter(dedup.get("video_filters"), frame_vf)
                dedup.setdefault("applied", []).append(frame_applied)
        else:
            dedup = build_dedup_filters(w, h, 0, mirror_enabled=False)
        applied = ",".join(dedup["applied"]) if dedup.get("applied") else "none"
        _log(f"\u53bb\u91cd\u6548\u679c: {applied}")

        vf = f"setpts=PTS-STARTPTS,scale=-2:{h}:force_original_aspect_ratio=decrease:flags=lanczos,crop={w}:{h},{_final_sharpen_vf()}"
        af = f"{_stable_audio_tail_filter()},afade=t=in:st=0:d=0.3"
        if dedup.get("video_filters"):
            vf = dedup["video_filters"] + "," + vf
        if dedup.get("audio_filters"):
            af = dedup["audio_filters"] + "," + af
        vf = _append_stable_video_tail_filter(vf, cfg["fps"])

        needs_complex = "aevalsrc" in af or "amix" in af
        _combined_transition_dedup_done = False
        if any(_transition_mask) and not needs_complex:
            _log("片段转场: 合并轻叠化与去重编码，减少一次整片重编码")
            transition_ok, _ = _concat_clips_with_light_dissolve(
                ffmpeg, temp_files, nosub_file, _transition_duration, _log,
                cancel_event=cancel_event,
                transition_mask=_transition_mask,
                video_filter=vf,
                audio_filter=af,
                video_codec_args=_final_vcodec_args(),
            )
            if transition_ok:
                _combined_transition_dedup_done = True
            else:
                _log("片段转场: 合并编码失败，回退普通去重链路")
        elif any(_transition_mask) and needs_complex:
            _log("片段转场: 当前音频去重含双音轨融合，暂不合并编码，保留完整去重效果")
            transition_raw = os.path.join(tmp, "raw_transition.mp4")
            transition_ok, _ = _concat_clips_with_light_dissolve(
                ffmpeg, temp_files, transition_raw, _transition_duration, _log,
                cancel_event=cancel_event,
                transition_mask=_transition_mask,
            )
            if transition_ok:
                raw_concat = transition_raw
            else:
                _log("片段转场: 单独轻叠化失败，回退普通去重链路")

        if _combined_transition_dedup_done:
            dedup_cmd = None
        else:
            dedup_cmd = [ffmpeg, "-y", "-i", raw_concat]
            dedup_cmd += ["-vf", vf, "-af", af]
            dedup_cmd += _stable_cfr_output_args(cfg["fps"])
            dedup_cmd += _final_vcodec_args()
            dedup_cmd += ["-c:a", cfg["codec_a"], "-b:a", cfg["bitrate_a"], "-shortest"]
            dedup_cmd += ["-avoid_negative_ts", "make_zero", "-movflags", "+faststart"]
            dedup_cmd += [nosub_file]

        try:
            if dedup_cmd is not None:
                ok, rc, stderr_data = _run_ffmpeg_with_hw_fallback(
                    dedup_cmd,
                    dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"),
                    600,
                    _log,
                    "混剪去重",
                    nosub_file,
                    software_args=_final_software_vcodec_args(),
                    cancel_event=cancel_event,
                )
            if dedup_cmd is not None and not ok:
                _log(f"Dedup failed (rc={rc}), using raw")
                if stderr_data:
                    for line in stderr_data.strip().split("\n")[-10:]:
                        if line.strip():
                            _log(f"  ffmpeg: {line.strip()[:120]}")
                shutil.copy2(raw_concat, nosub_file)
        except subprocess.TimeoutExpired:
            _log("Dedup timeout, using raw")
            shutil.copy2(raw_concat, nosub_file)
        except:
            shutil.copy2(raw_concat, nosub_file)

        if not os.path.exists(nosub_file):
            shutil.copy2(raw_concat, nosub_file)
    _log(f"阶段耗时: 去重 {time.time() - _dedup_stage_started:.1f}s")

    _nosub_duration = _probe_media_duration(nosub_file)
    _duration_ok, _duration_contract = _validate_actual_duration_contract(
        _nosub_duration,
        target_duration,
        margin=1.0,
        shortage_grace_seconds=_duration_shortage_grace,
        user_confirmed=_user_confirmed_clips,
        duration_tolerance=duration_tolerance,
    )
    _log(
        f"成片时长预验收: {_duration_contract['actual']:.1f}s，"
        f"要求{_duration_contract['low']:.0f}-{_duration_contract['high']:.0f}s"
    )
    if not _duration_ok:
        _duration_error = (
            f"成片时长未达标：实际{_duration_contract['actual']:.1f}秒，"
            f"目标{_duration_contract['target']:.0f}秒"
            f"（允许{_duration_contract['low']:.0f}-{_duration_contract['high']:.0f}秒）"
        )
        _log(_duration_error + "，已停止字幕烧录")
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(_duration_error)

    # ============================================================
    # Step 6: Subtitle overlay (if enabled)
    # ============================================================
    _subtitle_stage_started = time.time()
    if subtitle_overlay and os.path.exists(nosub_file) and os.path.getsize(nosub_file) > 10000:
        try:
            _add_subtitles_final(nosub_file, final, w, h, tmp, _log, pip_path, pip_size, pip_opacity, pip_pos)
        except Exception as e:
            _log(f"Subtitle overlay failed: {e}")
            shutil.copy2(nosub_file, final)
    else:
        shutil.copy2(nosub_file, final)
    _log(f"阶段耗时: 字幕/画中画 {time.time() - _subtitle_stage_started:.1f}s")

    # ============================================================
    # Cleanup + return
    # ============================================================
    final_mb = os.path.getsize(final) / (1024 * 1024) if os.path.exists(final) else 0
    _actual_dur = 0.0
    try:
        _ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if os.name == "nt" else ""))
        _r = subprocess.run(
            [_ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", final],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=_NO_WINDOW,
        )
        if _r.returncode == 0 and _r.stdout.strip():
            _actual_dur = float(_r.stdout.strip())
    except Exception:
        _LOG.warning("unexpected error", exc_info=True)
        pass

    if _actual_dur > 0:
        _final_duration_ok, _final_contract = _validate_actual_duration_contract(
            _actual_dur,
            target_duration,
            margin=1.0,
            shortage_grace_seconds=_duration_shortage_grace,
            user_confirmed=_user_confirmed_clips,
            duration_tolerance=duration_tolerance,
        )
        if not _final_duration_ok:
            _duration_error = (
                f"最终成片时长未达标：实际{_final_contract['actual']:.1f}秒，"
                f"目标{_final_contract['target']:.0f}秒"
                f"（允许{_final_contract['low']:.0f}-{_final_contract['high']:.0f}秒）"
            )
            _log(_duration_error)
            try:
                os.remove(final)
            except OSError:
                _LOG.warning("failed to remove temp file", exc_info=True)
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(_duration_error)

    _report_old_dur, _report_old_tol = TARGET_DURATION, TARGET_DURATION_TOLERANCE
    try:
        TARGET_DURATION = target_duration
        _report_duration_contract = DurationContract.create(
            target_duration,
            1.0,
            tolerance=duration_tolerance,
        )
        TARGET_DURATION_TOLERANCE = max(
            0.0,
            _report_duration_contract.final_max - _report_duration_contract.final_target,
        )
        _report = _build_cut_report(sorted_clips, len(sorted_clips), len(sorted_clips), final, final_mb)
        _print_cut_report(_report, _log)
    finally:
        TARGET_DURATION, TARGET_DURATION_TOLERANCE = _report_old_dur, _report_old_tol

    _elapsed = max(0.0, time.time() - _mix_run_started)
    if _elapsed >= 60:
        _elapsed_text = f"{int(_elapsed // 60)}分{_elapsed % 60:.1f}秒"
    else:
        _elapsed_text = f"{_elapsed:.1f}秒"
    shutil.rmtree(tmp, ignore_errors=True)

    _log(f"\nMix done!")
    _log(f"  Sources: {len(video_list)} videos")
    _log(f"  Clips: {len(sorted_clips)}")
    if _actual_dur > 0:
        _log(f"  成品真实时长: {_actual_dur:.1f}s")
    _log(f"  Output: {final}")
    _log(f"  Size: {final_mb:.1f}MB")
    _log(f"  混剪总用时: {_elapsed_text}")

    return {"ok": True, "output_path": final, "clips": len(sorted_clips), "sources": len(video_list), "size_mb": final_mb}
