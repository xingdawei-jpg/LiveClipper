# -*- coding: utf-8 -*-
"""AI candidate content review used before the final clip director."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import ssl
from ssl_context import create_ssl_context
import tempfile
import time
from typing import Any, Iterable, Mapping, Sequence
import urllib.request

from ai_model_config import ai_chat_completions_url
from ai_cost_ledger import record_ai_call
from candidate_quality import candidate_quality_flags, hook_candidate_quality_flags
from content_policy import (
    blocks_role,
    interaction_policy_kind,
    normalize_content_policy,
    policy_prompt_lines,
)
from marketing_intent import (
    MarketingIntentBundle,
    build_marketing_intent_bundle,
    marketing_intent_prompt_contract,
)
from selection_safety import hook_ineligible_reason, live_interaction_or_size_response_reason


CONTENT_REVIEW_VERSION = "content-review-v40"
CONTENT_REVIEW_ENV = "LIVECLIPPER_CONTENT_REVIEW_MODE"
CONTENT_REVIEW_DEFAULT_MODE = "off"
CONTENT_REVIEW_MAX_CARDS = 80
CONTENT_REVIEW_TARGET_DURATION = 180.0
CONTENT_REVIEW_CACHE_DAYS = 60
CONTENT_REVIEW_CACHE_MAX_FILES = 128
CONTENT_REVIEW_CACHE_MAX_BYTES = 50 * 1024 * 1024

_VALID_TIERS = {"main", "reserve"}
_VALID_DEPENDENCIES = {"independent", "needs_previous", "needs_next", "needs_both"}
_VALID_ROLES = {"effect", "evidence", "scene", "objection", "product"}
_VALID_TARGET_RELATIONS = {"primary", "supporting", "other", "unknown"}
_VALID_HOOK_PACKAGE_TIERS = {"A", "B", "C"}
_DIRECTOR_HOOK_PACKAGE_TIERS = {"A", "B"}
# Hook packages are passed to the same final segment contract as every
# Product proof.  A longer proof would be deleted later and leave a broken
# opening, so it is not an executable package at review time.
_DIRECTOR_OPENING_SEGMENT_MAX_SECONDS = 8.0
_VALID_HOOK_PROOF_RELATIONS = {
    "visual_result",
    "design_reason",
    "material_evidence",
    "wearing_experience",
    "scene_projection",
    "identity_projection",
    "social_proof",
    "source_value",
    "price_value",
    "after_sale_confidence",
    "other_grounded",
}
_VALID_HOOK_SEMANTIC_SIGNALS = {
    "emotion",
    "strong_judgment",
    "identity",
    "style_projection",
    "pain_hit",
    "result",
    "contrast",
    "curiosity",
    "scene",
    "social_proof",
    "source",
    "price_value",
    "after_sale",
    "cta",
    "concrete_value",
}
_VALID_DELIVERY_SIGNALS = {"unknown", "textual_low", "textual_medium", "textual_high"}
_LOW_VALUE_QUALITY_TAGS = (
    "泛泛夸赞", "直播铺垫", "展示铺垫", "互动", "残句", "重复", "上新预告",
    "尺码", "身高", "体重",
)
_STRONG_EVIDENCE_MARKERS = (
    "原因", "解释", "细节", "实物", "展示", "对比", "效果", "工艺",
    "面料", "场景", "证明", "依据", "上身",
)
_TOPIC_FAMILY_RULES = (
    ("\u53e3\u611f\u98df\u6b32", ("\u53e3\u611f", "\u5473\u9053", "\u9999\u5473", "\u98df\u6b32", "\u597d\u5403", "\u8106", "\u751c", "\u9c9c")),
    ("\u65b0\u9c9c\u54c1\u8d28", ("\u65b0\u9c9c", "\u73b0\u6458", "\u73b0\u505a", "\u54c1\u8d28", "\u4fdd\u8d28")),
    ("\u4ea7\u5730\u6eaf\u6e90", ("\u4ea7\u5730", "\u6eaf\u6e90", "\u679c\u56ed", "\u57fa\u5730")),
    ("\u89c4\u683c\u5206\u91cf", ("\u89c4\u683c", "\u5206\u91cf", "\u51c0\u91cd", "\u514b\u91cd", "\u5927\u5c0f", "\u4efd\u91cf")),
    ("\u53d1\u8d27\u4fdd\u9c9c", ("\u53d1\u8d27", "\u7269\u6d41", "\u4fdd\u9c9c", "\u5305\u88c5", "\u51b7\u94fe")),
    ("\u573a\u666f\u5403\u6cd5", ("\u5403\u6cd5", "\u65e9\u9910", "\u4e0b\u5348\u8336", "\u96f6\u98df", "\u70f9\u996a")),
    ("\u989c\u8272\u6c1b\u56f4", ("\u989c\u8272", "\u663e\u767d", "\u80a4\u8272", "\u8272\u5f69", "\u4eae\u8272", "\u9971\u548c", "\u8272\u8c03")),
    ("\u7248\u578b\u663e\u7626", ("\u7248\u578b", "\u663e\u7626", "\u906e\u8089", "\u4fee\u9970", "\u6536\u8170", "\u817f\u578b", "\u80a9\u578b")),
    ("\u4e0a\u8eab\u6548\u679c", ("\u4e0a\u8eab\u6548\u679c", "\u4e0a\u8eab\u53cd\u5dee", "\u8bd5\u7a7f\u6548\u679c", "\u4e0a\u8eab", "\u7a7f\u4e0a", "\u4e00\u7a7f", "\u8bd5\u7a7f", "\u6302\u7740", "\u6302\u8d77\u6765", "\u955c\u5b50\u91cc", "\u7a7f\u8d77\u6765")),
    ("\u9762\u6599\u8d28\u611f", ("\u9762\u6599", "\u6750\u8d28", "\u6210\u5206", "\u83b1\u8d5b\u5c14", "\u4e9a\u9ebb", "\u5168\u68c9", "\u624b\u611f", "\u4eb2\u80a4", "\u900f\u6c14", "\u5782\u5760", "\u6297\u76b1", "\u6c34\u6d17")),
    ("\u6d41\u884c\u8d8b\u52bf", ("\u6d41\u884c\u8d8b\u52bf", "\u6d41\u884c", "\u8d8b\u52bf", "\u5f53\u5b63", "\u4eca\u5e74", "\u672c\u5b63", "\u70ed\u95e8", "\u79c0\u573a", "\u65f6\u88c5\u5468")),
    ("\u98ce\u683c\u5b9a\u4f4d", ("\u98ce\u683c\u5b9a\u4f4d", "\u6b3e\u5f0f\u98ce\u683c", "\u98ce\u683c\u6c14\u8d28", "\u5b66\u9662", "\u7f8e\u5f0f", "\u97e9\u7cfb", "\u6cd5\u5f0f", "\u65e5\u7cfb", "\u8fa3\u59b9", "\u751c\u9177", "\u5c0f\u9999\u98ce", "\u8001\u94b1\u98ce", "\u5343\u91d1\u98ce", "\u8f7b\u5962", "\u8857\u5934", "\u677e\u5f1b", "\u4fcf\u76ae", "\u51cf\u9f84", "\u4f18\u96c5", "\u5f97\u4f53", "\u6c14\u8d28", "\u6e05\u7eaf", "\u5e05\u6c14", "\u5c0f\u4f17", "\u4e0d\u70c2\u5927\u8857", "\u590d\u53e4\u98ce")),
    ("\u5c3a\u5bf8\u957f\u5ea6", ("\u5c3a\u7801", "\u5c3a\u5bf8", "\u957f\u5ea6", "\u88d9\u957f", "\u8863\u957f", "\u8896\u957f", "\u8eab\u9ad8", "\u4f53\u91cd")),
    ("\u7a7f\u7740\u4f53\u9a8c", ("\u7a7f\u7740", "\u8212\u9002", "\u51c9\u5feb", "\u4e0d\u624e", "\u4e0d\u95f7", "\u5f39\u529b")),
    ("\u5de5\u827a\u7ec6\u8282", ("\u5de5\u827a", "\u505a\u5de5", "\u8d70\u7ebf", "\u7ebd\u6263", "\u62fc\u63a5", "\u7ec6\u8282", "\u8bbe\u8ba1")),
    ("\u573a\u666f\u642d\u914d", ("\u573a\u666f", "\u642d\u914d", "\u901a\u52e4", "\u7ea6\u4f1a", "\u51fa\u95e8", "\u804c\u573a", "\u5ea6\u5047", "\u4eba\u7fa4", "\u590f\u5929", "\u590f\u5b63", "\u65e9\u79cb", "\u521d\u79cb", "\u79cb\u5929", "\u6362\u5b63")),
    ("\u5bf9\u6bd4\u4f18\u52bf", ("\u5bf9\u6bd4", "\u4f18\u52bf", "\u4e0d\u540c", "\u72ec\u5bb6", "\u666e\u901a\u6b3e")),
    ("\u6027\u4ef7\u6bd4", ("\u6027\u4ef7\u6bd4", "\u5212\u7b97", "\u503c\u5f97")),
    ("\u60c5\u7eea\u611f\u67d3", ("\u60ca\u8273", "\u559c\u6b22", "\u597d\u770b", "\u6f02\u4eae", "\u5e05", "\u60c5\u7eea")),
    ("\u7d27\u8feb\u7a00\u7f3a", ("\u7a00\u7f3a", "\u9650\u91cf", "\u65ad\u7801", "\u5e93\u5b58")),
)


class ContentReviewError(RuntimeError):
    pass


def normalize_review_mode(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "": CONTENT_REVIEW_DEFAULT_MODE,
        "false": "off",
        "0": "off",
        "disabled": "off",
        "true": "on",
        "1": "on",
        "enabled": "on",
    }
    text = aliases.get(text, text)
    return text if text in {"off", "shadow", "on"} else CONTENT_REVIEW_DEFAULT_MODE


def resolve_review_mode(settings: Mapping[str, Any] | None = None) -> str:
    env_value = os.environ.get(CONTENT_REVIEW_ENV)
    if env_value is not None:
        return normalize_review_mode(env_value)
    return normalize_review_mode((settings or {}).get("content_review_mode"))


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_list(value: Any, *, limit: int, item_limit: int) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        text = _clean_text(raw, item_limit)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)

def _normalize_topic(value: Any) -> str:
    topic = _clean_text(value, 40) or "\u5176\u4ed6"
    if topic in {"\u5176\u4ed6", "\u901a\u7528\u5356\u70b9"}:
        return topic
    for family, keywords in _TOPIC_FAMILY_RULES:
        if topic == family or any(keyword in topic for keyword in keywords):
            return family
    return topic


def _reconcile_apparel_topic(
    reported_topic: Any,
    candidate_text: Any,
    subtopic: Any = "",
) -> str:
    """Correct only decisive apparel taxonomy conflicts against source text.

    The review model owns the detailed selling-point interpretation.  This
    guardrail only prevents four repeatedly observed family-level errors from
    leaking to the director and preview: try-on contrast becoming comfort,
    style identity becoming trend, seasonal availability becoming comfort, and
    an actual declared trend being downgraded to generic style.
    """
    topic = _normalize_topic(reported_topic)
    text = re.sub(r"\s+", "", f"{candidate_text or ''} {subtopic or ''}")
    if not text:
        return topic

    has_true_trend = any(
        marker in text
        for marker in (
            "\u6d41\u884c", "\u8d8b\u52bf", "\u5f53\u5b63", "\u4eca\u5e74",
            "\u672c\u5b63", "\u70ed\u95e8", "\u79c0\u573a", "\u65f6\u88c5\u5468",
        )
    )
    has_style_identity = any(
        marker in text
        for marker in (
            "\u5b66\u9662", "\u7f8e\u5f0f", "\u97e9\u7cfb", "\u6cd5\u5f0f", "\u65e5\u7cfb",
            "\u8fa3\u59b9", "\u751c\u9177", "\u5c0f\u9999\u98ce", "\u8001\u94b1\u98ce",
            "\u5343\u91d1\u98ce", "\u8f7b\u5962", "\u8857\u5934", "\u677e\u5f1b\u611f",
            "\u4fcf\u76ae", "\u51cf\u9f84", "\u4f18\u96c5", "\u5f97\u4f53", "\u6c14\u8d28",
            "\u6e05\u7eaf", "\u5e05\u6c14", "\u5c0f\u4f17", "\u4e0d\u70c2\u5927\u8857",
            "\u590d\u53e4\u98ce", "\u98ce\u683c", "\u9ad8\u667a\u611f", "\u9ad8\u667a\u5546",
            "\u9ad8\u5b66\u5386", "\u767d\u5bcc\u7f8e",
        )
    )
    has_try_on_contrast = (
        any(marker in text for marker in ("\u6302\u7740", "\u6302\u8d77\u6765", "\u8bd5\u7a7f", "\u8bd5\u4e00\u4e0b"))
        or any(marker in text for marker in ("\u4e0a\u8eab\u6548\u679c", "\u4e0a\u8eab\u4e4b\u540e", "\u4e0a\u8eab\u4ee5\u540e", "\u7a7f\u4e0a\u8eab", "\u4e00\u7a7f", "\u4e0d\u4e0a\u8eab"))
        or (
            any(marker in text for marker in ("\u4e0a\u8eab", "\u7a7f\u4e0a", "\u7a7f\u8d77\u6765"))
            and any(marker in text for marker in ("\u60ca\u8273", "\u7cbe\u81f4", "\u4e0d\u4e00\u6837", "\u597d\u770b", "\u6548\u679c", "\u770b\u8d77\u6765", "\u666e\u901a"))
        )
        or "\u4e00\u5b9a\u8981\u4e0a\u8eab" in text
    )
    has_seasonal_availability = any(
        marker in text
        for marker in ("\u590f\u5929", "\u590f\u5b63", "\u65e9\u79cb", "\u521d\u79cb", "\u79cb\u5929", "\u6362\u5b63")
    )
    has_comfort_claim = any(
        marker in text
        for marker in ("\u8212\u670d", "\u8212\u9002", "\u4eb2\u80a4", "\u4e0d\u95f7", "\u4e0d\u70ed", "\u4e0d\u52d2", "\u4e0d\u7d27\u7ef7", "\u6d3b\u52a8\u65b9\u4fbf")
    )

    # A verified specific family (for example shoulder shaping or a concrete
    # commute outfit) must not be overwritten merely because the speaker says
    # "上身" while demonstrating it.  Reconcile only the broad families that
    # caused the observed label drift.
    reconcilable_topics = {
        "\u7a7f\u7740\u4f53\u9a8c",
        "\u6d41\u884c\u8d8b\u52bf",
        "\u98ce\u683c\u5b9a\u4f4d",
        "\u5176\u4ed6",
        "\u901a\u7528\u5356\u70b9",
    }
    if has_try_on_contrast and topic in reconcilable_topics:
        return "\u4e0a\u8eab\u6548\u679c"
    if has_true_trend and topic in reconcilable_topics:
        return "\u6d41\u884c\u8d8b\u52bf"
    if has_style_identity and topic in reconcilable_topics:
        return "\u98ce\u683c\u5b9a\u4f4d"
    if has_seasonal_availability and not has_comfort_claim and topic in reconcilable_topics:
        return "\u573a\u666f\u642d\u914d"
    return topic


def _normalize_target_relation(value: Any) -> str:
    relation = _clean_text(value, 24).lower()
    aliases = {
        "\u4e3b\u5546\u54c1": "primary",
        "\u4e3b\u4f53": "primary",
        "\u4e3b\u8bb2": "primary",
        "\u76f4\u63a5": "primary",
        "\u8f85\u52a9": "supporting",
        "\u642d\u914d": "supporting",
        "\u4f50\u8bc1": "supporting",
        "\u5176\u4ed6\u5546\u54c1": "other",
        "\u5176\u4ed6\u54c1\u7c7b": "other",
        "\u65e0\u6cd5\u5224\u65ad": "unknown",
        "\u672a\u77e5": "unknown",
    }
    relation = aliases.get(relation, relation)
    return relation if relation in _VALID_TARGET_RELATIONS else "unknown"


@dataclass(frozen=True)
class ContentCard:
    candidate_id: int
    topic: str
    subtopic: str
    buyer_value: str
    evidence_type: str
    evidence_quote: str
    roles: tuple[str, ...]
    dependency: str
    quality_tags: tuple[str, ...]
    tier: str
    primary_subject: str = ""
    target_relation: str = "unknown"
    subject_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "topic": self.topic,
            "subtopic": self.subtopic,
            "buyer_value": self.buyer_value,
            "evidence_type": self.evidence_type,
            "evidence_quote": self.evidence_quote,
            "roles": list(self.roles),
            "dependency": self.dependency,
            "quality_tags": list(self.quality_tags),
            "tier": self.tier,
            "primary_subject": self.primary_subject,
            "target_relation": self.target_relation,
            "subject_evidence": self.subject_evidence,
        }


def _content_card_priority(card: "ContentCard") -> int:
    """Return an internal-only ordering value; never expose it as a user score."""
    relation_rank = {
        "primary": 36,
        "supporting": 22,
        "unknown": 10,
        "other": -24,
    }.get(card.target_relation, 0)
    tier_rank = 24 if card.tier == "main" else 8
    evidence_text = f"{card.evidence_type} {' '.join(card.quality_tags)}"
    evidence_rank = 12 if any(marker in evidence_text for marker in _STRONG_EVIDENCE_MARKERS) else 0
    role_rank = (
        (6 if "evidence" in card.roles else 0)
        + (5 if "effect" in card.roles else 0)
        + (4 if "objection" in card.roles else 0)
        + (2 if "scene" in card.roles else 0)
    )
    context_rank = 4 if card.dependency == "independent" else 0
    quote_rank = 3 if card.evidence_quote else 0
    tag_text = " ".join(card.quality_tags)
    low_value_penalty = 28 if any(tag in tag_text for tag in _LOW_VALUE_QUALITY_TAGS) else 0
    return relation_rank + tier_rank + evidence_rank + role_rank + context_rank + quote_rank - low_value_penalty


def content_card_priority(card: "ContentCard") -> int:
    """Public read-only priority used by director fallbacks, not the UI."""
    return _content_card_priority(card)


def _card_hook_eligible(card: "ContentCard") -> bool:
    """A reviewed card may be body material but still be unfit for the opening."""
    if card.dependency != "independent" or card.target_relation == "other":
        return False
    tag_text = " ".join(card.quality_tags)
    return not any(tag in tag_text for tag in _LOW_VALUE_QUALITY_TAGS)


def _textual_delivery_signal(text: Any) -> str:
    """Return a conservative text-only proxy until audio delivery is available.

    This deliberately does not claim to measure loudness, pitch, or speaking
    rate. It only captures visible delivery cues such as a natural reaction,
    intensifiers, and rhythmic repetition, so it can rank otherwise valid
    openings without overriding the safety and semantic gates.
    """
    compact = re.sub(r"\s+", "", str(text or ""))
    if len(compact) < 6:
        return "unknown"
    score = 0
    if re.search(r"(?:哇|天哪|我的天|真的|太|巨|超级|绝了|无语|牛掰|好帅|好看)", compact):
        score += 1
    if re.search(r"[！!]{1,}|(?:真的很|太.+?了|超级.+?的)", compact):
        score += 1
    if re.search(r"(?:我的菜|很久没出|全公司|我自己留|我买了|巨好)", compact):
        score += 1
    if re.search(r"(.{2,6})\1", compact):
        score += 1
    if score >= 3:
        return "textual_high"
    if score == 2:
        return "textual_medium"
    return "textual_low" if score else "unknown"


def _inferred_hook_semantic_signals(text: Any, topic: Any = "") -> tuple[str, ...]:
    """Derive opening signals only from the spoken Hook, never its topic tag."""
    # Keep ``topic`` for compatibility with existing callers, but do not let a
    # broad label such as "版型显瘦" manufacture a pain/result signal for a
    # sentence that merely lists a collar function.
    del topic
    compact = re.sub(r"\s+", "", f"{text or ''}")
    signals: list[str] = []
    rules = (
        # These are attraction mechanisms, not generic selling-point tags.
        # A neutral phrase such as "material is not stiff" remains useful body
        # evidence, but it has no reason to occupy the first three seconds.
        ("emotion", r"(?:哇|天哪|我的天|无语|牛掰|好帅|太好看|巨好看)"),
        ("strong_judgment", r"(?:真的(?:很|太|好|帅|喜欢)|巨(?:好|.?穿)|超级(?:推荐|好|显|舒服|喜欢)|太(?:好看|帅|显|绝|适合|对味)|一定要|必须|很久没出|闭眼)"),
        ("identity", r"(?:有眼光|有审美|自己就是大佬|上一个level|高级感)"),
        ("style_projection", r"(?:明星机场|穿出.{0,6}(?:气场|风格|调性)|(?:风格|气场|调性).{0,6}(?:拉满|拿捏|对味|绝|帅)|女总裁)"),
        ("pain_hit", r"(?:梨形|胯宽|腿粗|小肚子|显瘦|遮肉|修饰)"),
        ("result", r"(?:显白|显瘦|显高|显腿长|显腿直|显比例|往(?:里|内|前|上)收|收住|拉长|修饰|不(?:会)?显(?:胖|壮|矮|腿粗|胯宽|大臂))"),
        ("contrast", r"(?:不是|反而|区别|不一样|最怕|其实不是)"),
        ("curiosity", r"(?:最厉害|值钱|重点|就在这里|你看这里)"),
        ("scene", r"(?:穿出去|朋友问(?:链接)?|回头率|参加活动|出街)"),
        ("social_proof", r"(?:人手一件|全公司|我自己留|我买了|回购)"),
        ("source", r"(?:原厂|买手店|原版|源头|专柜)"),
        ("price_value", r"(?:一万|零头|值这个价|性价比)"),
        ("after_sale", r"(?:不满意.*退|包退|退换|售后)"),
        ("cta", r"(?:闭眼买|直接拍|冲它|给我买)"),
    )
    for signal, pattern in rules:
        if re.search(pattern, compact):
            signals.append(signal)
    return tuple(signals[:4]) or ("concrete_value",)


def _hook_promise_family(text: Any) -> str:
    compact = re.sub(r"\s+", "", str(text or ""))
    families = (
        ("source", r"(?:原厂|原版|买手店|源头|专柜|代工)"),
        ("social", r"(?:人手一件|全公司|我自己留|我买了|回购|大家都买)"),
        ("price", r"(?:一万|零头|值这个价|性价比|价格)"),
        ("after_sale", r"(?:不满意.*退|包退|退换|售后)"),
        ("coverage", r"(?:长度|裙长|衣长|走光|露(?:腿|底)|遮(?:住|到|腿)|覆盖|安全)"),
        ("material", r"(?:面料|材质|手感|羊毛|亚麻|莱赛尔|全棉|透气|不扎)"),
        ("comfort", r"(?:舒服|好穿|不闷|轻薄|亲肤|弹力)"),
        ("color", r"(?:颜色|色|显白|黄皮|粉色|藏青|灰调)"),
        ("fit", r"(?:显瘦|胯|腿|腰|肩宽|小肚子|梨形|遮肉|修饰)"),
        ("style", r"(?:好帅|很帅|好看|风格|机场|气质|高级|利落|调性)"),
        ("scene", r"(?:通勤|约会|出门|活动|度假|上班)"),
    )
    for family, pattern in families:
        if re.search(pattern, compact):
            return family
    return ""


def _proof_relation_is_grounded(hook_text: Any, proof_text: Any, relation: str) -> bool:
    """Reject an obviously unrelated proof without demanding word-for-word continuity."""
    compact_proof = re.sub(r"\s+", "", str(proof_text or ""))
    if not compact_proof:
        return False
    relation_markers = {
        "visual_result": r"(?:你看|上身|显|肩|腰|腿|胯|脸|白|型|廓形|比例|视觉|利落)",
        "design_reason": r"(?:因为|设计|肩线|版型|剪裁|缝|线|收|廓形|领口|腰头)",
        "material_evidence": r"(?:面料|材质|手感|羊毛|亚麻|莱赛尔|全棉|纱|垂感)",
        "wearing_experience": r"(?:舒服|好穿|不闷|不扎|轻薄|亲肤|弹力|透气)",
        "scene_projection": r"(?:通勤|约会|出门|活动|度假|上班|搭配)",
        "identity_projection": r"(?:气质|风格|高级|利落|调性|帅|好看)",
        "social_proof": r"(?:人手一件|全公司|我自己留|我买了|回购|大家都买)",
        "source_value": r"(?:原厂|原版|买手店|源头|专柜|代工)",
        "price_value": r"(?:一万|零头|值这个价|性价比|价格)",
        "after_sale_confidence": r"(?:不满意.*退|包退|退换|售后)",
    }
    if relation == "other_grounded":
        hook_chars = set(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", str(hook_text or "")))
        proof_chars = set(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", compact_proof))
        return len(hook_chars.intersection(proof_chars)) >= 2
    marker = relation_markers.get(relation)
    if not marker or not re.search(marker, compact_proof):
        return False
    allowed_relations = {
        "source": {"source_value"},
        "social": {"social_proof"},
        "price": {"price_value"},
        "after_sale": {"after_sale_confidence"},
        "coverage": {"visual_result", "design_reason"},
        "material": {"material_evidence", "wearing_experience"},
        "comfort": {"material_evidence", "wearing_experience"},
        "color": {"visual_result", "scene_projection"},
        "fit": {"visual_result", "design_reason", "wearing_experience"},
        "style": {"visual_result", "design_reason", "scene_projection", "identity_projection"},
        "scene": {"scene_projection", "visual_result", "identity_projection"},
    }
    family = _hook_promise_family(hook_text)
    if family == "coverage":
        return bool(re.search(r"(?:长度|裙|衣长|走光|内衬|遮|覆盖|安全)", compact_proof))
    return not family or relation in allowed_relations.get(family, set())


def _has_director_grade_hook_mechanism(semantic_signals: Sequence[str]) -> bool:
    """Keep ordinary feature lists from becoming opening contracts."""
    mechanisms = {
        "emotion",
        "strong_judgment",
        "identity",
        "style_projection",
        "pain_hit",
        "result",
        "contrast",
        "curiosity",
        "scene",
        "social_proof",
        "source",
        "price_value",
        "after_sale",
        "cta",
    }
    return bool(mechanisms.intersection(semantic_signals))


def _normalize_hook_package_tier(value: Any, *, semantic_signals: Sequence[str], delivery_signal: str) -> str:
    """Accept A/B only when the original Hook itself carries opening pull."""
    requested = _clean_text(value, 8).upper()
    grounded = _has_director_grade_hook_mechanism(semantic_signals)
    high_value = {
        "emotion", "strong_judgment", "identity", "style_projection",
        "pain_hit", "result", "contrast", "curiosity",
    }
    high_value_count = len(high_value.intersection(semantic_signals))

    if requested == "A":
        if grounded and (delivery_signal == "textual_high" or high_value_count >= 2):
            return "A"
        return "B" if grounded else "C"
    if requested == "B":
        return "B" if grounded else "C"
    if requested == "C":
        return "C"
    if grounded and (delivery_signal == "textual_high" or high_value_count >= 2):
        return "A"
    return "B" if grounded else "C"


@dataclass(frozen=True)
class HookPair:
    """Compatibility contract used by the current live director path."""
    hook_id: int
    followup_id: int
    topic: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "followup_id": self.followup_id,
            "topic": self.topic,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class HookPackage:
    hook_id: int
    followup_id: int
    topic: str
    reason: str
    # These fields make the opening a verifiable promise/proof contract rather
    # than two individually good sentences placed next to one another.
    hook_promise: str = ""
    proof_relation: str = ""
    package_complete: bool = False
    semantic_signals: tuple[str, ...] = ()
    delivery_signal: str = "unknown"
    opening_tier: str = "C"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hook_id": self.hook_id,
            "followup_id": self.followup_id,
            "topic": self.topic,
            "reason": self.reason,
            "hook_promise": self.hook_promise,
            "proof_relation": self.proof_relation,
            "package_complete": bool(self.package_complete),
            "semantic_signals": list(self.semantic_signals),
            "delivery_signal": self.delivery_signal,
            "opening_tier": self.opening_tier,
        }


@dataclass(frozen=True)
class NarrativeOpportunity:
    """A reviewed opening chapter the director may build on.

    This is deliberately narrower than a full-video story template.  It only
    locks the proven Hook -> proof opening, then exposes a few supporting
    cards and possible next themes.  The director still decides whether a
    later chapter belongs in this particular duration and sequence.
    """
    narrative_id: str
    hook_id: int
    followup_id: int
    topic: str
    hook_promise: str
    proof_relation: str
    opening_support_ids: tuple[int, ...] = ()
    next_topics: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "narrative_id": self.narrative_id,
            "hook_id": self.hook_id,
            "followup_id": self.followup_id,
            "topic": self.topic,
            "hook_promise": self.hook_promise,
            "proof_relation": self.proof_relation,
            "opening_support_ids": list(self.opening_support_ids),
            "next_topics": list(self.next_topics),
        }


@dataclass(frozen=True)
class ContentReviewBundle:
    cache_key: str
    candidate_digest: str
    category: str
    model: str
    cards: tuple[ContentCard, ...]
    retained_duration: float
    hook_pairs: tuple[HookPair, ...] = ()
    # A complete A/B HookPackage is the preferred live-opening contract. A
    # HookPair remains the safe fallback when the review did not find one.
    hook_packages: tuple[HookPackage, ...] = ()
    marketing_intent: MarketingIntentBundle | None = None
    # A focused second pass is only needed when the broad review returned no
    # verified opening pair. Persisting its completed state keeps cache hits
    # free of another model call, including the valid "no pair exists" result.
    hook_pair_reviewed: bool = False
    cache_hit: bool = False
    version: str = CONTENT_REVIEW_VERSION

    @property
    def allowed_candidate_ids(self) -> set[int]:
        return {card.candidate_id for card in self.cards}

    @property
    def hook_candidate_ids(self) -> set[int]:
        active_packages = self.director_hook_packages
        if active_packages:
            return {package.hook_id for package in active_packages}
        return {pair.hook_id for pair in self.hook_pairs}

    @property
    def director_hook_packages(self) -> tuple[HookPackage, ...]:
        """Return only complete opening packages strong enough to constrain AI."""
        cards_by_id = self.card_map()
        return tuple(
            package for package in self.hook_packages
            if package.package_complete
            and package.opening_tier in _DIRECTOR_HOOK_PACKAGE_TIERS
            # Reserve material remains available for a later evidence chapter
            # or duration/source coverage.  It must not displace a reviewed
            # main choice in the first three seconds or its immediate proof.
            and (hook_card := cards_by_id.get(package.hook_id)) is not None
            and (followup_card := cards_by_id.get(package.followup_id)) is not None
            and hook_card.tier == "main"
            and followup_card.tier == "main"
            and _card_hook_eligible(hook_card)
            and followup_card.dependency == "independent"
            and followup_card.target_relation != "other"
        )

    @property
    def narrative_opportunities(self) -> tuple[NarrativeOpportunity, ...]:
        """Derive small, grounded opening chapters from executable packages.

        A Hook Package proves the first handoff.  We extend it only with
        reviewed main cards of the same topic, then advertise possible later
        themes without imposing a whole-video single-theme prison.
        """
        cards_by_id = self.card_map()
        main_cards = [
            card for card in self.cards
            if card.tier == "main"
            and card.dependency == "independent"
            and card.target_relation != "other"
        ]
        opportunities: list[NarrativeOpportunity] = []
        seen: set[tuple[int, int]] = set()
        for package in self.director_hook_packages:
            key = (package.hook_id, package.followup_id)
            if key in seen:
                continue
            seen.add(key)
            hook_card = cards_by_id.get(package.hook_id)
            proof_card = cards_by_id.get(package.followup_id)
            if hook_card is None or proof_card is None:
                continue
            topic = _normalize_topic(hook_card.topic) or _normalize_topic(package.topic)
            if not topic or topic == "其他":
                continue
            opening_support_ids = tuple(
                card.candidate_id
                for card in sorted(main_cards, key=_content_card_priority, reverse=True)
                if card.candidate_id not in key
                and _normalize_topic(card.topic) == topic
            )[:3]
            next_topics: list[str] = []
            for card in sorted(main_cards, key=_content_card_priority, reverse=True):
                next_topic = _normalize_topic(card.topic)
                if (
                    not next_topic
                    or next_topic in {"其他", topic}
                    or next_topic in next_topics
                ):
                    continue
                next_topics.append(next_topic)
                if len(next_topics) >= 3:
                    break
            opportunities.append(NarrativeOpportunity(
                narrative_id=f"ARC-{len(opportunities) + 1:02d}",
                hook_id=package.hook_id,
                followup_id=package.followup_id,
                topic=topic,
                hook_promise=package.hook_promise,
                proof_relation=package.proof_relation,
                opening_support_ids=opening_support_ids,
                next_topics=tuple(next_topics),
            ))
            if len(opportunities) >= 4:
                break
        return tuple(opportunities)

    def card_map(self) -> dict[int, ContentCard]:
        return {card.candidate_id: card for card in self.cards}

    def _hook_thread_contract(
        self,
        records: Sequence[HookPair | HookPackage],
    ) -> dict[int, dict[str, Any]]:
        """Derive flexible, reviewed Hook continuation threads.

        The recorded follow-up is the strongest proven fulfilment. The
        director may use another clean candidate from the same reviewed topic
        when it makes the spoken handoff clearer, but it may not switch the
        product or promise family.
        """
        cards_by_id = self.card_map()
        mutable: dict[int, dict[str, Any]] = {}
        for record in records:
            hook_card = cards_by_id.get(record.hook_id)
            followup_card = cards_by_id.get(record.followup_id)
            if (
                hook_card is None
                or followup_card is None
                or not _card_hook_eligible(hook_card)
                or hook_card.tier != "main"
                or followup_card.tier != "main"
                or followup_card.dependency != "independent"
                or followup_card.target_relation == "other"
            ):
                continue
            # The card topic is normalized against the shared taxonomy. The
            # pair's free-form topic is explanatory only and must not split a
            # valid color/fabric thread because the reviewer named it loosely.
            topic = _normalize_topic(hook_card.topic) or _normalize_topic(record.topic)
            if not topic or topic == "其他":
                continue
            thread = mutable.setdefault(
                hook_card.candidate_id,
                {
                    "hook_id": hook_card.candidate_id,
                    "topic": topic,
                    "primary_subject": hook_card.primary_subject,
                    "seed_followup_ids": set(),
                    "allowed_followup_ids": set(),
                    "reasons": [],
                },
            )
            thread["seed_followup_ids"].add(followup_card.candidate_id)
            thread["allowed_followup_ids"].add(followup_card.candidate_id)
            if record.reason:
                thread["reasons"].append(record.reason)

        for thread in mutable.values():
            topic = str(thread["topic"])
            hook_id = int(thread["hook_id"])
            for card in self.cards:
                if (
                    card.candidate_id == hook_id
                    or card.dependency != "independent"
                    or card.target_relation == "other"
                    or _normalize_topic(card.topic) != topic
                ):
                    continue
                thread["allowed_followup_ids"].add(card.candidate_id)

        result: dict[int, dict[str, Any]] = {}
        for hook_id, thread in mutable.items():
            allowed_ids = sorted(
                int(value) for value in thread["allowed_followup_ids"]
                if int(value) != hook_id
            )
            if not allowed_ids:
                continue
            result[hook_id] = {
                "hook_id": hook_id,
                "topic": str(thread["topic"]),
                "primary_subject": str(thread["primary_subject"] or ""),
                "seed_followup_ids": sorted(int(value) for value in thread["seed_followup_ids"]),
                "allowed_followup_ids": allowed_ids,
                "reasons": list(dict.fromkeys(thread["reasons"]))[:3],
            }
        return result

    def hook_thread_contract(self) -> dict[int, dict[str, Any]]:
        """Fallback thread contract derived from ordinary reviewed HookPairs."""
        return self._hook_thread_contract(self.hook_pairs)

    def hook_package_thread_contract(self) -> dict[int, dict[str, Any]]:
        """Thread contract for complete A/B promise-and-proof opening packages."""
        return self._hook_thread_contract(self.director_hook_packages)

    def director_hook_contract(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], dict[int, dict[str, Any]], str]:
        """Return the sole Hook source for one director task.

        A strong package wins over ordinary pairs. This deliberately avoids a
        later generic HookPair replacing an already proven Hook -> proof setup.
        """
        packages = self.director_hook_packages
        if packages:
            return (
                tuple(package.to_dict() for package in packages),
                self.hook_package_thread_contract(),
                "hook_package",
            )
        return (
            tuple(pair.to_dict() for pair in self.hook_pairs),
            self.hook_thread_contract(),
            "hook_pair" if self.hook_pairs else "none",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "cache_key": self.cache_key,
            "candidate_digest": self.candidate_digest,
            "category": self.category,
            "model": self.model,
            "retained_duration": round(self.retained_duration, 3),
            "cards": [card.to_dict() for card in self.cards],
            "hook_pairs": [pair.to_dict() for pair in self.hook_pairs],
            "hook_packages": [package.to_dict() for package in self.hook_packages],
            "hook_pair_reviewed": bool(self.hook_pair_reviewed),
            "marketing_intent": self.marketing_intent.to_dict() if self.marketing_intent else {},
        }

    def summary(self, mode: str, fallback_reason: str = "") -> dict[str, Any]:
        summary = {
            "mode": normalize_review_mode(mode),
            "version": self.version,
            "cache_hit": bool(self.cache_hit),
            "card_count": len(self.cards),
            "main_count": sum(1 for card in self.cards if card.tier == "main"),
            "reserve_count": sum(1 for card in self.cards if card.tier == "reserve"),
            "retained_duration": round(self.retained_duration, 1),
            "grounded_card_count": len(self.cards),
            "hook_pair_count": len(self.hook_pairs),
            "hook_package_count": len(self.hook_packages),
            "hook_package_complete_count": sum(
                1 for package in self.hook_packages if package.package_complete
            ),
            "hook_package_a_count": sum(
                1 for package in self.hook_packages if package.opening_tier == "A"
            ),
            "hook_package_b_count": sum(
                1 for package in self.hook_packages if package.opening_tier == "B"
            ),
            "hook_package_c_count": sum(
                1 for package in self.hook_packages if package.opening_tier == "C"
            ),
            "hook_thread_count": len(self.director_hook_contract()[1]),
            "narrative_opportunity_count": len(self.narrative_opportunities),
            "director_hook_contract": self.director_hook_contract()[2],
            "director_hook_package_count": len(self.director_hook_packages),
            "hook_pair_reviewed": bool(self.hook_pair_reviewed),
            "fallback_reason": str(fallback_reason or ""),
        }
        marketing_summary = (self.marketing_intent or MarketingIntentBundle("")).summary()
        summary.update({
            "marketing_intent_version": marketing_summary["version"],
            "marketing_intent_response_present": marketing_summary["response_present"],
            "marketing_intent_count": marketing_summary["intent_count"],
            "marketing_arc_count": marketing_summary["arc_count"],
            "marketing_eligible_arc_count": marketing_summary["eligible_arc_count"],
            "marketing_arc_rejection_count": marketing_summary["rejection_count"],
            "primary_subject_count": sum(
                1 for card in self.cards if card.target_relation == "primary"
            ),
            "supporting_subject_count": sum(
                1 for card in self.cards if card.target_relation == "supporting"
            ),
            "other_subject_count": sum(
                1 for card in self.cards if card.target_relation == "other"
            ),
            "strong_evidence_count": sum(
                1 for card in self.cards
                if any(
                    marker in f"{card.evidence_type} {' '.join(card.quality_tags)}"
                    for marker in _STRONG_EVIDENCE_MARKERS
                )
            ),
        })
        return summary

    def director_hint(self) -> str:
        lines = [
            "\n\u2605AI\u5185\u5bb9\u5ba1\u7a3f\u5df2\u5b8c\u6210\u2605 \u4e0b\u5217\u7f16\u53f7\u662f\u7ecf\u8fc7\u540c\u4e3b\u9898\u6bd4\u8f83\u540e\u4fdd\u7559\u7684\u4e3b\u9009/\u5907\u7528\u5185\u5bb9\u3002",
            "\u5fc5\u987b\u4fdd\u6301\u539f\u53e5\u548c\u7f16\u53f7\uff1bmain\u4f18\u5148\uff0creserve\u4ec5\u7528\u4e8e\u8865\u8db3\u4e0d\u540c\u5356\u70b9\u6216\u65f6\u957f\u3002",
            "\u5185\u5bb9\u5361\u53ea\u8bf4\u660e\u5356\u70b9\u4ef7\u503c\u548c\u4e0a\u4e0b\u6587\u4f9d\u8d56\uff0c\u4e0d\u6307\u5b9aHook\u6216Close\u3002\u4f60\u5fc5\u987b\u7ed3\u5408\u5168\u7247\u53d9\u4e8b\u72ec\u7acb\u51b3\u5b9a\u5f00\u5934\u548c\u6536\u5c3e\u3002",
            "\u6709\u4e3b\u5546\u54c1\u65f6\u4f18\u5148\u9009\u201c\u4e0e\u4e3b\u5546\u54c1:primary\u201d\u4e14\u6709\u5177\u4f53\u539f\u6587\u8bc1\u636e\u7684\u5361\uff1b"
            "supporting\u53ea\u80fd\u7528\u4e8e\u8bc1\u660e\u4e3b\u5546\u54c1\uff1bother\u53ea\u80fd\u4f5c\u65f6\u957f\u6216\u8bed\u5883reserve\uff0c\u7edd\u4e0d\u53ef\u4f5cHook\u6216\u4e3b\u7ebf\u5356\u70b9\u3002",
        ]
        for card in sorted(self.cards, key=_content_card_priority, reverse=True):
            role_text = "/".join(card.roles) or "product"
            tag_text = "/".join(card.quality_tags)
            lines.append(
                f"- #{card.candidate_id:02d} [{card.tier}] {card.topic}/{card.subtopic}; "
                f"\u4ef7\u503c:{card.buyer_value}; \u8bc1\u636e:{card.evidence_type or '\u65e0'}; "
                f"\u539f\u6587\u8bc1\u636e:\"{card.evidence_quote}\"; "
                f"\u4e3b\u4f53:{card.primary_subject or '\u672a\u5224\u660e'}; \u4e0e\u4e3b\u5546\u54c1:{card.target_relation}; "
                f"\u4e3b\u4f53\u8bc1\u636e:\"{card.subject_evidence or '\u65e0'}\"; "
                f"\u89d2\u8272:{role_text}; \u4f9d\u8d56:{card.dependency}"
                + (f"; \u6807\u7b7e:{tag_text}" if tag_text else "")
            )

        active_packages = self.director_hook_packages
        if active_packages:
            lines.append(
                "★已验证A/B HookPackage：首段必须从以下组合的Hook编号中选择；"
                "第二段延续同主题即可，不要求死接示例编号。"
            )
            for package in active_packages:
                lines.append(
                    f"- HookPackage[{package.opening_tier}] #{package.hook_id:02d} → "
                    f"#{package.followup_id:02d}; 承诺:\"{package.hook_promise}\"; "
                    f"兑现:{package.proof_relation}; 依据:{package.reason}"
                )

        thread_contract = (
            self.hook_package_thread_contract()
            if active_packages else self.hook_thread_contract()
        )
        for thread in thread_contract.values():
            seed_ids = "/".join(f"#{value:02d}" for value in thread["seed_followup_ids"])
            allowed_ids = "/".join(f"#{value:02d}" for value in thread["allowed_followup_ids"])
            lines.append(
                f"- Hook主题线程: #{thread['hook_id']:02d} [{thread['topic']}]；"
                f"已验证承接:{seed_ids}；第二段可从同主题安全候选中选择:{allowed_ids}。"
                "允许跨原始时间，但必须延续同一商品/主题的解释、证明或展开。"
            )

        return "\n".join(lines) + "\n"

    def topic_support(self, inventory: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
        inventory_map = {int(item.get("srt_index") or 0): item for item in inventory}
        result: dict[str, dict[str, float]] = {}
        for card in self.cards:
            topic = card.topic.strip()
            if not topic or topic in {"\u5176\u4ed6", "\u901a\u7528\u5356\u70b9"}:
                continue
            item = result.setdefault(topic, {"main": 0.0, "reserve": 0.0, "evidence": 0.0, "duration": 0.0})
            item[card.tier] += 1.0
            if card.evidence_type and card.evidence_type not in {"\u65e0", "\u6cdb\u6cdb\u5938\u8d5e"}:
                item["evidence"] += 1.0
            item["duration"] += float(inventory_map.get(card.candidate_id, {}).get("duration_sec") or 0.0)
        return result


def _user_data_dir() -> Path:
    try:
        from config import USER_DATA_DIR
        return Path(USER_DATA_DIR)
    except Exception:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "LiveClipper"


def content_review_cache_dir() -> Path:
    return _user_data_dir() / "cache" / "ai_content_review"


def build_cache_key(
    candidate_digest: str,
    category: str,
    main_product: str,
    avoid: Iterable[str],
    model: str,
    content_policy: Any = None,
    include_marketing_intent: bool = False,
) -> str:
    payload = {
        "version": CONTENT_REVIEW_VERSION,
        "candidate_digest": str(candidate_digest or ""),
        "category": _clean_text(category, 80),
        "main_product": _clean_text(main_product, 100),
        "avoid": sorted(_clean_list(list(avoid or []), limit=30, item_limit=80)),
        "model": _clean_text(model, 120),
        # Candidate review may be reused across duration/preference changes,
        # but never across a different content eligibility policy.
        "content_policy": normalize_content_policy(content_policy),
        "marketing_intent": bool(include_marketing_intent),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_path(cache_key: str) -> Path:
    return content_review_cache_dir() / f"{cache_key}.json"


def _load_cache(cache_key: str) -> dict[str, Any] | None:
    path = _cache_path(cache_key)
    try:
        if not path.is_file():
            return None
        max_age = CONTENT_REVIEW_CACHE_DAYS * 86400
        if time.time() - path.stat().st_mtime > max_age:
            path.unlink(missing_ok=True)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
                        _LOG.warning("os error", exc_info=True)
        return None


def _cleanup_cache() -> None:
    root = content_review_cache_dir()
    if not root.is_dir():
        return
    now = time.time()
    max_age = CONTENT_REVIEW_CACHE_DAYS * 86400
    files: list[tuple[float, int, Path]] = []
    for path in root.glob("*.json"):
        try:
            stat = path.stat()
            if now - stat.st_mtime > max_age:
                path.unlink(missing_ok=True)
                continue
            files.append((stat.st_mtime, stat.st_size, path))
        except OSError:
            continue
    files.sort(reverse=True)
    kept_bytes = 0
    for index, (_mtime, size, path) in enumerate(files):
        kept_bytes += size
        if index >= CONTENT_REVIEW_CACHE_MAX_FILES or kept_bytes > CONTENT_REVIEW_CACHE_MAX_BYTES:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                                _LOG.warning("os error", exc_info=True)


def _write_cache(bundle: ContentReviewBundle) -> None:
    root = content_review_cache_dir()
    root.mkdir(parents=True, exist_ok=True)
    target = _cache_path(bundle.cache_key)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", delete=False, dir=str(root), suffix=".tmp"
        ) as handle:
            temp_name = handle.name
            json.dump(bundle.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        temp_name = ""
        _cleanup_cache()
    finally:
        if temp_name:
            try:
                os.remove(temp_name)
            except OSError:
                                _LOG.warning("os error", exc_info=True)


def _extract_json_object(content: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", str(content or "").strip(), flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as direct_error:
        data = None
        for start, char in enumerate(cleaned):
            if char != "{":
                continue
            closers: list[str] = []
            in_string = False
            escaped = False
            for end in range(start, len(cleaned)):
                current = cleaned[end]
                if in_string:
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        in_string = False
                    continue
                if current == '"':
                    in_string = True
                elif current == "{":
                    closers.append("}")
                elif current == "[":
                    closers.append("]")
                elif current in "}]":
                    if not closers or current != closers[-1]:
                        break
                    closers.pop()
                    if not closers:
                        try:
                            candidate = json.loads(cleaned[start:end + 1])
                        except json.JSONDecodeError:
                            break
                        if isinstance(candidate, dict):
                            data = candidate
                            break
            if isinstance(data, dict):
                break
        if data is None:
            raise direct_error
    if not isinstance(data, dict):
        raise ContentReviewError("\u5ba1\u7a3f\u54cd\u5e94\u4e0d\u662fJSON\u5bf9\u8c61")
    return data


def _candidate_map(inventory: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for raw in inventory:
        try:
            candidate_id = int(raw.get("srt_index") or 0)
        except (TypeError, ValueError):
            continue
        if candidate_id <= 0:
            continue
        result[candidate_id] = {
            "srt_index": candidate_id,
            "source": _clean_text(raw.get("source"), 24),
            "start": max(0.0, float(raw.get("start") or 0.0)),
            "end": max(0.0, float(raw.get("end") or 0.0)),
            "has_exact_timeline": "start" in raw or "end" in raw,
            "story_block_id": _clean_text(raw.get("story_block_id"), 80),
            "continuity_group_id": _clean_text(raw.get("continuity_group_id"), 80),
            "duration_sec": max(0.0, float(raw.get("duration_sec") or 0.0)),
            "text": _clean_text(raw.get("text"), 240),
        }
    return result

def _reviewable_candidate_text(text: Any, content_policy: Any = None) -> bool:
    cleaned = re.sub(r"^\s*\[V\d+\]\s*", "", str(text or ""), flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    interaction_reason = live_interaction_or_size_response_reason(cleaned)
    interaction_blocked, _reason = blocks_role(
        content_policy,
        interaction_policy_kind(interaction_reason),
        cleaned,
    ) if interaction_reason else (False, "")
    if interaction_reason and interaction_blocked:
        return False
    if candidate_quality_flags(cleaned, content_policy=content_policy):
        return False
    hook_reason = hook_ineligible_reason(cleaned)
    if hook_reason in {"展示铺垫不可作Hook", "空泛口头语不可作Hook"}:
        return False
    if len(cleaned) < 6:
        return False
    prefix_match = re.match(r"^[\u4e00-\u9fffA-Za-z][,\uff0c\u3001;\uff1b:\uff1a]", cleaned)
    if prefix_match and prefix_match.group(0)[0] not in "\u54c7\u5450\u54e6\u5bf9\u662f\u8fd9\u90a3\u54ce":
        return False
    if re.search(r"(?:\u4f46\u662f|\u800c\u4e14|\u56e0\u4e3a|\u6240\u4ee5|\u7136\u540e)(?:\u6211|\u4f60|\u4ed6|\u5979|\u5b83)?[.\u3002!\uff01?\uff1f]$", cleaned):
        return False
    if re.search(r"([\u6211\u4f60\u4ed6\u5979\u5b83])\1{2,}", cleaned):
        return False
    if re.search(r"(\u522b\u5435|\u522b\u8bf4\u8bdd|\u5b89\u9759)[,\uff0c\s]*\1", cleaned):
        return False
    if re.search(r"(.{2,4})\1{2,}", cleaned):
        return False
    return True

def _strong_emotional_hook_text(text: Any) -> bool:
    """Allow a grounded live-reaction Hook without treating empty hype as value."""

    compact = re.sub(r"\s+", "", str(text or ""))
    if len(compact) < 6:
        return False
    has_reaction = bool(re.search(
        r"(?:\u54c7|\u592a\u597d\u770b|\u5f88\u597d\u770b|\u5f88\u5e05|\u597d\u5e05|\u6211\u7684\u83dc|\u771f\u7684\u5f88|\u8d85\u7ea7\u63a8\u8350|\u5de8\u597d\u7a7f)",
        compact,
    ))
    has_grounding = bool(re.search(
        r"(?:\u8fd9\u4ef6|\u8fd9\u6761|\u8fd9\u5957|\u897f\u88c5|\u88e4|\u88d9|\u4e0a\u8863|\u989c\u8272|\u8272|\u7248\u578b|\u578b|\u7a7f\u642d|\u98ce\u683c|\u663e\u767d|\u663e\u7626|\u6c14\u8d28|\u660e\u661f\u673a\u573a)",
        compact,
    ))
    return has_reaction and has_grounding


def _package_emotional_hook_text(text: Any, content_policy: Any = None) -> bool:
    """Permit real live-reaction wording only inside a proven HookPackage.

    A phrase such as "很帅，真的很帅，我的菜" can be valuable in the
    first second even though it is weak as a standalone text claim. It never
    becomes a normal Hook candidate here: the package validator still requires
    an immediate, relation-specific proof before accepting it diagnostically.
    """
    if not _reviewable_candidate_text(text, content_policy=content_policy):
        return False
    if hook_ineligible_reason(text) or hook_candidate_quality_flags(text):
        return False
    compact = re.sub(r"\s+", "", str(text or "")).strip("，。！？!?、 ")
    if not compact or compact.startswith(("然后", "而且", "但是", "因为", "就是", "你们看")):
        return False
    return bool(re.search(
        r"(?:哇|我的天|好帅|很帅|好看|很久没出|我的菜|巨好穿|超级推荐)"
        r".*(?:真的|太|我的菜|很久没出|超级|巨|！|!)",
        compact,
    ))


def _reviewable_hook_text(text: Any, content_policy: Any = None) -> bool:
    if not _reviewable_candidate_text(text, content_policy=content_policy):
        return False
    if hook_ineligible_reason(text):
        return False
    if hook_candidate_quality_flags(text):
        return False
    cleaned = re.sub(r"^\s*\[V\d+\]\s*", "", str(text or ""), flags=re.I).strip()
    compact = re.sub(r"\s+", "", cleaned)
    if re.match(r"^(?:喂|哎|诶|欸)[，,。！？!?\s]*(?:我|你|这|那)", compact):
        return False
    if re.match(r"^我(?:其实|也|还)?[^。！？!?]{0,14}(?:质疑|跟你们讲|想说|想问|觉得)", compact):
        return False
    if re.match(r"^(?:你们|大家)[^。！？!?]{0,10}(?:细品|看一下|看一眼|听我说)", compact):
        return False
    if re.match(
        r"^(?:\u554a|\u5440|\u5450|\u90a3|\u5462|\u54ce|\u8bf6|\u55ef|\u54e6)?[\uff0c,\u3001]*(?:\u548c|\u8ddf|\u4e0e|\u800c\u4e14|\u4f46\u662f|\u56e0\u4e3a|\u6240\u4ee5|\u7136\u540e|\u5305\u62ec|\u8fd8\u6709)",
        compact,
    ):
        return False
    if not _strong_emotional_hook_text(cleaned) and re.match(
        r"^(?:(?:\u975e\u5e38)|(?:\u7279\u522b)|\u592a|\u5f88){1,3}(?:\u72e0|\u7edd|\u70b8|\u65e0\u654c|\u597d\u770b|\u597d\u6f02\u4eae|\u725b|\u9876)(?:[\uff0c,\u3002.!\uff01?\uff1f]|$)",
        compact,
    ):
        return False
    if re.match(r"^(?:很|非常|特别|太)\s*[A-Za-z]{2,16}\s*的?(?:这个|这件|这条|它)", compact, re.I):
        return False
    if re.search(r"(?:拖欠|欠你们|等了?(?:很久|好久)|终于(?:来了|到了)|刚到|新品(?:来了|到了)|今天上新)", cleaned):
        return False
    if re.search(
        r"(?:\u8fd9\u4e2a|\u90a3\u4e2a|\u5b83|\u8fd9\u4ef6)\s*(?:\u548c|\u8ddf|\u4e0e)\s*[\u4e00-\u9fffA-Za-z0-9]{1,8}[\u3002.!\uff01?\uff1f]?$",
        cleaned,
    ):
        return False
    weak_prefixes = (
        "\u7136\u540e", "\u800c\u4e14", "\u4f46\u662f", "\u56e0\u4e3a", "\u5c31\u662f",
        "\u5176\u5b9e", "\u597d\u4e86", "\u597d\u7684", "\u662f\u7684", "\u5bf9\uff0c", "\u4e0d\u642d\u8fb9",
    )
    return not cleaned.startswith(weak_prefixes)


def _grounded_evidence_quote(value: Any, candidate_text: Any) -> str:
    quote = _clean_text(value, 120)
    source = re.sub(r"^\s*\[V\d+\]\s*", "", str(candidate_text or ""), flags=re.I).strip()
    quote_chars = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", quote).lower()
    if len(quote_chars) < 2:
        return ""

    source_chars: list[str] = []
    source_positions: list[int] = []
    for position, character in enumerate(source):
        if re.match(r"[0-9A-Za-z\u4e00-\u9fff]", character):
            source_chars.append(character.lower())
            source_positions.append(position)
    match_at = "".join(source_chars).find(quote_chars)
    if match_at < 0:
        return ""

    start = source_positions[match_at]
    end = source_positions[match_at + len(quote_chars) - 1] + 1
    return source[start:end].strip()


def _card_rejection_reason(
    raw: Any,
    allowed_ids: set[int],
    candidates: Mapping[int, Mapping[str, Any]],
) -> str:
    if isinstance(raw, (list, tuple)):
        if len(raw) >= 10:
            raw = {
                "candidate_id": raw[0],
                "evidence_quote": raw[5],
                "tier": raw[9],
            }
        elif len(raw) >= 9:
            raw = {
                "candidate_id": raw[0],
                "tier": raw[8],
            }
        else:
            return "schema"
    if not isinstance(raw, Mapping):
        return "schema"
    try:
        candidate_id = int(raw.get("candidate_id") or raw.get("srt_index") or 0)
    except (TypeError, ValueError):
        return "candidate_id"
    if candidate_id not in allowed_ids:
        return "candidate_id"
    if _clean_text(raw.get("tier"), 16).lower() not in _VALID_TIERS:
        return "tier"
    return "semantic"


def _topic_from_candidate_text(value: Any) -> str:
    text = _clean_text(value, 240)
    best_family = "\u5176\u4ed6"
    best_score = 0
    for family, keywords in _TOPIC_FAMILY_RULES:
        score = sum(len(keyword) for keyword in keywords if keyword in text)
        if score > best_score:
            best_family = family
            best_score = score
    return best_family


def _normalize_card(
    raw: Any,
    allowed_ids: set[int],
    candidates: Mapping[int, Mapping[str, Any]],
) -> ContentCard | None:
    if isinstance(raw, (list, tuple)):
        if len(raw) >= 13:
            raw = {
                "candidate_id": raw[0],
                "topic": raw[1],
                "subtopic": raw[2],
                "buyer_value": raw[3],
                "evidence_type": raw[4],
                "evidence_quote": raw[5],
                "roles": raw[6],
                "dependency": raw[7],
                "quality_tags": raw[8],
                "tier": raw[9],
                "primary_subject": raw[10],
                "target_relation": raw[11],
                "subject_evidence": raw[12],
            }
        elif len(raw) >= 10:
            raw = {
                "candidate_id": raw[0],
                "topic": raw[1],
                "subtopic": raw[2],
                "buyer_value": raw[3],
                "evidence_type": raw[4],
                "evidence_quote": raw[5],
                "roles": raw[6],
                "dependency": raw[7],
                "quality_tags": raw[8],
                "tier": raw[9],
            }
        elif len(raw) >= 9:
            raw = {
                "candidate_id": raw[0],
                "topic": raw[1],
                "subtopic": raw[2],
                "buyer_value": raw[3],
                "evidence_type": raw[4],
                "roles": raw[5],
                "dependency": raw[6],
                "quality_tags": raw[7],
                "tier": raw[8],
            }
        else:
            return None
    if not isinstance(raw, Mapping):
        return None
    try:
        candidate_id = int(raw.get("candidate_id") or raw.get("srt_index") or 0)
    except (TypeError, ValueError):
        return None
    if candidate_id not in allowed_ids:
        return None
    tier = _clean_text(raw.get("tier"), 16).lower()
    if tier not in _VALID_TIERS:
        return None
    dependency = _clean_text(raw.get("dependency"), 24).lower()
    if dependency not in _VALID_DEPENDENCIES:
        dependency = "independent"

    candidate_text = re.sub(
        r"^\s*\[V\d+\]\s*", "", str(candidates.get(candidate_id, {}).get("text") or ""), flags=re.I
    ).strip()
    evidence_quote = _grounded_evidence_quote(raw.get("evidence_quote"), candidate_text)
    evidence_bound = not bool(evidence_quote)
    if evidence_bound:
        evidence_quote = _clean_text(candidate_text, 120)
    subtopic = _clean_text(raw.get("subtopic"), 60)
    topic = _reconcile_apparel_topic(raw.get("topic"), candidate_text, subtopic)
    subtopic = subtopic or topic
    buyer_value = _clean_text(raw.get("buyer_value"), 100) or "\u8865\u5145\u5546\u54c1\u4fe1\u606f"
    evidence_type = _clean_text(raw.get("evidence_type"), 40) or "\u539f\u6587\u7ed1\u5b9a"
    roles = tuple(
        role for role in _clean_list(raw.get("roles"), limit=4, item_limit=24)
        if role in _VALID_ROLES
    ) or ("product",)
    quality_tags = _clean_list(raw.get("quality_tags"), limit=4, item_limit=30)
    if evidence_bound and "\u539f\u6587\u7ed1\u5b9a" not in quality_tags:
        quality_tags = tuple((*quality_tags, "\u539f\u6587\u7ed1\u5b9a")[:4])
    primary_subject = _clean_text(raw.get("primary_subject"), 50)
    target_relation = _normalize_target_relation(raw.get("target_relation"))
    subject_evidence = _grounded_evidence_quote(
        raw.get("subject_evidence"),
        candidate_text,
    )
    # Product ownership is metadata, never a new hard filter. Do not accept an
    # ungrounded model claim as a reason to hide a safe candidate.
    subject_chars = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", primary_subject).lower()
    candidate_chars = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", candidate_text).lower()
    if (
        target_relation != "unknown"
        and (
            not subject_evidence
            or (subject_chars and subject_chars not in candidate_chars)
        )
    ):
        target_relation = "unknown"
        primary_subject = ""
        subject_evidence = ""
    return ContentCard(
        candidate_id=candidate_id,
        topic=topic,
        subtopic=subtopic,
        buyer_value=buyer_value,
        evidence_type=evidence_type,
        evidence_quote=evidence_quote,
        roles=roles,
        dependency=dependency,
        quality_tags=quality_tags,
        tier=tier,
        primary_subject=primary_subject,
        target_relation=target_relation,
        subject_evidence=subject_evidence,
    )

def _opening_segment_is_executable(candidate: Mapping[str, Any]) -> bool:
    """Reject a Hook unit only when its real timeline proves it cannot render."""
    if not isinstance(candidate, Mapping):
        return False
    try:
        duration = float(candidate.get("duration_sec") or 0.0)
    except (TypeError, ValueError):
        return False
    if duration <= 0.0:
        return False
    # Older cache fixtures and compatibility callers can describe content
    # without exact bounds. Production review inventory always has start/end;
    # only those timeline-backed candidates receive the final render limit.
    if not bool(candidate.get("has_exact_timeline")):
        return True
    return duration <= _DIRECTOR_OPENING_SEGMENT_MAX_SECONDS


def _normalize_hook_package(
    raw: Any,
    *,
    card_ids: set[int],
    candidates: Mapping[int, Mapping[str, Any]],
    cards_by_id: Mapping[int, ContentCard] | None = None,
    content_policy: Any = None,
) -> HookPackage | None:
    """Keep only grounded, self-contained Hook -> immediate-proof options.

    The review model may recommend a pair, but it never owns the final order.
    The director later chooses one of these verified openings for its own story.
    """
    if not isinstance(raw, Mapping):
        return None
    try:
        hook_id = int(raw.get("hook_id") or 0)
        followup_id = int(raw.get("followup_id") or 0)
    except (TypeError, ValueError):
        return None
    if (
        hook_id <= 0
        or followup_id <= 0
        or hook_id == followup_id
        or hook_id not in card_ids
        or followup_id not in card_ids
    ):
        return None
    hook_text = str(candidates.get(hook_id, {}).get("text") or "")
    followup_text = str(candidates.get(followup_id, {}).get("text") or "")
    if not (
        _opening_segment_is_executable(candidates.get(hook_id, {}))
        and _opening_segment_is_executable(candidates.get(followup_id, {}))
    ):
        return None
    if not (
        _reviewable_hook_text(hook_text, content_policy=content_policy)
        or _package_emotional_hook_text(hook_text, content_policy=content_policy)
    ) or not _reviewable_candidate_text(
        followup_text,
        content_policy=content_policy,
    ):
        return None
    hook_card = (cards_by_id or {}).get(hook_id)
    if hook_card is not None and not _card_hook_eligible(hook_card):
        return None
    followup_card = (cards_by_id or {}).get(followup_id)
    if (
        followup_card is not None
        and (
            followup_card.dependency != "independent"
            or followup_card.target_relation == "other"
        )
    ):
        return None
    topic = _normalize_topic(raw.get("topic"))
    if not topic or topic == "\u5176\u4ed6":
        topic = _topic_from_candidate_text(hook_text)
    reason = _clean_text(raw.get("reason"), 100) or "\u627f\u63a5\u5f00\u5934\u7684\u5177\u4f53\u8d2d\u4e70\u4ef7\u503c"
    # A promise must be a literal, grounded part of the Hook. This avoids
    # accepting a prettier model paraphrase that the original delivery never
    # actually made.
    hook_promise = _grounded_evidence_quote(raw.get("hook_promise"), hook_text)
    proof_relation = _clean_text(raw.get("proof_relation"), 40).lower()
    package_complete = bool(raw.get("package_complete") is True)
    raw_signals = raw.get("semantic_signals") if isinstance(
        raw.get("semantic_signals"), (list, tuple, set)
    ) else ()
    if (
        not hook_promise
        or proof_relation not in _VALID_HOOK_PROOF_RELATIONS
        or not package_complete
        or not _proof_relation_is_grounded(hook_text, followup_text, proof_relation)
    ):
        return None

    # A model may accurately describe the card topic while overstating the
    # opening pull of this particular sentence.  Only retain claimed signals
    # that are also visible in the spoken Hook itself.
    inferred_signals = _inferred_hook_semantic_signals(hook_text, topic)
    model_signals = tuple(
        signal for signal in _clean_list(raw_signals, limit=4, item_limit=32)
        if signal in _VALID_HOOK_SEMANTIC_SIGNALS and signal in inferred_signals
    )
    semantic_signals = tuple(dict.fromkeys((*model_signals, *inferred_signals)))[:4]
    if not semantic_signals:
        return None
    delivery_signal = _textual_delivery_signal(hook_text)
    opening_tier = _normalize_hook_package_tier(
        raw.get("opening_tier"),
        semantic_signals=semantic_signals,
        delivery_signal=delivery_signal,
    )
    return HookPackage(
        hook_id=hook_id,
        followup_id=followup_id,
        topic=topic,
        reason=reason,
        hook_promise=hook_promise,
        proof_relation=proof_relation,
        package_complete=package_complete,
        semantic_signals=semantic_signals,
        delivery_signal=delivery_signal,
        opening_tier=opening_tier,
    )


def _normalize_hook_pair(
    raw: Any,
    *,
    card_ids: set[int],
    candidates: Mapping[int, Mapping[str, Any]],
    cards_by_id: Mapping[int, ContentCard] | None = None,
    content_policy: Any = None,
) -> HookPair | None:
    """Validate the stable HookPair contract used by the current director."""
    if isinstance(raw, (list, tuple)):
        if len(raw) < 2:
            return None
        raw = {
            "hook_id": raw[0],
            "followup_id": raw[1],
            "topic": raw[2] if len(raw) > 2 else "",
            "reason": raw[3] if len(raw) > 3 else "",
        }
    if not isinstance(raw, Mapping):
        return None
    try:
        hook_id = int(raw.get("hook_id") or 0)
        followup_id = int(raw.get("followup_id") or 0)
    except (TypeError, ValueError):
        return None
    if (
        hook_id <= 0
        or followup_id <= 0
        or hook_id == followup_id
        or hook_id not in card_ids
        or followup_id not in card_ids
    ):
        return None
    hook_text = str(candidates.get(hook_id, {}).get("text") or "")
    followup_text = str(candidates.get(followup_id, {}).get("text") or "")
    if not (
        _opening_segment_is_executable(candidates.get(hook_id, {}))
        and _opening_segment_is_executable(candidates.get(followup_id, {}))
    ):
        return None
    if not _reviewable_hook_text(hook_text, content_policy=content_policy) or not _reviewable_candidate_text(
        followup_text,
        content_policy=content_policy,
    ):
        return None
    if not _has_director_grade_hook_mechanism(
        _inferred_hook_semantic_signals(hook_text)
    ):
        # An ordinary feature list remains eligible as body material, but it
        # must not reopen a weak Hook path after no A/B package survived.
        return None
    hook_card = (cards_by_id or {}).get(hook_id)
    followup_card = (cards_by_id or {}).get(followup_id)
    if (
        (hook_card is not None and not _card_hook_eligible(hook_card))
        or (followup_card is not None and (
            followup_card.dependency != "independent"
            or followup_card.target_relation == "other"
        ))
    ):
        return None
    topic = _normalize_topic(raw.get("topic"))
    if not topic or topic == "其他":
        topic = _topic_from_candidate_text(hook_text)
    return HookPair(
        hook_id=hook_id,
        followup_id=followup_id,
        topic=topic,
        reason=_clean_text(raw.get("reason"), 100) or "承接开头的具体购买价值",
    )


def _infer_hook_proof_relation(hook_text: Any, proof_text: Any) -> str:
    """Infer a narrow, executable proof relation for a reviewed HookPair.

    This only fills structural fields the broad review occasionally omits. It
    never creates a Hook from arbitrary Product cards.
    """
    family = _hook_promise_family(hook_text)
    preferred_relations = {
        "fit": ("visual_result", "design_reason", "wearing_experience"),
        "style": (
            "identity_projection", "visual_result", "design_reason",
            "scene_projection",
        ),
        "material": ("material_evidence", "wearing_experience"),
        "comfort": ("wearing_experience", "material_evidence"),
        "color": ("visual_result", "scene_projection"),
        "scene": ("scene_projection", "visual_result", "identity_projection"),
        "source": ("source_value",),
        "social": ("social_proof",),
        "price": ("price_value",),
        "after_sale": ("after_sale_confidence",),
    }
    for relation in preferred_relations.get(family, ()):
        if _proof_relation_is_grounded(hook_text, proof_text, relation):
            return relation
    return ""


def _hook_promise_excerpt(hook_text: Any) -> str:
    """Keep a literal, compact promise quote for a synthesized package."""
    text = re.sub(r"^\s*\[V\d+\]\s*", "", str(hook_text or ""), flags=re.I).strip()
    if not text:
        return ""
    first_sentence = re.split(r"[。！？!?]", text, maxsplit=1)[0].strip("，、；;：: ")
    return (first_sentence or text)[:80]


def _synthesize_hook_package_from_pair(
    pair: HookPair,
    *,
    card_ids: set[int],
    candidates: Mapping[int, Mapping[str, Any]],
    cards_by_id: Mapping[int, ContentCard] | None = None,
    content_policy: Any = None,
) -> HookPackage | None:
    """Recover a package only when an existing reviewed pair proves it.

    DeepSeek occasionally returns a valid HookPair but omits the newer package
    fields. The pair is evidence, not a license to weaken the Hook gate: a
    typed proof relation and the original spoken promise still go through the
    normal package validator.
    """
    hook_text = str(candidates.get(pair.hook_id, {}).get("text") or "")
    proof_text = str(candidates.get(pair.followup_id, {}).get("text") or "")
    relation = _infer_hook_proof_relation(hook_text, proof_text)
    promise = _hook_promise_excerpt(hook_text)
    if not relation or not promise:
        return None
    signals = _inferred_hook_semantic_signals(hook_text, pair.topic)
    if not _has_director_grade_hook_mechanism(signals):
        return None
    return _normalize_hook_package(
        {
            "hook_id": pair.hook_id,
            "followup_id": pair.followup_id,
            "topic": pair.topic,
            "reason": pair.reason,
            "hook_promise": promise,
            "proof_relation": relation,
            "package_complete": True,
            "semantic_signals": list(signals),
            "opening_tier": "A" if _textual_delivery_signal(hook_text) == "textual_high" else "B",
        },
        card_ids=card_ids,
        candidates=candidates,
        cards_by_id=cards_by_id,
        content_policy=content_policy,
    )


def _rebalance_card_tiers(
    cards: Sequence[ContentCard], candidates: Mapping[int, Mapping[str, Any]]
) -> list[ContentCard]:
    sources = {
        str(candidates.get(card.candidate_id, {}).get("source") or "").strip().upper()
        for card in cards
        if str(candidates.get(card.candidate_id, {}).get("source") or "").strip()
    }
    mixed = len(sources) > 1
    topic_main_counts: dict[str, int] = {}
    source_topic_main_counts: dict[tuple[str, str], int] = {}
    balanced: list[ContentCard] = []
    for card in cards:
        if card.tier != "main":
            balanced.append(card)
            continue
        if card.target_relation == "other":
            balanced.append(replace(card, tier="reserve"))
            continue
        source = str(candidates.get(card.candidate_id, {}).get("source") or "").strip().upper()
        topic_key = card.topic or "\u5176\u4ed6"
        source_topic_key = (source, topic_key)
        if topic_main_counts.get(topic_key, 0) >= 4 or (
            mixed and source_topic_main_counts.get(source_topic_key, 0) >= 2
        ):
            balanced.append(replace(card, tier="reserve"))
            continue
        topic_main_counts[topic_key] = topic_main_counts.get(topic_key, 0) + 1
        source_topic_main_counts[source_topic_key] = source_topic_main_counts.get(source_topic_key, 0) + 1
        balanced.append(card)
    return balanced

def _validate_bundle(
    data: Mapping[str, Any],
    *,
    inventory: Sequence[Mapping[str, Any]],
    cache_key: str,
    candidate_digest: str,
    category: str,
    model: str,
    required_sources: Mapping[str, int] | None,
    main_product: str = "",
    content_policy: Any = None,
) -> ContentReviewBundle:
    candidates = _candidate_map(inventory)
    allowed_ids = {
        candidate_id for candidate_id, item in candidates.items()
        if _reviewable_candidate_text(item.get("text"), content_policy=content_policy)
    }
    if not allowed_ids:
        raise ContentReviewError("\u6ca1\u6709\u5b89\u5168\u5019\u9009")

    cards: list[ContentCard] = []
    rejection_counts: dict[str, int] = {}
    seen_ids: set[int] = set()
    signature_counts: dict[tuple[str, str, str], int] = {}
    raw_cards = data.get("cards") if isinstance(data.get("cards"), list) else []
    for raw in raw_cards:
        card = _normalize_card(raw, allowed_ids, candidates)
        if card is None:
            reason = _card_rejection_reason(raw, allowed_ids, candidates)
            rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
            continue
        if card.candidate_id in seen_ids:
            rejection_counts["duplicate"] = rejection_counts.get("duplicate", 0) + 1
            continue
        signature = (card.topic, card.subtopic, card.buyer_value)
        if signature_counts.get(signature, 0) >= 3:
            continue
        signature_counts[signature] = signature_counts.get(signature, 0) + 1
        seen_ids.add(card.candidate_id)
        cards.append(card)
        if len(cards) >= CONTENT_REVIEW_MAX_CARDS:
            break

    if not cards:
        rejection_text = ",".join(
            f"{reason}={count}" for reason, count in sorted(rejection_counts.items())
        ) or "none"
        top_keys = ",".join(sorted(str(key) for key in data.keys()))[:120] or "none"
        raise ContentReviewError(
            f"\u5ba1\u7a3f\u672a\u8fd4\u56de\u53ef\u7528\u5185\u5bb9\u5361"
            f"\uff08cards={len(raw_cards)}, reject={rejection_text}, keys={top_keys}\uff09"
        )
    cards = _rebalance_card_tiers(cards, candidates)
    retained_duration = sum(candidates[card.candidate_id]["duration_sec"] for card in cards)
    # A non-empty, grounded reviewed pool is valid even when it is short.
    # Duration is handled by the director's existing best-effort policy. A
    # duration rejection here would fall back to the raw pool and reintroduce
    # exactly the transcript residue the review rejected.

    requirements = {
        str(source or "").strip().upper(): max(1, int(count or 1))
        for source, count in (required_sources or {}).items()
        if str(source or "").strip()
    }
    if requirements:
        kept_by_source: dict[str, int] = {}
        for card in cards:
            source = str(candidates[card.candidate_id].get("source") or "").strip().upper()
            if source:
                kept_by_source[source] = kept_by_source.get(source, 0) + 1
        missing = [source for source, minimum in requirements.items() if kept_by_source.get(source, 0) < minimum]
        if missing:
            raise ContentReviewError("\u5ba1\u7a3f\u5019\u9009\u7f3a\u5c11\u6df7\u526a\u6765\u6e90:" + ",".join(missing))

    card_ids = {card.candidate_id for card in cards}
    cards_by_id = {card.candidate_id: card for card in cards}
    hook_pairs: list[HookPair] = []
    seen_pairs: set[tuple[int, int]] = set()
    raw_hook_pairs = data.get("hook_pairs") if isinstance(data.get("hook_pairs"), list) else []
    for raw_pair in raw_hook_pairs:
        pair = _normalize_hook_pair(
            raw_pair,
            card_ids=card_ids,
            candidates=candidates,
            cards_by_id=cards_by_id,
            content_policy=content_policy,
        )
        if pair is None:
            continue
        signature = (pair.hook_id, pair.followup_id)
        if signature in seen_pairs:
            continue
        seen_pairs.add(signature)
        hook_pairs.append(pair)
        if len(hook_pairs) >= 8:
            break

    hook_packages: list[HookPackage] = []
    seen_packages: set[tuple[int, int]] = set()
    raw_hook_packages = data.get("hook_packages") if isinstance(data.get("hook_packages"), list) else []
    for raw_package in raw_hook_packages:
        package = _normalize_hook_package(
            raw_package,
            card_ids=card_ids,
            candidates=candidates,
            cards_by_id=cards_by_id,
            content_policy=content_policy,
        )
        if package is None:
            continue
        signature = (package.hook_id, package.followup_id)
        if signature in seen_packages:
            continue
        seen_packages.add(signature)
        hook_packages.append(package)
        if len(hook_packages) >= 8:
            break

    marketing_intent = build_marketing_intent_bundle(
        data,
        cards=cards,
        inventory=inventory,
        candidate_digest=candidate_digest,
        main_product=main_product,
    )
    return ContentReviewBundle(
        cache_key=cache_key,
        candidate_digest=str(candidate_digest or ""),
        category=_clean_text(category, 80),
        model=_clean_text(model, 120),
        cards=tuple(cards),
        retained_duration=retained_duration,
        hook_pairs=tuple(hook_pairs),
        hook_packages=tuple(hook_packages),
        marketing_intent=marketing_intent,
        hook_pair_reviewed=bool(data.get("hook_pair_reviewed")),
    )


def _review_prompts(
    inventory: Sequence[Mapping[str, Any]],
    *,
    category: str,
    main_product: str,
    avoid: Sequence[str],
    required_sources: Mapping[str, int] | None,
    format_retry: bool,
    content_policy: Any = None,
    include_marketing_intent: bool = False,
) -> tuple[str, str]:
    compact_inventory = []
    for item in inventory:
        row = [
            int(item.get("srt_index") or 0),
            _clean_text(item.get("source"), 24),
            round(max(0.0, float(item.get("duration_sec") or 0.0)), 1),
        ]
        if include_marketing_intent:
            row.extend((
                _clean_text(item.get("story_block_id"), 40),
                _clean_text(item.get("continuity_group_id"), 40),
            ))
        row.append(_clean_text(item.get("text"), 240))
        compact_inventory.append(row)
    system_prompt = (
        "\u4f60\u662f\u5e26\u8d27\u77ed\u89c6\u9891\u7684\u5185\u5bb9\u5ba1\u7a3f\uff0c\u4e0d\u662f\u6700\u7ec8\u526a\u8f91\u5bfc\u6f14\u3002"
        "\u4f60\u53ea\u8d1f\u8d23\u8bc6\u522b\u503c\u5f97\u4f7f\u7528\u7684\u5177\u4f53\u5185\u5bb9\u3001\u4e3b\u9898\u3001\u8d2d\u4e70\u4ef7\u503c\u3001\u4e0a\u4e0b\u6587\u4f9d\u8d56\u548c\u8f6c\u5199\u98ce\u9669\u3002"
        "\u4e0d\u5f97\u51b3\u5b9a\u6700\u7ec8\u6210\u7247\u987a\u5e8f\u3001Close\u6216\u6539\u5199\u5b57\u5e55\uff0c\u4e5f\u4e0d\u5f97\u7f16\u9020\u7f16\u53f7\u3002"
        "\u4f60\u53ef\u4ee5\u63d0\u51fa\u5c11\u91cf\u53ef\u9a8c\u8bc1\u7684Hook+\u627f\u63a5\u5019\u9009\u7ec4\u5408\uff0c\u4f9b\u6700\u7ec8\u5bfc\u6f14\u81ea\u4e3b\u9009\u62e9\uff1b\u8fd9\u4e0d\u662f\u66ff\u5bfc\u6f14\u7f16\u6392\u5168\u7247\u3002"
        "\u6bcf\u5f20\u5361\u53ea\u9700\u9009\u62e9\u771f\u5b9e\u5019\u9009\u7f16\u53f7\u5e76\u5224\u65ad\u5185\u5bb9\u4ef7\u503c\uff1b\u7a0b\u5e8f\u4f1a\u7528\u7f16\u53f7\u7ed1\u5b9a\u539f\u5b57\u5e55\u4f5c\u4e3a\u552f\u4e00\u8bc1\u636e\u3002"
        "\u5185\u5bb9\u5361\u7684\u524d\u63d0\u662f\uff1a\u539f\u5b57\u5e55\u5fc5\u987b\u53ef\u4ee5\u9010\u5b57\u76f4\u63a5\u64ad\u51fa\uff0c\u8131\u79bb\u524d\u540e\u6587\u4ecd\u5b8c\u6574\u3001\u8bed\u4e49\u81ea\u7136\u3002"
        "\u7edd\u4e0d\u80fd\u6839\u636e\u4e0a\u4e0b\u6587\u66ff\u539f\u5b57\u5e55\u8111\u8865\u6216\u7ea0\u6b63\u9519\u8bcd\u3002\u51e1\u662f\u660e\u663eASR\u4e71\u7801\u3001\u4e3b\u8c13\u6216\u6307\u4ee3\u65ad\u88c2\u3001\u534a\u53e5\u3001\u76f4\u64ad\u95ee\u7b54\u3001"
        "\u4e2a\u4eba\u8eab\u9ad8\u4f53\u91cd\u6216\u62a5\u5c3a\u7801\u3001\u4ef7\u683c\u6216\u6210\u672c\u6216\u6bcf\u7c73\u62a5\u4ef7\u3001\u5e93\u5b58\u6216CTA\uff0c\u5373\u4f7f\u4f60\u80fd\u731c\u51fa\u60f3\u8868\u8fbe\u4ec0\u4e48\uff0c\u4e5f\u4e0d\u5f97\u8f93\u51fa\u5185\u5bb9\u5361\u3002"
        "\u53ea\u8f93\u51fa\u5355\u884c\u7d27\u51d1JSON\u5bf9\u8c61\uff0c\u4e0d\u8981Markdown\u3001\u89e3\u91ca\u6216\u7f29\u8fdb\u3002"
    )
    system_prompt += (
        "\u53ea\u8981\u5019\u9009\u5185\u4efb\u4e00\u5173\u952e\u5206\u53e5\u542b\u660e\u663e\u9519\u8bcd\u3001\u75c5\u53e5\u3001\u8df3\u53d8\u8bdd\u9898\u6216\u76f4\u64ad\u6b8b\u7559\uff0c\u5373\u4f7f\u4e2d\u95f4\u5939\u7740\u4e00\u4e2a\u5356\u70b9\u77ed\u8bed\uff0c\u4e5f\u6574\u6bb5\u4e0d\u5f97\u8f93\u51fa\u5185\u5bb9\u5361\uff1b"
        "\u4e0d\u5f97\u628a\u574f\u53e5\u5f53\u4f5c\u597d\u53e5\u7684\u5bb9\u5668\u3002"
    )
    source_rule = ""
    if required_sources:
        source_rule = "\u6df7\u526a\u6765\u6e90\u4e0d\u5f97\u9057\u6f0f:" + "\u3001".join(
            f"{source}\u81f3\u5c11{max(1, int(count or 1))}\u5f20\u5361"
            for source, count in sorted(required_sources.items())
        )
        source_rule += (
            "\u3002\u8d28\u91cf\u76f8\u5f53\u65f6\uff0ccards\u548cmain\u90fd\u8981\u4fdd\u7559\u5404\u6765\u6e90\u7684\u4ee3\u8868\u6027\u5185\u5bb9\uff1b"
            "\u4e0d\u5f97\u8ba9\u5355\u4e00\u6765\u6e90\u5360\u7edd\u5927\u591a\u6570\u3002"
        )
    content_policy_rule = "\n".join(policy_prompt_lines(content_policy)) or "\u65e0\u989d\u5916\u5185\u5bb9\u653f\u7b56"
    system_prompt += (
        "\n\u672c\u6b21\u5185\u5bb9\u4f7f\u7528\u653f\u7b56\u662f\u6700\u7ec8\u89c4\u5219\uff0c\u4f18\u5148\u4e8e\u4e0a\u6587\u5bf9\u4ef7\u683c/CTA/\u5c3a\u7801/\u4e92\u52a8\u7684\u6cdb\u5316\u63cf\u8ff0\u3002\n"
        + content_policy_rule
    )
    retry_rule = (
        "Previous response format was invalid. Return strict JSON only. "
        "Keep 18-28 representative cards, every short field under 24 Chinese characters, "
        "at most two quality tags per card, and at most four hook packages."
        if format_retry else ""
    )
    marketing_contract = marketing_intent_prompt_contract() if include_marketing_intent else ""
    schema_example = {
        "cards": [[1, "\u7248\u578b\u663e\u7626", "\u80a9\u5bbd\u4fee\u9970", "\u8bf4\u6e05\u80a9\u7ebf\u5982\u4f55\u5411\u5185\u6536", "\u539f\u56e0\u89e3\u91ca", "\u80a9\u7ebf\u4f1a\u5f80\u91cc\u6536", ["effect", "evidence"], "independent", ["\u5177\u4f53\u6548\u679c", "\u539f\u56e0\u89e3\u91ca"], "main", "\u8fd9\u4ef6\u4e0a\u8863", "primary", "\u80a9\u7ebf\u4f1a\u5f80\u91cc\u6536"]],
        "hook_packages": [{
            "hook_id": 1,
            "followup_id": 2,
            "topic": "\u7248\u578b\u663e\u7626",
            "reason": "\u4e0b\u4e00\u6bb5\u89e3\u91ca\u80a9\u7ebf\u5185\u6536\u7684\u8bbe\u8ba1\u539f\u56e0",
            "hook_promise": "\u80a9\u7ebf\u4f1a\u5f80\u91cc\u6536",
            "proof_relation": "design_reason",
            "package_complete": True,
            "semantic_signals": ["result"],
            "opening_tier": "B",
        }],
        "hook_pairs": [[1, 2, "版型显瘦", "下一段解释肩线内收的设计原因"]],
    }
    candidate_field_order = "\u5b89\u5168\u5019\u9009\u5b57\u6bb5\u987a\u5e8f:[\u7f16\u53f7,\u6765\u6e90,\u65f6\u957f\u79d2,\u539f\u5b57\u5e55]"
    marketing_field_order = ""
    if include_marketing_intent:
        schema_example.update({
            "marketing_intents": [[1, "identity_expression", "\u80a9\u7ebf\u6536\u51fa\u66f4\u5229\u843d\u7684\u7a7f\u7740\u5370\u8c61", "\u80a9\u7ebf\u4f1a\u5f80\u91cc\u6536"]],
            "narrative_arcs": [[1, [2], [3], "identity_expression", "2\u53f7\u89e3\u91ca\u80a9\u7ebf\u5185\u6536\u7684\u8bbe\u8ba1\u539f\u56e0\uff0c3\u53f7\u7ed9\u51fa\u4e0a\u8eab\u573a\u666f"]],
        })
        candidate_field_order = "\u5b89\u5168\u5019\u9009\u5b57\u6bb5\u987a\u5e8f:[\u7f16\u53f7,\u6765\u6e90,\u65f6\u957f\u79d2,\u6545\u4e8b\u533a\u95f4,\u8fde\u7eed\u7ec4,\u539f\u5b57\u5e55]"
        marketing_field_order = (
            "marketing_intents\u5b57\u6bb5\u987a\u5e8f:[\u5019\u9009\u7f16\u53f7,intent_type,\u53ef\u88ab\u539f\u53e5\u8bc1\u660e\u7684\u4e3b\u5f20,\u539f\u6587\u77ed\u5f15]\n"
            "narrative_arcs\u5b57\u6bb5\u987a\u5e8f:[\u5f00\u5934\u5019\u9009\u7f16\u53f7,\u76f4\u63a5\u8bc1\u660e\u5019\u9009\u7f16\u53f7\u6570\u7ec4,\u53ef\u9009\u7ed3\u679c\u5019\u9009\u7f16\u53f7\u6570\u7ec4,intent_type,\u5151\u73b0\u8bf4\u660e]"
        )
    schema_text = json.dumps(schema_example, ensure_ascii=False, separators=(",", ":"))
    user_prompt = f"""{retry_rule}
