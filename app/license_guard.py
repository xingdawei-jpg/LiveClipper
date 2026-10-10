# -*- coding: utf-8 -*-
"""Shared feature access guard for activation/trial checks.

注意：这里**不再**在模块顶层 import tkinter。桌面版已废弃，而本模块被 Web 服务端
（web_client/server.py 的 _ensure_feature_access）直接引用 —— 顶层 import tkinter
会让整条授权门禁硬依赖图形库。弹窗只在桌面调用方传了 root 时才会用到，
因此改为用到时再惰性导入。
"""


LOCKED_REASON = "未激活。加客服微信 LeyiDeco 可领 3 天免费试用码，或在官网购买后自动获取激活码。"


def _log(log_fn, message, level="info"):
    if not log_fn:
        return
    try:
        log_fn(message, level)
    except TypeError:
        try:
            log_fn(message)
        except Exception:
            pass
    except Exception:
        pass


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _record_event(event_type, feature_name, units=1, metadata=None):
    try:
        import license_events

        return license_events.record_event(
            event_type,
            feature=feature_name,
            units=units,
            metadata=metadata or {},
        )
    except Exception:
        return False



def get_feature_access(refresh=False, strict=False):
    """Return normalized access state without showing UI.

    strict=True（2026-10-10 修复）：不采信可能已经过期的内存缓存，改为重读本地授权
    状态（仍是纯本地判断，不联网，所以没有性能代价、也不影响离线宽限）。

    为什么付费功能必须传 strict=True：check_activation_cached 默认 allow_stale=True ——
    缓存超过 10 分钟后依然返回**旧结果**，只在后台异步刷新。授权到期或吊销后，旧结果
    会继续放行，于是 AI 照跑、费用照花，直到某次刷新后才失败。实测过一次批量 20 个
    视频全部走完 AI 才因授权过期整体失败。
    """
    try:
        import license_client as lc

        if refresh:
            status = lc.check_activation()
        else:
            status = lc.check_activation_cached(allow_stale=not strict)
        if status.get("activated"):
            return {"ok": True, "activated": True, "trial": False, "raw": status}

        if status.get("need_activate"):
            reason = status.get("reason") or LOCKED_REASON
            return {"ok": False, "activated": False, "trial": False, "reason": reason, "raw": status}

        if status.get("trial"):
            uses_left = _safe_int(status.get("uses_left"))
            if uses_left > 0:
                return {
                    "ok": True,
                    "activated": False,
                    "trial": True,
                    "uses_left": uses_left,
                    "raw": status,
                }

        trial = lc.check_trial()
        if trial.get("in_trial") and _safe_int(trial.get("uses_left")) > 0:
            return {
                "ok": True,
                "activated": False,
                "trial": True,
                "uses_left": _safe_int(trial.get("uses_left")),
                "raw": status,
            }

        reason = status.get("reason") or LOCKED_REASON
        return {"ok": False, "activated": False, "trial": False, "reason": reason, "raw": status}
    except Exception as exc:
        return {
            "ok": False,
            "activated": False,
            "trial": False,
            "reason": "授权检查异常：" + str(exc),
            "raw": {},
        }



def require_feature_access(feature_name, root=None, log_fn=None, show_dialog=True, refresh=False,
                           strict=False):
    """Gate a user-facing feature before it starts running.

    strict=True 时按本地最新授权状态判定，不采信可能过期的缓存 —— 会花钱（AI）的
    功能应当用 strict=True，避免"授权已失效却先跑一半再失败"。
    """
    access = get_feature_access(refresh=refresh, strict=strict)
    if access.get("ok"):
        warning = str((access.get("raw") or {}).get("online_warning") or "").strip()
        if warning:
            _log(log_fn, f"授权在线刷新暂时不可用，已使用本地签名授权继续：{warning}", "warn")
        _record_event(
            "feature_start",
            feature_name,
            metadata={
                "activated": bool(access.get("activated")),
                "trial": bool(access.get("trial")),
                "online_warning": bool(warning),
            },
        )
        if access.get("trial"):
            _log(log_fn, f"试用模式：{feature_name}可用，剩余 {access.get('uses_left', 0)} 次。", "warn")
        return True

    reason = access.get("reason") or LOCKED_REASON
    _record_event("feature_denied", feature_name, metadata={"reason": reason})
    _log(log_fn, f"{feature_name}已锁定：{reason}", "err")
    if show_dialog and root is not None:
        try:
            from tkinter import messagebox  # 仅在桌面调用方传了 root 时才需要

            messagebox.showwarning(
                "功能已锁定",
                f"{feature_name}需要激活后使用。\n\n{reason}",
                parent=root,
            )
        except Exception:
            pass
    return False


def consume_trial_after_success(feature_name, units=1, root=None, log_fn=None):
    """Consume trial quota after successful user-visible output."""
    try:
        import license_client as lc

        access = get_feature_access(refresh=False)
        _record_event(
            "feature_success",
            feature_name,
            units=units,
            metadata={
                "activated": bool(access.get("activated")),
                "trial": bool(access.get("trial")),
            },
        )
        if not access.get("trial"):
            return None

        remaining = None
        for _ in range(max(1, _safe_int(units, 1))):
            remaining = lc.consume_trial_use()
            if remaining <= 0:
                break

        if remaining is None:
            return None

        if remaining > 0:
            _log(log_fn, f"{feature_name}完成，试用剩余 {remaining} 次。", "warn")
            try:
                lc._set_activation_cache({"trial": True, "uses_left": remaining})
            except Exception:
                pass
        else:
            _log(log_fn, f"{feature_name}完成，试用次数已用完，请激活后继续使用。", "warn")
            try:
                lc._set_activation_cache({"need_activate": True, "reason": LOCKED_REASON})
            except Exception:
                pass
            if root is not None:
                try:
                    from tkinter import messagebox  # 仅桌面调用方传了 root 时才需要

                    messagebox.showinfo("试用已用完", LOCKED_REASON, parent=root)
                except Exception:
                    pass
        return remaining
    except Exception as exc:
        _log(log_fn, "试用次数扣减异常：" + str(exc), "warn")
        return None
