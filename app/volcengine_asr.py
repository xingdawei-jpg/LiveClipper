# -*- coding: utf-8 -*-
"""
火山引擎 ASR 封装 — 通过 TOS 上传音频后调用大模型语音识别
"""

import os
import sys
import time
import json
import uuid

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
    poll_interval = 10  # 火山引擎QPS低，10秒轮询避免429
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
                # 正常处理中，继续轮询
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
                continue
            # status_code为空且message为空：可能是异常响应，最多等3轮
            elif not status_code and not message:
                _empty_count += 1
                if _empty_count >= 3:
                    _log("volcengine_asr: 连续3次空响应，终止轮询")
                    _cleanup_tos(_tos_client, bucket, obj_key, _log)
                    return None
                continue
            
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