\u54c1\u7c7b:{category or '\u901a\u7528'}
\u4e3b\u5546\u54c1:{main_product or '\u672a\u6307\u5b9a'}
\u7528\u6237\u8981\u6c42\u907f\u5f00:{'\u3001'.join(avoid) if avoid else '\u65e0'}
{source_rule}
\u5185\u5bb9\u4f7f\u7528\u653f\u7b56:\n{content_policy_rule}

\u8981\u6c42:
1. \u5ba1\u9605\u5168\u90e8\u5019\u9009\uff0c\u4f46\u53ea\u8f93\u51fa\u503c\u5f97\u4ea4\u7ed9\u5bfc\u6f14\u7684\u5361\u3002cards\u4e0a\u9650{CONTENT_REVIEW_MAX_CARDS}\u4e0d\u662f\u586b\u6ee1\u76ee\u6807\uff0c\u901a\u5e3825-55\u9879\uff1b\u5185\u5bb9\u5145\u8db3\u65f6main+reserve\u539f\u7247\u5408\u8ba1\u5c3d\u91cf\u8fbe\u5230{CONTENT_REVIEW_TARGET_DURATION:.0f}\u79d2\u3002\u8d28\u91cf\u6c38\u8fdc\u9ad8\u4e8e\u65f6\u957f\uff0c\u5b81\u53ef\u5c11\u4e8e\u76ee\u6807\u4e5f\u4e0d\u5f97\u7528\u6b8b\u53e5\u3001\u4e71\u7801\u3001\u95f2\u804a\u3001\u7eaf\u5c55\u793a\u94fa\u57ab\u6216\u91cd\u590d\u5185\u5bb9\u51d1\u79d2\u6570\u3002
2. \u540c\u4e00\u5177\u4f53\u5b50\u4e3b\u9898\u53ea\u75591\u4e2amain\u548c1-2\u4e2areserve\u3002\u5019\u9009\u8d28\u91cf\u7528\u5c3d\u5c31\u505c\uff0c\u4e0d\u8981\u7528\u91cd\u590d\u6a21\u677f\u51d1\u6570\u3002
3. topic\u7528\u7a33\u5b9a\u5356\u70b9\u7c7b\u522b\uff0csubtopic\u5199\u5177\u4f53\u95ee\u9898\uff1bbuyer_value\u53ea\u6982\u62ec\u8be5\u539f\u53e5\u5bf9\u8d2d\u4e70\u51b3\u7b56\u7684\u4ef7\u503c\u3002
4. primary_subject\u662f\u539f\u53e5\u5b9e\u9645\u5728\u8bb2\u7684\u5546\u54c1/\u5bf9\u8c61\uff0c\u4e0d\u80fd\u56e0\u4e3a\u987a\u5e26\u63d0\u5230\u800c\u7b97\u4e3b\u4f53\u3002target_relation\u53ea\u80fd\u662fprimary/supporting/other/unknown\uff1aprimary=\u4e3b\u5546\u54c1\u662f\u672c\u6bb5\u4e3b\u4f53\uff0csupporting=\u4e3b\u8981\u4e3a\u4e3b\u5546\u54c1\u63d0\u4f9b\u642d\u914d\u6216\u8bc1\u660e\uff0cother=\u4e3b\u4f53\u5df2\u8f6c\u4e3a\u53e6\u4e00\u5546\u54c1\uff0cunknown=\u539f\u53e5\u4e0d\u8db3\u4ee5\u5224\u65ad\u3002subject_evidence\u5fc5\u987b\u662f\u539f\u53e5\u91cc\u4e00\u6bb5\u8fde\u7eed\u7684\u77ed\u5f15\uff0c\u6ca1\u6709\u8bc1\u636e\u5c31\u8fd4\u56deunknown\u3002
5. roles\u53ea\u80fd\u4f7f\u7528effect,evidence,scene,objection,product\u3002\u8fd9\u4e9b\u53ea\u662f\u5185\u5bb9\u529f\u80fd\uff0c\u4e0d\u662f\u6210\u7247\u4f4d\u7f6e\u3002dependency\u53ea\u80fd\u4f7f\u7528independent,needs_previous,needs_next,needs_both\u3002
6. topic/subtopic/buyer_value\u5fc5\u987b\u53ea\u4f9d\u636e\u8be5\u7f16\u53f7\u539f\u5b57\u5e55\uff0c\u4e0d\u5f97\u628a\u201c\u5c55\u793a\u4e00\u4e0b\u201d\u81c6\u6d4b\u6210\u5ea6\u5047\u3001\u901a\u52e4\u6216\u5176\u4ed6\u672a\u8bf4\u51fa\u7684\u573a\u666f\uff1b\u4e0d\u5f97\u6539\u5199\u5b57\u5e55\uff0c\u7a0b\u5e8f\u4f1a\u6309\u7f16\u53f7\u7ed1\u5b9a\u539f\u6587\u3002
7. quality_tags\u7528\u7b80\u77ed\u6807\u7b7e\uff0c\u5982\u5177\u4f53\u6548\u679c\u3001\u539f\u56e0\u89e3\u91ca\u3001\u5b9e\u6d4b\u8bc1\u636e\u3001\u4eba\u7fa4\u660e\u786e\u3001\u573a\u666f\u6e05\u6670\u3001ASR\u98ce\u9669\u3002\u53ea\u6709\u539f\u53e5\u786e\u5b9e\u5c5e\u4e8e\u6cdb\u6cdb\u5938\u8d5e\u3001\u5c55\u793a\u94fa\u57ab\u6216\u91cd\u590d\u65f6\u624d\u5982\u5b9e\u6253\u6807\uff0c\u4e0d\u5f97\u628a\u5b83\u4eec\u4f2a\u88c5\u6210\u5177\u4f53\u4ef7\u503c\u3002
8. \u660e\u663e\u4ece\u534a\u53e5\u5f00\u59cb\u6216\u7ed3\u5c3e\u672a\u5b8c\u3001\u6307\u4ee3\u4e0d\u660e\u3001ASR\u4e71\u7801\u3001\u8fde\u7eed\u7ed3\u5df4\u91cd\u590d\u3001\u7eaf\u4e92\u52a8\u6216\u7eaf\u94fa\u57ab\u7684\u5019\u9009\u4e0d\u5f97\u8fdb\u5165main/reserve\u3002\u4e0d\u8981\u628a\u6b8b\u53e5\u4ec5\u6807\u8bb0dependency\u540e\u7ee7\u7eed\u4fdd\u7559\u3002
9. tier\u5fc5\u987b\u771f\u5b9e\u5206\u5c42\uff1amain\u53ea\u7ed9\u540c\u7c7b\u4e2d\u6700\u5b8c\u6574\u3001\u6700\u5177\u4f53\u3001\u8bc1\u636e\u6700\u5f3a\u7684\u8868\u8fbe\uff0c\u5176\u4f59\u9ad8\u8d28\u91cf\u8868\u8fbe\u6807reserve\u3002\u6807other\u7684\u5f53\u7136\u53ef\u4f5c\u5b89\u5168reserve\uff0c\u4f46\u4e0d\u5f97\u6807main\u6216\u4f5cHook\u3002\u5185\u5bb9\u5145\u8db3\u65f6main\u7ea6\u536035%-65%\uff0c\u7981\u6b62\u5168\u90e8\u6807main\uff1b\u540c\u4e00\u5927topic\u6700\u591a4\u5f20main\uff0c\u6df7\u526a\u65f6\u540c\u4e00\u6765\u6e90+\u540c\u4e00\u5927topic\u6700\u591a2\u5f20main\u3002
10. topic\u4f7f\u7528\u7a33\u5b9a\u5927\u7c7b\uff1a\u670d\u9970\u4f18\u5148\u7528\u7248\u578b\u663e\u7626/\u4e0a\u8eab\u6548\u679c/\u9762\u6599\u8d28\u611f/\u989c\u8272\u6c1b\u56f4/\u98ce\u683c\u5b9a\u4f4d/\u573a\u666f\u642d\u914d/\u5de5\u827a\u7ec6\u8282/\u5c3a\u5bf8\u957f\u5ea6/\u7a7f\u7740\u4f53\u9a8c/\u5bf9\u6bd4\u4f18\u52bf/\u6d41\u884c\u8d8b\u52bf\uff1b\u98df\u54c1\u4f18\u5148\u7528\u53e3\u611f\u98df\u6b32/\u65b0\u9c9c\u54c1\u8d28/\u4ea7\u5730\u6eaf\u6e90/\u89c4\u683c\u5206\u91cf/\u53d1\u8d27\u4fdd\u9c9c/\u573a\u666f\u5403\u6cd5\uff1b\u65b0\u54c1\u7c7b\u7528\u5bf9\u5e94\u7a33\u5b9a\u5927\u7c7b\u3002\u5206\u7c7b\u8fb9\u754c\u5fc5\u987b\u4e25\u683c\uff1a\u4e0a\u8eab\u6548\u679c\u53ea\u6307\u6302\u7740\u3001\u5e73\u94fa\u6216\u8bd5\u7a7f\u524d\u540e\u4e0e\u7a7f\u4e0a\u540e\u7684\u89c6\u89c9\u53cd\u5dee\u3001\u6574\u4f53\u6548\u679c\uff0c\u4e0d\u80fd\u62ff\u8212\u670d/\u900f\u6c14\u5192\u5145\uff1b\u7a7f\u7740\u4f53\u9a8c\u53ea\u6307\u89e6\u611f\u3001\u6e29\u5ea6\u3001\u675f\u7f1a\u548c\u6d3b\u52a8\u611f\u53d7\uff1b\u98ce\u683c\u5b9a\u4f4d\u6307\u5b66\u9662\u3001\u7f8e\u5f0f\u3001\u97e9\u7cfb\u3001\u4fcf\u76ae\u3001\u51cf\u9f84\u3001\u4f18\u96c5\u3001\u6c14\u8d28\u7b49\u6b3e\u5f0f\u8eab\u4efd\uff0c\u4e0d\u7b49\u4e8e\u6d41\u884c\u8d8b\u52bf\uff1b\u6d41\u884c\u8d8b\u52bf\u53ea\u5728\u539f\u53e5\u660e\u786e\u8bf4\u5f53\u5b63\u3001\u4eca\u5e74\u3001\u6d41\u884c\u3001\u8d8b\u52bf\u3001\u70ed\u95e8\u6216\u79c0\u573a\u65f6\u4f7f\u7528\uff1b\u590f\u5929\u3001\u65e9\u79cb\u3001\u6362\u5b63\u7b49\u7a7f\u7740\u65f6\u95f4\u548c\u6e29\u5ea6\u5f52\u573a\u666f\u642d\u914d\uff1b\u5c3a\u5bf8\u957f\u5ea6\u53ea\u5199\u771f\u5b9e\u88d9\u957f\u3001\u8863\u957f\u3001\u8986\u76d6\u4f4d\u7f6e\u6216\u5c3a\u5bf8\u4e8b\u5b9e\uff0c\u5355\u7eaf\u201c\u77ed\u767e\u8936\u88d9\u7684\u98ce\u683c\u201d\u5f52\u98ce\u683c\u5b9a\u4f4d\u3002

