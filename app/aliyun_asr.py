# -*- coding: utf-8 -*-
"""
阿里云 DashScope ASR 封装 — 使用 paraformer-v2 模型（字级时间戳）

API 流程（异步模式）：
1. 上传音频文件到阿里云 OSS（获取公网URL）
2. 提交识别任务（POST /api/v1/services/audio/asr/transcription）
3. 轮询任务状态（GET /api/v1/tasks/{task_id}）
4. 解析结果，提取 segments

认证：DashScope API Key（sk-xxx）
"""

import json
import os
import sys
import subprocess
import time
import hashlib
import tempfile

_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# DashScope API
DASHSCOPE_BASE = "https://dashscope.aliyuncs.com"
ASR_URL = f"{DASHSCOPE_BASE}/api/v1/services/audio/asr/transcription"
TASK_URL = f"{DASHSCOPE_BASE}/api/v1/tasks"

# 最大轮询时间（秒）
MAX_POLL_TIME = 300
# 轮询间隔（秒）
POLL_INTERVAL = 3


def _get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _curl_get(url, api_key, timeout=30):
    """用 curl GET 请求"""
    r = subprocess.run(
        ["curl.exe", "-s", "-k", "--max-time", str(timeout),
         "-X", "GET", url,
         "-H", f"Authorization: Bearer {api_key}"],
        capture_output=True, timeout=timeout + 5,
        creationflags=_NO_WINDOW
    )
    return json.loads(r.stdout.decode("utf-8", errors="replace"))


