"""
客户端激活验证模块（离线 HMAC 验证版）
- 不需要服务器，激活码自带签名，本地验证
- 激活信息缓存到本地，启动时自动检查

使用方式：
  from license_client import check_activation, activate_with_code
"""

import hmac
import hashlib
import time
import json
import os
import sys
import platform
import uuid
from datetime import datetime
import os
import sys


# ============================================================
# 密钥（与 license_generator.py 一致）
# 通过混淆方式存储，不直接暴露明文
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

CACHE_FILE = "license_cache.json"
LICENSE_FILE = "license.dat"
TRIAL_USES = 10  # 试用次数

PLAN_NAMES = {"01": "月付", "02": "季付", "03": "年付"}
PLAN_DAYS = {"01": 30, "02": 90, "03": 365}


def _get_base_path():
    """获取程序根目录（用于非数据文件）"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_data_path():
    """获取用户数据目录（缓存/激活码等可写文件）"""
    from platform_config import LICENSE_CACHE_DIR
    data_dir = LICENSE_CACHE_DIR
    if not os.path.exists(data_dir):
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception:
            pass
    return data_dir


def _load_cache():
    """加载本地激活缓存"""
    path = os.path.join(_get_data_path(), CACHE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_cache(data):
    """保存激活缓存"""
    path = os.path.join(_get_data_path(), CACHE_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _save_license_code(code):
    """保存激活码到本地"""
    path = os.path.join(_get_data_path(), LICENSE_FILE)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(code.strip())
        return True
    except Exception:
        return False


def _load_license_code():
    """从本地读取激活码"""
    path = os.path.join(_get_data_path(), LICENSE_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def validate_code(code):
    """
    验证激活码的有效性（纯本地 HMAC 验证）
    
    返回: dict
      ok=True:  {ok, plan, plan_name, days, expires_date, days_left}
      ok=False: {ok, msg}
    """
    if not code:
        return {"ok": False, "msg": "激活码为空"}

    # 去掉格式化符号
    raw = code.replace("-", "").strip().lower()
    
    if len(raw) != 32:
        return {"ok": False, "msg": "激活码格式错误"}

    try:
        # 解析结构
        plan_hex = raw[0:2]
        expires_hex = raw[2:10]
        nonce_hex = raw[10:12]
        signature = raw[12:32]

        # 验证套餐类型
        if plan_hex not in PLAN_NAMES:
            return {"ok": False, "msg": "激活码无效（未知套餐）"}

        # 验证 HMAC 签名
        payload = plan_hex + expires_hex + nonce_hex
        expected_sig = hmac.new(
            SECRET_KEY.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()[:20]

        if not hmac.compare_digest(signature, expected_sig):
            return {"ok": False, "msg": "激活码无效（签名错误）"}

        # 检查过期时间
        expires_at = int(expires_hex, 16)
        now = int(time.time())

        if now > expires_at:
            expired_date = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
            return {"ok": False, "msg": f"激活码已于 {expired_date} 过期"}

        # 计算剩余天数
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
    用激活码激活（绑定机器）
    返回: {ok, msg, info}
    """
    result = validate_code(code)
    
    if result["ok"]:
        _save_license_code(code)
        _save_cache({
            "code": code,
            "plan": result["plan"],
            "plan_name": result["plan_name"],
            "expires_at": result["expires_at"],
            "expires_date": result["expires_date"],
            "activated_at": int(time.time()),
            "machine_id": _get_machine_id(),
        })
        return {"ok": True, "msg": "激活成功", "info": result}
    else:
        return result




def _get_machine_id():
    """生成机器唯一标识"""
    raw = f"{platform.processor()}-{platform.node()}-{os.getenv('USERNAME', 'user')}-{uuid.getnode()}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _get_trial_info():
    """获取试用信息"""
    cache = _load_cache()
    if cache and cache.get("trial_start") is not None:
        return cache
    return None


def _start_trial():
    """首次启动，初始化试用（绑定机器）"""
    cache = _load_cache() or {}
    mid = _get_machine_id()
    cache["trial_start"] = int(time.time())
    cache["trial_machine_id"] = mid
    cache["trial_uses_left"] = TRIAL_USES
    _save_cache(cache)
    return cache


def consume_trial_use():
    """消耗一次试用次数，返回剩余次数（-1 表示无试用资格）"""
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
    """
    检查试用状态（含机器绑定验证）
    返回: {in_trial, uses_left, total_uses}
    """
    trial = _get_trial_info()
    if not trial:
        return {"in_trial": False, "uses_left": 0, "total_uses": TRIAL_USES}

    # 机器绑定验证
    cached_mid = trial.get("trial_machine_id", "")
    current_mid = _get_machine_id()
    if cached_mid and cached_mid != current_mid:
        return {"in_trial": False, "uses_left": 0, "total_uses": TRIAL_USES}

    uses_left = trial.get("trial_uses_left", 0)

    if uses_left > 0:
        return {"in_trial": True, "uses_left": uses_left, "total_uses": TRIAL_USES}
    else:
        return {"in_trial": False, "uses_left": 0, "total_uses": TRIAL_USES}


def check_activation():
    """
    检查当前激活状态（启动时调用）
    返回: {activated, ...} 或 {trial, days_left} 或 {need_activate, reason}
    """
    # 1. 检查正式激活
    cache = _load_cache()
    
    if cache and cache.get("code"):
        code = cache["code"]
        result = validate_code(code)
        if result["ok"]:
            return {
                "activated": True,
                "plan_name": result["plan_name"],
                "days_left": result["days_left"],
                "expires_date": result["expires_date"],
            }

    # 检查是否有保存的激活码（缓存可能被清了）
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
            return {
                "activated": True,
                "plan_name": result["plan_name"],
                "days_left": result["days_left"],
                "expires_date": result["expires_date"],
            }
    
    # 2. 没有激活，检查试用
    trial = check_trial()
    if trial["in_trial"]:
        return {
            "trial": True,
            "uses_left": trial["uses_left"],
        }

    # 3. 试用过期，需要激活
    # 如果没有试用记录，自动开始试用
    if not _get_trial_info():
        _start_trial()
        return {
            "trial": True,
            "uses_left": TRIAL_USES,
        }

    return {"need_activate": True, "reason": "试用次数已用完，请激活"}

# 兼容旧接口
def check_license(code, log_fn=None):
    """兼容旧版接口，内部调用 validate_code"""
    return validate_code(code)

def save_license_code(code):
    return _save_license_code(code)

def load_license_code():
    return _load_license_code()


if __name__ == "__main__":
    # 测试模式
    import datetime
    
    print("激活验证模块测试")
    print("=" * 40)
    
    status = check_activation()
    if status["activated"]:
        print(f"✅ 已激活")
        print(f"   套餐: {status['plan_name']}")
        print(f"   剩余: {status['days_left']} 天")
        print(f"   过期: {status['expires_date']}")
    else:
        print(f"❌ 未激活: {status.get('reason', '')}")
        print(f"   请输入激活码启动软件")
