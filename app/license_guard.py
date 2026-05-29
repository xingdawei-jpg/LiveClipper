# -*- coding: utf-8 -*-
"""Shared feature access guard for activation/trial checks."""

from tkinter import messagebox


LOCKED_REASON = "试用次数已用完，请激活后继续使用。"


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


def get_feature_access(refresh=False):
    """Return normalized access state without showing UI."""
    try:
        import license_client as lc

        status = lc.check_activation() if refresh else lc.check_activation_cached()
        try:
            lc._set_activation_cache(status)
        except Exception:
            pass

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


def require_feature_access(feature_name, root=None, log_fn=None, show_dialog=True, refresh=False):
    """Gate a user-facing feature before it starts running."""
    access = get_feature_access(refresh=refresh)
    if access.get("ok"):
        if access.get("trial"):
            _log(log_fn, f"试用模式：{feature_name}可用，剩余 {access.get('uses_left', 0)} 次。", "warn")
        return True

    reason = access.get("reason") or LOCKED_REASON
    _log(log_fn, f"{feature_name}已锁定：{reason}", "err")
    if show_dialog and root is not None:
        try:
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
                    messagebox.showinfo("试用已用完", LOCKED_REASON, parent=root)
                except Exception:
                    pass
        return remaining
    except Exception as exc:
        _log(log_fn, "试用次数扣减异常：" + str(exc), "warn")
        return None
