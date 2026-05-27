# -*- coding: utf-8 -*-
"""阿里云 Qwen3-ASR 封装 — 使用 qwen3-asr-flash-filetrans 异步 API（支持长音频+时间戳）"""
import json, os, time, ssl, urllib.request

ASR_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/"


def qwen_asr(audio_path, api_key=None, oss_ak=None, oss_sk=None,
             oss_bucket=None, oss_endpoint=None, timeout=600, log_fn=print):
    """使用 Qwen3-ASR-Filetrans 识别音频，返回 segments 列表

    返回: [{"start": float秒, "end": float秒, "text": str}, ...]
    """
    def _log(msg):
        if log_fn: log_fn(msg)

    if not all([api_key, oss_ak, oss_sk, oss_bucket]):
        _log("qwen_asr: 缺少API配置")
        return None

    # 1. 上传到OSS并获取签名URL
    from aliyun_asr import _oss_upload_and_sign
    obj_key = f"asr/qwen_{os.path.basename(audio_path)}_{int(time.time())}"
    audio_url = _oss_upload_and_sign(audio_path, oss_ak, oss_sk, oss_bucket,
                                      oss_endpoint or "oss-cn-beijing.aliyuncs.com",
                                      obj_key, _log)
    if not audio_url:
        _log("qwen_asr: OSS上传失败")
        return None

    # 2. 提交异步任务
    body = {
        "model": "qwen3-asr-flash-filetrans",
        "input": {"file_url": audio_url},
        "parameters": {"channel_id": [0], "enable_itn": False, "enable_words": True}
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable"
    }
    req = urllib.request.Request(ASR_URL, data=json.dumps(body).encode(),
                                  headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read().decode())
        task_id = result.get("output", {}).get("task_id", "")
        if not task_id:
            _log(f"qwen_asr: 提交失败 - {result.get('message', '')}")
            return None
        _log(f"qwen_asr: 任务已提交, id={task_id}")
    except Exception as e:
        _log(f"qwen_asr: 提交异常: {e}")
        return None

    # 3. 轮询
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    start = time.time()
    poll_interval = 5
    while time.time() - start < timeout:
        time.sleep(poll_interval)
        elapsed = int(time.time() - start)
        _log(f"qwen_asr: 轮询中 ({elapsed}s)...")
        req = urllib.request.Request(f"{TASK_URL}{task_id}",
                                      headers={"Authorization": f"Bearer {api_key}",
                                               "X-DashScope-Async": "enable"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
                st = json.loads(r.read().decode())
        except Exception as e:
            _log(f"qwen_asr: 轮询异常: {e}")
            continue

        ts = st.get("output", {}).get("task_status", "")
        if ts == "SUCCEEDED":
            tu = st.get("output", {}).get("result", {}).get("transcription_url", "")
            if not tu:
                _log("qwen_asr: 完成但无结果URL")
                return None
            try:
                with urllib.request.urlopen(urllib.request.Request(tu), context=ctx, timeout=30) as tr:
                    td = json.loads(tr.read().decode())
            except Exception as e:
                _log(f"qwen_asr: 下载结果失败: {e}")
                return None

            segments = []
            for t in td.get("transcripts", []):
                for s in t.get("sentences", []):
                    segments.append({
                        "start": s.get("start_time", 0) / 1000.0,
                        "end": s.get("end_time", 0) / 1000.0,
                        "text": s.get("text", "").strip()
                    })
            _log(f"qwen_asr: 识别完成，{len(segments)} 条语音段")
            return segments

        elif ts == "FAILED":
            _log(f"qwen_asr: 识别失败")
            return None
        elif ts in ("PENDING", "RUNNING"):
            continue
        else:
            _log(f"qwen_asr: 未知状态 {ts}")
            return None

    _log(f"qwen_asr: 超时 ({timeout}s)")
    return None
