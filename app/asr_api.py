"""
云端语音识别模块 - 支持 OpenAI 兼容的 ASR API
需要返回带时间戳的 segments（start/end/text 格式）

支持的服务（已验证有时间戳输出）：
  - SiliconFlow TeleSpeechASR: https://api.siliconflow.cn/v1
    模型: TeleAI/TeleSpeechASR（推荐，快+准+有时间戳）
  - OpenAI Whisper API: https://api.openai.com/v1
    模型: whisper-large-v3
  - Groq Whisper API: https://api.groq.com/openai/v1
    模型: whisper-large-v3（免费+极快）

⚠️ 以下服务不带时间戳，无法用于切片：
  - SiliconFlow SenseVoiceSmall（仅返回纯文字）
"""
import json
import os
import sys
import ssl
import subprocess
import tempfile
import urllib.request
import urllib.error


# ============ 分段识别参数 ============
CHUNK_SECONDS = 120      # 每段最大时长（秒）（DashScope Whisper 支持 120s）
OVERLAP_SECONDS = 1       # 相邻段重叠时长（秒），防止断句
# 音频时长超过此阈值时启用分段识别（短音频直接整段送）
SPLIT_THRESHOLD = 90


def _get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_asr_settings():
    path = os.path.join(_get_base_path(), "ai_settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = json.load(f)
        return {
            "api_key": s.get("asr_api_key", ""),
            "base_url": s.get("asr_base_url", "https://api.siliconflow.cn/v1"),
            "asr_model": s.get("asr_model", "whisper-large-v3"),
            "asr_enabled": False,
        }
    except Exception:
        return {
            "api_key": "", "base_url": "https://api.siliconflow.cn/v1",
            "asr_model": "FunAudioLLM/SenseVoiceSmall", "asr_enabled": False,
        }


def save_asr_settings(settings):
    path = os.path.join(_get_base_path(), "ai_settings.json")
    try:
        existing = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = json.load(f)
        existing.update(settings)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def is_asr_enabled():
    s = load_asr_settings()
    return bool(s.get("api_key") and s.get("base_url") and s.get("asr_enabled", False))


def _get_audio_duration(audio_path):
    """用 ffprobe 获取音频时长（秒），失败返回 0"""
    ffprobe = "ffprobe"
    for candidate in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "_internal", "ffmpeg", "ffprobe.exe"),
        r"C:\ffmpeg\bin\ffprobe.exe",
    ]:
        if os.path.exists(candidate):
            ffprobe = candidate
            break
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, encoding="utf-8", timeout=10
        )
        return float(r.stdout.strip())
    except Exception:
        return 0


