# -*- coding: utf-8 -*-
"""
客户端激活验证模块（离线 HMAC + 飞书多维表格设备绑定）

- 激活码本地 HMAC 验证
- 设备绑定通过飞书多维表格校验（一码一机）
- 解绑需在原设备操作，或联系管理员

使用方式：
  from license_client import check_activation, activate_with_code, deactivate_device
"""

import hmac
import hashlib
import time
import json
import os
import sys
import platform
import uuid
import urllib.request
import urllib.error
from datetime import datetime


# ============================================================
# 密钥（与 license_generator.py 一致）
# ============================================================
_K1 = "6c63386633613265"
_K2 = "3764316239633666"
_K3 = "3465306135643863"
_K4 = "3366376232653961"
_K5 = "3164346336663065"
_K6 = "3862336135643763"
_K7 = "3966326534623661"
_K8 = "38643063336637"

def _get_key():
    return bytes.fromhex(_K1 + _K2 + _K3 + _K4 + _K5 + _K6 + _K7 + _K8).decode()

SECRET_KEY = _get_key()

# ============================================================
# 飞书多维表格配置（设备绑定服务端）
# 凭据以混淆 hex 存储，防止简单字符串搜索
# ============================================================
# TODO: David 需要在飞书开放平台创建一个应用，获取 app_id 和 app_secret
# 然后把 hex 编码后的值填入下面
# 编码方法: python -c "print(''.join(f'{ord(c):02x}' for c in '你的值'))"
_FS_APP_ID_HEX = "636c695f61393532633963373039373935626339"       # 飞书应用 app_id 的 hex 编码
_FS_APP_SECRET_HEX = "683930746c554c56676e7055544c46436468325533756a443670386d78754a45"   # 飞书应用 app_secret 的 hex 编码

_BITABLE_APP_TOKEN = "Ree8bSUX3aYUiesdr7WcTlJqnge"
_BITABLE_TABLE_ID = "tblWZH21Y2cXotHw"
_FIELD_CODE = "fld4Vbhds7"         # 激活码
_FIELD_DEVICE = "fldPfARnF7"       # 设备ID
_FIELD_DEVICE_INFO = "fldZ09gLPn"  # 设备信息

CACHE_FILE = "license_cache.json"
LICENSE_FILE = "license.dat"
TRIAL_USES = 10

PLAN_NAMES = {"01": "月付", "02": "季付", "03": "年付"}
PLAN_DAYS = {"01": 30, "02": 90, "03": 365}


def _get_base_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_data_path():
    from platform_config import LICENSE_CACHE_DIR
    data_dir = LICENSE_CACHE_DIR
    if not os.path.exists(data_dir):
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception:
            pass
    return data_dir


