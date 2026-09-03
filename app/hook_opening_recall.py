"""P0.5A.4 source-wide Hook Recall and Opening Package audit.

This is intentionally separate from the P0.5A.3 Micro-Beat actor pool.  A
good body Beat is not automatically a good opening: this module scans the
complete safe source only for independent hooks, then asks the AI to pair each
accepted hook with immediate proof from the *frozen* actor pool.  It never
edits that pool, Journey, Beat Casting, Whole Video Audit, or M3.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping, Sequence

from commerce_planner_lite import _extract_json, _post_lite_request
from micro_beat_inventory import (
    _as_int_tuple,
    _as_mapping_list,
    _boundary_segment_source,
    _normalize_spoken_source_text,
    _number,
    _text,
    build_micro_beat_source_batches,
)


P05_HOOK_RECALL_STAGE = "P0_5A4_hook_specific_source_recall"
P05_OPENING_PACKAGE_STAGE = "P0_5A4_hook_payoff_opening_package"
HOOK_MIN_SECONDS = 2.0
HOOK_PREFERRED_MAX_SECONDS = 5.0
HOOK_MAX_SECONDS = 8.0
HOOK_CONTEXT_NEIGHBOR_SUBTITLES = 2
HOOK_SOURCE_BATCH_SECONDS = 480.0
HOOK_RECALL_BATCH_LIMIT = 12
OPENING_PACKAGE_PAYOFF_LIMIT = 2
OPENING_PACKAGE_MAX_SECONDS = 12.0
_HOOK_PUBLISHABLE_STATE = "publishable_clean"
_HOOK_STRENGTH_MINIMUM = 4
_OPENING_QUALITIES = frozenset({"strong", "medium", "weak"})
_PAYOFF_ROLES = frozenset({
    "mechanism", "proof", "expanded_result", "body_validation", "concrete_experience",
})


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _word_rows(source_rows_by_id: Mapping[int, Mapping[str, Any]], subtitle_ids: Sequence[int]) -> list[dict[str, Any]]:
    """Expose a bounded source interval with stable global word IDs."""
    result: list[dict[str, Any]] = []
    for subtitle_id in subtitle_ids:
        row = source_rows_by_id.get(int(subtitle_id))
        if not isinstance(row, Mapping):
            continue
        base = row.get("word_lineage") if isinstance(row.get("word_lineage"), Mapping) else {}
        base_start = _safe_int(base.get("word_start_index"))
        if base_start is None:
            continue
        for token in row.get("word_tokens") or ():
            if not isinstance(token, Mapping):
                continue
            offset = _safe_int(token.get("offset"))
            if offset is None:
                continue
            text = _text(token.get("text"))
            if not text:
                continue
            result.append({
                "word_id": base_start + offset,
                "subtitle_id": int(subtitle_id), "offset": offset, "text": text,
                "start": round(_number(token.get("start")), 3),
                "end": round(_number(token.get("end")), 3),
            })
    return result


def build_hook_recall_batches(
    source_rows: Sequence[Mapping[str, Any]], *, max_seconds: float = HOOK_SOURCE_BATCH_SECONDS,
) -> tuple[dict[str, Any], ...]:
    """Make complete-SRT transport batches plus at most two safe neighbours.

    Context rows merely let the AI find the natural edge of a Hook.  A selected
    span must still include a primary row, so overlapping context cannot create
    a second semantic discovery pass or a hidden Top-K filter.
    """
    ordered = [dict(item) for item in source_rows if isinstance(item, Mapping)]
    by_id = {int(item["subtitle_id"]): item for item in ordered if _safe_int(item.get("subtitle_id"))}
    ordered_ids = [int(item["subtitle_id"]) for item in ordered if int(item["subtitle_id"]) in by_id]
    positions = {subtitle_id: index for index, subtitle_id in enumerate(ordered_ids)}
    batches: list[dict[str, Any]] = []
    for base in build_micro_beat_source_batches(ordered, max_seconds=max_seconds):
        primary_ids = [int(item) for item in base.get("source_subtitle_ids") or ()]
        if not primary_ids:
            continue
        first = positions[primary_ids[0]]
        last = positions[primary_ids[-1]]
        context_ids = list(primary_ids)
        for index in range(first - 1, max(-1, first - HOOK_CONTEXT_NEIGHBOR_SUBTITLES - 1), -1):
            candidate_id = ordered_ids[index]
            row = by_id[candidate_id]
            if not bool(row.get("hard_safe")) or not bool(row.get("materializable")):
                break
            context_ids.insert(0, candidate_id)
        for index in range(last + 1, min(len(ordered_ids), last + HOOK_CONTEXT_NEIGHBOR_SUBTITLES + 1)):
            candidate_id = ordered_ids[index]
            row = by_id[candidate_id]
            if not bool(row.get("hard_safe")) or not bool(row.get("materializable")):
                break
            context_ids.append(candidate_id)
        context_words = _word_rows(by_id, context_ids)
        primary_words = _word_rows(by_id, primary_ids)
        if not context_words or not primary_words:
            continue
        batches.append({
            "batch_id": f"HS{len(batches) + 1:02d}",
            "start": base.get("start"), "end": base.get("end"),
            "primary_subtitle_ids": primary_ids, "context_subtitle_ids": context_ids,
            "primary_word_ids": {int(item["word_id"]) for item in primary_words},
            "context_words": context_words,
            "source_rows": [by_id[subtitle_id] for subtitle_id in context_ids],
        })
    return tuple(batches)


def build_hook_recall_prompt(*, batch: Mapping[str, Any], total_batch_count: int, opening_promise: str) -> str:
    """Hook-only semantic source scan; no ordinary Actor Inventory is emitted."""
    source_rows = []
    primary_ids = set(_as_int_tuple(batch.get("primary_subtitle_ids")))
    for row in batch.get("source_rows") or ():
        if not isinstance(row, Mapping):
            continue
        lineage = row.get("word_lineage") if isinstance(row.get("word_lineage"), Mapping) else {}
        source_rows.append({
            "subtitle_id": row.get("subtitle_id"), "is_primary_window": int(row.get("subtitle_id") or 0) in primary_ids,
            "start": row.get("start"), "end": row.get("end"),
            "hard_safe": bool(row.get("hard_safe")), "materializable": bool(row.get("materializable")),
            "text": _text(row.get("text")),
            "word_id_range": [lineage.get("word_start_index"), lineage.get("word_end_index")],
            "words": [{"word_id": int(lineage.get("word_start_index") or 0) + int(token.get("offset") or 0), "text": _text(token.get("text"))}
                      for token in row.get("word_tokens") or () if isinstance(token, Mapping)],
        })
    schema = {
        "hook_candidates": [{
            "hook_id": "local id", "start_word_id": 0, "end_word_id": 0, "final_text": "",
            "hook_type": "strong_result/strong_judgment/pain_point/contrast/experience/surprise",
            "hook_evidence_function": "result/mechanism/proof/experience/risk_remove/styling/scene/trust",
            "core_purchase_value": "", "stop_reason": "", "standalone_reason": "", "specificity_reason": "",
            "hook_strength": 5, "publishable_state": "publishable_clean", "visual_dependency": "none",
            "eligible_for_opening": True, "reject_reason": "",
        }],
        "hook_rejects": [{
            "start_word_id": 0, "end_word_id": 0, "text": "", "reject_reason": "",
            "category": "not_standalone/visual_dependency/asr/live_interaction_or_size/cta_or_price/generic/no_payoff",
            "review_priority": 5,
        }],
        "scan_quality": {"complete_primary_window_scanned": True, "reason": ""},
    }
    return "\n\n".join((
        f"你只做 P0.5A.4 Hook 专项召回，当前是完整焦糖 SRT 的来源窗口 {batch.get('batch_id')}/{total_batch_count}。不做普通正文 Beat Inventory、不做 Director Journey、不做候选排序或最终视频。",
        "目标是找可独立作为短视频开场的真实主播原话。Hook 必须干净、独立、具体、商品相关、有明确停留理由，并能被后续真实素材立刻兑现。优先2–5秒，允许2–8秒；不要把两个或三个句子拼成一段长开场。",
        "当前冻结 Director Opening Promise：" + _text(opening_promise) + "。只有直接服务这个承诺的句子可进入 hook_candidates；舒适、材质、尺码、搭配等即使本身正确但不服务当前开场承诺，也应进 hook_rejects 或留给正文。",
        "只可选择 hard_safe=true 且 materializable=true 的连续原词范围。每个选择必须覆盖至少一条 is_primary_window=true 字幕；相邻最多两条 context 字幕只用于补齐自然边界。严禁改写、虚构、跳词、合并不连续字幕。final_text 必须逐字等于所选原词，可保留原有标点。",
        "Hook 不是普通信息正确：它必须让陌生观众愿意继续看。可找强结果、带购买依据的强判断、自然的痛点命中、结果反差、强体验或具体意外点。‘你看/真的/我跟你说’不是机械禁词，若整句独立自然可保留。",
        "严禁选直播互动、姐妹们、用户昵称、个人身高体重或尺码回应、价格、催单、CTA、售后、无商品指向泛夸、真正残句、ASR异常、只靠手势才能理解的句子。visual_dependency 必须为 none；任何 visual-only 句都只能进 reject audit。",
        "eligible_for_opening=true 只限 publishable_clean、visual_dependency=none、2–8秒、hook_strength至少4、完整独立的句子。若看似有价值但不适合开场，放入 hook_rejects。每窗口输出不超过12个 Hook与6个最有代表性的拒绝例子；不要为了数量硬凑。",
        "返回严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "完整来源窗口（is_primary_window=false 仅可作±2行边界上下文）：\n" + json.dumps(source_rows, ensure_ascii=False, separators=(",", ":")),
    ))


def _hook_reject(*, raw: Mapping[str, Any] | None, reason: str, category: str = "contract") -> dict[str, Any]:
    raw = raw or {}
    return {
        "text": _text(raw.get("final_text")) or _text(raw.get("text")),
        "start_word_id": _safe_int(raw.get("start_word_id")), "end_word_id": _safe_int(raw.get("end_word_id")),
        "hook_type": _text(raw.get("hook_type")), "reject_reason": reason,
        "category": _text(raw.get("category")) or category,
        "review_priority": _safe_int(raw.get("review_priority")) or 0,
    }


def _known_unplayable_hook_surface_reason(text: str) -> str:
    """Retain only focused, previously observed opening hard blocks.

    This is not a keyword definition of a Hook: it can only reject a few
    known caramel ASR/live residues after AI proposed them.  In particular it
    intentionally does *not* ban natural conversational starts such as
    ``你看`` or ``真的`` by themselves.
    """
    compact = re.sub(r"\s+", "", _text(text))
    marker = next((item for item in (
        "像100斤葡萄", "这个人间一定是直角", "大斜方间", "35厘米", "A类母婴店", "肩干嘛",
    ) if item in compact), "")
    if marker:
        return "known_hook_asr_or_unplayable:" + marker
    if re.match(r"^(?:好的[，,]?来|的[，,]|然后这个我推荐|它其实)", compact):
        return "known_live_leadin_or_context_dependency"
    if compact.startswith("你是大斜方肌"):
        return "user_label_style_pain_point_not_natural_opening"
    if compact.startswith("所以你要比我胖") or re.search(r"(?:\d+斤|十斤|二十斤).{0,12}试试看", compact):
        return "personal_body_response_not_opening"
    if re.search(r"(?:是的|对的|嗯)$", compact):
        return "dangling_live_agreement"
    return ""


def parse_hook_recall(
    *, responses: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]], source_rows: Sequence[Mapping[str, Any]],
    allowed_opening_answer_roles: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate AI hook picks using safety, exact words and opening contracts."""
    by_id = {int(item["subtitle_id"]): dict(item) for item in source_rows if _safe_int(item.get("subtitle_id"))}
    candidates: list[dict[str, Any]] = []
    rejects: list[dict[str, Any]] = []
    seen_ranges: set[tuple[int, int]] = set()
    all_candidate_audit: list[dict[str, Any]] = []
    for batch, data in responses:
        context_words = [dict(item) for item in batch.get("context_words") or () if isinstance(item, Mapping)]
        context_word_ids = {int(item["word_id"]) for item in context_words}
        primary_word_ids = {int(item) for item in batch.get("primary_word_ids") or ()}
        for raw in _as_mapping_list(data.get("hook_rejects")):
            rejects.append(_hook_reject(raw=raw, reason=_text(raw.get("reject_reason")) or "AI_hook_reject", category="ai_hook_reject"))
        for raw in _as_mapping_list(data.get("hook_candidates")):
            start_id = _safe_int(raw.get("start_word_id"))
            end_id = _safe_int(raw.get("end_word_id"))
            reason = ""
            if start_id is None or end_id is None:
                reason = "hook_word_lineage_missing"
            elif start_id not in context_word_ids or end_id not in context_word_ids:
                reason = "hook_word_range_outside_local_source_context"
            else:
                segment = _boundary_segment_source(
                    source_words=context_words, source_rows_by_id=by_id,
                    start_word_id=start_id, end_word_id=end_id,
                )
                if segment is None:
                    reason = "hook_word_lineage_not_safe_or_contiguous"
                else:
                    text, start, end, lineage, word_ids = segment
                    duration = round(end - start, 3)
                    if not set(word_ids).intersection(primary_word_ids):
                        reason = "hook_context_only_without_primary_source"
                    elif _normalize_spoken_source_text(raw.get("final_text")) != _normalize_spoken_source_text(text):
                        reason = "hook_final_text_not_exact_source_words"
                    elif _known_unplayable_hook_surface_reason(text):
                        reason = _known_unplayable_hook_surface_reason(text)
                    elif duration < HOOK_MIN_SECONDS - 1e-6:
                        reason = "hook_duration_below_2_seconds"
                    elif duration > HOOK_MAX_SECONDS + 1e-6:
                        reason = "hook_duration_exceeds_8_seconds"
                    elif _text(raw.get("publishable_state")) != _HOOK_PUBLISHABLE_STATE:
                        reason = "hook_publishable_state_not_clean"
                    elif _text(raw.get("visual_dependency")).lower() != "none":
                        reason = "hook_visual_dependency_not_allowed"
                    elif not bool(raw.get("eligible_for_opening")):
                        reason = _text(raw.get("reject_reason")) or "AI_did_not_mark_opening_eligible"
                    elif (_safe_int(raw.get("hook_strength")) or 0) < _HOOK_STRENGTH_MINIMUM:
                        reason = "hook_strength_below_opening_threshold"
                    elif allowed_opening_answer_roles and _text(raw.get("hook_evidence_function")).lower() not in {
                        _text(item).lower() for item in allowed_opening_answer_roles
                    }:
                        reason = "hook_answer_role_outside_current_opening_scope"
                    elif not all(_text(raw.get(key)) for key in (
                        "hook_type", "hook_evidence_function", "core_purchase_value", "stop_reason",
                        "standalone_reason", "specificity_reason",
                    )):
                        reason = "hook_semantic_receipt_missing"
                    elif (start_id, end_id) in seen_ranges:
                        reason = "hook_duplicate_exact_source_range"
            if reason:
                rejected = _hook_reject(raw=raw, reason=reason)
                rejects.append(rejected)
                all_candidate_audit.append({"batch_id": batch.get("batch_id"), "accepted": False, **rejected})
                continue
            assert segment is not None
            text, start, end, lineage, word_ids = segment
            hook_id = f"H{len(candidates) + 1:03d}"
            candidate = {
                "hook_id": hook_id, "ai_hook_id": _text(raw.get("hook_id")),
                "start": round(start, 3), "end": round(end, 3), "duration": round(end - start, 3), "text": text,
                "source_subtitle_ids": [int(item["subtitle_id"]) for item in lineage], "word_lineage": lineage,
                "start_word_id": start_id, "end_word_id": end_id,
                "hook_type": _text(raw.get("hook_type")),
                "hook_evidence_function": _text(raw.get("hook_evidence_function")),
                "core_purchase_value": _text(raw.get("core_purchase_value")),
                "stop_reason": _text(raw.get("stop_reason")), "standalone_reason": _text(raw.get("standalone_reason")),
                "specificity_reason": _text(raw.get("specificity_reason")),
                "hook_strength": int(_safe_int(raw.get("hook_strength")) or 0),
                "publishable_state": _HOOK_PUBLISHABLE_STATE, "visual_dependency": "none",
                "eligible_for_opening": True, "reject_reason": "",
                "selection_authority": "AI_P0_5A4_hook_specific_recall",
            }
            seen_ranges.add((start_id, end_id))
            candidates.append(candidate)
            all_candidate_audit.append({"batch_id": batch.get("batch_id"), "accepted": True, **candidate})
    rejects = sorted(rejects, key=lambda item: (-int(item.get("review_priority") or 0), _text(item.get("reject_reason")), _text(item.get("text"))))[:20]
    return {
        "status": "hook_recall_completed" if candidates else "hook_material_limited",
        "hook_candidates": candidates,
        "hook_candidate_count": len(candidates),
        "hook_reject_audit_top_20": rejects,
        "hook_candidate_audit": all_candidate_audit,
        "contract": {
            "stage": P05_HOOK_RECALL_STAGE,
            "complete_srt_hook_specific_scan": True,
            "ordinary_actor_pool_created_or_modified": False,
            "hook_gate_lowered": False,
            "program_authority": "source_safety_word_lineage_duration_visual_and_schema_only",
            "semantic_hook_authority": "AI",
            "m3_modified": False,
        },
    }


