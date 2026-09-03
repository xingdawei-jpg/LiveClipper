"""Source-only commercial asset annotations for CandidateLedger.

The annotator describes how a hard-safe candidate can serve a commercial
story.  It does not identify what appears in a frame, does not change the
candidate pool, and does not decide a story order.  Ambiguous wording stays
available as an ``unknown`` supporting asset instead of being discarded.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from candidate_ledger import CandidateAsset


_STYLING = re.compile(r"搭(?:配)?|配(?:牛仔裤|裤|裙|外套|马甲)|叠穿|内搭|外搭")
_LIFESTYLE = re.compile(r"通勤|上班|出游|旅行|度假|海边|早秋|夏天|春天|秋天|日常|约会|学院|开学|学生")
_DESIGN = re.compile(r"肩线|版型|剪裁|领口|袖口|下摆|腰线|褶|扣|设计")
_WEARING_EFFECT = re.compile(r"显瘦|遮肉|显高|显比例|修饰|上身|不挑人|减龄|利落|有型|肩.{0,16}(?:瘦|窄)")
_TRUST = re.compile(r"面料|材质|成分|工艺|透气|防晒|舒适|亲肤|品质|质感|安全|可降解|精梳棉|纤维|弹力")
_FIT_OR_LENGTH = re.compile(r"裙长|长度|大货.{0,12}(?:长|加长)|长.{0,8}(?:公分|厘米)|不走光|安全裤")
_QUESTION = re.compile(r"(?:吗|呢|好不好|是不是)[。！？!?]?$")
_OFFERING = re.compile(r"我.{0,8}安排|给你.{0,8}安排|单独.{0,8}(?:安排|上)")
# This intentionally needs an explicit conversion action.  It is category
# neutral: "卖爆" alone remains ambiguous and is not treated as a filter.
_INDEPENDENT_SALE = re.compile(r"拍(?:链接|下单)|点(?:链接|小黄车)|加购|上车|下单")


def _candidate_id(raw: Mapping[str, Any]) -> int:
    try:
        return int(raw.get("candidate_id") or raw.get("srt_index") or raw.get("id") or 0)
    except (TypeError, ValueError):
        return 0


def classify_commercial_asset(raw: Mapping[str, Any]) -> CandidateAsset:
    """Classify one candidate conservatively from spoken text only.

    The order is commercial safety first, then recognisable story use.  No
    product/category vocabulary is required; product relation is a soft,
    auditable context rather than an admission gate.
    """

    candidate_id = _candidate_id(raw)
    text = str(raw.get("text") or "").strip()
    if _INDEPENDENT_SALE.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "related_product", "confidence": "high"},
            "asset_role": "unknown",
            "story_permission": "unavailable",
            "evidence_source": "asr",
            "reason": "检测到明确的独立转化动作；它不是搭配或使用场景，不能承担当前故事章节。",
        })
    if _FIT_OR_LENGTH.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "same_product", "confidence": "medium"},
            "asset_role": "product_proof",
            "story_permission": "main_story",
            "evidence_source": "asr",
            "reason": "长度或安全性是可直接推进购买判断的产品事实。",
        })
    if _QUESTION.search(text) or _OFFERING.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "unknown", "confidence": "low"},
            "asset_role": "unknown",
            "story_permission": "supporting_story",
            "evidence_source": "asr",
            "reason": "互动提问或另行安排的口播缺少独立证明语义，保留供人工审计，不作为主故事事实。",
        })
    if _STYLING.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "related_product", "confidence": "medium"},
            "asset_role": "styling_scene",
            "story_permission": "supporting_story",
            "evidence_source": "asr",
            "reason": "搭配/叠穿表达可说明当前商品的穿搭结果，不是另一件商品的独立销售。",
        })
    if _LIFESTYLE.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "unknown", "confidence": "low"},
            "asset_role": "lifestyle_scene",
            "story_permission": "supporting_story",
            "evidence_source": "asr",
            "reason": "场景词为商业故事补充使用代入；未凭空判断画面或商品主体。",
        })
    if _DESIGN.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "same_product", "confidence": "medium"},
            "asset_role": "design_explanation",
            "story_permission": "main_story",
            "evidence_source": "asr",
            "reason": "设计或版型解释可以直接推进商品为什么成立的主故事。",
        })
    if _WEARING_EFFECT.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "unknown", "confidence": "medium"},
            "asset_role": "wearing_effect",
            "story_permission": "main_story",
            "evidence_source": "asr",
            "reason": "上身结果可作为主故事证据；主体不明不等于无效。",
        })
    if _TRUST.search(text):
        return CandidateAsset.from_mapping({
            "candidate_id": candidate_id,
            "subject_context": {"product_focus": "same_product", "confidence": "medium"},
            "asset_role": "trust_signal",
            "story_permission": "main_story",
            "evidence_source": "asr",
            "reason": "材质、舒适或品质说明可提供购买信任。",
        })
    return CandidateAsset.from_mapping({
        "candidate_id": candidate_id,
        "subject_context": {"product_focus": "unknown", "confidence": "low"},
        "asset_role": "unknown",
        "story_permission": "supporting_story",
        "evidence_source": "asr",
        "reason": "语义不足以可靠定性，保留为可人工审计的辅助资产而非过滤。",
    })


def classify_commercial_assets(items: Iterable[Mapping[str, Any]]) -> tuple[CandidateAsset, ...]:
    return tuple(
        classify_commercial_asset(item)
        for item in items or ()
        if isinstance(item, Mapping) and _candidate_id(item) > 0
    )


def independent_sale_messages(items: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Report explicit transaction messages even if hard safety excluded them."""

    result: list[dict[str, Any]] = []
    for item in items or ():
        if not isinstance(item, Mapping):
            continue
        text = str(item.get("text") or "").strip()
        if _INDEPENDENT_SALE.search(text):
            result.append({
                "candidate_id": _candidate_id(item),
                "text": text,
                "reason": "明确转化动作；硬安全层应排除。仅凭 ASR 不断言它属于哪一个商品。",
            })
    return tuple(result)