10. hook_pairs\u7ed9\u51fa0-8\u7ec4\u53ef\u9009\u5f00\u5934\u7ec4\u5408\uff0c\u53ea\u6709\u771f\u7684\u5b58\u5728\u5f3a\u5f00\u5934\u65f6\u624d\u8f93\u51fa\u3002\u6bcf\u9879\u5fc5\u987b\u662f\u201c\u72ec\u7acb\u8bf4\u6e05\u5177\u4f53\u8d2d\u4e70\u4ef7\u503c\u201d\u7684\u5b8c\u6574Hook\uff0c\u4ee5\u53ca\u4e0b\u4e00\u6bb5\u7acb\u523b\u89e3\u91ca\u3001\u8bc1\u660e\u6216\u5151\u73b0\u5b83\u7684\u5b8c\u6574\u5019\u9009\u3002\u76f4\u64ad\u4e92\u52a8\u3001\u5e93\u5b58\u95ee\u7b54\u3001\u4e2a\u4eba\u8eab\u9ad8\u4f53\u91cd\u5c3a\u7801\u3001\u4ef7\u683c/CTA\u3001\u4e0a\u65b0\u9884\u544a\u3001\u7eaf\u5c55\u793a\u94fa\u57ab\u3001\u6cdb\u6cdb\u5938\u8d5e\u3001\u6b8b\u53e5\u90fd\u7edd\u4e0d\u53ef\u8fdb\u5165hook_pairs\u3002\u82e5\u6ca1\u6709\u8db3\u591f\u5f3a\u7684\u7ec4\u5408\uff0c\u8fd4\u56de\u7a7a\u6570\u7ec4\uff0c\u4e0d\u5f97\u51d1\u6570\u3002