def _load_cache():
    path = os.path.join(_get_data_path(), CACHE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data):
    path = os.path.join(_get_data_path(), CACHE_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _save_license_code(code):
    path = os.path.join(_get_data_path(), LICENSE_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code.strip())
        return True
    except Exception:
        return False


def _load_license_code():
    path = os.path.join(_get_data_path(), LICENSE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


# ============================================================
# 飞书 API 调用
# ============================================================

def _hex_decode(hex_str):
    """hex 解码"""
    try:
        return bytes.fromhex(hex_str).decode("utf-8") if hex_str else ""
    except Exception:
        return ""


def _get_feishu_token():
    """获取飞书 app_access_token"""
    app_id = _hex_decode(_FS_APP_ID_HEX)
    app_secret = _hex_decode(_FS_APP_SECRET_HEX)
    if not app_id or not app_secret:
        return None
    try:
        url = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
        data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            if result.get("code") == 0:
                return result.get("app_access_token")
    except Exception:
        pass
    return None


def _feishu_request(method, path, body=None):
    """调用飞书 API（自动带 token）"""
    token = _get_feishu_token()
    if not token:
        return None
    try:
        url = f"https://open.feishu.cn/open-apis{path}"
        raw = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=raw, method=method,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _query_device_binding(code):
    """查询激活码的设备绑定，返回 {record_id, machine_id} 或 None"""
    try:
        raw_code = code.replace("-", "").strip().lower()
        # 飞书 filter 必须用字段名（中文），不能用 field_id
        import urllib.parse as _urlp
        filt = _urlp.quote(f'CurrentValue.[激活码]="{raw_code}"', safe="")
        resp = _feishu_request("GET",
            f"/bitable/v1/apps/{_BITABLE_APP_TOKEN}/tables/{_BITABLE_TABLE_ID}/records"
            f"?filter={filt}&page_size=5")
        if resp and resp.get("code") == 0:
            items = resp.get("data", {}).get("items", [])
            if items:
                rec = items[0]
                fields = rec.get("fields", {})
                mid = fields.get("设备ID", "")
                if isinstance(mid, list):
                    mid = mid[0].get("text", "") if mid else ""
                return {"record_id": rec["record_id"], "machine_id": str(mid).strip()}
        return None
    except Exception:
        return None

def _bind_device(code, machine_id, device_info=""):
    """绑定设备（写入或更新多维表格记录）"""
    try:
        raw_code = code.replace("-", "").strip().lower()
        existing = _query_device_binding(code)

        if existing:
            # 更新
            resp = _feishu_request("PUT",
                f"/bitable/v1/apps/{_BITABLE_APP_TOKEN}/tables/{_BITABLE_TABLE_ID}/records/{existing['record_id']}",
                {"fields": {"设备ID": machine_id, "设备信息": device_info}})
            return resp and resp.get("code") == 0
        else:
            # 新增
            resp = _feishu_request("POST",
                f"/bitable/v1/apps/{_BITABLE_APP_TOKEN}/tables/{_BITABLE_TABLE_ID}/records",
                {"fields": {
                    "多行文本": f"用户_{machine_id[:8]}",
                    "激活码": raw_code,
                    "设备ID": machine_id,
                    "设备信息": device_info,
                }})
            return resp and resp.get("code") == 0
    except Exception:
        return False

def _unbind_device(code):
    """解绑设备（清空多维表格中的设备ID字段）"""
    try:
        existing = _query_device_binding(code)
        if existing:
            resp = _feishu_request("PUT",
                f"/bitable/v1/apps/{_BITABLE_APP_TOKEN}/tables/{_BITABLE_TABLE_ID}/records/{existing['record_id']}",
                {"fields": {"设备ID": "", "设备信息": ""}})
            return resp and resp.get("code") == 0
        return True
    except Exception:
        return False

def _get_machine_id():
    raw = f"{platform.processor()}-{platform.node()}-{os.getenv('USERNAME', 'user')}-{uuid.getnode()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _get_device_info():
    try:
        return f"{platform.node()} | {platform.system()} {platform.release()} | {os.getenv('USERNAME', '')}"
    except Exception:
        return "unknown"


# ============================================================
# 试用
# ============================================================

def _get_trial_info():
    cache = _load_cache()
    if cache and cache.get("trial_start") is not None:
        return cache
    return None


def _start_trial():
    cache = _load_cache() or {}
    mid = _get_machine_id()
    cache["trial_start"] = int(time.time())
    cache["trial_machine_id"] = mid
    cache["trial_uses_left"] = TRIAL_USES
    _save_cache(cache)
    return cache


def consume_trial_use():
    trial = _get_trial_info()
    if not trial:
        return -1
    cached_mid = trial.get("trial_machine_id", "")
    current_mid = _get_machine_id()
    if cached_mid and cached_mid != current_mid:
        return -1
    uses_left = trial.get("trial_uses_left", 0)
    if uses_left <= 0:
        return -1
    uses_left -= 1
    trial["trial_uses_left"] = uses_left
    _save_cache(trial)
    return uses_left


def check_trial():
    trial = _get_trial_info()
    if not trial:
        return {"in_trial": False, "uses_left": 0, "total_uses": TRIAL_USES}
    cached_mid = trial.get("trial_machine_id", "")
    current_mid = _get_machine_id()
    if cached_mid and cached_mid != current_mid:
        return {"in_trial": False, "uses_left": 0, "total_uses": TRIAL_USES}
    uses_left = trial.get("trial_uses_left", 0)
    if uses_left > 0:
        return {"in_trial": True, "uses_left": uses_left, "total_uses": TRIAL_USES}
    else:
        return {"in_trial": False, "uses_left": 0, "total_uses": TRIAL_USES}


# ============================================================
# 激活验证核心
# ============================================================

def validate_code(code):
    if not code:
        return {"ok": False, "msg": "激活码为空"}
    raw = code.replace("-", "").strip().lower()
    if len(raw) != 32:
        return {"ok": False, "msg": "激活码格式错误"}
    try:
        plan_hex = raw[0:2]
        expires_hex = raw[2:10]
        nonce_hex = raw[10:12]
        signature = raw[12:32]
        if plan_hex not in PLAN_NAMES:
            return {"ok": False, "msg": "激活码无效（未知套餐）"}
        payload = plan_hex + expires_hex + nonce_hex
        expected_sig = hmac.new(
            SECRET_KEY.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()[:20]
        if not hmac.compare_digest(signature, expected_sig):
            return {"ok": False, "msg": "激活码无效（签名错误）"}
        expires_at = int(expires_hex, 16)
        now = int(time.time())
        if now > expires_at:
            expired_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
            return {"ok": False, "msg": f"激活码已于 {expired_date} 过期"}
        days_left = (expires_at - now) // 86400
        expires_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
        return {
            "ok": True,
            "plan": {"01": "monthly", "02": "quarterly", "03": "yearly"}.get(plan_hex, "monthly"),
            "plan_name": PLAN_NAMES[plan_hex],
            "days": PLAN_DAYS[plan_hex],
            "expires_at": expires_at,
            "expires_date": expires_date,
            "days_left": days_left,
        }
    except Exception as e:
        return {"ok": False, "msg": f"激活码解析失败: {str(e)}"}


def activate_with_code(code):
    """
    激活码激活（含服务端设备绑定校验）
    
    流程：
    1. 本地 HMAC 验证
    2. 查飞书多维表格：激活码是否已绑其他设备
    3. 已绑其他设备 → 拒绝
    4. 绑定当前设备（本地 + 服务端）
    """
    # Step 1: 本地验证
    result = validate_code(code)
    if not result["ok"]:
        return result

    current_mid = _get_machine_id()

    # Step 2: 服务端设备绑定校验
    binding = _query_device_binding(code)
    if binding and binding.get("machine_id"):
        bound_mid = binding["machine_id"]
        if bound_mid and bound_mid != current_mid:
            return {
                "ok": False,
                "msg": "该激活码已绑定其他设备，请先在原设备解绑，或联系管理员",
            }

    # Step 3: 本地保存
    _save_license_code(code)
    _save_cache({
        "code": code,
        "plan": result["plan"],
        "plan_name": result["plan_name"],
        "expires_at": result["expires_at"],
        "expires_date": result["expires_date"],
        "activated_at": int(time.time()),
        "machine_id": current_mid,
    })

    # Step 4: 服务端绑定（非阻塞，失败不回滚本地）
    device_info = _get_device_info()
    _bind_device(code, current_mid, device_info)

    return {"ok": True, "msg": "激活成功", "info": result}


def deactivate_device():
    """
    解绑当前设备
    
    流程：服务端清绑定 → 本地清缓存
    返回: {ok, msg}
    """
    code = _load_license_code()
    if not code:
        return {"ok": False, "msg": "未找到激活码"}

    current_mid = _get_machine_id()
    cache = _load_cache()
    if cache:
        cached_mid = cache.get("machine_id", "")
        if cached_mid and cached_mid != current_mid:
            return {"ok": False, "msg": "当前设备与绑定设备不匹配"}

    # 服务端解绑
    unbind_ok = _unbind_device(code)

    # 清空本地
    _save_cache({})
    lic_path = os.path.join(_get_data_path(), LICENSE_FILE)
    try:
        if os.path.exists(lic_path):
            os.remove(lic_path)
    except Exception:
        pass

    if unbind_ok:
        return {"ok": True, "msg": "设备已解绑，可在新设备上激活"}
    else:
        return {"ok": True, "msg": "本地已解绑（服务端同步失败，请联系管理员）"}


def check_activation():
    """检查当前激活状态（启动时调用）"""
    cache = _load_cache()

    if cache and cache.get("code"):
        code = cache["code"]
        result = validate_code(code)
        if result["ok"]:
            # 启动时静默校验服务端设备绑定
            try:
                binding = _query_device_binding(code)
                if binding and binding.get("machine_id"):
                    if binding["machine_id"] != _get_machine_id():
                        return {"need_activate": True, "reason": "该激活码已在其他设备激活，请重新激活或联系管理员"}
                elif not binding:
                    # 老用户首次更新：自动绑定当前设备到服务端
                    _bind_device(code, _get_machine_id(), _get_device_info())
            except Exception:
                pass
            return {
                "activated": True,
                "plan_name": result["plan_name"],
                "days_left": result["days_left"],
                "expires_date": result["expires_date"],
            }

    code = _load_license_code()
    if code:
        result = validate_code(code)
        cached_mid = _load_cache()
        prev_mid = cached_mid.get("machine_id", "") if cached_mid else ""
        current_mid = _get_machine_id()
        if prev_mid and prev_mid != current_mid:
            return {"need_activate": True, "reason": "设备已更换，请重新激活"}
        if result["ok"]:
            _save_cache({
                "code": code,
                "plan": result["plan"],
                "plan_name": result["plan_name"],
                "expires_at": result["expires_at"],
                "expires_date": result["expires_date"],
                "activated_at": int(time.time()),
                "machine_id": current_mid,
            })
            # 同步服务端绑定
            try:
                _bind_device(code, current_mid, _get_device_info())
            except Exception:
                pass
            return {
                "activated": True,
                "plan_name": result["plan_name"],
                "days_left": result["days_left"],
                "expires_date": result["expires_date"],
            }

    trial = check_trial()
    if trial["in_trial"]:
        return {"trial": True, "uses_left": trial["uses_left"]}

    if not _get_trial_info():
        _start_trial()
        return {"trial": True, "uses_left": TRIAL_USES}

    return {"need_activate": True, "reason": "试用次数已用完，请激活"}


def check_license(code, log_fn=None):
    """兼容旧版接口"""
    result = validate_code(code)
    if log_fn and not result["ok"]:
        log_fn(f"激活码验证失败: {result['msg']}")
    return result
