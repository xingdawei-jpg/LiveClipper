# -*- coding: utf-8 -*-
"""Grounded marketing-intent evidence and narrative-arc validation.

This module is intentionally side-effect free except for the optional shadow
observation writer.  It never changes candidate text, timing, source IDs, or
the director's selected order.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping, Sequence


MARKETING_INTENT_VERSION = "marketing-intent-v3"
MARKETING_INTENT_SHADOW_SCHEMA = "marketing-intent-shadow-v1"
_VALID_INTENTS = {
    "product_origin",
    "product_distinction",
    "identity_expression",
    "ownership_scene",
    "emotional_turn",
    "product_proof",
}
_ARC_OPENING_INTENTS = {
    "product_origin",
    "product_distinction",
    "identity_expression",
    "ownership_scene",
    "emotional_turn",
}
_VALID_PROOF_STRENGTHS = {"direct", "supporting", "weak", "none"}
_VALID_PRODUCT_SCOPES = {"locked", "inferred_same_product", "unknown", "conflict"}
_VALID_CONTINUITIES = {"strong", "recoverable", "weak"}
_DIRECT_EVIDENCE_HINTS = (
    "原因", "细节", "工艺", "材质", "面料", "版型", "效果", "实测", "体验",
    "场景", "搭配", "设计", "做法", "数据", "证明", "解释",
)


@dataclass(frozen=True)
class IntentEvidence:
    candidate_id: int
    intent_type: str
    claim: str
    evidence_quote: str
    source_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "intent_type": self.intent_type,
            "claim": self.claim,
            "evidence_quote": self.evidence_quote,
            "source_id": self.source_id,
        }


@dataclass(frozen=True)
class NarrativeProof:
    candidate_id: int
    strength: str

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, "strength": self.strength}


@dataclass(frozen=True)
class NarrativeArc:
    arc_id: str
    intent_type: str
    opening_candidate_id: int
    proof_links: tuple[NarrativeProof, ...]
    result_candidate_ids: tuple[int, ...]
    product_scope: str
    continuity: str
    source_id: str
    story_block_id: str
    candidate_ids: tuple[int, ...]
    activation_eligible: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "arc_id": self.arc_id,
            "intent_type": self.intent_type,
            "opening_candidate_id": self.opening_candidate_id,
            "proof_links": [item.to_dict() for item in self.proof_links],
            "result_candidate_ids": list(self.result_candidate_ids),
            "product_scope": self.product_scope,
            "continuity": self.continuity,
            "source_id": self.source_id,
            "story_block_id": self.story_block_id,
            "candidate_ids": list(self.candidate_ids),
            "activation_eligible": self.activation_eligible,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ArcRejection:
    draft_index: int
    reason: str
    opening_candidate_id: int = 0
    candidate_ids: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_index": self.draft_index,
            "reason": self.reason,
            "opening_candidate_id": self.opening_candidate_id,
            "candidate_ids": list(self.candidate_ids),
        }


@dataclass(frozen=True)
class MarketingIntentBundle:
    candidate_digest: str
    intents: tuple[IntentEvidence, ...] = ()
    arcs: tuple[NarrativeArc, ...] = ()
    rejections: tuple[ArcRejection, ...] = ()
    response_present: bool = False
    version: str = MARKETING_INTENT_VERSION

    @property
    def eligible_arcs(self) -> tuple[NarrativeArc, ...]:
        return tuple(arc for arc in self.arcs if arc.activation_eligible)

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "response_present": self.response_present,
            "intent_count": len(self.intents),
            "arc_count": len(self.arcs),
            "eligible_arc_count": len(self.eligible_arcs),
            "rejection_count": len(self.rejections),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "candidate_digest": self.candidate_digest,
            "response_present": self.response_present,
            "intents": [item.to_dict() for item in self.intents],
            "arcs": [item.to_dict() for item in self.arcs],
            "rejections": [item.to_dict() for item in self.rejections],
        }


def marketing_intent_prompt_contract() -> str:
    """Return the bounded extra schema for the existing content-review call."""
    return """