def _split_audio_ffmpeg(audio_path, chunk_sec, overlap_sec, log_fn):
    """
    用 FFmpeg 将音频切割为多段，返回 [(chunk_path, offset_seconds), ...]
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    duration = _get_audio_duration(audio_path)
    if duration <= 0:
        _log("云端ASR分段: 无法获取音频时长，跳过分段")
        return [(audio_path, 0.0)]

    if duration <= SPLIT_THRESHOLD:
        _log(f"云端ASR分段: 音频 {duration:.0f}s <= {SPLIT_THRESHOLD}s，直接整段识别")
        return [(audio_path, 0.0)]

    ffmpeg = "ffmpeg"
    for candidate in [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "_internal", "ffmpeg", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
    ]:
        if os.path.exists(candidate):
            ffmpeg = candidate
            break

    # 计算分段
    step = max(1, chunk_sec - overlap_sec)
    chunks = []
    start = 0.0
    idx = 0
    while start < duration:
        end = min(start + chunk_sec, duration)
        chunks.append((start, end))
        start += step
        idx += 1

    _log(f"云端ASR分段: 音频 {duration:.0f}s 切为 {len(chunks)} 段 (每段 {chunk_sec}s, 重叠 {overlap_sec}s)")

    # 过滤太短的段（<5s 的段识别质量差，合并到前一段）
    MIN_CHUNK = 5
    chunks = [(s, e) for s, e in chunks if (e - s) >= MIN_CHUNK]
    if chunks:
        # 确保最后一段延伸到音频结尾
        last_start, last_end = chunks[-1]
        if last_end < duration:
            chunks[-1] = (last_start, duration)
    _log(f"云端ASR分段: 过滤后 {len(chunks)} 段")

    # 创建临时目录存放分段
    temp_dir = tempfile.mkdtemp(prefix="liveclipper_asr_")
    ext = os.path.splitext(audio_path)[1] or ".mp3"
    result = []

    for i, (s, e) in enumerate(chunks):
        chunk_path = os.path.join(temp_dir, f"chunk_{i:03d}{ext}")
        # FFmpeg 切割（精确到毫秒）
        cmd = [
            ffmpeg, "-y",
            "-i", audio_path,
            "-ss", f"{s:.3f}",
            "-to", f"{e:.3f}",
            "-acodec", "copy",
            chunk_path
        ]
        rc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode
        if rc == 0 and os.path.exists(chunk_path):
            result.append((chunk_path, s))
        else:
            _log(f"云端ASR分段: 第 {i+1} 段切割失败")

    return result


def _merge_segments_results(chunk_results, log_fn):
    """
    合并多段 ASR 的 segments，返回完整 segments 列表。
    chunk_results: [(segments_list, offset_seconds), ...]
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    merged = []
    for segs, offset in chunk_results:
        if not segs:
            continue
        for seg in segs:
            seg["start"] = seg.get("start", 0) + offset
            seg["end"] = seg.get("end", 0) + offset
            merged.append(seg)

    # 按开始时间排序
    merged.sort(key=lambda s: s.get("start", 0))

    # 去重叠：如果相邻段有重叠（因 OVERLAP_SECONDS），保留文本更长的那个
    if OVERLAP_SECONDS > 0 and len(merged) > 1:
        deduped = [merged[0]]
        for seg in merged[1:]:
            prev = deduped[-1]
            # 如果当前段的开始时间在前一段结束之前（重叠区域），跳过较短的
            if seg["start"] < prev["end"]:
                prev_text_len = len(prev.get("text", ""))
                seg_text_len = len(seg.get("text", ""))
                if seg_text_len > prev_text_len:
                    deduped[-1] = seg
                # 否则保留前一段（跳过当前）
            else:
                deduped.append(seg)
        merged = deduped

    _log(f"云端ASR分段合并: {len(merged)} 个 segments")
    return merged


