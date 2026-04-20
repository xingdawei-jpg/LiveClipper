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
import subprocess
import urllib.request
import urllib.error
from datetime import datetime


# ============================================================
# 调试日志（激活问题排查，排查完后删除）
# ============================================================

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW - hide console window



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

PLAN_NAMES = {"01": "月付", "02": "季付", "03": "年付", "04": "永久"}
PLAN_DAYS = {"01": 30, "02": 90, "03": 365, "04": 36500}  # 04=永久, 36500天=100年

B36_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

def _decode_b36(s):
    """Base36 decode"""
    s = s.upper().strip()
    v = 0
    for c in s:
        idx = B36_CHARS.find(c)
        if idx < 0:
            return 0
        v = v * 36 + idx
    return v


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
    """查询激活码的设备绑定，返回 {record_id, machine_id, activate_date, status, distributor_id} 或 None"""
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
                activate_date = fields.get("激活日期", 0)
                if isinstance(activate_date, list):
                    activate_date = activate_date[0] if activate_date else 0
                status = fields.get("状态", "")
                if isinstance(status, list):
                    status = status[0].get("text", "") if status else ""
                distributor = fields.get("分销商ID", "")
                if isinstance(distributor, list):
                    distributor = distributor[0].get("text", "") if distributor else ""
                return {
                    "record_id": rec["record_id"],
                    "machine_id": str(mid).strip(),
                    "activate_date": int(activate_date) if activate_date else 0,
                    "status": str(status).strip(),
                    "distributor_id": str(distributor).strip(),
                }
        return None
    except Exception:
        return None

def _parse_code_dates(code):
    """从激活码中解析出激活日期和到期日期（毫秒时间戳，供飞书日期字段使用）"""
    try:
        raw = code.replace("-", "").strip().lower()
        if len(raw) == 36:
            # 36位码：plan(2) + dist_id(4base36) + expires(8) + nonce(2) + sig(20)
            expires_hex = raw[6:14]
        elif len(raw) == 34:
            # 34位码：plan(2) + dist_id(2) + expires(8) + nonce(2) + sig(20)
            expires_hex = raw[4:12]
        elif len(raw) == 32:
            # 32位码：plan(2) + expires(8) + nonce(2) + sig(20)
            expires_hex = raw[2:10]
        else:
            return None, None
        expires_at = int(expires_hex, 16)
        now = int(time.time())
        # 激活日期=今天，到期日期=激活码中的expires
        activate_ts = now * 1000  # 毫秒时间戳
        expire_ts = expires_at * 1000
        return activate_ts, expire_ts
    except Exception:
        return None, None