HookPackage rule: output hook_pairs and hook_packages together. A complete A/B HookPackage becomes the live director's only Hook source; hook_pairs are used only when no A/B package survives validation. A package must state Hook promise, proof relation, completion, semantic signals, and A/B/C tier. Never output a package when the second sentence is merely a different good selling point rather than proof of the promise.

Hook thread rule: every hook_pairs item records one verified seed proof, not an exclusive second-subtitle ID. The director may use another reviewed, independent candidate from the same topic thread as the second segment when it explains, proves, or extends the Hook more clearly. Never use this flexibility to change product, topic, or safety policy.

{marketing_contract}

\u8f93\u51faschema\uff08\u5fc5\u987b\u7528\u6570\u7ec4\u77ed\u683c\u5f0f\uff09:
{schema_text}
cards\u5b57\u6bb5\u987a\u5e8f:[\u5019\u9009\u7f16\u53f7,topic,subtopic,buyer_value,evidence_type,evidence_quote,roles,dependency,quality_tags,tier,primary_subject,target_relation,subject_evidence]
hook_pairs\u5b57\u6bb5\u987a\u5e8f:[Hook\u5019\u9009\u7f16\u53f7,\u627f\u63a5\u793a\u4f8b\u7f16\u53f7,\u4e3b\u9898\u7ebf\u7a0b,\u627f\u63a5\u7406\u7531]
{marketing_field_order}