def _call_asr_api(audio_path, settings, log_fn):
    """
    单次调用 ASR API，返回 segments 列表（不转 SRT，由上层处理）
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    api_key = settings.get("api_key", "")
    base_url = settings.get("base_url", "").rstrip("/")
    asr_model = settings.get("asr_model", "whisper-large-v3")

    if "/audio/transcriptions" not in base_url:
        url = f"{base_url}/audio/transcriptions"
    else:
        url = base_url

    # 构建 multipart/form-data 上传音频
    boundary = "----LiveCutterBoundary7d3b9c4e"
    filename = os.path.basename(audio_path)
    with open(audio_path, "rb") as f:
        audio_data = f.read()

    body_parts = []
    body_parts.append(f"--{boundary}")
    body_parts.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"')
    body_parts.append("Content-Type: application/octet-stream")
    body_parts.append("")
    body_parts.append("")

    body_parts.append(f"--{boundary}")
    body_parts.append('Content-Disposition: form-data; name="model"')
    body_parts.append("")
    body_parts.append(asr_model if asr_model else "whisper-large-v3")

    body_parts.append(f"--{boundary}")
    body_parts.append('Content-Disposition: form-data; name="response_format"')
    body_parts.append("")
    body_parts.append("verbose_json")

    body_parts.append(f"--{boundary}")
    body_parts.append('Content-Disposition: form-data; name="language"')
    body_parts.append("")
    body_parts.append("zh")

    body_parts.append(f"--{boundary}--")

    header_text = "\r\n".join(body_parts[:4]) + "\r\n"
    mid_text = "\r\n".join(body_parts[5:-1]) + "\r\n"
    footer = body_parts[-1]

    body = header_text.encode("utf-8") + audio_data + b"\r\n" + mid_text.encode("utf-8") + footer.encode("utf-8")

    # Use curl.exe to avoid Python SSL/TLS issues (same as DeepSeek calls)
    import tempfile as _tf
    tmp_body = _tf.NamedTemporaryFile(delete=False, suffix=".bin")
    tmp_body.write(body)
    tmp_body.close()
    try:
        curl_cmd = [
            "curl.exe", "-s", "-k", "--max-time", "300",
            "-X", "POST", url,
            "-H", f"Authorization: Bearer {api_key}",
            "-H", f"Content-Type: multipart/form-data; boundary={boundary}",
            "-d", "@" + tmp_body.name,
        ]
        r = subprocess.run(curl_cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            raise Exception(f"curl failed: {r.stderr.decode('utf-8', errors='replace')[:200]}")
        result = json.loads(r.stdout.decode("utf-8", errors="replace"))
    finally:
        try:
            os.unlink(tmp_body.name)
        except Exception:
            pass

    segments = result.get("segments", [])
    text = result.get("text", "")

    # 如果没有 segments 但有 text，用 text 构造伪 segments（无精确时间戳）
    if not segments and text:
        return _text_to_segments(text)

    return segments


def _text_to_segments(text):
    """当 API 只返回纯文本时，按句号分段生成伪 segments"""
    import re
    sentences = re.split(r'[。！？\n]', text)
    sentences = [s.strip() for s in sentences if s.strip()]

    segments = []
    current_time = 0.0
    for sent in sentences:
        duration = max(2.0, min(len(sent) * 0.3, 8.0))
        segments.append({
            "start": current_time,
            "end": current_time + duration,
            "text": sent
        })
        current_time += duration
    return segments


def cloud_asr(audio_path, log_fn=None):
    """
    调用云端 ASR API 识别音频，返回 SRT 格式字幕
    自动分段：长音频切为 60s 段分别识别，合并结果

    Args:
        audio_path: WAV/MP3 音频文件路径
        log_fn: 日志回调

    Returns:
        SRT 文本内容（str），失败返回 None
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    settings = load_asr_settings()
    api_key = settings.get("api_key", "")
    base_url = settings.get("base_url", "").rstrip("/")
    asr_model = settings.get("asr_model", "whisper-large-v3")

    if not api_key or not base_url:
        _log("⚠️ 云端语音识别未配置，请填写 ASR Key 和 URL，或关闭云端ASR使用本地识别")
        return None

    if not os.path.exists(audio_path):
        _log(f"云端ASR: 音频文件不存在 {audio_path}")
        return None

    _log(f"云端ASR: 调用识别服务 ({asr_model})...")

    try:
        # 分段切割音频
        chunks = _split_audio_ffmpeg(audio_path, CHUNK_SECONDS, OVERLAP_SECONDS, _log)

        if len(chunks) == 1:
            # 短音频：直接识别
            segments = _call_asr_api(chunks[0][0], settings, _log)
        else:
            # 长音频：逐段识别（第一段先测密度，不行立即回退）
            chunk_results = []
            for i, (chunk_path, offset) in enumerate(chunks):
                _log(f"云端ASR分段: 识别第 {i+1}/{len(chunks)} 段 (偏移 {offset:.0f}s)...")
                try:
                    segs = _call_asr_api(chunk_path, settings, _log)
                except Exception as e:
                    _log(f"⚠️ 云端识别第 {i+1} 段失败，正在重试...")
                    segs = []

                # 第一段测密度：如果太低立即放弃，不浪费后续 API 调用
                if i == 0 and segs:
                    # 第一段时长：用实际段长或默认 chunk 时长
                    if len(chunks) > 1:
                        first_dur = chunks[0][1] if chunks[0][1] > 0 else CHUNK_SECONDS
                    else:
                        first_dur = _get_audio_duration(audio_path)
                    if first_dur > 0:
                        first_density = len(segs) / (first_dur / 60)
                        if first_density < 8:
                            remaining = len(chunks) - 1
                            _log(f"云端ASR分段: 首段密度 {first_density:.1f}条/分钟 过低(需≥8)")
                            _log(f"云端ASR分段: 跳过剩余 {remaining} 段，回退Whisper")
                            # 清理临时文件
                            for cp, _ in chunks:
                                try: os.remove(cp)
                                except: pass
                            try:
                                d = os.path.dirname(chunks[0][0])
                                if d and os.path.isdir(d): os.rmdir(d)
                            except: pass
                            return None

                chunk_results.append((segs, offset))

            # 合并所有段的结果
            segments = _merge_segments_results(chunk_results, _log)

        # 清理临时分段文件
        if len(chunks) > 1:
            for chunk_path, _ in chunks:
                try:
                    os.remove(chunk_path)
                except Exception:
                    pass
            # 尝试删除临时目录
            try:
                chunk_dir = os.path.dirname(chunks[0][0])
                if chunk_dir and os.path.isdir(chunk_dir):
                    os.rmdir(chunk_dir)
            except Exception:
                pass

        if not segments:
            _log("云端ASR: 未返回任何内容")
            return None

        # 转为 SRT 格式
        srt_lines = []
        for i, seg in enumerate(segments, 1):
            start = float(seg.get("start", 0))
            end = float(seg.get("end", start + 3))
            text = seg.get("text", "").strip()

            if not text:
                continue

            srt_lines.append(str(i))
            srt_lines.append(_sec_to_srt_time(start) + " --> " + _sec_to_srt_time(end))
            srt_lines.append(text)
            srt_lines.append("")

        srt_content = "\n".join(srt_lines)
        seg_count = len(srt_lines) // 4
        _log(f"云端ASR: 生成 {seg_count} 条字幕")

        # 质量门控：字幕密度过低说明 ASR 把多句合并成大段，无法用于精细选片
        # 短音频(<120s)跳过检查，通常是后处理阶段
        duration = _get_audio_duration(audio_path)
        if duration > 0 and duration >= 120:
            density = seg_count / (duration / 60)
            if density < 8:
                _log(f"云端ASR: 密度 {density:.1f}条/分钟 过低(需≥8)，自动回退Whisper")
                return None

        return srt_content

    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if e.code == 401:
            _log(f"⚠️ 云端语音识别失败：API Key 无效或已过期，请检查 ASR 设置")
        elif e.code == 404:
            _log(f"云端ASR: 接口地址不存在 (HTTP 404)，请检查 Base URL")
            _log(f"云端ASR: 错误详情: {error_body}")
        elif e.code == 413:
            _log(f"⚠️ 云端语音识别失败：音频文件过大，已自动切换到本地识别")
        else:
            _log(f"⚠️ 云端语音识别失败 (HTTP {e.code})，已自动切换到本地识别")
        return None
    except Exception as e:
        _log(f"云端ASR: 调用失败: {e}")
        return None


def _sec_to_srt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _text_to_srt(text):
    """当 API 只返回纯文本时，按句号分段生成 SRT"""
    segments = _text_to_segments(text)

    srt_lines = []
    for i, seg in enumerate(segments, 1):
        srt_lines.append(str(i))
        srt_lines.append(_sec_to_srt_time(seg["start"]) + " --> " + _sec_to_srt_time(seg["end"]))
        srt_lines.append(seg["text"])
        srt_lines.append("")

    return "\n".join(srt_lines)
