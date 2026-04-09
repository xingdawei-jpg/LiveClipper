"""
语音识别模块：用 faster_whisper 从视频音频自动生成 SRT 字幕
"""

import os
import re
import subprocess
import tempfile

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

sys_path = os.path.dirname(os.path.abspath(__file__))
if sys_path not in __import__('sys').path:
    __import__('sys').path.insert(0, sys_path)

from config import FFMPEG_PATH


def get_ffmpeg_cmd():
    if FFMPEG_PATH and os.path.exists(FFMPEG_PATH):
        return FFMPEG_PATH
    return "ffmpeg"


def extract_audio(video_path, output_wav, log_fn=None):
    """用 FFmpeg 从视频提取 16kHz 单声道 WAV 音频"""
    def _log(msg):
        if log_fn:
            log_fn(msg)

    ffmpeg = get_ffmpeg_cmd()
    cmd = [
        ffmpeg, "-y",
        "-i", video_path,
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        output_wav
    ]

    _log("正在提取音频...")
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace"
    , creationflags=_NO_WINDOW)

    if result.returncode != 0 or not os.path.exists(output_wav):
        _log(f"音频提取失败: {result.stderr[:200]}")
        return False

    size_mb = os.path.getsize(output_wav) / (1024 * 1024)
    _log(f"音频提取完成 ({size_mb:.1f}MB)")
    return True


def _ensure_whisper_model(model_size="small", log_fn=None):
    """确保 Whisper 模型已下载，支持多镜像源自动切换和重试"""
    import os as _os
    from huggingface_hub import scan_cache_dir

    # 检查本地是否已有缓存
    try:
        cache = scan_cache_dir()
        for repo in cache.repos:
            if f"faster-whisper-{model_size}" in repo.repo_id:
                if log_fn: log_fn(f"Whisper {model_size} 模型已缓存，跳过下载")
                return True
    except Exception:
        pass

    # 需要下载：尝试多个镜像源
    mirrors = [
        ("hf-mirror.com", "https://hf-mirror.com"),
        ("HuggingFace 官方", "https://huggingface.co"),
    ]
    for mirror_name, mirror_url in mirrors:
        _os.environ['HF_ENDPOINT'] = mirror_url
        if log_fn: log_fn(f"尝试从 {mirror_name} 下载 Whisper 模型...")
        try:
            from faster_whisper import WhisperModel
            # 触发下载
            _m = WhisperModel(model_size, device="cpu", compute_type="int8")
            del _m
            if log_fn: log_fn(f"✅ Whisper {model_size} 模型下载成功（{mirror_name}）")
            return True
        except Exception as e:
            if log_fn: log_fn(f"⚠️ {mirror_name} 下载失败: {e}")
            continue

    if log_fn: log_fn("❌ 所有镜像源均下载失败")
    return False


def transcribe_to_srt(audio_path, srt_output, log_fn=None, whisper_model="small"):
    """用 faster_whisper 识别音频，输出 SRT 字幕文件（支持多镜像源自动切换）"""
    import os as _os

    def _log(msg):
        if log_fn:
            log_fn(msg)

    try:
        from faster_whisper import WhisperModel
    except ImportError as _e:
        _log(f"Whisper 导入失败: {_e}")
        return False

    # 多镜像源尝试加载模型
    mirrors = [
        ("hf-mirror.com", "https://hf-mirror.com"),
        ("HuggingFace 官方", "https://huggingface.co"),
    ]
    model = None
    for mirror_name, mirror_url in mirrors:
        _os.environ['HF_ENDPOINT'] = mirror_url
        try:
            _log(f"正在加载语音识别模型 ({whisper_model})...")
            model = WhisperModel(whisper_model, device="cpu", compute_type="int8")
            break  # 成功加载，跳出循环
        except Exception as _e:
            _log(f"⚠️ {mirror_name} 加载失败，尝试下一个源...")
            continue

    if model is None:
        _log("❌ Whisper 模型下载失败，所有镜像源均不可用")
        _log("💡 建议：1) 检查网络连接后重试  2) 开启云端ASR（火山引擎）")
        return False

    _log("正在识别语音（可能需要几分钟，请耐心等待）...")

    segments_iter, info = model.transcribe(
        audio_path,
        language="zh",
        beam_size=5,
        vad_filter=True,
    )

    _log(f"识别语言: {info.language} (概率: {info.language_probability:.2f})")

    # 逐段收集，给出进度反馈
    segments = []
    last_progress_time = 0
    for seg in segments_iter:
        segments.append(seg)
        if seg.end > last_progress_time + 60:
            _log(f"  已识别到 {int(seg.end)}s...")
            last_progress_time = seg.end

    _log(f"识别完成，共 {len(segments)} 条语音段")

    # 生成 SRT 内容
    srt_lines = []
    for i, seg in enumerate(segments, 1):
        start = seg.start
        end = seg.end
        text = seg.text.strip()

        if not text:
            continue

        start_h = int(start // 3600)
        start_m = int((start % 3600) // 60)
        start_s = int(start % 60)
        start_ms = int((start % 1) * 1000)

        end_h = int(end // 3600)
        end_m = int((end % 3600) // 60)
        end_s = int(end % 60)
        end_ms = int((end % 1) * 1000)

        srt_lines.append(f"{i}")
        srt_lines.append(
            f"{start_h:02d}:{start_m:02d}:{start_s:02d},{start_ms:03d}"
            f" --> "
            f"{end_h:02d}:{end_m:02d}:{end_s:02d},{end_ms:03d}"
        )
        srt_lines.append(text)
        srt_lines.append("")

    srt_content = "\n".join(srt_lines)

    with open(srt_output, "w", encoding="utf-8") as f:
        f.write(srt_content)

    _log(f"字幕生成完成: {len(segments)} 条 -> {os.path.basename(srt_output)}")
    return True


def generate_srt(video_path, log_fn=None, whisper_model="small"):
    """
    从视频自动生成 SRT 字幕文件。
    返回 SRT 文件路径，失败返回 None。
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    temp_dir = os.path.join(tempfile.gettempdir(), "live_cutter_stt")
    os.makedirs(temp_dir, exist_ok=True)

    # 生成临时文件名（用系统临时目录避免中文路径）
    import hashlib
    video_hash = hashlib.md5(video_path.encode("utf-8")).hexdigest()[:8]
    wav_path = os.path.join(temp_dir, f"audio_{video_hash}.wav")
    srt_path = os.path.join(temp_dir, f"sub_{video_hash}.srt")

    # 提取音频
    if not extract_audio(video_path, wav_path, log_fn):
        return None

    # 语音识别
    if not transcribe_to_srt(wav_path, srt_path, log_fn, whisper_model=whisper_model):
        return None

    # 清理临时音频文件
    try:
        os.remove(wav_path)
    except Exception:
        pass

    return srt_path


def cleanup_srt(srt_path):
    """清理临时 SRT 文件"""
    try:
        if srt_path and os.path.exists(srt_path):
            os.remove(srt_path)
    except Exception:
        pass