def _bind_device(code, machine_id, device_info="", status="已激活"):
    """绑定设备（写入或更新多维表格记录，含激活/到期日期/状态/分销商ID）"""
    try:
        raw_code = code.replace("-", "").strip().lower()
        existing = _query_device_binding(code)
        activate_ts, expire_ts = _parse_code_dates(code)
        
        # 解析分销商ID（36位码含4位base36, 34位码含2位hex, 32位码无）
        dist_id = ""
        if len(raw_code) == 36:
            dist_id = raw_code[2:6].upper()
        elif len(raw_code) == 34:
            dist_id = raw_code[2:4]
        
        # 日期字段（飞书多维表格日期类型需要毫秒时间戳）
        date_fields = {}
        if activate_ts:
            date_fields["激活日期"] = activate_ts
        if expire_ts:
            date_fields["到期日期"] = expire_ts

        if existing:
            # 更新
            fields = {"设备ID": machine_id, "设备信息": device_info, "状态": status}
            if dist_id:
                fields["分销商ID"] = dist_id
            fields.update(date_fields)
            resp = _feishu_request("PUT",
                f"/bitable/v1/apps/{_BITABLE_APP_TOKEN}/tables/{_BITABLE_TABLE_ID}/records/{existing['record_id']}",
                {"fields": fields})
            return resp and resp.get("code") == 0
        else:
            # 新增
            fields = {
                "多行文本": f"用户_{machine_id[:8]}",
                "激活码": raw_code,
                "设备ID": machine_id,
                "设备信息": device_info,
                "状态": status,
            }
            if dist_id:
                fields["分销商ID"] = dist_id
            fields.update(date_fields)
            resp = _feishu_request("POST",
                f"/bitable/v1/apps/{_BITABLE_APP_TOKEN}/tables/{_BITABLE_TABLE_ID}/records",
                {"fields": fields})
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
    """生成硬件指纹（CPU+硬盘+主板+MAC），跨平台兼容"""
    parts = []
    
    # CPU序列号
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "cpu", "get", "ProcessorId"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "ProcessorId"]
            if lines:
                parts.append(lines[0])
        else:
            # macOS: sysctl or system_profiler
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            if r.stdout.strip():
                parts.append(r.stdout.strip())
    except Exception:
        pass
    
    # 硬盘序列号（系统盘）
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "diskdrive", "get", "SerialNumber"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "SerialNumber"]
            if lines:
                parts.append(lines[0])
        else:
            # macOS: diskutil info
            r = subprocess.run(["diskutil", "info", "/"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            for line in r.stdout.split("\n"):
                if "Volume UUID" in line:
                    parts.append(line.split(":")[-1].strip())
                    break
    except Exception:
        pass
    
    # 主板序列号
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "baseboard", "get", "SerialNumber"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "SerialNumber"]
            if lines and lines[0] and lines[0] != "Default string":
                parts.append(lines[0])
    except Exception:
        pass
    
    # MAC地址（兜底）
    mac = uuid.getnode()
    parts.append(f"{mac:012x}")
    
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _get_device_info():
    try:
        return f"{platform.node()} | {platform.system()} {platform.release()} | {os.getenv('USERNAME', '')}"
    except Exception:
        return "unknown"


def _get_fingerprints():
    """收集7组件硬件指纹（防盗2.0加强版）
    返回 dict: {cpu_id, disk_serial, baseboard_serial, mac, bios_serial, volume_serial, ram_total}
    """
    fps = {}
    
    # 1. CPU ProcessorId
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "cpu", "get", "ProcessorId"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "ProcessorId"]
            if lines:
                fps["cpu_id"] = lines[0]
        else:
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True, timeout=5)
            if r.stdout.strip():
                fps["cpu_id"] = r.stdout.strip()
    except Exception:
        pass
    
    # 2. 硬盘序列号
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "diskdrive", "get", "SerialNumber"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "SerialNumber"]
            if lines:
                fps["disk_serial"] = lines[0]
        else:
            r = subprocess.run(["diskutil", "info", "/"],
                             capture_output=True, text=True, timeout=5)
            for line in r.stdout.split("\n"):
                if "Volume UUID" in line:
                    fps["disk_serial"] = line.split(":")[-1].strip()
                    break
    except Exception:
        pass
    
    # 3. 主板序列号
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "baseboard", "get", "SerialNumber"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "SerialNumber"]
            if lines and lines[0] and lines[0] != "Default string":
                fps["baseboard_serial"] = lines[0]
    except Exception:
        pass
    
    # 4. MAC地址
    try:
        mac = uuid.getnode()
        fps["mac"] = f"{mac:012x}"
    except Exception:
        pass
    
    # 5. BIOS序列号
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "bios", "get", "SerialNumber"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "SerialNumber"]
            if lines and lines[0]:
                fps["bios_serial"] = lines[0]
        else:
            # macOS: IOPlatformSerialNumber
            r = subprocess.run(["ioreg", "-l"],
                             capture_output=True, text=True, timeout=5)
            for line in r.stdout.split("\n"):
                if "IOPlatformSerialNumber" in line:
                    parts = line.split('"')
                    if len(parts) >= 4:
                        fps["bios_serial"] = parts[-2]
                    break
    except Exception:
        pass
    
    # 6. 系统盘卷序列号
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "logicaldisk", "where", "DeviceID='C:'", "get", "VolumeSerialNumber"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "VolumeSerialNumber"]
            if lines and lines[0]:
                fps["volume_serial"] = lines[0]
    except Exception:
        pass
    
    # 7. 内存总量
    try:
        if platform.system() == "Windows":
            r = subprocess.run(["wmic", "computersystem", "get", "TotalPhysicalMemory"],
                             capture_output=True, text=True, timeout=5, creationflags=_NO_WINDOW)
            lines = [l.strip() for l in r.stdout.strip().split("\n")
                     if l.strip() and l.strip() != "TotalPhysicalMemory"]
            if lines and lines[0]:
                fps["ram_total"] = lines[0]
        else:
            r = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, timeout=5)
            if r.stdout.strip():
                fps["ram_total"] = r.stdout.strip()
    except Exception:
        pass
    
    return fps


