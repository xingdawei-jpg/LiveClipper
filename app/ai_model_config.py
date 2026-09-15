"""Shared AI model defaults and OpenAI-compatible endpoint helpers."""

from __future__ import annotations

import urllib.parse


DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
LEGACY_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
LEGACY_DOUBAO_MODEL = "doubao-1-5-pro-32k-250115"

# The Ark console presents Seed 2.1 with title-case display names, while the
# chat-completions API requires the versioned model IDs listed below.
ARK_MODEL_DISPLAY_ALIASES = {
    "doubao-seed-2.1-pro": "doubao-seed-2-1-pro-260628",
    "doubao-seed-2.1-turbo": "doubao-seed-2-1-turbo-260628",
}

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
_MODELS_SUFFIX = "/models"
_RESPONSES_SUFFIX = "/responses"


def normalize_ai_base_url(base_url: str | None, default: str = DEEPSEEK_DEFAULT_BASE_URL) -> str:
    """Normalize a user-entered base URL while preserving non-DeepSeek /v1 bases."""
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return default

    lower = url.lower()
    for suffix in (_CHAT_COMPLETIONS_SUFFIX, _MODELS_SUFFIX, _RESPONSES_SUFFIX):
        if lower.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            lower = url.lower()
            break

    if lower in ("https://api.deepseek.com/v1", "http://api.deepseek.com/v1"):
        url = url[:-3].rstrip("/")

    return url or default


def normalize_ai_model_defaults(settings: dict | None) -> dict:
    data = dict(settings or {})
    # 用户从控制台复制 key / 模型名时经常带上换行、空格或引号。base_url 一直在清洗，
    # 但 api_key 与 model 之前没有写回清洗值 → Authorization 头会带着空白发出 → 平台 401。
    api_key = str(data.get("api_key") or "").strip().strip('"').strip("'").strip()
    base_url = normalize_ai_base_url(data.get("base_url"))
    model = str(data.get("model") or "").strip().strip('"').strip("'").strip()

    data["api_key"] = api_key
    data["base_url"] = base_url
    if base_url == LEGACY_DOUBAO_BASE_URL:
        data["model"] = ARK_MODEL_DISPLAY_ALIASES.get(model.lower(), model)
        model = data["model"]
    if not model and base_url != LEGACY_DOUBAO_BASE_URL:
        data["model"] = DEEPSEEK_DEFAULT_MODEL
    else:
        data["model"] = model
    data["enabled"] = bool(api_key)

    if (
        not api_key
        and base_url == LEGACY_DOUBAO_BASE_URL
        and model == LEGACY_DOUBAO_MODEL
    ):
        data["base_url"] = DEEPSEEK_DEFAULT_BASE_URL
        data["model"] = DEEPSEEK_DEFAULT_MODEL

    return data


def describe_ai_http_error(code: int, base_url: str = "", model: str = "", body: str = "") -> str:
    """Turn a provider HTTP failure into one actionable line.

    设计原则：
    * 平台自己返回的错误原文永远保留（区分“key 不对” / “欠费” / “模型不存在”最快）；
    * **401/403 且响应体为空**是一个异常信号：真实平台一定会解释原因，
      空体通常意味着中间有东西拦截（代理/安全软件/风控/IP 限制），必须单独提示。
    """
    text = " ".join(str(body or "").split())[:400]
    lower = text.lower()
    model_name = str(model or "").strip() or "（未填）"
    host = ""
    try:
        host = urllib.parse.urlparse(normalize_ai_base_url(base_url)).hostname or ""
    except Exception:  # noqa: BLE001 - 提示文案不允许抛错
        host = ""

    head = f"AI 连接失败（HTTP {code}）"
    advice = ""
    if code in (401, 403):
        if not text:
            advice = (
                "服务端没有返回任何错误内容，这通常不是模型平台自己报的错。"
                "请依次排查：① 本机是否开着代理 / VPN / 加速器（换手机热点重试一次即可判定）；"
                "② 安全软件或企业网关是否拦截了 HTTPS；"
                "③ 是否刚刚密集调用被平台风控（等 10~30 分钟再试）；"
                "④ 当前出口 IP 是否被平台限制。"
            )
        elif any(k in lower for k in ("authentication", "invalid api key", "invalid_api_key", "unauthorized", "鉴权", "权限")):
            advice = "API Key 无效、已失效，或与当前平台地址不匹配。"
        elif any(k in lower for k in ("insufficient", "balance", "quota", "欠费", "余额")):
            advice = "账号余额或调用额度不足，请到模型平台充值，或换一个可用 Key。"
        else:
            advice = "API Key 无效或没有该接口权限。"
    elif code == 402:
        advice = "账号余额不足，请到模型平台充值或更换 Key。"
    elif code == 404:
        advice = f"接口地址或模型不存在：请核对 Base URL（当前 {normalize_ai_base_url(base_url) or '（未填）'}）与模型名（当前 {model_name}）。"
    elif code == 429:
        advice = "调用过于频繁或额度受限，请稍后重试（选片预览会连续调用多次，属正常现象）。"
    elif code in (500, 502, 503, 504):
        advice = "模型平台暂时不可用，请稍后重试。"

    lines = [head + ("：" + advice if advice else "")]
    lines.append(f"当前配置：地址 {host or '（未填）'}，模型 {model_name}")
    lines.append(f"平台原文：{text}" if text else "平台原文：（空——服务端未返回任何错误内容）")
    return "\n".join(lines)


def ai_chat_completions_url(base_url: str | None) -> str:
    return f"{normalize_ai_base_url(base_url)}{_CHAT_COMPLETIONS_SUFFIX}"


def ai_models_url(base_url: str | None) -> str:
    return f"{normalize_ai_base_url(base_url)}{_MODELS_SUFFIX}"