{candidate_field_order}
{json.dumps(compact_inventory, ensure_ascii=False, separators=(',', ':'))}"""
    user_prompt += (
        "\nHookPackage contract: output both hook_pairs and hook_packages. A complete A/B package is the "
        "director's preferred Hook contract; ordinary hook_pairs only provide fallback when no A/B package exists. Each package item must be an object with "
        "hook_id, followup_id, topic, reason, hook_promise, proof_relation, package_complete, "
        "semantic_signals, and opening_tier. hook_promise must be a continuous short quote from the Hook itself, "
        "not a rewritten marketing claim. proof_relation must be one of visual_result, design_reason, "
        "material_evidence, wearing_experience, scene_projection, identity_projection, social_proof, "
        "source_value, price_value, after_sale_confidence, other_grounded. package_complete=true only when "
        "the followup directly proves that exact promise. A good Hook plus an unrelated good selling point is "
        "not a complete package. semantic_signals may only use emotion, strong_judgment, identity, "
        "style_projection, pain_hit, result, contrast, curiosity, scene, social_proof, source, price_value, "
        "after_sale, cta. opening_tier is A, B, or C: A is a real attention spike, B is a strong visual or "
        "purchase-value opening, C is only a complete natural introduction. A short emotional reaction is allowed "
        "when it has a product/style/result anchor and the followup proves it.\n"
        "A/B must be evidenced by the spoken Hook itself: strong emotion or judgement, a visible result, a clear "
        "pain/persona, contrast, curiosity, identity/style projection, scene, social proof, source, allowed price, "
        "after-sale confidence, or allowed CTA. Do not borrow a signal from the topic label. A plain feature list "
        "such as a collar that can stand or turn is a body card, not an A/B Hook, unless the original sentence itself "
        "also carries one of those attention mechanisms.\n"
        "\nHook policy override: the task content policy is authoritative. "
        "Price, CTA, origin/source claims, social proof, and after-sale commitments are "
        "only forbidden when the policy says block or body_only. When allowed, they still "
        "need a complete, credible Hook plus immediate proof. A short emotional reaction may "
        "be a Hook when it has a real product, style, or result anchor and the next card proves it."
    )
    return system_prompt, user_prompt

def _post_review_request(
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    stage: str = "candidate_analysis",
) -> str:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "top_p": 0.8,
        "max_tokens": 6000,
        "response_format": {"type": "json_object"},
    }
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body["thinking"] = {"type": "disabled"}
    if "seed" in model.lower():
        body["reasoning_effort"] = "low"
    request = urllib.request.Request(
        ai_chat_completions_url(base_url),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    context = create_ssl_context()
    try:
        with urllib.request.urlopen(request, timeout=180, context=context) as response:
            result = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        record_ai_call(
            module="content_review", stage=stage, model=model, request_payload=body,
            success=False, error_type=type(error).__name__,
        )
        raise
    record_ai_call(
        module="content_review", stage=stage, model=model, request_payload=body,
        response_payload=result, success=True,
    )
    content = str(result.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    if not content:
        raise ContentReviewError("\u5ba1\u7a3fAI\u8fd4\u56de\u7a7a\u5185\u5bb9")
    return content


def review_candidates(
    *,
    api_key: str,
    base_url: str,
    model: str,
    inventory: Sequence[Mapping[str, Any]],
    candidate_digest: str,
    category: str = "",
    main_product: str = "",
    avoid: Sequence[str] = (),
    required_sources: Mapping[str, int] | None = None,
    content_policy: Any = None,
    include_marketing_intent: bool = False,
    log_fn=None,
) -> ContentReviewBundle:
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    content_policy = normalize_content_policy(content_policy)
    review_inventory = tuple(
        item for item in inventory
        if isinstance(item, Mapping) and _reviewable_candidate_text(
            item.get("text"),
            content_policy=content_policy,
        )
    )
    rejected_count = len(inventory) - len(review_inventory)
    if rejected_count:
        log(f"AI\u5185\u5bb9\u5ba1\u7a3f: \u5ba1\u7a3f\u524d\u6392\u9664 {rejected_count} \u6761\u660e\u663e\u8f6c\u5199\u6b8b\u7247")
    if not review_inventory:
        raise ContentReviewError("\u6ca1\u6709\u53ef\u5ba1\u7a3f\u7684\u5b8c\u6574\u5019\u9009")

    cache_key = build_cache_key(
        candidate_digest,
        category,
        main_product,
        avoid,
        model,
        content_policy=content_policy,
        include_marketing_intent=include_marketing_intent,
    )
    cached = _load_cache(cache_key)
    # The key already carries the version, but old installations or manually
    # restored cache files can still put an earlier bundle at this path.  Never
    # let such a result silently exercise an older review contract.
    if cached and str(cached.get("version") or "") != CONTENT_REVIEW_VERSION:
        log("AI内容审稿: 忽略旧版缓存，重新审稿")
        try:
            _cache_path(cache_key).unlink(missing_ok=True)
        except OSError:
            _LOG.warning("unable to remove stale content review cache", exc_info=True)
        cached = None
    if cached:
        try:
            bundle = _validate_bundle(
                cached,
                inventory=review_inventory,
                cache_key=cache_key,
                candidate_digest=candidate_digest,
                category=category,
                model=model,
                required_sources=required_sources,
                main_product=main_product,
                content_policy=content_policy,
            )
            log("AI\u5185\u5bb9\u5ba1\u7a3f: \u547d\u4e2d\u672c\u5730\u7f13\u5b58")
            return replace(bundle, cache_hit=True)
        except ContentReviewError:
            try:
                _cache_path(cache_key).unlink(missing_ok=True)
            except OSError:
                                _LOG.warning("os error", exc_info=True)

    last_format_error: Exception | None = None
    for attempt in range(2):
        system_prompt, user_prompt = _review_prompts(
            review_inventory,
            category=category,
            main_product=main_product,
            avoid=list(avoid or []),
            required_sources=required_sources,
            format_retry=bool(attempt),
            content_policy=content_policy,
            include_marketing_intent=include_marketing_intent,
        )
        log("AI\u5185\u5bb9\u5ba1\u7a3f: \u8c03\u7528\u6a21\u578b..." if not attempt else "AI\u5185\u5bb9\u5ba1\u7a3f: \u683c\u5f0f\u91cd\u8bd5...")
        content = _post_review_request(
            api_key, base_url, model, system_prompt, user_prompt, stage="candidate_analysis"
        )
        try:
            data = _extract_json_object(content)
        except (json.JSONDecodeError, ContentReviewError, ValueError) as exc:
            last_format_error = exc
            if attempt == 0:
                continue
            raise ContentReviewError(str(exc)) from exc
        raw_cards = data.get("cards") if isinstance(data.get("cards"), list) else []
        first_shape = "none"
        if raw_cards:
            first = raw_cards[0]
            if isinstance(first, (list, tuple)):
                first_shape = f"array[{len(first)}]"
            elif isinstance(first, Mapping):
                first_shape = "object[" + ",".join(sorted(str(key) for key in first.keys()))[:120] + "]"
            else:
                first_shape = type(first).__name__
        log(
            "AI\u5185\u5bb9\u5ba1\u7a3f: \u54cd\u5e94\u7ed3\u6784 "
            f"keys={','.join(sorted(str(key) for key in data.keys()))[:120] or 'none'}, "
            f"cards={len(raw_cards)}, first={first_shape}"
        )
        try:
            bundle = _validate_bundle(
                data,
                inventory=review_inventory,
                cache_key=cache_key,
                candidate_digest=candidate_digest,
                category=category,
                model=model,
                required_sources=required_sources,
                main_product=main_product,
                content_policy=content_policy,
            )
        except Exception as exc:
            log(
                f"AI\u5185\u5bb9\u5ba1\u7a3f: \u7ed3\u6784\u6821\u9a8c\u5f02\u5e38 "
                f"{type(exc).__name__}: {exc}"
            )
            raise
        log(
            f"AI\u5185\u5bb9\u5ba1\u7a3f: \u7ed3\u6784\u6821\u9a8c\u901a\u8fc7 "
            f"cards={len(bundle.cards)}, duration={bundle.retained_duration:.1f}s"
        )
        _write_cache(bundle)
        log("AI\u5185\u5bb9\u5ba1\u7a3f: \u7f13\u5b58\u5199\u5165\u5b8c\u6210")
        return bundle
    raise ContentReviewError(str(last_format_error or "\u5185\u5bb9\u5ba1\u7a3f\u683c\u5f0f\u5931\u8d25"))

def _hook_pair_repair_prompts(
    bundle: ContentReviewBundle,
    inventory: Sequence[Mapping[str, Any]],
    *,
    category: str,
    main_product: str,
    content_policy: Any = None,
) -> tuple[str, str]:
    """Build a small semantic review request without giving away story order."""
    candidate_map = _candidate_map(inventory)
    approved_cards: list[dict[str, Any]] = []
    for card in bundle.cards:
        candidate = candidate_map.get(card.candidate_id)
        if not candidate:
            continue
        approved_cards.append({
            "srt_index": card.candidate_id,
            "source": _clean_text(candidate.get("source"), 24),
            "duration_sec": round(float(candidate.get("duration_sec") or 0.0), 1),
            "text": _clean_text(candidate.get("text"), 240),
            "topic": card.topic,
            "buyer_value": card.buyer_value,
            "dependency": card.dependency,
            "tier": card.tier,
            "primary_subject": card.primary_subject,
            "target_relation": card.target_relation,
            "subject_evidence": card.subject_evidence,
            "quality_tags": list(card.quality_tags),
        })

    system_prompt = (
        "你是带货短视频的开头语义复核员，不是最终剪辑导演。"
        "只从已经审稿通过的原字幕中找少量真实的Hook+承接示例，绝不重写字幕、编造编号或安排全片顺序。"
        "只输出一个紧凑JSON对象，不要Markdown或解释。"
    )
    user_prompt = (
        f"品类:{category or '通用'}\n"
        f"主商品:{main_product or '未指定'}\n"
        "任务: 广审已经保留内容卡，但尚未形成可执行的A/B级HookPackage；普通HookPair只能作为候选依据，不能直接当作完整开场。请只做一次严格复核。\n"
        "Hook必须是一句脱离直播上下文也完整成立的、具体的购买价值陈述，明确说出商品属性、效果、痛点、人群或使用场景中的至少一项。"
        "强情绪评价也允许作为Hook，但它必须有真实商品、风格或结果指向，且下一段立刻证明它。"
        "承接示例必须解释、证明或兑现同一项购买价值，不能只是换一个卖点；它用于建立主题线程，不会锁死导演的第二句编号。\n"
        "只有tier=main的Hook和承接示例才可形成可执行的Hook组合；reserve只能留给后续证据、时长或混剪来源补足。\n"
        "绝对排除: target_relation=other、报尺码、个人身高体重试穿、问答互动、库存对话、上新预告、展示铺垫、连接词开头、泛泛夸赞、"
        "只说气场/高级/女总裁等空泛身份想象、半句、口头重复或没有商品购买信息的聊天。\n"
        "价格/CTA是否可作Hook必须遵守下方内容政策；仅正文或禁止的内容均不可作Hook。\n"
        "若没有真正合格的组合，必须返回空数组，不得凑数。最多返回3组。\n"
        "输出格式: {\"hook_pairs\":[[Hook编号,承接编号,\"主题\",\"承接如何兑现\"]],\"hook_packages\":[{\"hook_id\":Hook编号,\"followup_id\":承接编号,\"topic\":\"主题\",\"reason\":\"承接如何兑现\",\"hook_promise\":\"Hook中连续原话\",\"proof_relation\":\"design_reason\",\"package_complete\":true,\"semantic_signals\":[\"result\"],\"opening_tier\":\"B\"}]}\n"
        "内容使用政策:\n"
        + "\n".join(policy_prompt_lines(content_policy))
        + "\n"
        "已审稿内容卡:\n"
        + json.dumps(approved_cards, ensure_ascii=False, separators=(",", ":"))
    )
    user_prompt += (
        "\nHookPackage contract: hook_promise must be a continuous short quote from the Hook, not a new claim. "
        "proof_relation must be visual_result, design_reason, material_evidence, wearing_experience, "
        "scene_projection, identity_projection, social_proof, source_value, price_value, "
        "after_sale_confidence, or other_grounded. package_complete is true only when the exact Hook promise "
        "is proved by the followup. semantic_signals use only emotion, strong_judgment, identity, "
        "style_projection, pain_hit, result, contrast, curiosity, scene, social_proof, source, price_value, "
        "after_sale, cta. opening_tier is A, B, or C.\n"
        "Policy override: price, CTA, origin/source claims, social proof, and after-sale "
        "commitments follow the task policy. They are only excluded from Hooks when block or "
        "body_only; when allowed they still require a credible immediate proof."
        " A HookPair also needs a real attention mechanism in its original first sentence: emotion, "
        "strong judgement, clear result, pain/persona, contrast, curiosity, identity/style, scene, "
        "social proof, source, allowed price, after-sale confidence, or allowed CTA. A neutral "
        "function list is body content, not a fallback HookPair."
    )
    return system_prompt, user_prompt


def repair_hook_pairs(
    *,
    api_key: str,
    base_url: str,
    model: str,
    inventory: Sequence[Mapping[str, Any]],
    bundle: ContentReviewBundle,
    category: str = "",
    main_product: str = "",
    content_policy: Any = None,
    log_fn=None,
) -> ContentReviewBundle:
    """Run one focused Hook-pair review when the broad review returned none.

    An empty successful response is meaningful and cached. Transport, JSON, or
    validation failures leave the original bundle untouched so the caller can
    use its normal non-blocking fallback behavior.
    """
    def log(message: str) -> None:
        if log_fn:
            log_fn(message)

    content_policy = normalize_content_policy(content_policy)
    # The broad reviewer can find a valid HookPair but omit the newer
    # HookPackage fields. In that case run this small package-only pass once.
    # A completed pass, including a real "none found", is cached.
    if bundle.director_hook_packages or bundle.hook_pair_reviewed:
        return bundle

    candidates = _candidate_map(inventory)
    card_ids = {
        card.candidate_id for card in bundle.cards
        if card.candidate_id in candidates
    }
    if len(card_ids) < 2:
        resolved = replace(bundle, hook_pair_reviewed=True)
        _write_cache(resolved)
        log("AI内容审稿: 内容卡不足两条，确认无可验证Hook组合")
        return resolved

    system_prompt, user_prompt = _hook_pair_repair_prompts(
        bundle,
        inventory,
        category=category,
        main_product=main_product,
        content_policy=content_policy,
    )
    last_error = ""
    for attempt in range(2):
        try:
            log(
                "AI内容审稿: 尚未形成A/B HookPackage，启动Hook语义复核..."
                if attempt == 0 else "AI内容审稿: Hook语义复核格式重试..."
            )
            content = _post_review_request(
                api_key,
                base_url,
                model,
                system_prompt,
                user_prompt + (
                    "\n上次响应格式无效。这次只能返回指定JSON对象。" if attempt else ""
                ),
                stage="content_review_hook_repair",
            )
            data = _extract_json_object(content)
            raw_pairs = data.get("hook_pairs")
            if not isinstance(raw_pairs, list):
                raise ContentReviewError("Hook语义复核缺少hook_pairs数组")
            pairs: list[HookPair] = list(bundle.hook_pairs)
            seen: set[tuple[int, int]] = {
                (pair.hook_id, pair.followup_id) for pair in pairs
            }
            for raw_pair in raw_pairs:
                pair = _normalize_hook_pair(
                    raw_pair,
                    card_ids=card_ids,
                    candidates=candidates,
                    cards_by_id=bundle.card_map(),
                    content_policy=content_policy,
                )
                if pair is None:
                    continue
                signature = (pair.hook_id, pair.followup_id)
                if signature in seen:
                    continue
                seen.add(signature)
                pairs.append(pair)
                if len(pairs) >= 3:
                    break
            packages: list[HookPackage] = list(bundle.hook_packages)
            seen_packages: set[tuple[int, int]] = {
                (package.hook_id, package.followup_id) for package in packages
            }
            raw_packages = data.get("hook_packages")
            if isinstance(raw_packages, list):
                for raw_package in raw_packages:
                    package = _normalize_hook_package(
                        raw_package,
                        card_ids=card_ids,
                        candidates=candidates,
                        cards_by_id=bundle.card_map(),
                        content_policy=content_policy,
                    )
                    if package is None:
                        continue
                    signature = (package.hook_id, package.followup_id)
                    if signature in seen_packages:
                        continue
                    seen_packages.add(signature)
                    packages.append(package)
                    if len(packages) >= 3:
                        break
            synthesized = 0
            if not packages:
                for pair in pairs:
                    package = _synthesize_hook_package_from_pair(
                        pair,
                        card_ids=card_ids,
                        candidates=candidates,
                        cards_by_id=bundle.card_map(),
                        content_policy=content_policy,
                    )
                    if package is None:
                        continue
                    signature = (package.hook_id, package.followup_id)
                    if signature in seen_packages:
                        continue
                    seen_packages.add(signature)
                    packages.append(package)
                    synthesized += 1
                    if len(packages) >= 3:
                        break
            resolved = replace(
                bundle,
                hook_pairs=tuple(pairs),
                hook_packages=tuple(packages),
                hook_pair_reviewed=True,
            )
            _write_cache(resolved)
            log(
                f"AI内容审稿: Hook语义复核完成，验证组合{len(resolved.hook_pairs)}组，"
                f"完整HookPackage{len(resolved.director_hook_packages)}组"
                + (f"（由已验证HookPair补全{synthesized}组）" if synthesized else "")
            )
            return resolved
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

    log(f"AI内容审稿: Hook语义复核未完成，保留原审稿结果（{last_error[:160]}）")
    return bundle


@dataclass(frozen=True)
class FinalSequenceReview:
    status: str
    issues: tuple[str, ...]
    clips: tuple[dict[str, Any], ...]
    expansion_plan: tuple[dict[str, Any], ...] = ()

    def summary(self, *, applied: bool = False, fallback_reason: str = "") -> dict[str, Any]:
        return {
            "status": self.status,
            "issue_count": len(self.issues),
            "applied": bool(applied),
            "expansion_count": len(self.expansion_plan),
            "fallback_reason": str(fallback_reason or ""),
        }


@dataclass(frozen=True)
class FinalSequenceAudit:
    """A read-only verdict over a director-owned final sequence."""

    status: str
    issues: tuple[str, ...]
    opening_issue: bool = False

    def summary(self, *, opening_repair_applied: bool = False, fallback_reason: str = "") -> dict[str, Any]:
        return {
            "status": self.status,
            "issue_count": len(self.issues),
            "opening_issue": bool(self.opening_issue),
            "opening_repair_applied": bool(opening_repair_applied),
            "fallback_reason": str(fallback_reason or ""),
        }


def _normalize_final_sequence_review(
    data: Mapping[str, Any],
    *,
    allowed_candidate_ids: Iterable[int],
) -> FinalSequenceReview:
    status = _clean_text(data.get("status"), 16).lower()
    if status not in {"pass", "revise"}:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u7f3a\u5c11\u6709\u6548status")
    issues = _clean_list(data.get("issues"), limit=8, item_limit=100)
    if status == "pass":
        return FinalSequenceReview(status="pass", issues=issues, clips=())

    allowed_ids = {int(value) for value in allowed_candidate_ids}
    raw_clips = data.get("clips")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u4fee\u8ba2\u7247\u5355\u4e3a\u7a7a")

    normalized: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for raw in raw_clips:
        if not isinstance(raw, Mapping):
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u7247\u5355\u9879\u683c\u5f0f\u65e0\u6548")
        clip_type = _clean_text(raw.get("clip_type"), 16).lower()
        if clip_type not in {"hook", "product", "close"}:
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5305\u542b\u65e0\u6548clip_type")
        indices = raw.get("srt_indices")
        if isinstance(indices, int) and not isinstance(indices, bool):
            indices = [indices]
        if not isinstance(indices, list) or not 1 <= len(indices) <= 3:
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5305\u542b\u65e0\u6548\u5019\u9009\u7f16\u53f7")
        try:
            normalized_ids = [int(value) for value in indices if not isinstance(value, bool)]
        except (TypeError, ValueError):
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5019\u9009\u7f16\u53f7\u4e0d\u662f\u6574\u6570")
        if (
            len(normalized_ids) != len(indices)
            or normalized_ids != sorted(set(normalized_ids))
            or any(right != left + 1 for left, right in zip(normalized_ids, normalized_ids[1:]))
            or any(value not in allowed_ids for value in normalized_ids)
            or used_ids.intersection(normalized_ids)
        ):
            raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u5019\u9009\u7f16\u53f7\u8d8a\u754c\u3001\u91cd\u590d\u6216\u4e0d\u8fde\u7eed")
        used_ids.update(normalized_ids)
        try:
            trim_priority = max(0, int(raw.get("trim_priority") or 0))
        except (TypeError, ValueError):
            trim_priority = 0
        normalized.append({
            "clip_type": clip_type,
            "srt_indices": normalized_ids,
            "focus": _clean_text(raw.get("focus"), 40),
            "reason": _clean_text(raw.get("reason"), 60),
            "trim_priority": trim_priority,
        })

    hook_positions = [index for index, item in enumerate(normalized) if item["clip_type"] == "hook"]
    close_positions = [index for index, item in enumerate(normalized) if item["clip_type"] == "close"]
    if not hook_positions and normalized:
        normalized[0]["clip_type"] = "hook"
        hook_positions = [0]
        issues = tuple((*issues, "\u9996\u6bb5Hook\u7c7b\u578b\u5df2\u5f52\u4e00")[:8])
    if hook_positions != [0]:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u4fee\u8ba2\u7ed3\u679c\u5fc5\u987b\u4e14\u53ea\u80fd\u4ee5Hook\u5f00\u5934")
    if close_positions and close_positions != [len(normalized) - 1]:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1Close\u53ea\u80fd\u4f4d\u4e8e\u672b\u6bb5")

    selected_groups = {tuple(item["srt_indices"]) for item in normalized}
    close_group = tuple(normalized[-1]["srt_indices"]) if close_positions else ()
    raw_expansion = data.get("expansion_plan")
    expansion_plan: list[dict[str, Any]] = []
    expansion_ids: set[int] = set()
    priorities: set[int] = set()
    if isinstance(raw_expansion, list):
        for response_order, raw in enumerate(raw_expansion):
            if not isinstance(raw, Mapping):
                continue
            indices = raw.get("srt_indices")
            if isinstance(indices, int) and not isinstance(indices, bool):
                indices = [indices]
            anchor = raw.get("after_srt_indices")
            if isinstance(anchor, int) and not isinstance(anchor, bool):
                anchor = [anchor]
            try:
                normalized_ids = [int(value) for value in indices if not isinstance(value, bool)]
                anchor_ids = tuple(int(value) for value in anchor if not isinstance(value, bool))
                priority = int(raw.get("priority", response_order + 1) or response_order + 1)
                after_order = max(1, int(raw.get("after_order") or 1))
            except (TypeError, ValueError):
                continue
            if (
                not isinstance(indices, list)
                or len(normalized_ids) != len(indices)
                or not 1 <= len(normalized_ids) <= 3
                or normalized_ids != sorted(set(normalized_ids))
                or any(right != left + 1 for left, right in zip(normalized_ids, normalized_ids[1:]))
                or any(value not in allowed_ids for value in normalized_ids)
                or used_ids.intersection(normalized_ids)
                or expansion_ids.intersection(normalized_ids)
                or anchor_ids not in selected_groups
                or anchor_ids == close_group
                or priority <= 0
                or priority in priorities
            ):
                continue
            priorities.add(priority)
            expansion_ids.update(normalized_ids)
            expansion_plan.append({
                "priority": priority,
                "after_srt_indices": list(anchor_ids),
                "after_order": after_order,
                "srt_indices": normalized_ids,
                "focus": _clean_text(raw.get("focus"), 40),
                "reason": _clean_text(raw.get("reason"), 80),
            })
            if len(expansion_plan) >= 8:
                break
    expansion_plan.sort(key=lambda item: int(item["priority"]))
    return FinalSequenceReview(
        status="revise",
        issues=issues,
        clips=tuple(normalized),
        expansion_plan=tuple(expansion_plan),
    )


def _final_review_duration_estimate(
    review: FinalSequenceReview,
    duration_by_id: Mapping[int, float],
) -> tuple[float, float]:
    main_ids = {
        int(candidate_id)
        for item in review.clips
        for candidate_id in item.get("srt_indices", [])
    }
    expansion_ids = {
        int(candidate_id)
        for item in review.expansion_plan
        for candidate_id in item.get("srt_indices", [])
        if int(candidate_id) not in main_ids
    }
    main_duration = sum(float(duration_by_id.get(candidate_id, 0.0)) for candidate_id in main_ids)
    expansion_duration = sum(
        float(duration_by_id.get(candidate_id, 0.0)) for candidate_id in expansion_ids
    )
    return main_duration, expansion_duration


def _final_objective_issues(
    sequence: Sequence[Mapping[str, Any]],
    content_policy: Any = None,
) -> list[str]:
    if not sequence:
        return ["\u7247\u5355\u4e3a\u7a7a"]
    issues: list[str] = []
    policy = normalize_content_policy(content_policy)
    first_text = _clean_text(sequence[0].get("text"), 360)
    compact_first = re.sub(r"[\s,\uff0c\u3002.!\uff01?\uff1f]", "", first_text)
    if re.search(r"(?:\u60f3\u8981|\u9700\u8981|\u60f3\u770b).{0,24}\u7684[\u554a\u5440\u5462\u5427]?$", compact_first):
        issues.append("\u5f53\u524dHook\u53ea\u53ec\u5524\u4eba\u7fa4\u9700\u6c42\uff0c\u6ca1\u6709\u8bf4\u660e\u5546\u54c1\u7ed3\u679c")
    if re.search(r"\u60f3\u642d.{0,20}\u7ed9\u4f60\u770b\u4e00\u773c|\u5c31\u8fd9\u4e48\u642d", compact_first):
        issues.append("\u5f53\u524dHook\u662f\u5c55\u793a\u94fa\u57ab\uff0c\u4e0d\u662f\u72ec\u7acb\u8d2d\u4e70\u4ef7\u503c")
    if re.match(r"^(?:\u7b2c[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b\u4e5d\u5341\d]+\u70b9|\u8fd8\u6709(?:\u4e00\u70b9|\u4e00\u4e2a)|\u518d\u8bf4(?:\u4e00\u70b9|\u4e00\u4e2a))", compact_first):
        issues.append("\u5f53\u524dHook\u4ece\u7f16\u53f7\u6216\u4e0a\u6587\u627f\u63a5\u5f00\u59cb\uff0c\u4e0d\u80fd\u72ec\u7acb\u6210\u7acb")
    if re.search(
        r"(?:\u4f1a\u663e\u5f97|\u4f1a\u8ba9|\u53ef\u4ee5\u628a|\u80fd\u591f\u628a).{0,18}"
        r"(?:\u6240\u6709\u7684\u8089|\u6574\u4e2a|\u8fd9\u79cd|\u8fd9\u4e2a|\u90a3\u4e2a|\u4f60\u7684|\u5b83\u7684)$",
        compact_first,
    ):
        issues.append("\u5f53\u524dHook\u7ed3\u5c3e\u8c13\u8bed\u6216\u7ed3\u679c\u672a\u8bf4\u5b8c")
    if re.search(r"(?:\u7136\u540e|\u6240\u4ee5|\u56e0\u4e3a|\u4f46\u662f|\u800c\u4e14|\u5982\u679c|\u5c31\u4f1a|\u76f8\u5f53\u4e8e|\u5305\u62ec)$", compact_first):
        issues.append("\u5f53\u524dHook\u4ee5\u8fde\u63a5\u6210\u5206\u6216\u672a\u5b8c\u6210\u7ed3\u6784\u7ed3\u675f")

    production_pattern = re.compile(
        r"\u5e2e\u6211\u6295\u56de|\u6295\u56de\u521a\u521a|\u8bbe\u8ba1\u70b9|\u5bfc\u64ad|\u5207\u56de|\u4e0a\u753b\u9762"
    )
    purchase_cta_pattern = re.compile(
        r"(?:\u63a8\u8350|\u5efa\u8bae|\u503c\u5f97)(?:\u4f60\u4eec|\u5927\u5bb6|\u59d0\u59b9\u4eec)?(?:\u76f4\u63a5)?\u62cd(?!\u7167|\u6444)"
    )
    for order, item in enumerate(sequence, 1):
        text = _clean_text(item.get("text"), 360)
        duration = float(item.get("duration_sec") or 0.0)
        interaction_reason = live_interaction_or_size_response_reason(text)
        if interaction_reason:
            issues.append(f"第{order}段含{interaction_reason}")
        if order == 1 or str(item.get("clip_type") or "").lower() == "hook":
            hook_reason = hook_ineligible_reason(text)
            if hook_reason:
                issues.append(f"第{order}段不可作Hook:{hook_reason}")
            hook_quality = hook_candidate_quality_flags(text)
            if hook_quality:
                issues.append(f"第{order}段不可作Hook:{','.join(hook_quality)}")
        if production_pattern.search(text):
            issues.append(f"\u7b2c{order}\u6bb5\u542b\u5bfc\u64ad/\u6295\u6d41/\u5207\u753b\u9762\u6307\u4ee4")
        role = "hook" if order == 1 or str(item.get("clip_type") or "").lower() == "hook" else "body"
        cta_blocked, _cta_reason = blocks_role(policy, "cta", text, role=role)
        if purchase_cta_pattern.search(text) and cta_blocked:
            issues.append(f"\u7b2c{order}\u6bb5\u542b\u63a8\u8350\u62cd\u5355\u7c7bCTA")
        if str(item.get("clip_type") or "").lower() == "product" and duration > 12.0:
            issues.append(f"\u7b2c{order}\u6bb5{duration:.1f}\u79d2\u8fc7\u957f\uff0c\u5e94\u6362\u6210\u66f4\u77ed\u7684\u5b8c\u6574\u5356\u70b9")
    return list(dict.fromkeys(issues))


def audit_final_sequence(
    *,
    api_key: str,
    base_url: str,
    model: str,
    selected_sequence: Sequence[Mapping[str, Any]],
    hook_pairs: Sequence[Mapping[str, Any]] = (),
    hook_threads: Mapping[int, Mapping[str, Any]] | None = None,
    content_policy: Any = None,
    log_fn=None,
) -> FinalSequenceAudit:
    """Audit quality without giving the reviewer authority to re-edit.

    The final reviewer may identify a weak opening or continuity problem, but
    it cannot return candidate IDs, replacement clips, or an expansion plan.
    A flagged opening is repaired only by the bounded director operation.
    """
    normalized_sequence = []
    for order, item in enumerate(selected_sequence or (), 1):
        if not isinstance(item, Mapping):
            continue
        normalized_sequence.append({
            "order": order,
            "clip_type": _clean_text(item.get("clip_type"), 16).lower(),
            "srt_indices": [
                int(value) for value in (item.get("srt_indices") or [])
                if str(value).strip().isdigit()
            ][:3],
            "duration_sec": round(max(0.0, float(item.get("duration_sec") or 0.0)), 1),
            "text": _clean_text(item.get("text"), 360),
            "focus": _clean_text(item.get("focus"), 40),
        })
    if not normalized_sequence:
        raise ContentReviewError("成片终审缺少可审阅片单")

    content_policy = normalize_content_policy(content_policy)
    objective_issues = _final_objective_issues(normalized_sequence, content_policy)
    pair_lines = []
    for pair in hook_pairs or ():
        try:
            hook_id = int(pair.get("hook_id") or 0)
            followup_id = int(pair.get("followup_id") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if hook_id > 0 and followup_id > 0:
            pair_lines.append(f"#{hook_id}->#{followup_id}")
    thread_lines = []
    for raw_hook_id, raw_thread in (hook_threads or {}).items():
        try:
            hook_id = int(raw_hook_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(raw_thread, Mapping) or hook_id <= 0:
            continue
        followup_ids = []
        for raw_followup_id in raw_thread.get("allowed_followup_ids") or ():
            try:
                followup_id = int(raw_followup_id)
            except (TypeError, ValueError):
                continue
            if followup_id > 0 and followup_id != hook_id:
                followup_ids.append(followup_id)
        if followup_ids:
            thread_lines.append(
                f"#{hook_id}[{_clean_text(raw_thread.get('topic'), 40) or '同一卖点'}]"
                f"->" + "/".join(f"#{value}" for value in sorted(set(followup_ids)))
            )

    system_prompt = (
        "你是带货短视频的成片审稿人，只做审计，不做导演。"
        "你不能选择候选、不能输出编号、不能要求重排，也不能给出替换片单。"
        "检查开头两句是否作为一个整体成立、第二句是否立即兑现、正文是否重复或断裂、结尾是否自然。"
        "当已知Hook主题线程表明前两句属于同一验证组合时，第一句可以是短促的强情绪、强态度或强判断；"
        "应按两句整体判断，不能只因第一句单独信息量低就判为问题。"
        "直播互动、报尺码、个人身高体重、价格CTA、上新预告、展示铺垫和泛泛夸赞一律判为问题。"
        "只输出JSON对象：{\"status\":\"pass|flag\",\"issues\":[\"...\"],\"opening_issue\":true|false}。"
        "只有不存在实质问题才可pass；flag最多列出6条可验证问题。"
    )
    user_prompt = (
        "当前导演片单：\n"
        + json.dumps(normalized_sequence, ensure_ascii=False, separators=(",", ":"))
        + (
            "\n已知Hook主题线程：" + ", ".join(thread_lines)
            + "。第二段允许使用同一线程内更清楚的解释、证明或展开，不要求固定接审稿示例编号。"
            if thread_lines else ("\n已知开头组合：" + ", ".join(pair_lines) if pair_lines else "")
        )
        + ("\n程序已发现的客观问题：" + "；".join(objective_issues[:6]) if objective_issues else "")
    )
    if log_fn:
        log_fn("AI成片终审: 只审计，不重排片单...")
    system_prompt += (
        " Task content policy is authoritative: price, CTA, origin/source claims, social proof, "
        "and after-sale commitments are issues only when the policy says block or body_only."
    )
    user_prompt += "\nContent policy:\n" + "\n".join(policy_prompt_lines(content_policy))
    content = _post_review_request(
        api_key, base_url, model, system_prompt, user_prompt, stage="content_review_final_audit"
    )
    data = _extract_json_object(content)
    status = _clean_text(data.get("status"), 16).lower()
    if status not in {"pass", "flag"}:
        raise ContentReviewError("成片终审缺少pass/flag状态")
    issues = _clean_list(data.get("issues"), limit=6, item_limit=120)
    if objective_issues and status == "pass":
        status = "flag"
        issues = tuple(dict.fromkeys([*objective_issues, *issues]))[:6]
    elif objective_issues:
        issues = tuple(dict.fromkeys([*objective_issues, *issues]))[:6]
    try:
        opening_issue = bool(data.get("opening_issue"))
    except Exception:
        opening_issue = False
    opening_markers = ("Hook", "开头", "首段", "承接", "第一句", "第二句")
    if any(any(marker in issue for marker in opening_markers) for issue in issues):
        opening_issue = True
    if status == "pass":
        opening_issue = False
    return FinalSequenceAudit(
        status=status,
        issues=tuple(issues),
        opening_issue=opening_issue,
    )


def review_final_sequence(
    *,
    api_key: str,
    base_url: str,
    model: str,
    selected_sequence: Sequence[Mapping[str, Any]],
    inventory: Sequence[Mapping[str, Any]],
    allowed_candidate_ids: Iterable[int],
    category: str = "",
    preference: str = "",
    duration_low: float = 0.0,
    duration_high: float = 0.0,
    required_sources: Mapping[str, int] | None = None,
    hook_pairs: Sequence[Mapping[str, Any]] = (),
    hook_threads: Mapping[int, Mapping[str, Any]] | None = None,
    allowed_hook_ids: Iterable[int] | None = None,
    log_fn=None,
) -> FinalSequenceReview:
    allowed_ids = {int(value) for value in allowed_candidate_ids}
    allowed_hook_set = {
        int(value) for value in (allowed_hook_ids or [])
        if str(value).strip().isdigit() and int(value) in allowed_ids
    }
    hook_followups_by_hook: dict[int, set[int]] = {}
    for raw_pair in hook_pairs or ():
        try:
            hook_id = int(raw_pair.get("hook_id") or 0)
            followup_id = int(raw_pair.get("followup_id") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if hook_id in allowed_ids and followup_id in allowed_ids and hook_id != followup_id:
            hook_followups_by_hook.setdefault(hook_id, set()).add(followup_id)
    hook_thread_contract = False
    if hook_threads:
        normalized_threads: dict[int, set[int]] = {}
        for raw_hook_id, raw_thread in hook_threads.items():
            try:
                hook_id = int(raw_hook_id)
            except (TypeError, ValueError):
                continue
            if not isinstance(raw_thread, Mapping) or hook_id not in allowed_ids:
                continue
            followups = set()
            for raw_followup_id in raw_thread.get("allowed_followup_ids") or ():
                try:
                    followup_id = int(raw_followup_id)
                except (TypeError, ValueError):
                    continue
                if followup_id in allowed_ids and followup_id != hook_id:
                    followups.add(followup_id)
            if followups:
                normalized_threads[hook_id] = followups
        if normalized_threads:
            hook_followups_by_hook = normalized_threads
            hook_thread_contract = True
    compact_inventory = []
    for item in inventory:
        if int(item.get("srt_index") or 0) not in allowed_ids:
            continue
        compact_item = {
            "srt_index": int(item.get("srt_index") or 0),
            "source": _clean_text(item.get("source"), 24),
            "duration_sec": round(max(0.0, float(item.get("duration_sec") or 0.0)), 1),
            "text": _clean_text(item.get("text"), 240),
        }
        for field, limit in (
            ("topic", 40),
            ("subtopic", 60),
            ("buyer_value", 100),
            ("evidence_type", 40),
            ("dependency", 24),
            ("tier", 12),
        ):
            value = _clean_text(item.get(field), limit)
            if value:
                compact_item[field] = value
        for field in ("roles", "quality_tags"):
            values = _clean_list(item.get(field), limit=5, item_limit=30)
            if values:
                compact_item[field] = list(values)
        compact_inventory.append(compact_item)
    if not selected_sequence or not compact_inventory:
        raise ContentReviewError("\u6210\u7247\u7ec8\u5ba1\u7f3a\u5c11\u7247\u5355\u6216\u5019\u9009")
    candidate_durations = sorted(
        float(item.get("duration_sec") or 0.0) for item in compact_inventory
        if float(item.get("duration_sec") or 0.0) > 0.0
    )
    typical_duration = candidate_durations[len(candidate_durations) // 2] if candidate_durations else 4.0
    recommended_min_clips = max(6, int(math.ceil(float(duration_low or 0.0) / 8.0)))
    recommended_max_clips = max(
        recommended_min_clips + 3,
        int(math.ceil(float(duration_low or 0.0) / 5.0)),
    )
    selected_duration = sum(float(item.get("duration_sec") or 0.0) for item in selected_sequence)
    inventory_duration = sum(candidate_durations)
    known_issues = _final_objective_issues(selected_sequence)
    known_issue_text = "\uff1b".join(dict.fromkeys(known_issues)) or "\u65e0\u7a0b\u5e8f\u9884\u6807\u8bb0\uff0c\u4ecd\u9700\u4f60\u901a\u8bfb\u5224\u65ad"

    system_prompt = (
        "\u4f60\u662f\u5e26\u8d27\u77ed\u89c6\u9891\u7684\u6700\u7ec8\u5185\u5bb9\u4e3b\u7f16\u3002"
        "\u8f93\u5165\u7684selected_sequence\u662f\u5bfc\u6f14\u5df2\u7ecf\u6392\u597d\u7684\u5b8c\u6574\u7247\u5355\uff0cinventory\u662f\u5141\u8bb8\u66ff\u6362\u7684\u5168\u90e8\u5b89\u5168\u5019\u9009\u3002"
        "\u4f60\u53ea\u6709\u4e00\u6b21\u7ec8\u5ba1\u673a\u4f1a\uff1b\u5408\u683c\u5c31pass\uff0c\u4e0d\u5408\u683c\u5fc5\u987b\u8fd4\u56de\u4e00\u4efd\u5b8c\u6574\u65b0\u7247\u5355\uff0c\u4e0d\u80fd\u53ea\u8fd4\u56de\u5c40\u90e8\u589e\u5220\u64cd\u4f5c\u3002"
        "\u4e0d\u5f97\u6539\u5199\u5b57\u5e55\u3001\u65f6\u95f4\u6233\u6216\u6765\u6e90\uff0c\u53ea\u80fd\u4f7f\u7528inventory\u4e2d\u7684srt_index\u3002"
        "\u53ea\u8f93\u51faJSON\u5bf9\u8c61\uff0c\u4e0d\u8981Markdown\u6216\u89e3\u91ca\u3002"
    )
    source_rule = ""
    if required_sources:
        source_rule = "\u6df7\u526a\u6765\u6e90\u6700\u4f4e\u8981\u6c42:" + "\u3001".join(
            f"{source}\u81f3\u5c11{max(1, int(count or 1))}\u6bb5"
            for source, count in sorted(required_sources.items())
        )
    hook_pair_rule = ""
    if hook_followups_by_hook:
        pair_text = "\u3001".join(
            f"#{hook_id}->#{followup_id}"
            for hook_id, followup_ids in sorted(hook_followups_by_hook.items())
            for followup_id in sorted(followup_ids)
        )
        if hook_thread_contract:
            hook_pair_rule = (
                "Reviewed Hook topic threads: " + pair_text
                + ". A revised sequence may use only a listed left-side ID as Hook. "
                "The second segment may use any listed same-topic right-side ID, including a later timestamp, "
                "but must explain, prove, or extend the same product promise.\n"
            )
        else:
            hook_pair_rule = (
                "\u5ba1\u7a3fHook\u5408\u540c:" + pair_text
                + "\u3002\u82e5\u4fee\u8ba2\u7247\u5355\uff0cHook\u5fc5\u987b\u4ec5\u4f7f\u7528\u5de6\u4fa7\u7f16\u53f7\uff0c"
                "\u7b2c2\u6bb5\u5fc5\u987b\u7d27\u63a5\u4f7f\u7528\u8be5Hook\u5217\u51fa\u7684\u4efb\u4e00\u53f3\u4fa7\u7f16\u53f7\u3002\u4e0d\u5f97\u53e6\u9009\u5f00\u5934\u3002\n"
            )
    elif allowed_hook_set:
        hook_pair_rule = (
            "Strict Hook ID contract: "
            + ", ".join(f"#{value}" for value in sorted(allowed_hook_set))
            + ". A revised sequence may use only one of these IDs as its first Hook; do not choose another reviewed line as the opening.\n"
        )
    user_prompt = (
        f"\u54c1\u7c7b:{category or '\u901a\u7528'}\n"
        f"\u504f\u597d\u4e3b\u7ebf:{preference or '\u81ea\u52a8'}\n"
        f"\u539f\u7247\u65f6\u957f\u5408\u540c:{float(duration_low):.1f}-{float(duration_high):.1f}\u79d2\n"
        f"\u5f53\u524d\u7247\u5355\u539f\u7247\u5408\u8ba1:{selected_duration:.1f}\u79d2\uff1b\u5168\u90e8\u5ba1\u7a3f\u5019\u9009\u5408\u8ba1:{inventory_duration:.1f}\u79d2\uff1b"
        f"\u82e5revise\u5efa\u8bae\u7ea6{recommended_min_clips}-{recommended_max_clips}\u4e2a\u7247\u6bb5\uff0c\u4f18\u5148\u75282-3\u4e2a\u8fde\u7eed\u4e14\u8bed\u4e49\u5bc6\u5207\u7684\u5019\u9009\u7ec4\u62106-10\u79d2\u5b8c\u6574\u7247\u6bb5\uff0c"
        "\u4e0d\u8981\u4e3a\u51d1\u65f6\u957f\u62c6\u6210\u5927\u91cf2-3\u79d2\u788e\u53e5\uff1b\u6700\u7ec8\u4ee5\u6309inventory.duration_sec\u9010\u9879\u5b9e\u7b97\u8fbe\u5230\u4e0b\u9650\u4e3a\u51c6\n"
        f"\u7a0b\u5e8f\u9884\u6807\u8bb0\u95ee\u9898:{known_issue_text}\n"
        f"{source_rule}\n"
        f"{hook_pair_rule}"
        "\u7ec8\u5ba1\u6807\u51c6:\n"
        "1. Hook\u5fc5\u987b\u8131\u79bb\u4e0a\u4e0b\u6587\u4e5f\u80fd\u72ec\u7acb\u8bf4\u5b8c\u4e00\u4e2a\u5177\u4f53\u8d2d\u4e70\u4ef7\u503c\uff0c\u7b2c2\u6bb5\u5fc5\u987b\u7acb\u5373\u89e3\u91ca\u3001\u8bc1\u660e\u6216\u5151\u73b0\u5b83\u3002\u201c\u60f3\u8981X\u7684\u201d\u3001\u201c\u9700\u8981X\u7684\u201d\u53ea\u662f\u53ec\u5524\u4eba\u7fa4\uff0c\u6ca1\u6709\u8bf4\u660e\u5546\u54c1\u5982\u4f55\u89e3\u51b3\u95ee\u9898\uff0c\u4e0d\u662fHook\u3002"
        "\u201c\u60f3\u770bX\u5c31\u7ed9\u4f60\u770b\u4e00\u773c\u201d\u3001\u201c\u5c31\u8fd9\u4e48\u642d\u201d\u7c7b\u5c55\u793a\u94fa\u57ab\u4e0d\u662fHook\u3002\n"
        "2. \u5220\u6389\u660e\u663eASR\u4e71\u7801\u3001\u534a\u53e5\u3001\u6307\u4ee3\u4e0d\u660e\u3001\u7eaf\u4e92\u52a8\u3001\u5bfc\u64ad/\u6295\u6d41/\u5207\u753b\u9762\u6307\u4ee4\u3001\u4e0e\u8d2d\u4e70\u4ef7\u503c\u65e0\u5173\u7684\u73a9\u7b11\u548c\u7a7a\u6d1e\u5938\u8d5e\u3002\n"
        "3. \u540c\u4e49\u91cd\u590d\u53ea\u7559\u6700\u5b8c\u6574\u3001\u6700\u5177\u4f53\u7684\u8868\u8fbe\uff1b\u504f\u597d\u662f\u4e3b\u7ebf\u800c\u4e0d\u662f\u552f\u4e00\u4e3b\u9898\uff0c\u6b63\u6587\u5e94\u8986\u76d6\u81f3\u5c113\u4e2a\u6709\u8bc1\u636e\u7684\u5356\u70b9\u89d2\u5ea6\u3002\n"
        "inventory\u5df2\u63d0\u4f9btopic/subtopic/buyer_value/evidence_type/tier\uff1b\u76f8\u540csubtopic\u539f\u5219\u4e0a\u4e0d\u8d85\u8fc72\u4e2a\u7247\u6bb5\uff0c\u8bed\u4e49\u76f8\u8fd1\u65f6\u4f18\u5148main\u7ea7\u3001\u8bc1\u636e\u5177\u4f53\u4e14\u53e5\u5b50\u5b8c\u6574\u7684\u5019\u9009\u3002\n"
        "4. \u76f8\u90bb\u7247\u6bb5\u8981\u80fd\u81ea\u7136\u542c\u61c2\uff0c\u7ed3\u5c3e\u5fc5\u987b\u662f\u81ea\u7136\u603b\u7ed3\u6216\u9009\u62e9\u7406\u7531\uff0c\u4e0d\u5f97\u4ee5\u6b8b\u53e5\u3001\u7eaf\u5c3a\u7801\u6216\u65e0\u5173\u5bf9\u8bdd\u7ed3\u675f\u3002\n"
        "5. revise\u5fc5\u987b\u4fdd\u6301\u65f6\u957f\u5408\u540c\u548c\u6df7\u526a\u6765\u6e90\u6700\u4f4e\u8981\u6c42\uff0c\u53ea\u67091\u4e2aHook\u4e14\u5728\u9996\u6bb5\uff0cClose\u5982\u5b58\u5728\u53ea\u80fd\u5728\u672b\u6bb5\u3002\u4fee\u8ba2\u4e0d\u5f97\u53ea\u5220\u7247\uff1b\u5148\u4fdd\u7559\u5f53\u524d\u7247\u5355\u4e2d\u7684\u4f18\u8d28\u9aa8\u67b6\uff0c\u518d\u4eceinventory\u66ff\u6362\u3001\u8865\u8db3\u4e0d\u540c\u5356\u70b9\u3002\u8f93\u51fa\u524d\u5fc5\u987b\u5728\u5185\u90e8\u9010\u9879\u76f8\u52a0duration_sec\uff0c\u4f4e\u4e8e\u4e0b\u9650\u65f6\u7ee7\u7eed\u8865\u5165\u5b8c\u6574\u9ad8\u8d28\u91cf\u7247\u6bb5\uff0c\u4e0d\u5f97\u63d0\u4ea4\u65f6\u957f\u4e0d\u8db3\u7684revise\u3002\u53ea\u8981\u7a0b\u5e8f\u9884\u6807\u8bb0\u4e86\u95ee\u9898\uff0c\u5c31\u7981\u6b62pass\uff0crevise\u5fc5\u987b\u81f3\u5c11\u66f4\u6362\u3001\u5220\u9664\u6216\u91cd\u63921\u4e2a\u7f16\u53f7\uff0c\u4e0d\u5f97\u539f\u6837\u4ea4\u5377\u3002\n"
        "6. revise\u9664\u5b8c\u6574clips\u5916\uff0c\u8fd8\u5fc5\u987b\u8f93\u51fa4-8\u4e2aexpansion_plan\u5907\u7528Product\u3002\u5907\u7528\u7247\u4e0d\u5f97\u4e0eclips\u91cd\u590d\uff0cpriority\u4ece1\u5f00\u59cb\u4e14\u4e0d\u91cd\u590d\uff1bafter_srt_indices\u5fc5\u987b\u7cbe\u786e\u6307\u5411clips\u4e2d\u7684\u975eClose\u7247\u6bb5\uff0c\u8868\u793a\u5e94\u63d2\u5728\u8be5\u6bb5\u4e4b\u540e\u3002\u5907\u7528\u7247\u4e5f\u5fc5\u987b\u5b8c\u6574\u3001\u5e72\u51c0\u3001\u5177\u4f53\uff0c\u5e76\u6309\u5bf9\u53d9\u4e8b\u7684\u5e2e\u52a9\u6392\u5e8f\u3002\n"
        "\u8f93\u51fa\u683c\u5f0f:\n"
        '{"status":"pass","issues":[],"clips":[]}\n'
        "\u6216\n"
        '{"status":"revise","issues":["Hook\u53ea\u662f\u5c55\u793a\u94fa\u57ab"],"clips":['
        '{"clip_type":"hook","srt_indices":[3],"focus":"\u5177\u4f53\u6548\u679c","reason":"\u72ec\u7acb\u8d2d\u4e70\u4ef7\u503c","trim_priority":0},'
        '{"clip_type":"product","srt_indices":[8],"focus":"\u539f\u56e0\u8bc1\u636e","reason":"\u7acb\u5373\u5151\u73b0Hook","trim_priority":0},'
        '{"clip_type":"close","srt_indices":[12],"focus":"\u603b\u7ed3","reason":"\u81ea\u7136\u6536\u675f","trim_priority":0}],"expansion_plan":['
        '{"priority":1,"after_srt_indices":[8],"after_order":2,"srt_indices":[18],"focus":"\u4e0d\u540c\u5356\u70b9","reason":"\u65f6\u957f\u4e0d\u8db3\u65f6\u4f18\u5148\u8865\u5165"}]}\n'
        "\u5f53\u524d\u5b8c\u6574\u7247\u5355:\n"
        + json.dumps(list(selected_sequence), ensure_ascii=False, separators=(",", ":"))
        + "\n\u5168\u90e8\u53ef\u7528\u5019\u9009:\n"
        + json.dumps(compact_inventory, ensure_ascii=False, separators=(",", ":"))
    )
    duration_by_id = {
        int(item["srt_index"]): float(item.get("duration_sec") or 0.0)
        for item in compact_inventory
    }
    correction = ""
    last_error = ""
    for attempt in range(2):
        if log_fn:
            label = "\u8c03\u7528\u6a21\u578b" if attempt == 0 else "\u5408\u540c\u7ea0\u6b63\u91cd\u8bd5"
            log_fn(f"AI\u6210\u7247\u7ec8\u5ba1: {label}...")
        content = _post_review_request(
            api_key,
            base_url,
            model,
            system_prompt,
            user_prompt + correction,
            stage="content_review_final_repair",
        )
        try:
            data = _extract_json_object(content)
            review = _normalize_final_sequence_review(
                data,
                allowed_candidate_ids=allowed_ids,
            )
            contract_issues: list[str] = []
            if review.status == "pass" and known_issues:
                contract_issues.append(
                    "\u5df2\u6709\u5ba2\u89c2\u95ee\u9898\u9884\u6807\u8bb0\uff0c\u4e0d\u5141\u8bb8pass"
                )
            if review.status == "revise":
                if hook_followups_by_hook:
                    first = review.clips[0] if review.clips else {}
                    second = review.clips[1] if len(review.clips) > 1 else {}
                    first_indices = list(first.get("srt_indices") or [])
                    second_indices = list(second.get("srt_indices") or [])
                    hook_id = first_indices[0] if len(first_indices) == 1 else 0
                    expected_followups = hook_followups_by_hook.get(int(hook_id or 0), set())
                    if (
                        str(first.get("clip_type") or "").lower() != "hook"
                        or not expected_followups
                        or len(second_indices) != 1
                        or second_indices[0] not in expected_followups
                    ):
                        contract_issues.append(
                            "\u7ec8\u5ba1Hook\u672a\u9075\u5b88\u5ba1\u7a3f\u7684Hook+\u627f\u63a5\u7f16\u53f7\u5408\u540c"
                        )
                if not hook_followups_by_hook and allowed_hook_set:
                    first = review.clips[0] if review.clips else {}
                    first_indices = list(first.get("srt_indices") or [])
                    hook_id = first_indices[0] if len(first_indices) == 1 else 0
                    if (
                        str(first.get("clip_type") or "").lower() != "hook"
                        or hook_id not in allowed_hook_set
                    ):
                        contract_issues.append(
                            "final review selected a Hook outside the strict candidate contract"
                        )
                main_duration, expansion_duration = _final_review_duration_estimate(
                    review,
                    duration_by_id,
                )
                combined_duration = main_duration + expansion_duration
                if duration_low > 0 and main_duration + 0.05 < float(duration_low):
                    minimum_skeleton = float(duration_low) * 0.75
                    if main_duration + 0.05 < minimum_skeleton:
                        contract_issues.append(
                            f"clips\u4e3b\u7247\u5355\u4ec5{main_duration:.1f}\u79d2\uff0c"
                            f"\u4f4e\u4e8e\u7a33\u5b9a\u53d9\u4e8b\u9aa8\u67b6{minimum_skeleton:.1f}\u79d2"
                        )
                if duration_high > 0 and main_duration - 0.05 > float(duration_high):
                    contract_issues.append(
                        f"clips\u4e3b\u7247\u5355{main_duration:.1f}\u79d2\uff0c"
                        f"\u8d85\u8fc7\u4e0a\u9650{float(duration_high):.1f}\u79d2"
                    )
                if duration_low > 0 and combined_duration + 0.05 < float(duration_low):
                    contract_issues.append(
                        f"clips\u4e0e\u5168\u90e8expansion_plan\u5408\u8ba1\u4ec5"
                        f"{combined_duration:.1f}\u79d2"
                    )
                inventory_by_id = {
                    int(item["srt_index"]): item for item in compact_inventory
                }
                revised_sequence = []
                for item in review.clips:
                    selected_items = [
                        inventory_by_id[candidate_id]
                        for candidate_id in item.get("srt_indices", [])
                        if candidate_id in inventory_by_id
                    ]
                    revised_sequence.append({
                        "clip_type": item.get("clip_type"),
                        "duration_sec": sum(
                            float(candidate.get("duration_sec") or 0.0)
                            for candidate in selected_items
                        ),
                        "text": " ".join(
                            _clean_text(candidate.get("text"), 240)
                            for candidate in selected_items
                        ),
                    })
                revised_objective_issues = _final_objective_issues(revised_sequence)
                if revised_objective_issues:
                    contract_issues.append(
                        "\u4fee\u8ba2\u7247\u5355\u4ecd\u6709\u5ba2\u89c2\u95ee\u9898:"
                        + "\u3001".join(revised_objective_issues[:4])
                    )
            if not contract_issues:
                return review
            last_error = "\uff1b".join(contract_issues)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt == 0:
            safety_margin = min(8.0, max(3.0, float(duration_low or 0.0) * 0.05))
            target_floor = float(duration_low or 0.0) + safety_margin
            skeleton_floor = float(duration_low or 0.0) * 0.8
            previous = _clean_text(content, 12000)
            correction = (
                "\n\u4e0a\u4e00\u6b21\u7ec8\u5ba1\u8f93\u51fa\u672a\u901a\u8fc7\u673a\u5668\u53ef\u9a8c\u8bc1\u5408\u540c:"
                + last_error
                + "\u3002\n\u8bf7\u91cd\u65b0\u8f93\u51fa\u5b8c\u6574JSON\u3002revise\u65f6clips\u4e3b\u7247\u5355\u4f18\u5148\u8fbe\u5230"
                + f"{target_floor:.1f}\u79d2\u5de6\u53f3\uff0c\u4e14\u4e0d\u5f97\u8d85\u8fc7{float(duration_high or 0.0):.1f}\u79d2;"
                f"\u82e5\u4e3a\u4fdd\u8bc1\u8bed\u53e5\u8d28\u91cf\u9700\u8981\u7559\u51fa\u8865\u7247\u7a7a\u95f4\uff0cclips\u4e0d\u5f97\u4f4e\u4e8e{skeleton_floor:.1f}\u79d2\uff0c"
                f"\u4e14clips\u4e0eexpansion_plan\u5408\u8ba1\u5fc5\u987b\u81f3\u5c11{target_floor:.1f}\u79d2\u3002"
                "\u5148\u4fdd\u7559\u539f\u7247\u5355\u4e2d\u6ca1\u6709\u95ee\u9898\u7684\u9aa8\u67b6\uff0c\u518d\u66ff\u6362\u88ab\u6807\u8bb0\u7684Hook\u3001\u957f\u6bb5\u6216\u5bfc\u64ad\u8bdd\u672f;"
                f"\u5efa\u8baeclips\u7ea6{recommended_min_clips}-{recommended_max_clips}\u6bb5\uff0c\u4e0d\u5f97\u7528\u5927\u91cf\u788e\u53e5\u51d1\u65f6\u957f\u3002\n"
                "\u4e0a\u4e00\u6b21\u65e0\u6548\u8f93\u51fa:\n"
                + previous
            )
            if log_fn:
                log_fn(f"AI\u6210\u7247\u7ec8\u5ba1: {last_error}\uff0c\u4ea4\u56deAI\u505a\u4e00\u6b21\u5408\u540c\u7ea0\u6b63")
            continue
        raise ContentReviewError(f"\u6210\u7247\u7ec8\u5ba1\u5408\u540c\u7ea0\u6b63\u5931\u8d25: {last_error}")

    raise ContentReviewError(f"\u6210\u7247\u7ec8\u5ba1\u5931\u8d25: {last_error}")
