# -*- coding: utf-8 -*-
"""
火山引擎 ASR 封装 — 通过 TOS 上传音频后调用大模型语音识别
"""

import os
import time
import json
import uuid


def volcengine_asr(audio_path, app_id, access_token, tos_ak, tos_sk,
                   bucket="livec", timeout=300, log_fn=None):
    """
    调用火山引擎大模型 ASR 识别音频文件，返回 segments 列表。
    
    Args:
        audio_path: 音频文件路径
        app_id: 火山引擎 APP ID
        access_token: 火山引擎 Access Token
        tos_ak: TOS Access Key ID
        tos_sk: TOS Secret Access Key
        bucket: TOS bucket 名 (默认 livec)
        timeout: 最大等待秒数 (默认 300)
        log_fn: 日志回调函数
    
    Returns:
        list[dict] 格式 [{"start": float, "end": float, "text": str}, ...]
        失败返回 None
    """
    def _log(msg):
        if log_fn:
            log_fn(msg)

    try:
        import tos
    except ImportError:
        _log("volcengine_asr: tos SDK 未安装，跳过")
        return None

    if not all([app_id, access_token, tos_ak, tos_sk]):
        _log("volcengine_asr: 配置不完整，跳过")
        return None

    # 生成 TOS 上的临时对象 key
    ext = os.path.splitext(audio_path)[1].lower()
    if not ext:
        ext = ".wav"
    obj_key = f"asr_temp/{uuid.uuid4().hex}{ext}"

    # --- 1. 上传音频到 TOS ---
    _log(f"volcengine_asr: 上传音频到 TOS ({bucket}/{obj_key})...")
    try:
        client = tos.TosClientV2(
            ak=tos_ak,
            sk=tos_sk,
            endpoint="tos-cn-beijing.volces.com",
            region="cn-beijing",
        )
        client.put_object_from_file(bucket, obj_key, audio_path)
        _log("volcengine_asr: TOS 上传完成")
    except Exception as e:
        _log(f"volcengine_asr: TOS 上传失败: {e}")
        return None

    # 获取 pre_signed_url
    try:
        url_resp = client.pre_signed_url(
            tos.HttpMethodType.Http_Method_Get, bucket, obj_key, 3600
        )
        audio_url = url_resp.signed_url
        _log(f"volcengine_asr: 获取 pre_signed_url 成功")
    except Exception as e:
        _log(f"volcengine_asr: 获取 pre_signed_url 失败: {e}")
        _cleanup_tos(client, bucket, obj_key, _log)
        return None

    # --- 2. 提交 ASR 任务 ---
    import uuid as _uuid
    task_id = str(_uuid.uuid4())
    submit_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
    headers = {
        "Content-Type": "application/json",
        "X-Api-App-Key": str(app_id),
        "X-Api-Access-Key": access_token,
        "X-Api-Resource-Id": "volc.bigasr.auc",
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
            _cleanup_tos(client, bucket, obj_key, _log)
            return None
        
        _log(f"volcengine_asr: 任务已提交, id={task_id}")
    except Exception as e:
        _log(f"volcengine_asr: 提交异常: {e}")
        _cleanup_tos(client, bucket, obj_key, _log)
        return None

    # --- 3. 轮询结果 ---
    query_url = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
    start_time = time.time()
    poll_interval = 5

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

            if "Processing" in message or "PENDING" in str(message).upper():
                continue
            elif "silence" in message.lower() or "no valid speech" in message.lower():
                _log(f"volcengine_asr: 音频无有效语音: {message}")
                _cleanup_tos(client, bucket, obj_key, _log)
                return None
            elif status_code != "20000000":
                _log(f"volcengine_asr: 查询失败: status={status_code} msg={message}")
                _cleanup_tos(client, bucket, obj_key, _log)
                return None
            
            # --- 4. 解析结果 ---
            _log("volcengine_asr: 识别完成，解析结果...")
            data = json.loads(qr)
            result = data.get("result", {})
            utterances = result.get("utterances", [])

            segments = []
            for utt in utterances:
                text = utt.get("text", "").strip()
                if not text:
                    continue
                utt_start = utt.get("start_time", 0) / 1000.0  # ms -> s
                utt_end = utt.get("end_time", 0) / 1000.0
                if utt_end <= utt_start:
                    continue
                segments.append({"start": utt_start, "end": utt_end, "text": text})

            _log(f"volcengine_asr: 解析得到 {len(segments)} 条语音段")
            _cleanup_tos(client, bucket, obj_key, _log)
            return segments if segments else None

        except Exception as e:
            _log(f"volcengine_asr: 轮询异常: {e}")
            continue

    _log(f"volcengine_asr: 超时 ({timeout}s)")
    _cleanup_tos(client, bucket, obj_key, _log)
    return None


def _cleanup_tos(client, bucket, obj_key, _log):
    """删除 TOS 上的临时文件"""
    try:
        client.delete_object(bucket, obj_key)
        _log("volcengine_asr: 已清理 TOS 临时文件")
    except Exception:
        pass
