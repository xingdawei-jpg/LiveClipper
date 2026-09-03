"""User-configurable content policy for AI clip selection.

This module intentionally does not classify transcript text itself.  It only
normalizes the persisted policy and decides how a caller should treat a known
content kind.  Transcript text and timing therefore remain owned by the
candidate/final-safety pipeline.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


POLICY_ACTION_BLOCK = "block"
POLICY_ACTION_BODY = "body_only"
POLICY_ACTION_ALLOW = "allow"
POLICY_ACTION_PREFER = "prefer"
POLICY_ACTIONS = (
    POLICY_ACTION_BLOCK,
    POLICY_ACTION_BODY,
    POLICY_ACTION_ALLOW,
    POLICY_ACTION_PREFER,
)

POLICY_KINDS = (
    "price",
    "cta",
    "inventory_pressure",
    "source_claim",
    "social_proof",
    "after_sale",
    "size_interaction",
    "live_interaction",
)

_ACTION_ALIASES = {
    "": POLICY_ACTION_BLOCK,
    "block": POLICY_ACTION_BLOCK,
    "blocked": POLICY_ACTION_BLOCK,
    "禁止": POLICY_ACTION_BLOCK,
    "不使用": POLICY_ACTION_BLOCK,
    "body_only": POLICY_ACTION_BODY,
    "body": POLICY_ACTION_BODY,
    "仅正文": POLICY_ACTION_BODY,
    "正文可用": POLICY_ACTION_BODY,
    "allow": POLICY_ACTION_ALLOW,
    "allowed": POLICY_ACTION_ALLOW,
    "可用": POLICY_ACTION_ALLOW,
    "prefer": POLICY_ACTION_PREFER,
    "preferred": POLICY_ACTION_PREFER,
    "优先": POLICY_ACTION_PREFER,
}

_KIND_LABELS = {
    "price": "价格/报价",
    "cta": "促销/行动引导",
    "inventory_pressure": "库存/稀缺催促",
    "source_claim": "来源/原厂背书",
    "social_proof": "社会证明",
    "after_sale": "售后承诺",
    "size_interaction": "尺码/身高体重互动",
    "live_interaction": "直播互动回复",
    "custom": "自定义规则",
}

_ACTION_LABELS = {
    POLICY_ACTION_BLOCK: "禁止",
    POLICY_ACTION_BODY: "仅正文",
    POLICY_ACTION_ALLOW: "可用",
    POLICY_ACTION_PREFER: "优先",
}

DEFAULT_CONTENT_POLICY = {
    "price": POLICY_ACTION_BLOCK,
    "cta": POLICY_ACTION_BLOCK,
    "inventory_pressure": POLICY_ACTION_BLOCK,
    "source_claim": POLICY_ACTION_BLOCK,
    "social_proof": POLICY_ACTION_BLOCK,
    "after_sale": POLICY_ACTION_BLOCK,
    "size_interaction": POLICY_ACTION_BLOCK,
    "live_interaction": POLICY_ACTION_BLOCK,
    "custom_rules": [],
}


def default_content_policy() -> dict[str, Any]:
    """Return a fresh legacy-safe content policy."""

    return deepcopy(DEFAULT_CONTENT_POLICY)


def normalize_policy_action(value: Any) -> str:
    return _ACTION_ALIASES.get(str(value or "").strip().lower(), POLICY_ACTION_BLOCK)


def normalize_content_policy(value: Any) -> dict[str, Any]:
    """Normalize persisted policy without accepting arbitrary executable data."""

    raw = value if isinstance(value, dict) else {}
    policy = default_content_policy()
    for kind in POLICY_KINDS:
        policy[kind] = normalize_policy_action(raw.get(kind, policy[kind]))

    rules = raw.get("custom_rules")
    if not isinstance(rules, list):
        rules = []
    seen: set[tuple[str, str]] = set()
    normalized_rules: list[dict[str, str]] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        text = text[:80]
        action = normalize_policy_action(item.get("action"))
        key = (text.casefold(), action)
        if key in seen:
            continue
        seen.add(key)
        normalized_rules.append({"text": text, "action": action})
        if len(normalized_rules) >= 80:
            break
    policy["custom_rules"] = normalized_rules
    return policy


def apply_run_avoid_overrides(policy: Any, avoid: Any = ()) -> dict[str, Any]:
    """Return one immutable task policy with restrictive run-only avoids.

    The compact "本次避选" chips are deliberately not a second content-policy
    system.  A matching chip may only make this run stricter; it never changes
    the saved setting and it never turns another run's preference into a ban.
    Non-policy chips (for example repetition) stay as director instructions.
    """

    resolved = normalize_content_policy(policy)
    labels = {
        str(value or "").strip().casefold()
        for value in (avoid if isinstance(avoid, (list, tuple, set)) else (avoid,))
        if str(value or "").strip()
    }
    aliases = {
        "价格": "price",
        "报价": "price",
        "尺码": "size_interaction",
        "身高体重": "size_interaction",
        "库存": "inventory_pressure",
        "稀缺": "inventory_pressure",
        "促销": "cta",
        "促单": "cta",
        "行动引导": "cta",
    }
    for label, kind in aliases.items():
        if label.casefold() in labels:
            resolved[kind] = POLICY_ACTION_BLOCK
    return resolved


def interaction_policy_kind(reason: Any) -> str:
    """Map the shared interaction classifier's reason to a policy kind."""

    value = str(reason or "")
    if any(token in value for token in ("尺码", "身高", "体重", "试穿")):
        return "size_interaction"
    return "live_interaction"