11. 额外做一次营销意图观察，不决定最终片单。只识别原句已经明确说出的内容：product_origin（来源/发现过程）、product_distinction（具体差异）、identity_expression（气质/身份表达）、ownership_scene（拥有后的真实场景）、emotional_turn（有具体理由的情绪转折）、product_proof（产品事实或作用证明）。没有原句证据就不要标注。
12. narrative_arcs 只是弧草案，不是成片顺序。弧开头只能是 product_origin、product_distinction、identity_expression、ownership_scene 或 emotional_turn；product_proof 只能作为兑现开头主张的证据，绝不能当弧开头。每条草案必须有至少一个紧接着能证明同一主张的具体证据候选。直接证明候选必须同时在 marketing_intents 里单独标为 product_proof；如果证明跨多个连续候选，链上的每个证明候选都必须分别标成 product_proof，否则不要输出这条弧。不能拿另一个场景、情绪或泛泛夸赞充当证明。不得把库存稀缺、价格、CTA、直播互动、个人尺码身高体重或泛泛夸赞写成意图或弧。没有合格草案时返回空数组。
13. 不得把“高级感、好看、有气质”这种空话单独当作 identity_expression；必须同时有具体表达、穿着效果、设计细节或真实拥有场景作为原句依据。不要从字幕之外推断买手店、限量、明星同款或用户心理。