# ============================================================
# 服务器验证配置
# ============================================================
# 阿里云函数计算 API 地址（部署后填入）
_VERIFY_API_URL = "https://license-server-tsigpdxecv.cn-hangzhou.fcapp.run"  #防盗2.0 FC地址
_OFFLINE_GRACE_HOURS = 72  # 离线宽限时间（小时）
_REVOKED_MARKER = ".revoked"  # 熔断标记文件名


def _verify_online(code, machine_id):
    """联网验证激活码（防盗2.0：调用阿里云FC API + 7组件指纹匹配）
    返回: {valid, expires, revoked, plan, msg} 或 None（网络失败）
    """
    if not _VERIFY_API_URL:
        return None  # 未配置API地址，跳过联网验证
    
    try:
        import urllib.parse as _urlp
        fingerprints = _get_fingerprints()
        params = _urlp.urlencode({
            "code": code.replace("-", "").strip().lower(),
            "machine_id": machine_id,
            "fingerprints": json.dumps(fingerprints),
        })
        url = f"{_VERIFY_API_URL}/verify?{params}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result
    except Exception:
        return None  # 网络失败，走离线宽限




def _fc_activate(code, machine_id, result_info, expires_at):
    """调用FC /activate 注册设备绑定"""
    if not _VERIFY_API_URL:
        return False
    try:
        fingerprints = _get_fingerprints()
        body = json.dumps({
            "code": code.replace("-", "").strip().lower(),
            "machine_id": machine_id,
            "fingerprints": fingerprints,
            "device_info": _get_device_info(),
            "plan": result_info.get("plan", ""),
            "plan_name": result_info.get("plan_name", ""),
            "plan_hex": result_info.get("plan_hex", ""),
            "expires_at": expires_at,
        }).encode("utf-8")
        url = f"{_VERIFY_API_URL}/activate"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("valid", False)
    except Exception:
        return False


def _fc_unbind(code, machine_id):
    """调用FC /unbind 解绑设备"""
    if not _VERIFY_API_URL:
        return False
    try:
        fingerprints = _get_fingerprints()
        body = json.dumps({
            "code": code.replace("-", "").strip().lower(),
            "machine_id": machine_id,
            "fingerprints": fingerprints,
        }).encode("utf-8")
        url = f"{_VERIFY_API_URL}/unbind"
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("ok", False)
    except Exception:
        return False
def _get_last_online_verify():
    """获取上次联网验证成功的时间戳"""
    cache = _load_cache()
    return cache.get("last_online_verify", 0)


def _update_online_verify_time():
    """更新联网验证时间"""
    cache = _load_cache() or {}
    cache["last_online_verify"] = int(time.time())
    _save_cache(cache)


def _is_offline_grace_expired():
    """检查离线宽限期是否已过"""
    last = _get_last_online_verify()
    if last == 0:
        return False  # 从未联网验证过（老用户），不强制
    elapsed_hours = (int(time.time()) - last) / 3600
    return elapsed_hours > _OFFLINE_GRACE_HOURS