def _curl_post_json(url, api_key, body, timeout=30):
    """用 curl POST JSON 请求"""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8")
    json.dump(body, tmp, ensure_ascii=False)
    tmp.close()
    try:
        r = subprocess.run(
            ["curl.exe", "-s", "-k", "--max-time", str(timeout),
             "-X", "POST", url,
             "-H", f"Authorization: Bearer {api_key}",
             "-H", "Content-Type: application/json",
             "-H", "X-DashScope-Async: enable",
             "-d", f"@{tmp.name}"],
            capture_output=True, timeout=timeout + 5,
            creationflags=_NO_WINDOW
        )
        return json.loads(r.stdout.decode("utf-8", errors="replace"))
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _upload_to_oss(audio_path, oss_config, log_fn=None):
    """
    上传音频文件到阿里云 OSS，返回签名 URL
    
    oss_config: {
        "access_key_id": "...",
        "access_key_secret": "...",
        "bucket": "...",
        "endpoint": "oss-cn-beijing.aliyuncs.com"
    }
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    file_hash = hashlib.md5(audio_path.encode("utf-8")).hexdigest()[:8]
    ext = os.path.splitext(audio_path)[1] or ".wav"
    object_key = f"asr/{file_hash}_{int(time.time())}{ext}"

    ak = oss_config.get("access_key_id", "")
    sk = oss_config.get("access_key_secret", "")
    bucket = oss_config.get("bucket", "")
    endpoint = oss_config.get("endpoint", "oss-cn-beijing.aliyuncs.com")

    if not all([ak, sk, bucket]):
        _log("aliyun_asr: OSS 配置不完整，需要 access_key_id/access_key_secret/bucket")
        return None

    _log(f"aliyun_asr: 上传音频到 OSS ({bucket}/{object_key})...")

    try:
        result = _oss_upload_and_sign(audio_path, ak, sk, bucket, endpoint, object_key, _log)
        if result:
            _log(f"aliyun_asr: OSS 上传成功，签名 URL 已生成")
        return result
    except Exception as e:
        _log(f"aliyun_asr: OSS 上传失败: {e}")
        return None


def _oss_upload_and_sign(file_path, ak, sk, bucket, endpoint, object_key, log_fn):
    """上传文件到 OSS 并生成签名 URL"""
    import hmac
    import base64
    
    content_type = "audio/wav"
    date_str = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    
    # V1 签名上传
    canonicalized_resource = f"/{bucket}/{object_key}"
    string_to_sign = f"PUT\n\n{content_type}\n{date_str}\n{canonicalized_resource}"
    signature = base64.b64encode(
        hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    
    url = f"https://{bucket}.{endpoint}/{object_key}"
    
    r = subprocess.run(
        ["curl.exe", "-s", "-k", "--max-time", "60",
         "-X", "PUT", url,
         "-H", f"Authorization: OSS {ak}:{signature}",
         "-H", f"Date: {date_str}",
         "-H", f"Content-Type: {content_type}",
         "--data-binary", f"@{file_path}"],
        capture_output=True, timeout=65,
        creationflags=_NO_WINDOW
    )
    
    # 检查上传结果
    resp_text = r.stdout.decode("utf-8", errors="replace")
    if r.returncode != 0 and "<?xml" not in resp_text:
        # curl 本身失败
        log_fn(f"aliyun_asr: OSS curl 失败: returncode={r.returncode}")
        return None
    
    # 生成 V1 签名 URL（有效 1 小时）
    expires = int(time.time()) + 3600
    sign_str = f"GET\n\n\n{expires}\n{canonicalized_resource}"
    sig = base64.b64encode(
        hmac.new(sk.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha1).digest()
    ).decode("utf-8")
    # URL encode the signature
    import urllib.parse
    sig_encoded = urllib.parse.quote(sig, safe="")
    
    signed_url = f"{url}?OSSAccessKeyId={ak}&Expires={expires}&Signature={sig_encoded}"
    log_fn(f"aliyun_asr: OSS 上传完成，签名 URL 已生成")
    return signed_url


def aliyun_asr(audio_path, app_key=None, model=None, timeout=300, log_fn=None,
               oss_ak=None, oss_sk=None, oss_bucket=None, oss_endpoint=None):
    """
    调用阿里云 DashScope ASR（paraformer-v2）识别音频，返回 segments 列表。

    Args:
        audio_path: 音频文件路径（WAV/MP3）
        app_key: DashScope API Key
        model: 模型名（默认 paraformer-v2）
        timeout: 最大等待秒数
        log_fn: 日志回调
        oss_ak: 阿里云 OSS AccessKey ID（上传音频文件用）
        oss_sk: 阿里云 OSS AccessKey Secret
        oss_bucket: OSS Bucket 名称
        oss_endpoint: OSS Endpoint（默认 oss-cn-beijing.aliyuncs.com）

    Returns:
        list[dict] 格式 [{"start": float, "end": float, "text": str}, ...]
        失败返回 None
    """
    import base64  # needed for OSS signing
    
    def _log(msg):
        if log_fn:
            log_fn(msg)

    if not app_key:
        _log("aliyun_asr: 未配置 API Key")
        return None
    if not os.path.exists(audio_path):
        _log(f"aliyun_asr: 音频文件不存在 {audio_path}")
        return None
    if not model:
        model = "paraformer-v2"

    _log(f"aliyun_asr: 开始处理 ({model})...")

    # Step 1: 上传文件到 OSS，获取签名 URL
    audio_url = None
    if oss_ak and oss_sk and oss_bucket:
        oss_config = {
            "access_key_id": oss_ak,
            "access_key_secret": oss_sk,
            "bucket": oss_bucket,
            "endpoint": oss_endpoint or "oss-cn-beijing.aliyuncs.com",
            "api_key": app_key,
        }
        audio_url = _upload_to_oss(audio_path, oss_config, _log)
    else:
        _log("aliyun_asr: 未配置 OSS，无法上传音频文件")
        _log("aliyun_asr: 请在 AI 设置中配置 OSS AccessKey/Bucket/Endpoint")
        return None

    if not audio_url:
        _log("aliyun_asr: OSS 上传失败，无法获取音频 URL")
        return None

    # Step 2: 提交识别任务
    _log("aliyun_asr: 提交识别任务...")
    body = {
        "model": model,
        "input": {
            "file_urls": [audio_url]
        },
        "parameters": {
            "format": os.path.splitext(audio_path)[1].lstrip(".") or "wav",
            "sample_rate": 16000,
            "enable_words": True,
        }
    }

    result = _curl_post_json(ASR_URL, app_key, body, timeout=30)
    task_id = result.get("output", {}).get("task_id", "")
    
    if not task_id:
        err_msg = result.get("message", "unknown error")
        _log(f"aliyun_asr: 提交任务失败 - {err_msg}")
        return None

    _log(f"aliyun_asr: 任务已提交, task_id={task_id}")

    # Step 3: 轮询任务状态
    start_time = time.time()
    while time.time() - start_time < MAX_POLL_TIME:
        time.sleep(POLL_INTERVAL)
        status = _curl_get(f"{TASK_URL}/{task_id}", app_key)
        task_status = status.get("output", {}).get("task_status", "")
        
        if task_status == "SUCCEEDED":
            _log("aliyun_asr: 识别完成")
            return _parse_result(status, _log)
        elif task_status == "FAILED":
            err_msg = status.get("output", {}).get("message", "unknown")
            _log(f"aliyun_asr: 识别失败 - {err_msg}")
            return None
        elif task_status in ("PENDING", "RUNNING"):
            elapsed = int(time.time() - start_time)
            _log(f"aliyun_asr: 识别中... ({elapsed}s)")
        else:
            _log(f"aliyun_asr: 未知状态 {task_status}")
            return None

    _log("aliyun_asr: 轮询超时")
    return None


def _parse_result(status_data, log_fn=None):
    """解析 DashScope ASR 结果，提取 segments"""
    results = status_data.get("output", {}).get("results", [])
    if not results:
        if log_fn:
            log_fn("aliyun_asr: 结果为空")
        return None

    segments = []
    for result in results:
        subtask_status = result.get("subtask_status", "")
        if subtask_status != "SUCCEEDED":
            continue
        
        # 获取转录结果URL
        transcription_url = result.get("transcription_url", "")
        if not transcription_url:
            continue

        # 下载并解析结果
        try:
            import urllib.request
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(transcription_url)
            with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                trans_data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            if log_fn:
                log_fn(f"aliyun_asr: 下载结果失败 - {e}")
            continue

        # 解析 transcripts
        transcripts = trans_data.get("transcripts", [])
        # 断句标点集合：句号/问号/感叹号/逗号/顿号/分号
        _SPLIT_PUNCS = set("。！？，、；")
        for transcript in transcripts:
            # 优先用 words（字级时间戳）按标点细切，断句更精确
            words = transcript.get("words", [])
            if words:
                current_text = ""
                start_time = None
                for w in words:
                    if start_time is None:
                        start_time = w.get("begin_time", 0) / 1000.0
                    current_text += w.get("text", "")
                    # 遇到断句标点就切分
                    if w.get("text", "") in _SPLIT_PUNCS:
                        segments.append({
                            "start": start_time,
                            "end": w.get("end_time", 0) / 1000.0,
                            "text": current_text.strip(),
                        })
                        current_text = ""
                        start_time = None
                # 剩余未断句的内容
                if current_text.strip() and start_time is not None:
                    segments.append({
                        "start": start_time,
                        "end": words[-1].get("end_time", 0) / 1000.0,
                        "text": current_text.strip(),
                    })
            else:
                # 没有 words 时回退到 sentences（句级，粒度较粗）
                sentences = transcript.get("sentences", [])
                for sent in sentences:
                    text = sent.get("text", "").strip()
                    if not text:
                        continue
                    segments.append({
                        "start": sent.get("begin_time", 0) / 1000.0,
                        "end": sent.get("end_time", 0) / 1000.0,
                        "text": text,
                    })

    if log_fn:
        log_fn(f"aliyun_asr: 解析完成，{len(segments)} 条语音段")
    return segments if segments else None


def aliyun_asr_to_srt(audio_path, app_key=None, model=None, timeout=300, log_fn=None,
                       oss_ak=None, oss_sk=None, oss_bucket=None, oss_endpoint=None):
    """调用阿里云 ASR 识别音频，返回 SRT 格式字幕。"""
    segments = aliyun_asr(audio_path, app_key, model, timeout, log_fn,
                          oss_ak, oss_sk, oss_bucket, oss_endpoint)
    if not segments:
        return None

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

    return "\n".join(srt_lines)


def _sec_to_srt_time(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