marketing_intents 字段顺序：[候选编号,intent_type,原句可证明的购买主张,原文短引]
narrative_arcs 字段顺序：[开头候选编号,直接证明候选编号数组,可选结果候选编号数组,intent_type,这组原句如何兑现主张]
""".strip()


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _clean_ids(value: Any, *, limit: int) -> tuple[int, ...]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    result: list[int] = []
    seen: set[int] = set()
    for raw in values:
        try:
            candidate_id = int(raw)
        except (TypeError, ValueError):
            continue
        if candidate_id <= 0 or candidate_id in seen:
            continue
        seen.add(candidate_id)
        result.append(candidate_id)
        if len(result) >= limit:
            break
    return tuple(result)


def _normalize_intent_type(value: Any) -> str:
    intent_type = _clean_text(value, 40).lower()
    aliases = {
        "origin": "product_origin",
        "story": "product_origin",
        "distinction": "product_distinction",
        "identity": "identity_expression",
        "scene": "ownership_scene",
        "emotion": "emotional_turn",
        "proof": "product_proof",
    }
    intent_type = aliases.get(intent_type, intent_type)
    return intent_type if intent_type in _VALID_INTENTS else ""


def _normalized_chars(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "")).lower()


def _grounded_quote(value: Any, candidate_text: Any) -> str:
    quote = _clean_text(value, 120)
    source = _clean_text(candidate_text, 240)
    quote_chars = _normalized_chars(quote)
    source_chars = _normalized_chars(source)
    if len(quote_chars) < 2 or quote_chars not in source_chars:
        return ""
    return quote


def _candidate_map(inventory: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for raw in inventory:
        if not isinstance(raw, Mapping):
            continue
        try:
            candidate_id = int(raw.get("srt_index") or raw.get("candidate_id") or 0)
        except (TypeError, ValueError):
            continue
        if candidate_id <= 0:
            continue
        result[candidate_id] = {
            "candidate_id": candidate_id,
            "source_id": _clean_text(raw.get("source") or raw.get("source_id"), 40),
            "story_block_id": _clean_text(raw.get("story_block_id"), 80),
            "continuity_group_id": _clean_text(raw.get("continuity_group_id"), 80),
            "text": _clean_text(raw.get("text"), 240),
        }
    return result


def _card_map(cards: Iterable[Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for card in cards:
        if isinstance(card, Mapping):
            raw = card
            candidate_value = raw.get("candidate_id")
            roles = raw.get("roles") or ()
            evidence_type = raw.get("evidence_type") or ""
            evidence_quote = raw.get("evidence_quote") or ""
        else:
            candidate_value = getattr(card, "candidate_id", 0)
            roles = getattr(card, "roles", ()) or ()
            evidence_type = getattr(card, "evidence_type", "") or ""
            evidence_quote = getattr(card, "evidence_quote", "") or ""
        try:
            candidate_id = int(candidate_value or 0)
        except (TypeError, ValueError):
            continue
        if candidate_id <= 0:
            continue
        result[candidate_id] = {
            "roles": {str(item).strip().lower() for item in roles if str(item).strip()},
            "evidence_type": _clean_text(evidence_type, 80),
            "evidence_quote": _clean_text(evidence_quote, 120),
        }
    return result


def _is_direct_evidence(card: Mapping[str, Any]) -> bool:
    roles = set(card.get("roles") or ())
    evidence_type = str(card.get("evidence_type") or "")
    if "evidence" in roles:
        return True
    if "product" not in roles and "effect" not in roles:
        return False
    return any(hint in evidence_type for hint in _DIRECT_EVIDENCE_HINTS)


def _parse_intent(raw: Any, candidates: Mapping[int, Mapping[str, Any]], card_ids: set[int]) -> IntentEvidence | None:
    if isinstance(raw, (list, tuple)):
        if len(raw) < 3:
            return None
        raw = {
            "candidate_id": raw[0],
            "intent_type": raw[1],
            "claim": raw[2],
            "evidence_quote": raw[3] if len(raw) > 3 else "",
        }
    if not isinstance(raw, Mapping):
        return None
    ids = _clean_ids(raw.get("candidate_id") or raw.get("srt_index"), limit=1)
    if not ids or ids[0] not in card_ids:
        return None
    candidate_id = ids[0]
    intent_type = _normalize_intent_type(raw.get("intent_type") or raw.get("type"))
    claim = _clean_text(raw.get("claim") or raw.get("buyer_value"), 120)
    candidate = candidates.get(candidate_id) or {}
    quote = _grounded_quote(raw.get("evidence_quote"), candidate.get("text"))
    if not quote:
        quote = _clean_text(candidate.get("text"), 120)
    if not intent_type or len(claim) < 4 or len(quote) < 2:
        return None
    return IntentEvidence(
        candidate_id=candidate_id,
        intent_type=intent_type,
        claim=claim,
        evidence_quote=quote,
        source_id=str(candidate.get("source_id") or ""),
    )


def _parse_arc_draft(raw: Any) -> tuple[int, tuple[int, ...], tuple[int, ...], str, str] | None:
    if isinstance(raw, (list, tuple)):
        if len(raw) < 4:
            return None
        raw = {
            "opening_candidate_id": raw[0],
            "proof_candidate_ids": raw[1],
            "result_candidate_ids": raw[2],
            "intent_type": raw[3],
            "reason": raw[4] if len(raw) > 4 else "",
        }
    if not isinstance(raw, Mapping):
        return None
    opening_ids = _clean_ids(raw.get("opening_candidate_id") or raw.get("opening_id"), limit=1)
    if not opening_ids:
        return None
    proofs = _clean_ids(raw.get("proof_candidate_ids") or raw.get("proof_ids"), limit=3)
    results = _clean_ids(raw.get("result_candidate_ids") or raw.get("result_ids"), limit=3)
    intent_type = _normalize_intent_type(raw.get("intent_type") or raw.get("type"))
    reason = _clean_text(raw.get("reason"), 120)
    if not intent_type:
        return None
    return opening_ids[0], proofs, results, intent_type, reason


def _proof_strength(
    opening: Mapping[str, Any],
    proof: Mapping[str, Any],
    proof_card: Mapping[str, Any],
) -> str:
    if not _is_direct_evidence(proof_card):
        return "none"
    same_source = bool(opening.get("source_id")) and opening.get("source_id") == proof.get("source_id")
    same_continuity = bool(opening.get("continuity_group_id")) and (
        opening.get("continuity_group_id") == proof.get("continuity_group_id")
    )
    same_story = bool(opening.get("story_block_id")) and (
        opening.get("story_block_id") == proof.get("story_block_id")
    )
    if same_source and same_continuity:
        return "direct"
    if same_source and same_story:
        return "supporting"
    return "weak"


def _arc_continuity(candidates: Sequence[Mapping[str, Any]]) -> tuple[str, str, str]:
    sources = {str(item.get("source_id") or "").strip() for item in candidates}
    sources.discard("")
    story_blocks = {str(item.get("story_block_id") or "").strip() for item in candidates}
    story_blocks.discard("")
    groups = {str(item.get("continuity_group_id") or "").strip() for item in candidates}
    groups.discard("")
    source_id = next(iter(sources)) if len(sources) == 1 else ""
    story_block_id = next(iter(story_blocks)) if len(story_blocks) == 1 else ""
    if len(sources) == 1 and len(groups) == 1:
        return "strong", source_id, story_block_id
    if len(sources) == 1 and len(story_blocks) == 1:
        return "recoverable", source_id, story_block_id
    return "weak", source_id, story_block_id


def _product_scope(main_product: str, continuity: str) -> str:
    if _clean_text(main_product, 100):
        return "locked"
    if continuity in {"strong", "recoverable"}:
        return "inferred_same_product"
    return "unknown"


def build_marketing_intent_bundle(
    data: Mapping[str, Any],
    *,
    cards: Iterable[Any],
    inventory: Sequence[Mapping[str, Any]],
    candidate_digest: str,
    main_product: str = "",
) -> MarketingIntentBundle:
    """Validate model-proposed evidence and arcs against frozen candidates.

    Missing fields are a valid no-op result. This allows the existing content
    review cache and fallback behavior to remain compatible while Phase A is
    collecting shadow observations.
    """
    payload: Mapping[str, Any] = data
    cached_rejections: list[ArcRejection] = []
    response_present = isinstance(data.get("marketing_intents"), list) or isinstance(data.get("narrative_arcs"), list)
    cached = data.get("marketing_intent")
    if not response_present and isinstance(cached, Mapping) and cached.get("response_present") is True:
        payload = dict(data)
        payload["marketing_intents"] = [
            [
                item.get("candidate_id"), item.get("intent_type"),
                item.get("claim"), item.get("evidence_quote"),
            ]
            for item in (cached.get("intents") or [])
            if isinstance(item, Mapping)
        ]
        payload["narrative_arcs"] = [
            [
                item.get("opening_candidate_id"),
                [proof.get("candidate_id") for proof in (item.get("proof_links") or []) if isinstance(proof, Mapping)],
                item.get("result_candidate_ids") or [],
                item.get("intent_type"),
                item.get("reason"),
            ]
            for item in (cached.get("arcs") or [])
            if isinstance(item, Mapping)
        ]
        for item in cached.get("rejections") or []:
            if not isinstance(item, Mapping):
                continue
            draft_ids = _clean_ids(item.get("draft_index"), limit=1)
            opening_ids = _clean_ids(item.get("opening_candidate_id"), limit=1)
            cached_rejections.append(ArcRejection(
                draft_index=draft_ids[0] if draft_ids else 0,
                reason=_clean_text(item.get("reason"), 80) or "cached_rejection",
                opening_candidate_id=opening_ids[0] if opening_ids else 0,
                candidate_ids=_clean_ids(item.get("candidate_ids"), limit=7),
            ))
        response_present = True
    if not response_present:
        return MarketingIntentBundle(candidate_digest=str(candidate_digest or ""))

    candidates = _candidate_map(inventory)
    cards_by_id = _card_map(cards)
    card_ids = set(cards_by_id).intersection(candidates)
    intents: list[IntentEvidence] = []
    intent_by_candidate: dict[int, IntentEvidence] = {}
    for raw in payload.get("marketing_intents") or []:
        intent = _parse_intent(raw, candidates, card_ids)
        if intent is None or intent.candidate_id in intent_by_candidate:
            continue
        intent_by_candidate[intent.candidate_id] = intent
        intents.append(intent)
        if len(intents) >= 40:
            break

    arcs: list[NarrativeArc] = []
    rejections: list[ArcRejection] = []
    seen_arcs: set[tuple[int, tuple[int, ...], tuple[int, ...]]] = set()
    for draft_index, raw in enumerate(payload.get("narrative_arcs") or [], start=1):
        parsed = _parse_arc_draft(raw)
        if parsed is None:
            rejections.append(ArcRejection(draft_index, "invalid_schema"))
            continue
        opening_id, proof_ids, result_ids, intent_type, reason = parsed
        all_ids = (opening_id, *proof_ids, *result_ids)
        if opening_id not in card_ids or any(candidate_id not in card_ids for candidate_id in all_ids):
            rejections.append(ArcRejection(draft_index, "unknown_or_unreviewed_candidate", opening_id, _clean_ids(all_ids, limit=7)))
            continue
        if opening_id not in intent_by_candidate:
            rejections.append(ArcRejection(draft_index, "opening_has_no_grounded_intent", opening_id, _clean_ids(all_ids, limit=7)))
            continue
        if intent_by_candidate[opening_id].intent_type not in _ARC_OPENING_INTENTS:
            rejections.append(ArcRejection(draft_index, "product_proof_cannot_open_arc", opening_id, _clean_ids(all_ids, limit=7)))
            continue
        if intent_by_candidate[opening_id].intent_type != intent_type:
            rejections.append(ArcRejection(draft_index, "intent_type_mismatch", opening_id, _clean_ids(all_ids, limit=7)))
            continue
        proof_ids = tuple(candidate_id for candidate_id in proof_ids if candidate_id != opening_id)
        if not proof_ids:
            rejections.append(ArcRejection(draft_index, "missing_proof", opening_id, _clean_ids(all_ids, limit=7)))
            continue
        result_ids = tuple(candidate_id for candidate_id in result_ids if candidate_id not in {opening_id, *proof_ids})
        opening = candidates[opening_id]
        proof_links = tuple(
            NarrativeProof(
                candidate_id=candidate_id,
                strength=_proof_strength(opening, candidates[candidate_id], cards_by_id[candidate_id]),
            )
            for candidate_id in proof_ids
        )
        if not any(
            intent_by_candidate.get(candidate_id)
            and intent_by_candidate[candidate_id].intent_type == "product_proof"
            for candidate_id in proof_ids
        ):
            rejections.append(ArcRejection(draft_index, "proof_has_no_product_evidence_intent", opening_id, _clean_ids(all_ids, limit=7)))
            continue
        if not any(link.strength == "direct" for link in proof_links):
            rejections.append(ArcRejection(draft_index, "no_direct_proof", opening_id, _clean_ids(all_ids, limit=7)))
            continue
        arc_ids = _clean_ids((opening_id, *proof_ids, *result_ids), limit=7)
        signature = (opening_id, proof_ids, result_ids)
        if signature in seen_arcs:
            rejections.append(ArcRejection(draft_index, "duplicate_arc", opening_id, arc_ids))
            continue
        seen_arcs.add(signature)
        arc_candidates = [candidates[candidate_id] for candidate_id in arc_ids]
        continuity, source_id, story_block_id = _arc_continuity(arc_candidates)
        scope = _product_scope(main_product, continuity)
        activation_eligible = continuity == "strong" and scope in {"locked", "inferred_same_product"}
        arcs.append(NarrativeArc(
            arc_id=f"ARC-{len(arcs) + 1:02d}",
            intent_type=intent_type,
            opening_candidate_id=opening_id,
            proof_links=proof_links,
            result_candidate_ids=result_ids,
            product_scope=scope,
            continuity=continuity,
            source_id=source_id,
            story_block_id=story_block_id,
            candidate_ids=arc_ids,
            activation_eligible=activation_eligible,
            reason=reason or "grounded intent with a direct proof candidate",
        ))
        if len(arcs) >= 16:
            break

    return MarketingIntentBundle(
        candidate_digest=str(candidate_digest or ""),
        intents=tuple(intents),
        arcs=tuple(arcs),
        rejections=tuple((rejections + cached_rejections)[:40]),
        response_present=True,
    )


def _user_data_dir() -> Path:
    try:
        from config import USER_DATA_DIR
        return Path(USER_DATA_DIR)
    except Exception:
        return Path(os.environ.get("APPDATA", str(Path.home()))) / "LiveClipper"


def marketing_intent_shadow_path() -> Path:
    return _user_data_dir() / "ai_feedback" / "marketing_arc_shadow.jsonl"


def build_shadow_run_key(candidate_digest: str, selection_manifest: Mapping[str, Any] | None = None) -> str:
    payload = {
        "candidate_digest": str(candidate_digest or ""),
        "selection_digest": str((selection_manifest or {}).get("digest") or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _append_shadow_record(record: Mapping[str, Any], path: Path | None = None) -> bool:
    target = path or marketing_intent_shadow_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size > 8 * 1024 * 1024:
            lines = target.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
            target.write_text("\n".join(lines[-1000:]) + "\n", encoding="utf-8")
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")) + "\n")
        return True
    except OSError:
        return False


def append_shadow_observation(
    *,
    bundle: MarketingIntentBundle,
    review_cache_key: str,
    mode: str,
    category: str,
    selection_manifest: Mapping[str, Any] | None,
) -> str:
    """Persist an immutable Phase-A observation after a legacy final selection."""
    manifest = dict(selection_manifest or {})
    run_key = build_shadow_run_key(bundle.candidate_digest, manifest)
    record = {
        "schema": MARKETING_INTENT_SHADOW_SCHEMA,
        "event": "legacy_final_selection",
        "created_at": round(time.time(), 3),
        "run_key": run_key,
        "mode": str(mode or "shadow"),
        "review_cache_key": str(review_cache_key or ""),
        "candidate_digest": bundle.candidate_digest,
        "category": _clean_text(category, 80),
        "marketing_intent": bundle.to_dict(),
        "selection_manifest": manifest,
    }
    return run_key if _append_shadow_record(record) else ""


def append_manual_feedback_shadow(run_key: str, feedback: Mapping[str, Any]) -> bool:
    """Store a joinable manual decision without changing preference learning."""
    if not str(run_key or "").strip():
        return False
    fields = {
        "preview_id", "scope", "feedback_scope", "category", "target_duration",
        "selected_clip_count", "kept_segment_count", "rejected_segment_count",
        "rejected_clip_count", "role_samples",
    }
    record = {
        "schema": MARKETING_INTENT_SHADOW_SCHEMA,
        "event": "manual_preview_selection",
        "created_at": round(time.time(), 3),
        "run_key": str(run_key),
        "feedback": {key: feedback.get(key) for key in fields if key in feedback},
    }
    return _append_shadow_record(record)