def _check_revoked_marker():
    """检查本地熔断标记"""
    path = os.path.join(_get_data_path(), _REVOKED_MARKER)
    return os.path.exists(path)


def _write_revoked_marker():
    """写入熔断标记（远程熔断时调用）"""
    path = os.path.join(_get_data_path(), _REVOKED_MARKER)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"revoked_at": int(time.time()), "machine_id": _get_machine_id()}))
    except Exception:
        pass


def _clear_revoked_marker():
    """清除熔断标记（重新激活时调用）"""
    path = os.path.join(_get_data_path(), _REVOKED_MARKER)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _revoke_device(code):
    """吊销激活码（在飞书多维表格标记为"已吊销"）
    管理员在飞书多维表格中将状态改为"已吊销"，
    客户端下次联网验证时会收到revoked=true并写入本地熔断标记
    """
    try:
        existing = _query_device_binding(code)
        if existing:
            resp = _feishu_request("PUT",
                f"/bitable/v1/apps/{_BITABLE_APP_TOKEN}/tables/{_BITABLE_TABLE_ID}/records/{existing['record_id']}",
                {"fields": {"状态": "已吊销", "设备ID": "", "设备信息": ""}})
            return resp and resp.get("code") == 0
        return False
    except Exception:
        return False


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
    raw = code.replace("-", "").strip()
    # 兼容三种格式: 32位(旧版无分销商) / 34位(含distributor_id)
    if len(raw) not in (32, 34, 36):
        return {"ok": False, "msg": "激活码格式错误"}
    try:
        if len(raw) == 36:
            # v3.0 36位: plan(2hex) + dist_id(4base36) + expires(8hex) + nonce(2hex) + sig(20hex)
            plan_hex = raw[0:2].lower()
            dist_b36 = raw[2:6].upper()
            expires_hex = raw[6:14].lower()
            nonce_hex = raw[14:16].lower()
            signature = raw[16:36].lower()
            payload = plan_hex + dist_b36 + expires_hex + nonce_hex
            dist_id = dist_b36
        elif len(raw) == 34:
            # v2.0 34位: plan(2) + dist_id(2hex) + expires(8) + nonce(2) + sig(20)
            raw_lower = raw.lower()
            plan_hex = raw_lower[0:2]
            dist_id = raw_lower[2:4]
            expires_hex = raw_lower[4:12]
            nonce_hex = raw_lower[12:14]
            signature = raw_lower[14:34]
            payload = plan_hex + dist_id + expires_hex + nonce_hex
        else:
            # 旧版32位: plan(2) + expires(8) + nonce(2) + sig(20)
            raw_lower = raw.lower()
            plan_hex = raw_lower[0:2]
            dist_id = ""
            expires_hex = raw_lower[2:10]
            nonce_hex = raw_lower[10:12]
            signature = raw_lower[12:32]
            payload = plan_hex + expires_hex + nonce_hex
        
        if plan_hex not in PLAN_NAMES:
            return {"ok": False, "msg": "激活码无效（未知套餐）"}
        expected_sig = hmac.new(
            SECRET_KEY.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()[:20]
        if not hmac.compare_digest(signature, expected_sig):
            return {"ok": False, "msg": "激活码无效（签名错误）"}
        return {
            "ok": True,
            "plan": {"01": "monthly", "02": "quarterly", "03": "yearly", "04": "permanent"}.get(plan_hex, "monthly"),
            "plan_name": PLAN_NAMES[plan_hex],
            "days": PLAN_DAYS[plan_hex],
            "plan_hex": plan_hex,
            "dist_id": dist_id,
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

    # Step 3: 清除旧熔断标记
    _clear_revoked_marker()

    # Step 4: 联网服务器注册（新增）
    online_result = _verify_online(code.replace("-", "").strip().lower(), current_mid)
    if online_result is not None:
        if online_result.get("revoked"):
            return {"ok": False, "msg": "该激活码已被吊销，无法激活"}
        if online_result.get("valid") == False:
            return {"ok": False, "msg": online_result.get("msg", "服务器验证失败")}
        _update_online_verify_time()

    # Step 5: 本地保存
    plan_days = PLAN_DAYS.get(result.get("plan_hex", "01"), 30)
    activated_at = int(time.time())
    expires_at = activated_at + plan_days * 86400
    expires_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
    _save_license_code(code)
    _save_cache({
        "code": code,
        "plan": result["plan"],
        "plan_name": result["plan_name"],
        "expires_at": expires_at,
        "expires_date": expires_date,
        "activated_at": activated_at,
        "machine_id": current_mid,
    })

    # Step 6: 防盗2.0 FC注册（优先）
    plan_days = PLAN_DAYS.get(result.get("plan_hex", "01"), 30)
    fc_expires_at = activated_at + plan_days * 86400
    fc_ok = _fc_activate(code, current_mid, result, fc_expires_at)
    if fc_ok:
        _update_online_verify_time()

    # Step 7: 飞书服务端绑定（兼容旧版，非阻塞）
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

    # 防盗2.0 FC解绑（优先）
    fc_ok = _fc_unbind(code, current_mid)
    
    # 飞书服务端解绑（兼容旧版）
    unbind_ok = _unbind_device(code) if not fc_ok else True

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
    """检查当前激活状态（启动时调用）
    
    验证顺序：
    1. 本地熔断标记检查（最快，防断网绕过）
    2. 本地 HMAC 验证
    3. 联网服务器验证（非阻塞，失败走离线宽限）
    4. 飞书多维表格设备绑定校验（兼容旧版）
    5. 离线宽限期检查
    """
    # Step 0: 检查熔断标记（联网时查飞书状态，已激活则自动恢复）
    if _check_revoked_marker():
        # 尝试联网查飞书状态——管理员可能已改回"已激活"
        try:
            cache = _load_cache()
            code = cache.get("code", "") if cache else ""
            if code:
                binding = _query_device_binding(code)
                if binding and binding.get("status") != "已吊销":
                    # 飞书状态已恢复 → 清除本地熔断标记，继续验证
                    _clear_revoked_marker()
                else:
                    return {"need_activate": True, "reason": "授权已被吊销，请联系管理员"}
            else:
                return {"need_activate": True, "reason": "授权已被吊销，请联系管理员"}
        except Exception:
            # 联网失败，熔断标记仍生效（防止断网绕过）
            return {"need_activate": True, "reason": "授权已被吊销，请联网后重试"}

    cache = _load_cache()

    if cache and cache.get("code"):
        code = cache["code"]
        result = validate_code(code)
        if result["ok"]:
            # 方案B：到期时间 = activated_at + 套餐天数
            activated_at = cache.get("activated_at", 0)
            plan_days = PLAN_DAYS.get(result.get("plan_hex", "01"), 30)
            if activated_at > 0:
                expires_at = activated_at + plan_days * 86400
            else:
                try:
                    binding = _query_device_binding(code)
                    if binding and binding.get("activate_date", 0) > 0:
                        activated_at = binding["activate_date"] // 1000
                        expires_at = activated_at + plan_days * 86400
                        cache["activated_at"] = activated_at
                        _save_cache(cache)
                    else:
                        expires_at = cache.get("expires_at", 0)
                except Exception:
                    expires_at = cache.get("expires_at", 0)
            
            now = int(time.time())
            days_left = max(0, (expires_at - now) // 86400)
            expires_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
            
            if now > expires_at:
                # 永久版永不过期
                if result.get("plan_hex") == "04":
                    pass  # 永久版跳过过期检查
                else:
                    # 过期也写熔断标记，防止改系统时间绕过
                    _write_revoked_marker()
                    return {"need_activate": True, "reason": f"激活码已于 {expires_date} 过期，请续费"}
            
            # Step: 联网服务器验证（新增）
            online_result = _verify_online(code, _get_machine_id())
            if online_result is not None:
                # 联网成功
                _update_online_verify_time()
                
                if online_result.get("revoked"):
                    # 服务器说已吊销 → 写入本地熔断标记
                    _write_revoked_marker()
                    return {"need_activate": True, "reason": "授权已被吊销，请联系管理员"}
                
                if online_result.get("valid") == False:
                    # not_found = 本地有缓存但FC没记录（迁移场景），自动注册
                    if online_result.get("not_found"):
                        plan_days = PLAN_DAYS.get(result.get("plan_hex", "01"), 30)
                        fc_exp = cache.get("activated_at", int(time.time())) + plan_days * 86400
                        _fc_activate(code, _get_machine_id(), result, fc_exp)
                    else:
                        return {"need_activate": True, "reason": online_result.get("msg", "激活码无效")}
                
                # 服务器验证通过，同步过期信息
                server_expires = online_result.get("expires", 0)
                if server_expires and server_expires != expires_at:
                    # 服务器有过期时间且与本地不同，以服务器为准
                    expires_at = server_expires
                    days_left = max(0, (expires_at - now) // 86400)
                    expires_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
                    cache["expires_at"] = expires_at
                    _save_cache(cache)
            else:
                # 联网失败，检查离线宽限期
                if _is_offline_grace_expired():
                    return {"need_activate": True, "reason": f"已离线超过{_OFFLINE_GRACE_HOURS}小时，请联网验证"}
            
            # Step: 飞书多维表格校验（防盗2.0暂时关闭，仅保留吊销检测）
            try:
                binding = _query_device_binding(code)
                if binding:
                    # 仅检查吊销状态，不做设备绑定校验
                    if binding.get("status") == "已吊销":
                        _write_revoked_marker()
                        return {"need_activate": True, "reason": "授权已被吊销，请联系管理员"}
                    # 静默更新设备绑定（不阻塞）
                    _bind_device(code, _get_machine_id(), _get_device_info())
                else:
                    # 无记录时自动绑定
                    _bind_device(code, _get_machine_id(), _get_device_info())
            except Exception:
                pass
            
            return {
                "activated": True,
                "plan_name": result["plan_name"],
                "days_left": days_left,
                "expires_date": expires_date,
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
            # 方案B：到期时间 = 激活时间 + 套餐天数
            plan_days_s = PLAN_DAYS.get(result.get("plan_hex", "01"), 30)
            activated_at_s = int(time.time())
            expires_at_s = activated_at_s + plan_days_s * 86400
            expires_date_s = datetime.fromtimestamp(expires_at_s).strftime("%Y-%m-%d")
            _save_cache({
                "code": code,
                "plan": result["plan"],
                "plan_name": result["plan_name"],
                "expires_at": expires_at_s,
                "expires_date": expires_date_s,
                "activated_at": activated_at_s,
                "machine_id": current_mid,
            })
            # 同步服务端绑定
            try:
                _bind_device(code, current_mid, _get_device_info())
            except Exception:
                pass
            days_left_s = max(0, (expires_at_s - int(time.time())) // 86400)
            return {
                "activated": True,
                "plan_name": result["plan_name"],
                "days_left": days_left_s,
                "expires_date": expires_date_s,
            }

    trial = check_trial()
    if trial["in_trial"]:
        return {"trial": True, "uses_left": trial["uses_left"]}

    # 没有试用记录 → 开始试用
    trial_info = _get_trial_info()
    if not trial_info:
        _start_trial()
        return {"trial": True, "uses_left": TRIAL_USES}

    # 机器ID不匹配但从未激活过 → 允许新机器试用
    if trial_info.get("trial_machine_id") and trial_info.get("trial_machine_id") != _get_machine_id():
        if not cache.get("activation_code"):
            _start_trial()
            return {"trial": True, "uses_left": TRIAL_USES}

    return {"need_activate": True, "reason": "试用次数已用完，请激活"}


def check_license(code, log_fn=None):
    """兼容旧版接口"""
    result = validate_code(code)
    if log_fn and not result["ok"]:
        log_fn(f"激活码验证失败: {result['msg']}")
    return result
