# -*- coding: utf-8 -*-
"""Source-only M2.5 commercial coverage director.

This module deliberately sits *before* candidate selection.  It turns one M1
commercial story plus read-only ledger/content-card facts into a purchasing
recognition path.  It never returns candidate IDs, timestamps, subtitle order,
or a candidate whitelist.  M2 still chooses from the complete hard-safe pool;
M3 still only materializes M2's selected word spans.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from ai_cost_ledger import record_ai_call
from ai_model_config import ai_chat_completions_url
from ssl_context import create_ssl_context


COMMERCE_DIRECTOR_VERSION = "commerce-director-v1"
APPAREL_45S_COVERAGE_PROFILE = "apparel_45s"

# These are cognitive jobs, not clothing keywords or a candidate filter.  The
# same structure can gain other vertical profiles later without changing M1,
# M2, the Ledger, or M3.
APPAREL_45S_BEATS: tuple[dict[str, str | bool], ...] = (
    {"beat_id": "identity", "role": "identity", "need": "让陌生观众知道产品是什么以及为什么值得继续听", "type": "product_positioning", "required": True},
    {"beat_id": "difference", "role": "difference", "need": "说明它和常见替代品相比的关键差异", "type": "differentiator", "required": True},
    {"beat_id": "problem_or_benefit", "role": "benefit", "need": "建立用户顾虑、期待或直接购买收益", "type": "problem_solution", "required": True},
    {"beat_id": "visible_result", "role": "visual", "need": "让观众理解上身或使用后会发生什么变化", "type": "visible_result", "required": True},
    {"beat_id": "proof", "role": "proof", "need": "给出能降低购买不确定性的可信依据", "type": "proof", "required": True},
    {"beat_id": "scene_or_audience", "role": "scene", "need": "帮助观众代入适用人群或真实使用场景", "type": "scene_or_audience", "required": False},
    {"beat_id": "trust", "role": "trust", "need": "补充质量、体验、工艺或其他可信支撑", "type": "trust", "required": False},
)


@dataclass(frozen=True)
class CommerceEvidenceCard:
    """Read-only semantic card; source IDs are lineage metadata, not a pick list."""

    candidate_id: int
    text: str
    category: tuple[str, ...]
    business_value: str
    evidence: str
    strength: str
    story_tier: str
    materializable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "text": self.text,
            "category": list(self.category),
            "business_value": self.business_value,
            "evidence": self.evidence,
            "strength": self.strength,
            "story_tier": self.story_tier,
            "materializable": self.materializable,
            "lineage_note": "read_only_evidence_reference_not_a_candidate_whitelist",
        }


@dataclass(frozen=True)
class CommerceBeat:
    beat_id: str
    role: str
    need: str
    type: str
    required: bool
    availability: str
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "beat_id": self.beat_id,
            "role": self.role,
            "need": self.need,
            "type": self.type,
            "required": self.required,
            "availability": self.availability,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class CommerceStoryPlan:
    profile: str
    strategy_id: str
    opening_goal: str
    opening_promise: str
    beats: tuple[CommerceBeat, ...]
    coverage_status: str
    missing_required_beat_ids: tuple[str, ...]
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": COMMERCE_DIRECTOR_VERSION,
            "profile": self.profile,
            "strategy_id": self.strategy_id,
            "opening": {"goal": self.opening_goal, "promise": self.opening_promise},
            "beats": [beat.to_dict() for beat in self.beats],
            "coverage_status": self.coverage_status,
            "missing_required_beat_ids": list(self.missing_required_beat_ids),
            "notes": self.notes,
            "selection_boundary": {
                "contains_candidate_ids": False,
                "contains_timestamps": False,
                "contains_subtitle_order": False,
                "is_candidate_whitelist": False,
            },
        }


def _text(value: Any) -> str:
    return str(value or "").strip()


def _strategy_evidence_by_candidate(strategy: Any) -> dict[int, list[tuple[str, str, str]]]:
    result: dict[int, list[tuple[str, str, str]]] = {}
    for pool_name, tier in (("core_evidence_pool", "core"), ("supporting_evidence_pool", "supporting"), ("bridge_candidates", "bridge")):
        for item in tuple(getattr(strategy, pool_name, ()) or ()):
            ids = tuple(getattr(item, "subtitle_ids", ()) or ())
            role = _text(getattr(item, "role", ""))
            claim = _text(getattr(item, "claim", ""))
            for candidate_id in ids:
                try:
                    result.setdefault(int(candidate_id), []).append((tier, role, claim))
                except (TypeError, ValueError):
                    continue
    return result


def build_commerce_evidence_cards(
    *,
    strategy: Any,
    ledger_assets: Sequence[Mapping[str, Any]],
    executable_evidence: Mapping[int, Mapping[str, Any]],
) -> tuple[CommerceEvidenceCard, ...]:
    """Adapt existing Ledger/M1 facts into cards without removing a candidate."""

    m1_by_id = _strategy_evidence_by_candidate(strategy)
    cards: list[CommerceEvidenceCard] = []
    for asset in ledger_assets:
        try:
            candidate_id = int(asset.get("candidate_id") or 0)
        except (TypeError, ValueError):
            continue
        if candidate_id <= 0:
            continue
        facts = dict(executable_evidence.get(candidate_id) or {})
        annotations = m1_by_id.get(candidate_id, [])
        tiers = tuple(dict.fromkeys(item[0] for item in annotations))
        roles = tuple(dict.fromkeys(item[1] for item in annotations if item[1]))
        claims = tuple(dict.fromkeys(item[2] for item in annotations if item[2]))
        asset_role = _text(asset.get("asset_role") or "unknown")
        category = tuple(dict.fromkeys((asset_role, *roles))) or ("unknown",)
        tier = "/".join(tiers) if tiers else "unlinked_hard_safe"
        cards.append(CommerceEvidenceCard(
            candidate_id=candidate_id,
            text=_text(asset.get("text")),
            category=category,
            business_value="；".join(claims) or _text(asset.get("reason")) or "未由 M1 单独声明，但仍属于完整 hard-safe 素材。",
            evidence=_text(asset.get("subject_evidence") or asset.get("reason") or asset.get("text")),
            strength=tier,
            story_tier=tier,
            materializable=bool(facts.get("materializable", True)),
        ))
    return tuple(cards)


def _profile_templates(profile: str) -> tuple[dict[str, str | bool], ...]:
    if profile != APPAREL_45S_COVERAGE_PROFILE:
        raise ValueError(f"unsupported commerce coverage profile: {profile}")
    return APPAREL_45S_BEATS


def build_commerce_director_prompt(
    *,
    strategy: Any,
    cards: Sequence[CommerceEvidenceCard],
    target_duration: float,
    profile: str = APPAREL_45S_COVERAGE_PROFILE,
) -> str:
    """Build a pre-selection purchasing-path task, intentionally without timing."""

    template = _profile_templates(profile)
    strategy_payload = strategy.to_dict() if hasattr(strategy, "to_dict") else dict(strategy or {})
    return "\n".join((
        "你是 Commercial Director 的 M2.5：只设计商品成交认知路径，不能选片。",
        "这是一次 source-only 实验。你必须忠于 M1 已发现的同一商业故事；不得新发现卖点、价格、福利、库存、其他商品主题。",
        "你输出的 commerce_story_plan 不得含 candidate_id、subtitle_id、时间、片段顺序或候选白名单。",
        "证据卡只是只读商业资产地图。若某种购买信息没有事实支持，要明确 availability=insufficient_evidence，绝不能编造。",
        f"目标时长约 {max(1.0, float(target_duration)):.1f}s。先回答观众需要完成哪些购买认知，再由后续 M2 从完整 hard-safe 池选真实句子。",
        "M1 Commercial Story Brief:",
        json.dumps(strategy_payload, ensure_ascii=False, indent=2),
        "Coverage Profile（认知任务，不是逐词、逐句或逐候选的硬过滤）:",
        json.dumps(template, ensure_ascii=False, indent=2),
        "Read-only Commerce Evidence Cards:",
        json.dumps([card.to_dict() for card in cards], ensure_ascii=False, indent=2),
        "只返回 JSON，格式：",
        json.dumps({
            "opening": {"goal": "", "promise": ""},
            "beats": [{
                "beat_id": "必须逐一使用 Coverage Profile 中的 beat_id",
                "role": "", "need": "", "type": "", "required": True,
                "availability": "available 或 insufficient_evidence",
                "rationale": "只说明该认知任务为何服务当前 M1 故事；不写候选或时间",
            }],
            "coverage_status": "covered 或 insufficient_evidence",
            "missing_required_beat_ids": [],
            "notes": "",
        }, ensure_ascii=False, indent=2),
    ))


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = _text(text)
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Commerce Director 返回无法解析为 JSON")
        parsed = json.loads(cleaned[start:end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("Commerce Director JSON 根节点必须是对象")
    return parsed


def parse_commerce_story_plan(
    raw: Mapping[str, Any],
    *,
    strategy_id: str,
    profile: str = APPAREL_45S_COVERAGE_PROFILE,
) -> CommerceStoryPlan:
    """Reject selection leakage and normalize the fixed coverage contract."""

    forbidden = {"candidate_id", "candidate_ids", "subtitle_id", "subtitle_ids", "start", "end", "timestamp", "timestamps", "order", "sequence"}
    serialized = json.dumps(dict(raw or {}), ensure_ascii=False)
    # Exact JSON-key scan prevents harmless prose such as '时间' from failing,
    # while the output contract cannot silently turn into a selector.
    parsed_keys: set[str] = set()
    def collect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                parsed_keys.add(str(key))
                collect(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                collect(child)
    collect(raw)
    del serialized
    leaked = sorted(key for key in parsed_keys if key.lower() in forbidden)
    if leaked:
        raise ValueError(f"commerce_story_plan must not select candidate/timeline fields: {', '.join(leaked)}")

    templates = {str(item["beat_id"]): item for item in _profile_templates(profile)}
    raw_beats = raw.get("beats") or ()
    if isinstance(raw_beats, Mapping):
        raw_beats = (raw_beats,)
    by_id = {str(item.get("beat_id") or "").strip(): item for item in raw_beats if isinstance(item, Mapping)}
    unknown = sorted(set(by_id) - set(templates) - {""})
    if unknown:
        raise ValueError(f"unknown commerce beat ids: {', '.join(unknown)}")
    beats: list[CommerceBeat] = []
    missing_required: list[str] = []
    for beat_id, template in templates.items():
        item = by_id.get(beat_id, {})
        availability = _text(item.get("availability")).lower()
        if availability not in {"available", "insufficient_evidence"}:
            availability = "insufficient_evidence"
        required = bool(template["required"])
        if required and availability != "available":
            missing_required.append(beat_id)
        beats.append(CommerceBeat(
            beat_id=beat_id,
            role=_text(item.get("role")) or str(template["role"]),
            need=_text(item.get("need")) or str(template["need"]),
            type=_text(item.get("type")) or str(template["type"]),
            required=required,
            availability=availability,
            rationale=_text(item.get("rationale")),
        ))
    opening = raw.get("opening") if isinstance(raw.get("opening"), Mapping) else {}
    status = "covered" if not missing_required else "insufficient_evidence"
    return CommerceStoryPlan(
        profile=profile,
        strategy_id=strategy_id,
        opening_goal=_text(opening.get("goal")),
        opening_promise=_text(opening.get("promise")),
        beats=tuple(beats),
        coverage_status=status,
        missing_required_beat_ids=tuple(missing_required),
        notes=_text(raw.get("notes")),
    )


def plan_commerce_story_llm(
    *,
    strategy: Any,
    cards: Sequence[CommerceEvidenceCard],
    target_duration: float,
    api_key: str,
    base_url: str,
    model: str,
    profile: str = APPAREL_45S_COVERAGE_PROFILE,
    raw_response_hook: Callable[[str], None] | None = None,
) -> CommerceStoryPlan:
    prompt = build_commerce_director_prompt(
        strategy=strategy, cards=cards, target_duration=target_duration, profile=profile,
    )
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是商品成交路径导演。先设计购买认知覆盖，绝不选片、改写字幕或创造事实。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "top_p": 0.8,
        "max_tokens": 2200,
        "response_format": {"type": "json_object"},
    }
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body["thinking"] = {"type": "disabled"}
    request = urllib.request.Request(
        ai_chat_completions_url(base_url), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180, context=create_ssl_context()) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        record_ai_call(module="commerce_director", stage="M2_5_commerce_director", model=model, request_payload=body, success=False, error_type=f"http_{error.code}")
        raise RuntimeError(f"Commerce Director HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(module="commerce_director", stage="M2_5_commerce_director", model=model, request_payload=body, success=False, error_type=type(error).__name__)
        raise RuntimeError(f"Commerce Director 网络错误: {error}") from error
    record_ai_call(module="commerce_director", stage="M2_5_commerce_director", model=model, request_payload=body, response_payload=result, success=True)
    content = _text(result.get("choices", [{}])[0].get("message", {}).get("content"))
    if not content:
        raise RuntimeError("Commerce Director 返回空内容")
    if raw_response_hook:
        raw_response_hook(content)
    return parse_commerce_story_plan(_extract_json(content), strategy_id=_text(getattr(strategy, "strategy_id", "")), profile=profile)


def audit_commerce_story_coverage(plan: Any, commerce_plan: CommerceStoryPlan) -> dict[str, Any]:
    """Deterministically compare declared M2 beat mappings with M2.5 needs.

    It reports only.  In Director Experiment v1 it must never repair M2,
    filter evidence, or convert a short source into an invalid technical run.
    """

    mapped: dict[str, list[str]] = {beat.beat_id: [] for beat in commerce_plan.beats}
    for chapter in tuple(getattr(plan, "beats", ()) or ()):
        beat_id = _text(getattr(chapter, "commerce_beat_id", ""))
        chapter_id = _text(getattr(chapter, "chapter_id", ""))
        if beat_id in mapped and chapter_id:
            mapped[beat_id].append(chapter_id)
    required_available = [beat.beat_id for beat in commerce_plan.beats if beat.required and beat.availability == "available"]
    missing_materialization = [beat_id for beat_id in required_available if not mapped.get(beat_id)]
    return {
        "version": COMMERCE_DIRECTOR_VERSION,
        "coverage_contract_status": commerce_plan.coverage_status,
        "required_available_beat_ids": required_available,
        "required_unavailable_beat_ids": list(commerce_plan.missing_required_beat_ids),
        "chapter_mapping": mapped,
        "missing_materialization_beat_ids": missing_materialization,
        "status": "complete" if not missing_materialization else "incomplete",
        "passed": not missing_materialization,
        "enforcement": "experiment_report_only_no_local_repair_or_candidate_filter",
    }