def action_for(policy: Any, kind: str, text: Any = "") -> tuple[str, str]:
    """Return the policy action and the matching source for a known signal.

    An explicitly configured custom phrase wins over its broad content kind.
    Strict platform/legal safety remains outside this module and cannot be
    overridden here.
    """

    normalized = normalize_content_policy(policy)
    value = str(text or "").casefold()
    matches = [
        rule for rule in normalized["custom_rules"]
        if str(rule.get("text") or "").casefold() in value
    ]
    if matches:
        matched = max(matches, key=lambda rule: len(str(rule.get("text") or "")))
        return str(matched["action"]), f"自定义词:{matched['text']}"
    normalized_kind = kind if kind in POLICY_KINDS else "live_interaction"
    return str(normalized[normalized_kind]), _KIND_LABELS[normalized_kind]


def matching_custom_rule(policy: Any, text: Any) -> dict[str, str] | None:
    """Return the most specific custom rule that occurs in ``text``."""

    normalized = normalize_content_policy(policy)
    value = str(text or "").casefold()
    matches = [
        rule for rule in normalized["custom_rules"]
        if str(rule.get("text") or "").casefold() in value
    ]
    if not matches:
        return None
    return dict(max(matches, key=lambda rule: len(str(rule.get("text") or ""))))


def blocks_role(policy: Any, kind: str, text: Any = "", role: str = "body") -> tuple[bool, str]:
    """Return whether a known content signal is blocked for a clip role."""

    action, source = action_for(policy, kind, text)
    if action == POLICY_ACTION_BLOCK:
        return True, f"{source}:{_ACTION_LABELS[action]}"
    # Personal fitting replies and live-room chat never make a credible first
    # promise, even when a user chooses to retain them inside the body.
    if role == "hook" and (
        action == POLICY_ACTION_BODY
        or kind in {"size_interaction", "live_interaction"}
    ):
        return True, f"{source}:不可作Hook"
    return False, ""


def preferred_kinds(policy: Any, kinds: list[str] | tuple[str, ...], text: Any = "") -> list[str]:
    """Return policy signals that are a soft preference, never a hard filter."""

    result: list[str] = []
    for kind in kinds:
        action, source = action_for(policy, kind, text)
        if action == POLICY_ACTION_PREFER:
            result.append(source)
    return list(dict.fromkeys(result))


def policy_prompt_lines(policy: Any) -> list[str]:
    """Build short grounded direction for the director prompt."""

    normalized = normalize_content_policy(policy)
    lines: list[str] = []
    for kind in POLICY_KINDS:
        action = normalized[kind]
        label = _KIND_LABELS[kind]
        if action == POLICY_ACTION_BLOCK:
            lines.append(f"{label}：不得进入成片。")
        elif action == POLICY_ACTION_BODY:
            lines.append(f"{label}：可作为正文证据，不能作为Hook。")
        elif action == POLICY_ACTION_ALLOW:
            suffix = "；尺码和直播互动仍不可作为Hook。" if kind in {"size_interaction", "live_interaction"} else "。"
            lines.append(f"{label}：可在内容完整、有购买价值时使用{suffix}")
        elif action == POLICY_ACTION_PREFER:
            suffix = "；尺码和直播互动仍不可作为Hook。" if kind in {"size_interaction", "live_interaction"} else "。"
            lines.append(f"{label}：在内容质量相当时优先{suffix}")
    for rule in normalized["custom_rules"]:
        action = str(rule["action"])
        label = _ACTION_LABELS[action]
        if action == POLICY_ACTION_BLOCK:
            lines.append(f"自定义词“{rule['text']}”：禁止使用。")
        elif action == POLICY_ACTION_BODY:
            lines.append(f"自定义词“{rule['text']}”：仅可作正文。")
        elif action == POLICY_ACTION_ALLOW:
            lines.append(f"自定义词“{rule['text']}”：内容完整时可用。")
        else:
            lines.append(f"自定义词“{rule['text']}”：质量相当时优先。")
    return lines
