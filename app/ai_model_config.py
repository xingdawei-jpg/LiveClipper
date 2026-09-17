"""Shared AI model defaults and OpenAI-compatible endpoint helpers."""

from __future__ import annotations


DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
LEGACY_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
LEGACY_DOUBAO_MODEL = "doubao-1-5-pro-32k-250115"

_CHAT_COMPLETIONS_SUFFIX = "/chat/completions"
_MODELS_SUFFIX = "/models"


def normalize_ai_base_url(base_url: str | None, default: str = DEEPSEEK_DEFAULT_BASE_URL) -> str:
    """Normalize a user-entered base URL while preserving non-DeepSeek /v1 bases."""
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return default

    lower = url.lower()
    for suffix in (_CHAT_COMPLETIONS_SUFFIX, _MODELS_SUFFIX):
        if lower.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            lower = url.lower()
            break

    if lower in ("https://api.deepseek.com/v1", "http://api.deepseek.com/v1"):
        url = url[:-3].rstrip("/")

    return url or default


def normalize_ai_model_defaults(settings: dict | None) -> dict:
    data = dict(settings or {})
    api_key = str(data.get("api_key") or "").strip().strip('"').strip("'").strip()
    base_url = normalize_ai_base_url(data.get("base_url"))
    model = str(data.get("model") or "").strip().strip('"').strip("'").strip()

    data["base_url"] = base_url
    # 清洗后的 key / model 必须写回：用户从控制台复制 key 时常带上尾部换行或
    # 空格，之前只清洗了 base_url，导致 Authorization 头带着 \n 发出去 → 平台
    # 返回 HTTP 401（用户以为“key 是对的却总是失败”）。model 同理（带空格会 400）。
    data["api_key"] = api_key
    data["model"] = model or DEEPSEEK_DEFAULT_MODEL
    data["enabled"] = bool(api_key)

    if (
        not api_key
        and base_url == LEGACY_DOUBAO_BASE_URL
        and model == LEGACY_DOUBAO_MODEL
    ):
        data["base_url"] = DEEPSEEK_DEFAULT_BASE_URL
        data["model"] = DEEPSEEK_DEFAULT_MODEL

    return data


def ai_chat_completions_url(base_url: str | None) -> str:
    return f"{normalize_ai_base_url(base_url)}{_CHAT_COMPLETIONS_SUFFIX}"


def ai_models_url(base_url: str | None) -> str:
    return f"{normalize_ai_base_url(base_url)}{_MODELS_SUFFIX}"