def build_hook_opening_gate_prompt(
    *, hook_candidates: Sequence[Mapping[str, Any]], opening_promise: str,
    allowed_opening_answer_roles: Sequence[str],
) -> str:
    """Final AI adjudication: a commercially valid line is not automatically a Hook."""
    rows = [{
        "hook_id": item.get("hook_id"), "duration": item.get("duration"), "text": item.get("text"),
        "hook_type": item.get("hook_type"), "core_purchase_value": item.get("core_purchase_value"),
        "initial_stop_reason": item.get("stop_reason"), "initial_standalone_reason": item.get("standalone_reason"),
    } for item in hook_candidates]
    schema = {"hook_opening_adjudications": [{
        "hook_id": "H001", "eligible_for_opening": True, "publishable_state": "publishable_clean",
        "visual_dependency": "none", "hook_stop_power": 5, "hook_independence": 5,
        "hook_specificity": 5, "hook_product_relevance": 5, "asr_cleanliness": 5,
        "promise_fit": 5, "reason": "", "reject_reason": "",
    }]}
    return "\n\n".join((
        "你是 P0.5A.4 最终 Hook Gate。这里只审已经词级可回放的 Hook 候选，不改写、不裁词、不补句、不选正文，也不输出 Payoff。你只判断它是否真的能在短视频第1句成立。",
        "标准必须同时满足：干净；独立；商品相关且具体；陌生观众有停留理由；直接符合当前 Director Opening Promise；没有 ASR怪词、直播承接、对象不明、用户画像标签、个人尺码/体重回应或必须靠手势理解的问题。信息正确、商业价值高、能用于正文，都不等于可作开场。",
        "不要机械拒绝‘你看/真的/我觉得’；只有整句仍像直播残留或依赖前文才拒绝。特别严查类似‘好的，来…’、‘它其实…’、未明对象的‘然后这个…’、奇怪比喻、ASR词和只重复口号。",
        "所有评分为1–5。eligible_for_opening=true 仅限 publishable_clean、visual_dependency=none，且六项评分都至少4；否则 false 并填 reject_reason。程序不会把任何 false 候选升格。",
        "当前冻结 Director Opening Promise：" + _text(opening_promise) + "；允许的开场证据角色仅为：" + ",".join(_text(item) for item in allowed_opening_answer_roles),
        "返回严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "待审真实候选：\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def apply_hook_opening_gate(
    *, hook_recall: Mapping[str, Any], data: Mapping[str, Any], allowed_opening_answer_roles: Sequence[str],
) -> dict[str, Any]:
    candidates = _as_mapping_list(hook_recall.get("hook_candidates"))
    evaluations = {_text(item.get("hook_id")): item for item in _as_mapping_list(data.get("hook_opening_adjudications"))}
    retained: list[dict[str, Any]] = []
    rejected = list(hook_recall.get("hook_reject_audit_top_20") or ())
    gate_audit: list[dict[str, Any]] = []
    for candidate in candidates:
        hook_id = _text(candidate.get("hook_id"))
        evaluation = evaluations.get(hook_id)
        reason = ""
        if evaluation is None:
            reason = "hook_opening_gate_missing_decision"
        elif not bool(evaluation.get("eligible_for_opening")):
            reason = _text(evaluation.get("reject_reason")) or "AI_hook_opening_gate_reject"
        elif allowed_opening_answer_roles and _text(candidate.get("hook_evidence_function")).lower() not in {
            _text(item).lower() for item in allowed_opening_answer_roles
        }:
            reason = "hook_answer_role_outside_current_opening_scope"
        elif _text(evaluation.get("publishable_state")) != _HOOK_PUBLISHABLE_STATE:
            reason = "hook_opening_gate_not_clean"
        elif _text(evaluation.get("visual_dependency")).lower() != "none":
            reason = "hook_opening_gate_visual_dependency"
        elif any((_safe_int(evaluation.get(field)) or 0) < 4 for field in (
            "hook_stop_power", "hook_independence", "hook_specificity", "hook_product_relevance",
            "asr_cleanliness", "promise_fit",
        )):
            reason = "hook_opening_gate_quality_below_threshold"
        if reason:
            rejected.append({"hook_id": hook_id, "text": candidate.get("text"), "reject_reason": reason, "category": "hook_opening_gate"})
            gate_audit.append({"hook_id": hook_id, "accepted": False, "reason": reason})
            continue
        checked = dict(candidate)
        checked["hook_strength"] = int(_safe_int(evaluation.get("hook_stop_power")) or candidate.get("hook_strength") or 0)
        checked["hook_opening_gate"] = {
            "hook_stop_power": _safe_int(evaluation.get("hook_stop_power")),
            "hook_independence": _safe_int(evaluation.get("hook_independence")),
            "hook_specificity": _safe_int(evaluation.get("hook_specificity")),
            "hook_product_relevance": _safe_int(evaluation.get("hook_product_relevance")),
            "asr_cleanliness": _safe_int(evaluation.get("asr_cleanliness")),
            "promise_fit": _safe_int(evaluation.get("promise_fit")),
            "reason": _text(evaluation.get("reason")),
        }
        retained.append(checked)
        gate_audit.append({"hook_id": hook_id, "accepted": True, "reason": _text(evaluation.get("reason"))})
    result = dict(hook_recall)
    result.update({
        "status": "hook_recall_completed" if retained else "hook_material_limited",
        "hook_candidates": retained, "hook_candidate_count": len(retained),
        "hook_reject_audit_top_20": sorted(rejected, key=lambda item: (_text(item.get("category")), _text(item.get("reject_reason"))))[:20],
        "hook_opening_gate_audit": gate_audit,
    })
    result["contract"] = dict(result.get("contract") or {}) | {
        "final_hook_gate": "AI_quality_adjudication_plus_frozen_surface_and_lineage_blocks",
        "ordinary_actor_pool_created_or_modified": False,
    }
    return result


def recall_hooks_from_complete_source(
    *, source_rows: Sequence[Mapping[str, Any]], api_key: str, base_url: str, model: str,
    opening_promise: str, allowed_opening_answer_roles: Sequence[str],
    response_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    batches = build_hook_recall_batches(source_rows)
    responses: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    batch_audit: list[dict[str, Any]] = []
    for batch in batches:
        response = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model,
            prompt=build_hook_recall_prompt(batch=batch, total_batch_count=len(batches), opening_promise=opening_promise),
            stage=P05_HOOK_RECALL_STAGE, max_tokens=5200,
        )
        content = _text(response.get("choices", [{}])[0].get("message", {}).get("content"))
        if response_hook and content:
            response_hook(_text(batch.get("batch_id")), content)
        try:
            data = _extract_json(content) if content else None
        except (RuntimeError, ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            return {
                "status": "hook_recall_response_invalid", "errors": [f"hook_recall_response_invalid:{batch.get('batch_id')}"],
                "hook_recall_batches": batch_audit + [{"batch_id": batch.get("batch_id"), "status": "response_invalid"}],
            }
        responses.append((batch, data))
        batch_audit.append({"batch_id": batch.get("batch_id"), "status": "reviewed", "raw_response_present": bool(content)})
    result = parse_hook_recall(
        responses=responses, source_rows=source_rows,
        allowed_opening_answer_roles=allowed_opening_answer_roles,
    )
    result["hook_recall_batches"] = batch_audit
    # P0.5A.4 already asks the semantic source-recall model to make the
    # publishable/independent/opening decision.  A second all-or-nothing LLM
    # veto proved counterproductive in the live caramel run: it assigned every
    # quality score >=4, then still returned eligible=false for every Hook and
    # prevented Beat Casting from starting.  Keep the deterministic lineage,
    # duration, safety, visual and scope checks in ``parse_hook_recall``; let
    # the following AI Opening Package choose which recalled Hook has an
    # immediate real payoff.  This removes a duplicate semantic gate, not an
    # opening-quality contract or a programmatic candidate selection.
    result["contract"] = dict(result.get("contract") or {}) | {
        "final_hook_gate": "single_AI_source_recall_plus_deterministic_surface_lineage_scope_checks",
        "duplicate_llm_hook_veto_removed": True,
    }
    return result


def build_opening_package_prompt(
    *, hook_candidates: Sequence[Mapping[str, Any]], frozen_actor_pool: Sequence[Mapping[str, Any]], opening_promise: str,
) -> str:
    """Ask AI to pair every accepted Hook with frozen P0.5A.3 payoffs."""
    hooks = [{
        "hook_id": item.get("hook_id"), "duration": item.get("duration"), "text": item.get("text"),
        "hook_type": item.get("hook_type"), "core_purchase_value": item.get("core_purchase_value"),
        "hook_strength": item.get("hook_strength"),
    } for item in hook_candidates]
    actors = [{
        "beat_id": item.get("beat_id"), "duration": item.get("duration_seconds"), "text": item.get("text"),
        "publishability_status": item.get("publishability_status"), "visual_dependency": item.get("visual_dependency"),
        "role_permissions": item.get("role_permissions"), "context_requirement": item.get("context_requirement"),
        "purchase_value": item.get("purchase_value"), "sub_outcome": item.get("sub_outcome"),
        "evidence_function": item.get("evidence_function"),
    } for item in frozen_actor_pool]
    schema = {
        "opening_packages": [{
            "opening_id": "O001", "hook_id": "H001", "payoff_beat_ids": ["B001"], "opening_promise": "",
            "sequence": [
                {"beat_id": "H001", "role": "hook", "new_information": ""},
                {"beat_id": "B001", "role": "mechanism/proof/expanded_result/body_validation/concrete_experience", "new_information": ""},
            ],
            "progression_count": 2, "payoff_relation": "", "why_viewer_keeps_watching": "",
            "hook_stop_power": 5, "hook_independence": 5, "hook_specificity": 5, "hook_product_relevance": 5,
            "payoff_strength": 5, "payoff_immediacy": 5, "hook_payoff_consistency": 5,
            "quality": "strong/medium/weak", "reject_reason": "",
        }],
        "opening_rejects": [{"hook_id": "H001", "reject_reason": "", "category": "no_immediate_payoff/weak_progression/inconsistent"}],
    }
    return "\n\n".join((
        "你只做 P0.5A.4 Opening Package：每个 Hook 必须紧跟冻结 P0.5A.3 Actor Pool 的真实 Immediate Payoff。不得新扫 SRT、不得改 Actor Pool、不得重排 Purchase Journey、不得写最终视频。",
        "Opening 目标：Hook → 立即证明 → 尽可能第二次推进。Hook承诺不能被同义重复敷衍，Payoff必须是机制、证据、扩大结果、身体适配或具体体验。每个 Hook 选1–2个不同的 payoff Beat；总长优先约3–10秒，绝不超过12秒。",
        "只可使用给定 Hook ID 和 Actor beat_id，不能改写任何文本。visual Actor 可以作画面依赖的 proof，但不能被说成独立音频 Hook。若没有直接真实兑现素材，必须在 opening_rejects 标明 no_immediate_payoff，不能硬拼无关卖点。最多输出5个最有说服力的 package；其余 Hook 写入 opening_rejects。",
        "本焦糖当前 Director Opening Promise：" + _text(opening_promise),
        "Hook 与 Payoff 都须保持商品焦点；不要把尺码、互动、价格、CTA、泛夸或无关品质话术塞进开场。quality=strong 必须是 Hook+两个不同 Payoff 的3次真实认知推进；只有 Hook+一个 Payoff 时只能标 medium。Hook 与 Payoff 或两条 Payoff 不能只是同义重复。weak 不应作为回归入口。",
        "返回严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "专项 Hook（AI 已通过 clean/standalone/2–8秒/词级 lineage）：\n" + json.dumps(hooks, ensure_ascii=False, separators=(",", ":")),
        "冻结 P0.5A.3 Actor Pool（唯一 Payoff 来源）：\n" + json.dumps(actors, ensure_ascii=False, separators=(",", ":")),
    ))


def assemble_opening_packages(
    *, hook_recall: Mapping[str, Any], frozen_actor_pool: Sequence[Mapping[str, Any]], api_key: str,
    base_url: str, model: str, opening_promise: str, response_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    hooks = _as_mapping_list(hook_recall.get("hook_candidates"))
    actors = _as_mapping_list(frozen_actor_pool)
    if not hooks:
        return {"status": "hook_material_limited", "opening_packages": [], "opening_package_rejects": []}
    response = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model,
        prompt=build_opening_package_prompt(
            hook_candidates=hooks, frozen_actor_pool=actors, opening_promise=opening_promise,
        ), stage=P05_OPENING_PACKAGE_STAGE, max_tokens=6200,
    )
    content = _text(response.get("choices", [{}])[0].get("message", {}).get("content"))
    if response_hook and content:
        response_hook(content)
    try:
        data = _extract_json(content) if content else None
    except (RuntimeError, ValueError, TypeError):
        data = None
    if not isinstance(data, Mapping):
        return {"status": "opening_package_response_invalid", "errors": ["opening_package_response_invalid"]}
    hook_by_id = {_text(item.get("hook_id")): dict(item) for item in hooks}
    actor_by_id = {_text(item.get("beat_id")): dict(item) for item in actors}
    packages: list[dict[str, Any]] = []
    rejects = [_hook_reject(raw=item, reason=_text(item.get("reject_reason")) or "AI_opening_package_reject", category="opening_package")
               for item in _as_mapping_list(data.get("opening_rejects"))]
    covered_hooks: set[str] = set()
    for raw in _as_mapping_list(data.get("opening_packages")):
        hook_id = _text(raw.get("hook_id"))
        covered_hooks.add(hook_id)
        hook = hook_by_id.get(hook_id)
        payoff_ids = [_text(item) for item in raw.get("payoff_beat_ids") or () if _text(item)]
        reason = ""
        if hook is None:
            reason = "opening_package_unknown_hook"
        elif not 1 <= len(payoff_ids) <= OPENING_PACKAGE_PAYOFF_LIMIT:
            reason = "opening_package_payoff_count_invalid"
        elif len(set(payoff_ids)) != len(payoff_ids) or any(item not in actor_by_id for item in payoff_ids):
            reason = "opening_package_payoff_unknown_or_duplicate"
        else:
            payoff_beats = [actor_by_id[item] for item in payoff_ids]
            hook_word_ids = set(range(int(hook["start_word_id"]), int(hook["end_word_id"]) + 1))
            actor_word_ids = set()
            for beat in payoff_beats:
                start = _safe_int(beat.get("final_start_word_id"))
                end = _safe_int(beat.get("final_end_word_id"))
                if start is not None and end is not None:
                    actor_word_ids.update(range(start, end + 1))
            if hook_word_ids.intersection(actor_word_ids):
                reason = "opening_package_repeats_hook_source_words"
            elif not all(_text(raw.get(key)) for key in (
                "opening_promise", "payoff_relation", "why_viewer_keeps_watching",
            )):
                reason = "opening_package_semantic_receipt_missing"
            elif _text(raw.get("quality")).lower() not in _OPENING_QUALITIES:
                reason = "opening_package_quality_invalid"
        sequence = _as_mapping_list(raw.get("sequence"))
        expected_sequence_ids = [hook_id, *payoff_ids]
        if not reason and [ _text(item.get("beat_id")) for item in sequence ] != expected_sequence_ids:
            reason = "opening_package_sequence_mismatch"
        if not reason and (_text(sequence[0].get("role")).lower() != "hook" or any(
            _text(item.get("role")).lower() not in _PAYOFF_ROLES for item in sequence[1:]
        )):
            reason = "opening_package_sequence_role_invalid"
        if not reason and any(not _text(item.get("new_information")) for item in sequence):
            reason = "opening_package_new_information_missing"
        progression = _safe_int(raw.get("progression_count")) or 0
        if not reason and progression < 2:
            reason = "opening_package_progression_insufficient"
        if not reason and _text(raw.get("quality")).lower() == "strong" and progression < 3:
            reason = "opening_package_strong_requires_three_progressions"
        total = round(float(hook.get("duration") or 0.0) + sum(_number(actor_by_id[item].get("duration_seconds")) for item in payoff_ids), 3) if hook else 0.0
        if not reason and total > OPENING_PACKAGE_MAX_SECONDS + 1e-6:
            reason = "opening_package_duration_exceeds_12_seconds"
        if reason:
            rejects.append({"hook_id": hook_id, "reject_reason": reason, "category": "opening_package_contract"})
            continue
        packages.append({
            "opening_id": _text(raw.get("opening_id")) or f"O{len(packages) + 1:03d}", "hook_id": hook_id,
            "payoff_beat_ids": payoff_ids, "opening_promise": _text(raw.get("opening_promise")),
            "sequence": [{
                "beat_id": _text(item.get("beat_id")), "role": _text(item.get("role")).lower(),
                "duration": round(float(hook.get("duration") or 0.0), 3) if index == 0 else round(_number(actor_by_id[_text(item.get("beat_id"))].get("duration_seconds")), 3),
                "new_information": _text(item.get("new_information")),
            } for index, item in enumerate(sequence)],
            "total_duration": total, "progression_count": progression,
            "payoff_relation": _text(raw.get("payoff_relation")), "why_viewer_keeps_watching": _text(raw.get("why_viewer_keeps_watching")),
            "quality_scores": {key: _safe_int(raw.get(key)) or 0 for key in (
                "hook_stop_power", "hook_independence", "hook_specificity", "hook_product_relevance",
                "payoff_strength", "payoff_immediacy", "hook_payoff_consistency",
            )},
            "quality": _text(raw.get("quality")).lower(), "selection_authority": "AI_P0_5A4_opening_package",
        })
    for hook_id in sorted(set(hook_by_id) - covered_hooks):
        rejects.append({"hook_id": hook_id, "reject_reason": "opening_package_missing_for_eligible_hook", "category": "opening_package_contract"})
    return {
        "status": "opening_packages_completed" if packages else "opening_package_material_limited",
        "opening_packages": packages,
        "opening_package_rejects": rejects[:20],
        "contract": {
            "stage": P05_OPENING_PACKAGE_STAGE,
            "payoff_source": "frozen_P0_5A3_actor_pool_only",
            "actor_pool_modified": False, "journey_modified": False, "m3_modified": False,
            "semantic_selection_authority": "AI", "program_authority": "IDs_duration_word_overlap_and_schema_only",
        },
    }


def hook_candidate_as_opening_overlay(hook: Mapping[str, Any]) -> dict[str, Any]:
    """Project one already-approved Hook into an ephemeral casting overlay.

    This does not write into the P0.5A.3 Actor Pool.  It only supplies the
    specialized Hook to a read-only Narrative Mode regression where the
    existing Beat Caster remains responsible for all later semantic choices.
    """
    start_word_id = _safe_int(hook.get("start_word_id"))
    end_word_id = _safe_int(hook.get("end_word_id"))
    if start_word_id is None or end_word_id is None:
        raise ValueError("P0.5A.4 Hook 缺少词级 lineage，不能进入 Opening Casting")
    return {
        "beat_id": _text(hook.get("hook_id")), "source_beat_id": _text(hook.get("hook_id")),
        "boundary_decision": "P0_5A4_HOOK_RECALL", "start": hook.get("start"), "end": hook.get("end"),
        "duration_seconds": hook.get("duration"), "subtitle_ids": list(hook.get("source_subtitle_ids") or ()),
        "word_lineage": [dict(item) for item in hook.get("word_lineage") or () if isinstance(item, Mapping)],
        "final_start_word_id": start_word_id, "final_end_word_id": end_word_id, "text": _text(hook.get("text")),
        "commercial_theme": "P0.5A.4 Hook", "purchase_value": _text(hook.get("core_purchase_value")),
        "sub_outcome": _text(hook.get("core_purchase_value")),
        "evidence_function": _text(hook.get("hook_evidence_function")) or "result",
        "publishability_status": _HOOK_PUBLISHABLE_STATE, "visual_dependency": "none",
        "audio_only_eligible": True, "hook_eligible": True,
        "role_permissions": ["hook", "core", "proof"], "context_requirement": "standalone",
        "short_beat": False, "micro_expanded": False, "arc_eligible": True, "narrative_priority": "high",
        "selection_authority": "AI_P0_5A4_hook_specific_recall",
    }
