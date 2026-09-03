"""P0.5A source-wide Micro-Beat Inventory for commercial narration.

This module is deliberately outside the existing Candidate-first M2 route.
It makes no final-video decision, Arc grouping, ranking, reordering, or M3
request.  It exposes the complete source SRT to one AI inventory pass; the AI
decides which short, clean, commercially meaningful beats exist.  Code only
resolves the exact source/word lineage and rejects outputs that cannot be
played safely at their declared subtitle boundaries.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from commerce_planner_lite import (
    _extract_json,
    _post_lite_request,
    _quality_final_utterance_reject_reason,
)
from ai_clipper import (
    _is_backstage_instruction,
    _is_safety_blocked_text,
    _safety_price_cta_patterns,
    load_keywords,
)
from content_policy import default_content_policy
from selection_safety import live_interaction_or_size_response_reason
from semantic_word_binder import SemanticSrtWordTimeline
from clip_selector import _punctuation_after_words, _render_words


P05_MICRO_BEAT_INVENTORY_STAGE = "P0_5_micro_beat_inventory"
P05_MICRO_BEAT_QUALITY_STAGE = "P0_5_micro_beat_final_utterance_quality"
P05_MICRO_BEAT_BOUNDARY_QUALITY_STAGE = "P0_5_micro_beat_boundary_quality"
P05_MICRO_BEAT_ADJUDICATION_CALIBRATION_STAGE = "P0_5_micro_beat_publishable_adjudication_calibration"
P05_MICRO_BEAT_SHORT_RECALL_CALIBRATION_STAGE = "P0_5_micro_beat_short_recall_calibration"
MICRO_BEAT_PREFERRED_MIN_SECONDS = 2.0
MICRO_BEAT_PREFERRED_MAX_SECONDS = 5.0
MICRO_BEAT_MAX_SECONDS = 8.0
# 2s is deliberately a preference, not an admission threshold. The small
# hard floor only prevents word-like debris from becoming a Beat at all.
MICRO_BEAT_HARD_MIN_SECONDS = 0.8
MICRO_BEAT_SHORT_BEAT_MIN_SECONDS = 1.0
MICRO_BEAT_BOUNDARY_NEIGHBOR_SUBTITLES = 2
# Only used when replaying an old P0.5A discovery receipt so historical beat
# IDs keep their exact meaning before P0.5A.3 re-reviews Boundary decisions.
_LEGACY_MICRO_BEAT_SHORT_EXCEPTION_MIN_SECONDS = 1.5
MICRO_BEAT_SOURCE_BATCH_SECONDS = 480.0
MICRO_BEAT_QUALITY_BATCH_SIZE = 40
MICRO_BEAT_BOUNDARY_QUALITY_BATCH_SIZE = 8
MICRO_BEAT_BOUNDARY_ADJUDICATION_BATCH_SIZE = 8
MICRO_BEAT_FINAL_UTTERANCE_ADJUDICATION_PASSES = 4
MICRO_BEAT_PUBLISHABLE_ADJUDICATION_BATCH_SIZE = 8
_MICRO_BEAT_PUBLISHABILITY_STATUSES = frozenset({
    "publishable_clean", "publishable_visual", "reject",
})
_MICRO_BEAT_NARRATIVE_PRIORITIES = frozenset({"high", "medium", "low"})
_EVIDENCE_FUNCTIONS = frozenset({
    "result", "mechanism", "proof", "experience", "risk_remove", "styling",
    "scene", "trust", "other",
})
_MICRO_BEAT_SOCIAL_PROOF_OR_CTA_PATTERNS = (
    re.compile(r"(?:一|1)分钟[^，。！？!?]{0,12}(?:卖|单|走光)"),
    re.compile(r"(?:首批|今天)[^，。！？!?]{0,12}(?:走光|卖[^，。！？!?]{0,5}单)"),
    re.compile(r"(?:推荐|建议)[^，。！？!?]{0,12}拍"),
)
_BOUNDARY_DECISIONS = frozenset({
    "KEEP", "TRIM_AND_KEEP", "MICRO_EXPAND_AND_KEEP", "SPLIT", "REJECT",
})
_MICRO_BEAT_ROLE_PERMISSIONS = frozenset({"hook", "core", "proof", "support"})
_MICRO_BEAT_CONTEXT_REQUIREMENTS = frozenset({
    "standalone", "journey_context_ok", "visual_required",
})
_BOUNDARY_QUALITY_FLAGS = (
    "start_clean",
    "end_clean",
    "local_completeness",
    "context_dependency_resolved",
    "asr_publishable",
    "minimal_sufficient_expression",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "是"}


def _as_int_tuple(value: Any) -> tuple[int, ...]:
    values = value if isinstance(value, (list, tuple)) else (value,)
    result: list[int] = []
    for item in values:
        try:
            candidate_id = int(item)
        except (TypeError, ValueError):
            continue
        if candidate_id > 0 and candidate_id not in result:
            result.append(candidate_id)
    return tuple(result)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_mapping_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _normalize_spoken_source_text(value: Any) -> str:
    """Compare declared final wording without turning punctuation into a rewrite."""
    return re.sub(r"[\s，。！？、；：,.!?;:…]+", "", _text(value))


def _direct_source_safety_reason(
    text: str, *, forbidden_words: Sequence[str], price_patterns: Sequence[Any], policy: Mapping[str, Any],
) -> str:
    """Apply frozen source safety directly, without inheriting Candidate cuts."""
    if _is_backstage_instruction(text):
        return "backstage_instruction"
    if live_interaction_or_size_response_reason(text):
        return "live_interaction_or_personal_size_policy"
    if any(pattern.search(text) for pattern in _MICRO_BEAT_SOCIAL_PROOF_OR_CTA_PATTERNS):
        return "default_policy_social_proof_or_cta"
    if _is_safety_blocked_text(
        text, forbidden_words, price_patterns, content_policy=policy,
    ):
        return "frozen_content_safety"
    return ""


def build_micro_beat_source_rows(
    *,
    source_units: Sequence[Mapping[str, Any]],
    hard_safe_subtitle_ids: Sequence[int],
    word_timeline: SemanticSrtWordTimeline,
) -> tuple[dict[str, Any], ...]:
    """Prepare every SRT row without using a Candidate pool as a selector.

    P0.5's safety bit is derived directly from the original source row under
    the same frozen content policy.  The older Candidate Ledger remains a
    read-only audit annotation only: its earlier boundary/quality rejection
    must not erase a genuinely usable Micro Beat before M2 has seen it.
    """
    ledger_safe_ids = {int(value) for value in hard_safe_subtitle_ids}
    try:
        forbidden_words = load_keywords().get("forbidden_phrases", [])
    except Exception:
        forbidden_words = []
    price_patterns = _safety_price_cta_patterns()
    policy = default_content_policy()
    spans = word_timeline.by_subtitle_id
    rows: list[dict[str, Any]] = []
    for raw in source_units:
        try:
            subtitle_id = int(raw.get("id") or raw.get("subtitle_id") or 0)
        except (TypeError, ValueError):
            continue
        text = _text(raw.get("text"))
        start = _number(raw.get("start"), -1.0)
        end = _number(raw.get("end"), -1.0)
        if subtitle_id <= 0 or not text or start < 0 or end <= start:
            continue
        safety_block_reason = _direct_source_safety_reason(
            text, forbidden_words=forbidden_words, price_patterns=price_patterns, policy=policy,
        )
        span = spans.get(subtitle_id)
        word_bound = bool(
            span is not None
            and span.status == "bound"
            and span.word_start_index is not None
            and span.word_end_index is not None
            and span.word_start_time is not None
            and span.word_end_time is not None
        )
        raw_tokens = tuple(getattr(word_timeline, "words", ()))[
            int(span.word_start_index):int(span.word_end_index) + 1
        ] if word_bound and span is not None else ()
        word_tokens = [
            {
                "offset": index,
                "text": _text(word.get("text")),
                "start": round(_number(word.get("start")), 3),
                "end": round(_number(word.get("end")), 3),
            }
            for index, word in enumerate(raw_tokens)
            if _text(word.get("text")) and _number(word.get("end")) >= _number(word.get("start"))
        ]
        if word_bound and not word_tokens:
            # Unit-test fallbacks never authorize a production word trim.  The
            # actual semantic binder always supplies per-word source identity.
            word_tokens = [{"offset": 0, "text": text, "start": round(start, 3), "end": round(end, 3)}]
        rows.append({
            "subtitle_id": subtitle_id,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration_seconds": round(end - start, 3),
            "text": text,
            "hard_safe": not bool(safety_block_reason),
            "safety_block_reason": safety_block_reason,
            "candidate_ledger_safe": subtitle_id in ledger_safe_ids,
            "materializable": word_bound,
            "word_lineage": (
                {
                    "word_start_index": span.word_start_index,
                    "word_end_index": span.word_end_index,
                    "word_start_time": round(float(span.word_start_time), 3),
                    "word_end_time": round(float(span.word_end_time), 3),
                }
                if word_bound and span is not None else None
            ),
            "word_tokens": word_tokens,
        })
    return tuple(sorted(rows, key=lambda item: int(item["subtitle_id"])))


def build_micro_beat_source_batches(
    source_rows: Sequence[Mapping[str, Any]], *, max_seconds: float = MICRO_BEAT_SOURCE_BATCH_SECONDS,
) -> tuple[dict[str, Any], ...]:
    """Split the whole SRT only by deterministic elapsed time for transport.

    This is intentionally not a semantic clustering, ranking, or Candidate
    truncation operation.  Every source row appears in exactly one batch.
    """
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    batch_start: float | None = None
    for item in source_rows:
        row = dict(item)
        if current and batch_start is not None and float(row["end"]) - batch_start > max_seconds:
            batches.append(current)
            current = []
            batch_start = None
        if not current:
            batch_start = float(row["start"])
        current.append(row)
    if current:
        batches.append(current)
    return tuple({
        "batch_id": f"S{index:02d}",
        "start": round(float(rows[0]["start"]), 3),
        "end": round(float(rows[-1]["end"]), 3),
        "source_subtitle_ids": [int(row["subtitle_id"]) for row in rows],
        "source_rows": tuple(rows),
    } for index, rows in enumerate(batches, start=1))


def build_micro_beat_inventory_prompt(
    *, source_rows: Sequence[Mapping[str, Any]], batch_id: str, total_batch_count: int,
) -> str:
    """Ask one inventory pass to mine all source rows, not rank old clips."""
    compact_rows = [{
        "subtitle_id": item["subtitle_id"],
        "start": item["start"],
        "end": item["end"],
        "duration": item["duration_seconds"],
        "hard_safe": item["hard_safe"],
        "materializable": item["materializable"],
        "text": item["text"],
        "words": [token["text"] for token in item.get("word_tokens") or ()],
    } for item in source_rows]
    schema = {
        "inventory_scope": "complete_source_srt",
        "beats": [{
            "subtitle_ids": [1],
            "start_word_offset": None,
            "end_word_offset": None,
            "commercial_theme": "",
            "purchase_value": "",
            "sub_outcome": "",
            "evidence_function": "result/mechanism/proof/experience/risk_remove/styling/scene/trust/other",
            "standalone_quality": "why this exact original short utterance is complete and natural",
            "source_context_required": False,
            "why_this_is_a_new_beat": "what new buyer cognition it contributes, not a synonym",
            "short_beat_reason": "短 Beat 带来的局部购买推进；可为空",
        }],
        "rejected_important_segments": [{
            "subtitle_ids": [1],
            "reason": "fragment/live interaction/unsafe/ASR issue/redundant or otherwise unusable",
        }],
        "inventory_quality": {
            "complete_source_scanned": True,
            "no_live_fragments_selected": True,
            "no_synonym_repetition_selected": True,
            "reason": "",
        },
    }
    return "\n\n".join((
        f"你只做 P0.5A Micro-Beat Inventory 的确定性来源窗口 {batch_id}/{total_batch_count}，不做 Arc Assembly、导演选片、最终视频顺序或时长补齐。",
        "目标：从完整直播 SRT 中找回可用于未来商业叙事推进的真实短 Beat，而不是从旧 Candidate/Top12 中挑句。每个 Beat 最长 8 秒，2–5 秒只是偏好；0.8–2 秒的强 proof/result/mechanism 只要有明确局部购买推进也可保留。它只表达一个清晰的新购买认知。",
        "重要边界：输入包含完整源字幕。hard_safe=false 或 materializable=false 的行只能作为上下文，绝不能选择。hard_safe 是程序直接按冻结内容安全政策对原字幕计算的；旧 Candidate Ledger 不限制 Beat 是否存在。不能改写、合并非连续字幕、虚构字幕或借前后文补齐残句。不得把直播互动、价格/促销/发货话术、ASR 怪句或同义重复选为 Beat。",
        "同属一个大主题不等于重复：例如正面显窄、肩线收窄、腰胯修饰、后背显薄、160斤适穿是不同 sub_outcome，可以都入库。反过来，'很凉爽/特别凉爽/真的凉快'只是同义重复，保留最完整、最具体的一句。",
        "subtitle_ids 必须是当前完整 SRT 内连续的原始行。每行 words 是该行的零基词序列；你可用 start_word_offset / end_word_offset 选择首行/末行的精确词级边界，null 代表整行。不得截断一个词或只为缩短而破坏完整句子。短于 2 秒不是自动拒绝：若它在商品语境下表达清楚的新结果、机制或证明，就应保留；可写 short_beat_reason 供审计，但该字段不是准入条件。",
        "请尽可能完整地扫描全部字幕并找回所有值得进入后续 Arc 的高价值 Beat；不要为了固定数量凑条目，也不要只找每个主题的一句。每个被选 Beat 必须说明其新认知。为保证完整 JSON，所有文字注释均用 4–18 个汉字的短语；不要复述字幕正文、时间或字段名。",
        "rejected_important_segments 只记录最多 3 个最具代表性的拒绝例子，不要枚举所有未选字幕。其余未入库行无需逐条解释。",
        "返回严格 JSON，不要 Markdown。输出结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "完整原始 SRT（程序会重新计算边界、时长、安全和词级 lineage；不得把它当旧 Candidate 池）：\n" + json.dumps(compact_rows, ensure_ascii=False, separators=(",", ":")),
    ))


def _row_word_slice(
    row: Mapping[str, Any], *, start_offset: int | None = None, end_offset: int | None = None,
) -> tuple[list[dict[str, Any]], int, int] | None:
    tokens = [dict(item) for item in row.get("word_tokens") or () if isinstance(item, Mapping)]
    if not tokens:
        return None
    start = 0 if start_offset is None else start_offset
    end = len(tokens) - 1 if end_offset is None else end_offset
    if start < 0 or end < start or end >= len(tokens):
        return None
    return tokens[start:end + 1], start, end


def _selected_beat_source(
    rows: Sequence[Mapping[str, Any]], *, start_word_offset: int | None, end_word_offset: int | None,
) -> tuple[str, float, float, list[dict[str, Any]]] | None:
    rendered: list[str] = []
    lineage: list[dict[str, Any]] = []
    beat_start: float | None = None
    beat_end: float | None = None
    for index, row in enumerate(rows):
        selected = _row_word_slice(
            row,
            start_offset=start_word_offset if index == 0 else None,
            end_offset=end_word_offset if index == len(rows) - 1 else None,
        )
        if selected is None:
            return None
        words, actual_start_offset, actual_end_offset = selected
        full_words = [dict(item) for item in row.get("word_tokens") or ()]
        punctuation = _punctuation_after_words(_text(row.get("text")), full_words)
        text = _render_words(full_words, punctuation, actual_start_offset, actual_end_offset)
        if not text:
            return None
        rendered.append(text)
        first = words[0]
        last = words[-1]
        if beat_start is None:
            beat_start = _number(first.get("start"), -1.0)
        beat_end = _number(last.get("end"), -1.0)
        base = dict(row.get("word_lineage") or {})
        if not base:
            return None
        base.update({
            "subtitle_id": int(row["subtitle_id"]),
            "start_word_offset": actual_start_offset,
            "end_word_offset": actual_end_offset,
            "word_start_index": int(base["word_start_index"]) + actual_start_offset,
            "word_end_index": int(base["word_start_index"]) + actual_end_offset,
            "word_start_time": round(_number(first.get("start")), 3),
            "word_end_time": round(_number(last.get("end")), 3),
        })
        lineage.append(base)
    if beat_start is None or beat_end is None or beat_end <= beat_start:
        return None
    return "".join(rendered), beat_start, beat_end, lineage


def parse_micro_beat_inventory(
    *,
    data: Mapping[str, Any],
    source_rows: Sequence[Mapping[str, Any]],
    beat_id_start: int = 1,
    apply_static_p02_quality: bool = True,
    preserve_legacy_short_contract: bool = False,
) -> dict[str, Any]:
    """Validate an AI-declared inventory without choosing semantic content."""
    by_id = {int(item["subtitle_id"]): dict(item) for item in source_rows}
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for declared_index, raw in enumerate(_as_mapping_list(data.get("beats")), start=1):
        ids = _as_int_tuple(raw.get("subtitle_ids"))
        reason = ""
        rows = [by_id[item] for item in ids if item in by_id]
        start_word_offset = _optional_int(raw.get("start_word_offset"))
        end_word_offset = _optional_int(raw.get("end_word_offset"))
        source_selection: tuple[str, float, float, list[dict[str, Any]]] | None = None
        if not ids or len(rows) != len(ids):
            reason = "unknown_source_subtitle_id"
        elif list(ids) != sorted(ids) or any(right != left + 1 for left, right in zip(ids, ids[1:])):
            reason = "subtitle_ids_not_contiguous_source_span"
        elif any(item in used_ids for item in ids):
            reason = "overlapping_beat_source_lineage"
        elif any(not bool(item.get("hard_safe")) for item in rows):
            reason = "source_not_hard_safe"
        elif any(not bool(item.get("materializable")) for item in rows):
            reason = "source_word_lineage_unresolved"
        elif not all(_text(raw.get(key)) for key in (
            "commercial_theme", "purchase_value", "sub_outcome", "standalone_quality", "why_this_is_a_new_beat",
        )):
            reason = "required_commercial_annotation_missing"
        elif _text(raw.get("evidence_function")) not in _EVIDENCE_FUNCTIONS:
            reason = "evidence_function_invalid"
        elif (
            (raw.get("start_word_offset") not in (None, "") and start_word_offset is None)
            or (raw.get("end_word_offset") not in (None, "") and end_word_offset is None)
        ):
            reason = "word_offset_invalid"
        else:
            source_selection = _selected_beat_source(
                rows, start_word_offset=start_word_offset, end_word_offset=end_word_offset,
            )
            if source_selection is None:
                reason = "word_offset_out_of_source_lineage"
            else:
                text, start, end, _lineage = source_selection
                duration = round(end - start, 3)
                if duration > MICRO_BEAT_MAX_SECONDS + 1e-6:
                    reason = "duration_exceeds_micro_beat_max"
                elif preserve_legacy_short_contract and duration < _LEGACY_MICRO_BEAT_SHORT_EXCEPTION_MIN_SECONDS - 1e-6:
                    reason = "duration_below_short_complete_exception_min"
                elif preserve_legacy_short_contract and duration < MICRO_BEAT_PREFERRED_MIN_SECONDS and not _text(raw.get("short_complete_exception_reason")):
                    reason = "short_complete_exception_reason_missing"
                elif duration < MICRO_BEAT_HARD_MIN_SECONDS - 1e-6:
                    reason = "duration_below_micro_beat_hard_min"
                elif apply_static_p02_quality:
                    p02_reason = _quality_final_utterance_reject_reason(text)
                    if p02_reason:
                        reason = f"p0_2_final_utterance_quality:{p02_reason}"
        if reason:
            rejected.append({
                "declared_index": declared_index,
                "subtitle_ids": list(ids),
                "text": source_selection[0] if source_selection is not None else "".join(_text(item.get("text")) for item in rows),
                "reason": reason,
                "ai_annotations": dict(raw),
            })
            continue
        if source_selection is None:
            # Defensive only: all non-selected source paths return above.
            continue
        text, start, end, selected_word_lineage = source_selection
        duration = round(end - start, 3)
        beat_id = f"B{len(accepted) + int(beat_id_start):03d}"
        accepted.append({
            "beat_id": beat_id,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration_seconds": duration,
            "subtitle_ids": list(ids),
            "start_word_offset": start_word_offset,
            "end_word_offset": end_word_offset,
            "text": text,
            "commercial_theme": _text(raw.get("commercial_theme")),
            "purchase_value": _text(raw.get("purchase_value")),
            "sub_outcome": _text(raw.get("sub_outcome")),
            "evidence_function": _text(raw.get("evidence_function")),
            "standalone_quality": _text(raw.get("standalone_quality")),
            # A short proof may need the product / Journey already established
            # without being an unusable source fragment. Boundary + the
            # principal publishability adjudication still decide whether it
            # can ever be a final Actor and in which role.
            "source_context_required": _as_bool(raw.get("source_context_required")),
            "why_this_is_a_new_beat": _text(raw.get("why_this_is_a_new_beat")),
            "short_beat": duration < MICRO_BEAT_PREFERRED_MIN_SECONDS,
            "short_beat_reason": _text(raw.get("short_beat_reason")) or _text(raw.get("short_complete_exception_reason")),
            "lineage_status": "resolved",
            "word_lineage": selected_word_lineage,
            "selection_authority": "AI_micro_beat_inventory",
        })
        used_ids.update(ids)
    source_start = float(source_rows[0]["start"]) if source_rows else 0.0
    source_end = float(source_rows[-1]["end"]) if source_rows else 0.0
    total_usable = round(sum(float(item["duration_seconds"]) for item in accepted), 3)
    preferred = sum(
        MICRO_BEAT_PREFERRED_MIN_SECONDS <= float(item["duration_seconds"]) <= MICRO_BEAT_PREFERRED_MAX_SECONDS
        for item in accepted
    )
    return {
        "status": "inventory_mined_pending_p0_2_quality",
        "total_srt_duration_seconds": round(max(0.0, source_end - source_start), 3),
        "total_source_subtitles": len(source_rows),
        "total_hard_safe_materializable_subtitles": sum(
            bool(item.get("hard_safe")) and bool(item.get("materializable")) for item in source_rows
        ),
        "total_micro_beats": len(accepted),
        "total_usable_beat_seconds": total_usable,
        "preferred_2_to_5_second_beat_count": preferred,
        "short_complete_exception_count": sum(
            float(item["duration_seconds"]) < MICRO_BEAT_PREFERRED_MIN_SECONDS for item in accepted
        ),
        "beat_inventory": accepted,
        "contract_rejected_declared_beats": rejected,
        "ai_rejected_important_segments": _as_mapping_list(data.get("rejected_important_segments")),
        "inventory_quality": dict(data.get("inventory_quality")) if isinstance(data.get("inventory_quality"), Mapping) else {},
        "contract": {
            "stage": P05_MICRO_BEAT_INVENTORY_STAGE,
            "complete_srt_visible_to_ai": True,
            "old_candidate_pool_used_as_selector": False,
            "source_safety_derived_directly_not_from_candidate_ledger": True,
            "arc_assembly_performed": False,
            "dense_composition_performed": False,
            "m3_invoked": False,
            "min_preferred_seconds": MICRO_BEAT_PREFERRED_MIN_SECONDS,
            "hard_min_seconds": MICRO_BEAT_HARD_MIN_SECONDS,
            "max_seconds": MICRO_BEAT_MAX_SECONDS,
            "short_beat_reason_is_advisory": True,
            "source_selection_authority": "AI",
            "program_authority": "source_boundaries_safety_word_lineage_duration_P0_2_static_quality_only",
        },
    }


def build_micro_beat_quality_prompt(*, beat_inventory: Sequence[Mapping[str, Any]]) -> str:
    """Reuse P0.2 as a pure final-utterance gate, never as a recommender."""
    rows = [{
        "beat_id": item["beat_id"], "start": item["start"], "end": item["end"],
        "duration_seconds": item["duration_seconds"], "subtitle_ids": item["subtitle_ids"],
        "text": item["text"], "commercial_theme": item["commercial_theme"],
        "purchase_value": item["purchase_value"], "sub_outcome": item["sub_outcome"],
    } for item in beat_inventory]
    schema = {
        "beat_quality": [{
            "beat_id": "B001", "beat_start_clean": True, "beat_end_clean": True,
            "spoken_completeness": True, "sentence_cleanliness": True,
            "asr_quality": True, "final_utterance_eligible": True,
            "reason": "short P0.2 quality reason",
        }],
        "inventory_quality": {
            "all_beats_reviewed": True,
            "no_fragment_or_asr_false_positive_retained": True,
            "reason": "",
        },
    }
    return "\n\n".join((
        "你只做冻结的 P0.2 Final Utterance Quality，审查 P0.5A 已挖出的 Micro Beat。你不能增加 Beat、换用源字幕、重写原话、调整顺序、组成 Arc 或按卖点补库存。",
        "逐条检查：起句是否干净、收句是否闭合、单独播放是否完整自然、ASR 是否明显异常、是否带直播互动/承接残留。任何一项不通过，final_utterance_eligible=false。不要因为商业方向正确就放行怪句。",
        "以下无条件不合格：个人身高体重/报尺码问答；销量或“几分钟几百单”社会证明；促单、直播口令、上新预告；当前焦糖商品之外的未来新品；“你看/自己看/有没有觉得/你会觉得”这类依赖现场展示或观众回应的句子；以“然后/但是/也是/都/是但是”承接却无法单独成立的句子。不要因内容大致可懂而放行。",
        "必须对输入中的每个 beat_id 各输出恰好一条判断。reason 使用 4–18 个汉字短语；通过也需简短原因。",
        "返回严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "待审 Micro Beats：\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def apply_micro_beat_quality(
    *, inventory: Mapping[str, Any], data: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply only AI-declared P0.2 pass/fail decisions to mined Beat IDs."""
    base_beats = _as_mapping_list(inventory.get("beat_inventory"))
    by_id = {_text(item.get("beat_id")): dict(item) for item in base_beats}
    evaluations = _as_mapping_list(data.get("beat_quality"))
    by_evaluated_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in evaluations:
        beat_id = _text(item.get("beat_id"))
        if not beat_id:
            continue
        if beat_id in by_evaluated_id:
            duplicate_ids.add(beat_id)
        by_evaluated_id[beat_id] = item
    retained: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    required_flags = (
        "beat_start_clean", "beat_end_clean", "spoken_completeness",
        "sentence_cleanliness", "asr_quality", "final_utterance_eligible",
    )
    for beat in base_beats:
        beat_id = _text(beat.get("beat_id"))
        evaluation = by_evaluated_id.get(beat_id)
        if beat_id in duplicate_ids:
            reason = "quality_duplicate_beat_decision"
        elif evaluation is None:
            reason = "quality_decision_missing"
        elif not all(_as_bool(evaluation.get(flag)) for flag in required_flags):
            reason = _text(evaluation.get("reason")) or "p0_2_quality_not_passed"
        else:
            checked = dict(beat)
            checked["p0_2_micro_beat_quality"] = {
                key: _as_bool(evaluation.get(key)) for key in required_flags
            } | {"reason": _text(evaluation.get("reason"))}
            retained.append(checked)
            continue
        rejected.append({
            "beat_id": beat_id,
            "subtitle_ids": list(beat.get("subtitle_ids") or ()),
            "text": _text(beat.get("text")),
            "reason": reason,
            "ai_quality": dict(evaluation or {}),
        })
    result = dict(inventory)
    result.update({
        "status": "inventory_completed",
        "mined_before_p0_2_quality_count": len(base_beats),
        "mined_before_p0_2_quality_seconds": round(sum(
            _number(item.get("duration_seconds")) for item in base_beats
        ), 3),
        "total_micro_beats": len(retained),
        "total_usable_beat_seconds": round(sum(
            _number(item.get("duration_seconds")) for item in retained
        ), 3),
        "preferred_2_to_5_second_beat_count": sum(
            MICRO_BEAT_PREFERRED_MIN_SECONDS <= _number(item.get("duration_seconds")) <= MICRO_BEAT_PREFERRED_MAX_SECONDS
            for item in retained
        ),
        "short_complete_exception_count": sum(
            _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS for item in retained
        ),
        "beat_inventory": retained,
        "p0_2_quality_rejected_beats": rejected,
        "inventory_quality": {
            "mining": dict(inventory.get("inventory_quality") or {}),
            "p0_2_final_utterance_quality": (
                dict(data.get("inventory_quality"))
                if isinstance(data.get("inventory_quality"), Mapping) else {}
            ),
        },
    })
    result["contract"] = dict(result.get("contract") or {}) | {
        "p0_2_micro_beat_quality_applied": True,
        "quality_selection_authority": "AI_P0_2_final_utterance_quality",
    }
    return result


def reconstruct_frozen_micro_beat_candidates(
    *,
    raw_inventory_responses: Sequence[str],
    source_rows: Sequence[Mapping[str, Any]],
    allow_source_batch_layout_drift: bool = False,
) -> dict[str, Any]:
    """Recreate the P0.5A pre-quality inventory from its archived AI replies.

    P0.5A.1 deliberately consumes this fixed list rather than running source
    discovery again.  Re-parsing is only a lineage-integrity check: it cannot
    introduce an utterance that was absent from the frozen P0.5A reply.
    """
    batches = build_micro_beat_source_batches(source_rows)
    layout_drift_replayed = False
    if len(raw_inventory_responses) != len(batches):
        if allow_source_batch_layout_drift:
            # The AI declarations remain frozen.  This fallback merely lets a
            # later contract calibration resolve their exact subtitle/word
            # IDs when a safety/configuration drift changed batching layout.
            # It never re-runs source discovery or invents a Beat.
            batches = tuple({"batch_id": f"REPLAY{index:02d}", "source_rows": source_rows}
                            for index, _item in enumerate(raw_inventory_responses, start=1))
            layout_drift_replayed = True
        else:
            return {
                "status": "frozen_inventory_unavailable",
                "errors": ["raw_inventory_response_batch_count_mismatch"],
                "beat_inventory": [],
            }
    beats: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    next_beat_id = 1
    for batch, content in zip(batches, raw_inventory_responses):
        try:
            data = _extract_json(_text(content))
        except (RuntimeError, ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            return {
                "status": "frozen_inventory_unavailable",
                "errors": [f"raw_inventory_response_invalid:{batch['batch_id']}"],
                "beat_inventory": [],
            }
        parsed = parse_micro_beat_inventory(
            data=data,
            source_rows=batch["source_rows"],
            beat_id_start=next_beat_id,
            apply_static_p02_quality=False,
            preserve_legacy_short_contract=True,
        )
        next_beat_id += len(parsed["beat_inventory"])
        beats.extend(parsed["beat_inventory"])
        rejected.extend(parsed["contract_rejected_declared_beats"])
    fingerprint_payload = [
        {
            "beat_id": item["beat_id"],
            "subtitle_ids": item["subtitle_ids"],
            "start_word_offset": item.get("start_word_offset"),
            "end_word_offset": item.get("end_word_offset"),
            "text": item["text"],
            "purchase_value": item["purchase_value"],
            "sub_outcome": item["sub_outcome"],
            "evidence_function": item["evidence_function"],
        }
        for item in beats
    ]
    return {
        "status": "frozen_inventory_reconstructed",
        "raw_beat_count": len(beats),
        "raw_beat_seconds": round(sum(_number(item.get("duration_seconds")) for item in beats), 3),
        "raw_beat_fingerprint": hashlib.sha256(
            json.dumps(fingerprint_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "beat_inventory": beats,
        "contract_rejected_declared_beats": rejected,
        "contract": {
            "stage": P05_MICRO_BEAT_BOUNDARY_QUALITY_STAGE,
            "raw_discovery_replayed": True,
            "complete_srt_rescanned_for_discovery": False,
            "source_batch_layout_drift_replayed": layout_drift_replayed,
            "commercial_annotations_frozen": True,
            "arc_assembly_performed": False,
            "dense_composition_performed": False,
            "m3_invoked": False,
        },
    }


def _source_words_for_micro_beat(
    beat: Mapping[str, Any], *, source_rows_by_id: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """Expose the exact, already-selected source words for one frozen Beat."""
    words: list[dict[str, Any]] = []
    expected_ids = _as_int_tuple(beat.get("subtitle_ids"))
    lineage_rows = _as_mapping_list(beat.get("word_lineage"))
    if not expected_ids or len(lineage_rows) != len(expected_ids):
        return None
    for expected_subtitle_id, lineage in zip(expected_ids, lineage_rows):
        subtitle_id = _optional_int(lineage.get("subtitle_id"))
        row = source_rows_by_id.get(int(expected_subtitle_id))
        if subtitle_id != expected_subtitle_id or row is None:
            return None
        selected = _row_word_slice(
            row,
            start_offset=_optional_int(lineage.get("start_word_offset")),
            end_offset=_optional_int(lineage.get("end_word_offset")),
        )
        if selected is None:
            return None
        selected_words, _, _ = selected
        base = dict(row.get("word_lineage") or {})
        base_start = _optional_int(base.get("word_start_index"))
        if base_start is None:
            return None
        for token in selected_words:
            offset = _optional_int(token.get("offset"))
            if offset is None:
                return None
            words.append({
                "word_id": base_start + offset,
                "subtitle_id": expected_subtitle_id,
                "offset": offset,
                "text": _text(token.get("text")),
                "start": round(_number(token.get("start")), 3),
                "end": round(_number(token.get("end")), 3),
            })
    if not words or any(not item["text"] for item in words):
        return None
    if len({int(item["word_id"]) for item in words}) != len(words):
        return None
    return words


def _boundary_context_source_words(
    beat: Mapping[str, Any], *, source_rows_by_id: Mapping[int, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], set[int], list[int]] | None:
    """Return a bounded, safe source window for a Boundary decision.

    The frozen Beat remains the semantic anchor.  Its immediate safe subtitle
    neighbours are exposed only so AI can finish a clipped thought; code never
    picks a neighbour or a wording on its own.
    """
    original_words = _source_words_for_micro_beat(beat, source_rows_by_id=source_rows_by_id)
    original_ids = _as_int_tuple(beat.get("subtitle_ids"))
    if original_words is None or not original_ids:
        return None
    ordered_ids = sorted(source_rows_by_id)
    positions = {subtitle_id: index for index, subtitle_id in enumerate(ordered_ids)}
    if any(subtitle_id not in positions for subtitle_id in original_ids):
        return None
    first = positions[original_ids[0]]
    last = positions[original_ids[-1]]
    if last < first:
        return None

    context_ids: list[int] = list(original_ids)
    for index in range(first - 1, max(-1, first - MICRO_BEAT_BOUNDARY_NEIGHBOR_SUBTITLES - 1), -1):
        subtitle_id = ordered_ids[index]
        row = source_rows_by_id[subtitle_id]
        if not bool(row.get("hard_safe")) or not bool(row.get("materializable")):
            break
        context_ids.insert(0, subtitle_id)
    for index in range(last + 1, min(len(ordered_ids), last + MICRO_BEAT_BOUNDARY_NEIGHBOR_SUBTITLES + 1)):
        subtitle_id = ordered_ids[index]
        row = source_rows_by_id[subtitle_id]
        if not bool(row.get("hard_safe")) or not bool(row.get("materializable")):
            break
        context_ids.append(subtitle_id)

    words: list[dict[str, Any]] = []
    for subtitle_id in context_ids:
        row = source_rows_by_id[subtitle_id]
        selected = _row_word_slice(row)
        if selected is None:
            return None
        row_words, _start, _end = selected
        base = dict(row.get("word_lineage") or {})
        base_start = _optional_int(base.get("word_start_index"))
        if base_start is None:
            return None
        for token in row_words:
            offset = _optional_int(token.get("offset"))
            if offset is None:
                return None
            words.append({
                "word_id": base_start + offset,
                "subtitle_id": subtitle_id,
                "offset": offset,
                "text": _text(token.get("text")),
                "start": round(_number(token.get("start")), 3),
                "end": round(_number(token.get("end")), 3),
            })
    if not words or any(not item["text"] for item in words):
        return None
    if len({int(item["word_id"]) for item in words}) != len(words):
        return None
    return words, {int(item["word_id"]) for item in original_words}, context_ids


def _boundary_segment_source(
    *, source_words: Sequence[Mapping[str, Any]], source_rows_by_id: Mapping[int, Mapping[str, Any]],
    start_word_id: int | None, end_word_id: int | None,
) -> tuple[str, float, float, list[dict[str, Any]], list[int]] | None:
    """Resolve an AI-declared exact interval in one bounded source window."""
    if start_word_id is None or end_word_id is None:
        return None
    words = [dict(item) for item in source_words if isinstance(item, Mapping)]
    if not words:
        return None
    positions = {int(item["word_id"]): index for index, item in enumerate(words)}
    if start_word_id not in positions or end_word_id not in positions:
        return None
    start_position = positions[start_word_id]
    end_position = positions[end_word_id]
    if end_position < start_position:
        return None
    selected_words = words[start_position:end_position + 1]
    grouped: list[tuple[Mapping[str, Any], int, int]] = []
    for selected in selected_words:
        subtitle_id = int(selected["subtitle_id"])
        row = source_rows_by_id.get(subtitle_id)
        if row is None or not bool(row.get("hard_safe")) or not bool(row.get("materializable")):
            return None
        offset = int(selected["offset"])
        if grouped and int(grouped[-1][0]["subtitle_id"]) == subtitle_id:
            previous_row, group_start, group_end = grouped[-1]
            if offset != group_end + 1:
                return None
            grouped[-1] = (previous_row, group_start, offset)
        else:
            grouped.append((row, offset, offset))
    rendered: list[str] = []
    lineage: list[dict[str, Any]] = []
    for row, row_start_offset, row_end_offset in grouped:
        selected = _row_word_slice(row, start_offset=row_start_offset, end_offset=row_end_offset)
        if selected is None:
            return None
        row_words, actual_start_offset, actual_end_offset = selected
        full_words = [dict(item) for item in row.get("word_tokens") or ()]
        punctuation = _punctuation_after_words(_text(row.get("text")), full_words)
        rendered_text = _render_words(full_words, punctuation, actual_start_offset, actual_end_offset)
        if not rendered_text:
            return None
        base = dict(row.get("word_lineage") or {})
        base_start = _optional_int(base.get("word_start_index"))
        if base_start is None:
            return None
        base.update({
            "subtitle_id": int(row["subtitle_id"]),
            "start_word_offset": actual_start_offset,
            "end_word_offset": actual_end_offset,
            "word_start_index": base_start + actual_start_offset,
            "word_end_index": base_start + actual_end_offset,
            "word_start_time": round(_number(row_words[0].get("start")), 3),
            "word_end_time": round(_number(row_words[-1].get("end")), 3),
        })
        rendered.append(rendered_text)
        lineage.append(base)
    start = _number(selected_words[0].get("start"), -1.0)
    end = _number(selected_words[-1].get("end"), -1.0)
    if start < 0 or end <= start:
        return None
    return "".join(rendered), start, end, lineage, [int(item["word_id"]) for item in selected_words]


def _boundary_payload_for_beat(
    beat: Mapping[str, Any], *, source_rows_by_id: Mapping[int, Mapping[str, Any]],
) -> dict[str, Any] | None:
    context = _boundary_context_source_words(beat, source_rows_by_id=source_rows_by_id)
    if context is None:
        return None
    words, original_word_ids, context_subtitle_ids = context
    original_subtitle_ids = list(_as_int_tuple(beat.get("subtitle_ids")))
    return {
        "beat_id": _text(beat.get("beat_id")),
        "original_start": beat.get("start"),
        "original_end": beat.get("end"),
        "original_duration_seconds": beat.get("duration_seconds"),
        "original_text": _text(beat.get("text")),
        "original_subtitle_ids": original_subtitle_ids,
        "neighbor_subtitle_ids": [
            subtitle_id for subtitle_id in context_subtitle_ids if subtitle_id not in original_subtitle_ids
        ],
        "original_word_ids": sorted(original_word_ids),
        "source_words": [
            {"word_id": item["word_id"], "subtitle_id": item["subtitle_id"], "text": item["text"]}
            for item in words
        ],
    }


def build_micro_beat_boundary_quality_prompt(
    *, beat_inventory: Sequence[Mapping[str, Any]], source_rows_by_id: Mapping[int, Mapping[str, Any]],
) -> str:
    """Request AI boundary decisions, while code keeps source fidelity control."""
    rows: list[dict[str, Any]] = []
    for beat in beat_inventory:
        payload = _boundary_payload_for_beat(beat, source_rows_by_id=source_rows_by_id)
        if payload is not None:
            rows.append(payload)
    schema = {
        "boundary_decisions": [{
            "beat_id": "B001",
            "decision": "KEEP/TRIM_AND_KEEP/MICRO_EXPAND_AND_KEEP/SPLIT/REJECT",
            "current_start_is_earliest_natural": True,
            "current_end_is_latest_necessary": True,
            "trim_reason": "",
            "reject_reason": "",
            "segments": [{
                "segment_key": "A",
                "start_word_id": 100,
                "end_word_id": 120,
                "final_spoken_text": "从 source_words 原样拼出的最终口播",
                "short_beat_reason": "",
                "expansion_reason": "",
                "neighbor_subtitle_ids_used": [],
                "same_purchase_value": True,
                "no_new_purchase_value": True,
                "boundary_quality": {
                    "start_clean": True,
                    "end_clean": True,
                    "local_completeness": True,
                    "context_dependency_resolved": True,
                    "asr_publishable": True,
                    "minimal_sufficient_expression": True,
                },
            }],
        }],
        "boundary_quality_summary": {
            "all_frozen_beats_reviewed": True,
            "no_semantic_rewrite": True,
            "reason": "",
        },
    }
    return "\n\n".join((
        "你只做 P0.5A.3 Micro-Beat Boundary / Short Recall Calibration。输入是冻结的 P0.5A Raw Beat Candidate Inventory；不重新扫描 SRT、不找更多 Beat、不改商业价值/sub_outcome/evidence_function，不做 Arc、Composer、排序、M3 或视频。商业标签已经冻结，但本次故意不展示：它们绝不能替一条怪句、残句或 ASR 异常背书。你只根据真实原话和词级边界判断是否能直接播放。",
        "对每条冻结 Beat 都必须做且只能做一个决定：KEEP、TRIM_AND_KEEP、MICRO_EXPAND_AND_KEEP、SPLIT、REJECT。你决定语义边界和是否可发布；程序只验证 word lineage、连续性、时长、冻结安全和原文一致性，绝不替你选语义内容。",
        "每条都必须重新判断当前首词是否最早自然起点、当前末词是否最晚必要终点。只要有不损失商业含义的前导直播残留或后导互动/岔题，必须使用精确 word_id 裁掉；不要默认整条字幕边界。KEEP 只能用于现有边界已是最短充分表达。",
        "前导互动如“还好你聪明”、无意义“对对对”、承接接话应裁掉而非连主体一起拒绝。后导 CTA、公屏互动、尺码问答切入、下单提示、下一话题残片也应在主体结束词截掉。若裁后仍不完整，再 REJECT。",
        "不要把“你看/它其实/然后/所以/这个/真的/我觉得”当禁词。只有当前 Beat 脱离直播上下文时指代悬空、接话无来源、或信息未落地，才 REJECT。例：“你看连后背都显瘦”可成立；“你看”“所以它就是这样”“对，就是这个”不可成立。",
        "semantic_understandable 不等于 publishable_spoken_text。ASR 虽可猜但听起来不像人话，必须 REJECT；不得改写主播原话来修复。类似“自带3到5厘米的销售”“你穿它吹空”“放下来这个黑色的这个花边”“腰肩腰胯这一整个”“这个面料是”“热热啥呀”都不是靠裁到更短就能发布的自然口播，除非你能从真实 word_id 中留下一个独立、对象和结论都落地的完整句。每个保留 Segment 的 six boundary_quality 布尔项都必须为 true。",
        "最短充分表达不等于把句子削到残缺。一个 Beat 只保留一个主要新认知；若删去某些词后只剩名词短语、未完成谓语、无对象的指代、病句、逗号悬停或必须看直播画面才懂的描述，必须 REJECT。比如“一拉开160斤，轻轻松松”“32支超爽的一个竹节麻纱线”“用的是再生纤维素纤维，这个面料是”“放下来这个黑色的这个花边”都不是独立口播。最长 8 秒，2–5 秒只是偏好；0.8–2 秒的完整结果、机制、proof 或 support 可以 KEEP/SPLIT，short_beat_reason 仅供审计，不是通过条件。短 Beat 不等于“所以它”“从这里”这种碎片。",
        "输入 source_words 同时给出当前 Beat 的原始词与最多前后两条安全相邻字幕。仅当原 Beat 因字幕切断而未闭合时，才能用 MICRO_EXPAND_AND_KEEP：最终范围必须仍包含原 Beat 的词，并实际使用相邻 subtitle 的词；只能补齐同一个已冻结购买认知，不能引入新卖点、跨话题或扩成超过 8 秒。该决定必须填写 expansion_reason、neighbor_subtitle_ids_used、same_purchase_value=true、no_new_purchase_value=true。若无需邻句，绝不能假装 Micro Expand。",
        "先在心中完成两遍检查：第一遍只判断当前 Beat 能否靠真实边界（必要时极小范围邻句补齐）变成可播放的真人口播；不能就直接 REJECT。第二遍才为可保留内容选择最短的精确 word_id，并在 final_spoken_text 原样写出这段 source_words 拼成的口播。它必须是一条听起来自然、对象和结论都落地的句子，不是一个“模型认为大概能用”的片段。REJECT 是正确结果，不代表你漏掉素材；严禁为了保留数量把不完整短语标成 six flags 全 true。",
        "segments 的 start_word_id/end_word_id 必须完全来自该 Beat 给出的 source_words，且是连续原词。KEEP / TRIM_AND_KEEP / MICRO_EXPAND_AND_KEEP 返回一个 segment；SPLIT 返回两个或以上、时间顺序、不重叠的 segments；REJECT 返回空 segments。所有文字原因保持4–20个汉字，不要复述原话。",
        "必须对当前输入的每个 beat_id 各输出恰好一条决定。返回严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "冻结 Raw Beat Candidates：\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def _boundary_quality_values(raw_segment: Mapping[str, Any]) -> tuple[dict[str, bool], bool]:
    raw_quality = raw_segment.get("boundary_quality")
    quality = dict(raw_quality) if isinstance(raw_quality, Mapping) else {}
    values = {name: _as_bool(quality.get(name)) for name in _BOUNDARY_QUALITY_FLAGS}
    return values, all(values.values())


def _boundary_rejected_record(
    beat: Mapping[str, Any], *, decision: str, reason: str, raw_decision: Mapping[str, Any] | None,
) -> dict[str, Any]:
    return {
        "beat_id": _text(beat.get("beat_id")),
        "decision": decision or "REJECT",
        "original_start": beat.get("start"),
        "original_end": beat.get("end"),
        "original_duration_seconds": beat.get("duration_seconds"),
        "original_text": _text(beat.get("text")),
        "purchase_value": _text(beat.get("purchase_value")),
        "sub_outcome": _text(beat.get("sub_outcome")),
        "evidence_function": _text(beat.get("evidence_function")),
        "reject_reason": reason,
        "raw_boundary_decision": dict(raw_decision or {}),
        "lineage_status": "frozen_source_preserved",
    }


def parse_micro_beat_boundary_quality(
    *,
    frozen_inventory: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]],
    data: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate AI-declared P0.5A.1 boundaries without semantic auto-repair."""
    raw_beats = _as_mapping_list(frozen_inventory)
    source_rows_by_id = {int(row["subtitle_id"]): dict(row) for row in source_rows}
    decisions = _as_mapping_list(data.get("boundary_decisions"))
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in decisions:
        beat_id = _text(item.get("beat_id"))
        if not beat_id:
            continue
        if beat_id in by_id:
            duplicate_ids.add(beat_id)
        by_id[beat_id] = item
    final_beats: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    trim_cases: list[dict[str, Any]] = []
    split_cases: list[dict[str, Any]] = []
    for beat in raw_beats:
        beat_id = _text(beat.get("beat_id"))
        raw = by_id.get(beat_id)
        decision = _text((raw or {}).get("decision")).upper()
        if beat_id in duplicate_ids:
            reason = "boundary_quality_duplicate_decision"
        elif raw is None:
            reason = "boundary_quality_decision_missing"
        elif decision not in _BOUNDARY_DECISIONS:
            reason = "boundary_quality_decision_invalid"
        else:
            reason = ""
        original_words = _source_words_for_micro_beat(beat, source_rows_by_id=source_rows_by_id)
        boundary_context = _boundary_context_source_words(beat, source_rows_by_id=source_rows_by_id)
        if not reason and (original_words is None or boundary_context is None):
            reason = "frozen_beat_word_lineage_unresolved"
        if reason:
            record = _boundary_rejected_record(beat, decision=decision, reason=reason, raw_decision=raw)
            rejected.append(record)
            audits.append(record)
            continue
        assert raw is not None and original_words is not None and boundary_context is not None
        boundary_words, original_word_ids, context_subtitle_ids = boundary_context
        if decision == "REJECT":
            reject_reason = _text(raw.get("reject_reason")) or "AI_boundary_quality_reject"
            record = _boundary_rejected_record(beat, decision=decision, reason=reject_reason, raw_decision=raw)
            rejected.append(record)
            audits.append(record)
            continue
        raw_segments = _as_mapping_list(raw.get("segments"))
        # Some otherwise-valid boundary receipts describe two disjoint, exact
        # source spans as TRIM_AND_KEEP rather than SPLIT.  Their word ranges
        # are the semantic decision; normalising this label does not invent a
        # candidate or choose content programmatically.  It merely preserves
        # the two AI-declared children instead of turning a good short Beat
        # (for example, the complete "三伏天随便穿" sentence) into a contract
        # false negative.
        effective_decision = (
            "SPLIT"
            if decision == "TRIM_AND_KEEP" and len(raw_segments) > 1
            else decision
        )
        if not raw_segments:
            reason = "boundary_quality_segments_missing"
        elif effective_decision in {"KEEP", "TRIM_AND_KEEP", "MICRO_EXPAND_AND_KEEP"} and len(raw_segments) != 1:
            reason = "boundary_quality_single_segment_required"
        elif effective_decision == "SPLIT" and len(raw_segments) < 2:
            reason = "boundary_quality_split_children_required"
        else:
            reason = ""
        selected_children: list[dict[str, Any]] = []
        used_word_ids: set[int] = set()
        if not reason:
            for index, raw_segment in enumerate(raw_segments, start=1):
                start_word_id = _optional_int(raw_segment.get("start_word_id"))
                end_word_id = _optional_int(raw_segment.get("end_word_id"))
                quality, quality_passed = _boundary_quality_values(raw_segment)
                if not quality_passed:
                    reason = "AI_boundary_quality_not_publishable"
                    break
                selected = _boundary_segment_source(
                    source_words=boundary_words,
                    source_rows_by_id=source_rows_by_id,
                    start_word_id=start_word_id,
                    end_word_id=end_word_id,
                )
                if selected is None:
                    reason = "boundary_word_ids_outside_frozen_lineage"
                    break
                text, start, end, word_lineage, selected_word_ids = selected
                if not _text(raw_segment.get("final_spoken_text")):
                    reason = "boundary_final_spoken_text_missing"
                    break
                if _normalize_spoken_source_text(raw_segment.get("final_spoken_text")) != _normalize_spoken_source_text(text):
                    reason = "boundary_final_spoken_text_not_exact_source_words"
                    break
                if used_word_ids.intersection(selected_word_ids):
                    reason = "boundary_split_children_overlap"
                    break
                uses_neighbor_words = any(word_id not in original_word_ids for word_id in selected_word_ids)
                uses_original_words = any(word_id in original_word_ids for word_id in selected_word_ids)
                neighbor_subtitle_ids_used = sorted({
                    int(item["subtitle_id"]) for item in word_lineage
                    if int(item["subtitle_id"]) not in _as_int_tuple(beat.get("subtitle_ids"))
                })
                if effective_decision == "MICRO_EXPAND_AND_KEEP":
                    declared_neighbor_ids = sorted(_as_int_tuple(raw_segment.get("neighbor_subtitle_ids_used")))
                    if not uses_neighbor_words or not uses_original_words:
                        reason = "micro_expand_must_join_original_and_neighbor"
                        break
                    if not neighbor_subtitle_ids_used or declared_neighbor_ids != neighbor_subtitle_ids_used:
                        reason = "micro_expand_neighbor_lineage_mismatch"
                        break
                    if not _text(raw_segment.get("expansion_reason")):
                        reason = "micro_expand_reason_missing"
                        break
                    if not _as_bool(raw_segment.get("same_purchase_value")):
                        reason = "micro_expand_purchase_value_not_preserved"
                        break
                    if not _as_bool(raw_segment.get("no_new_purchase_value")):
                        reason = "micro_expand_introduces_new_purchase_value"
                        break
                elif uses_neighbor_words:
                    reason = "boundary_neighbor_words_require_micro_expand"
                    break
                used_word_ids.update(selected_word_ids)
                duration = round(end - start, 3)
                short_reason = _text(raw_segment.get("short_beat_reason")) or _text(raw_segment.get("short_complete_exception_reason"))
                if duration > MICRO_BEAT_MAX_SECONDS + 1e-6:
                    reason = "boundary_duration_exceeds_micro_beat_max"
                    break
                if duration < MICRO_BEAT_HARD_MIN_SECONDS - 1e-6:
                    reason = "boundary_duration_below_micro_beat_hard_min"
                    break
                child_key = _text(raw_segment.get("segment_key")) or chr(64 + index)
                child_id = beat_id if effective_decision != "SPLIT" else f"{beat_id}.{child_key}"
                selected_children.append({
                    "beat_id": child_id,
                    "source_beat_id": beat_id,
                    "boundary_decision": effective_decision,
                    "model_boundary_decision": decision,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "duration_seconds": duration,
                    "subtitle_ids": [int(item["subtitle_id"]) for item in word_lineage],
                    "start_word_offset": word_lineage[0]["start_word_offset"],
                    "end_word_offset": word_lineage[-1]["end_word_offset"],
                    "final_start_word_id": start_word_id,
                    "final_end_word_id": end_word_id,
                    "text": text,
                    "commercial_theme": _text(beat.get("commercial_theme")),
                    "purchase_value": _text(beat.get("purchase_value")),
                    "sub_outcome": _text(beat.get("sub_outcome")),
                    "evidence_function": _text(beat.get("evidence_function")),
                    "standalone_quality": _text(beat.get("standalone_quality")),
                    "why_this_is_a_new_beat": _text(beat.get("why_this_is_a_new_beat")),
                    "short_beat": duration < MICRO_BEAT_PREFERRED_MIN_SECONDS,
                    "short_beat_reason": short_reason,
                    "micro_expanded": effective_decision == "MICRO_EXPAND_AND_KEEP",
                    "expansion_reason": _text(raw_segment.get("expansion_reason")),
                    "neighbor_subtitle_ids_used": neighbor_subtitle_ids_used,
                    "same_purchase_value": _as_bool(raw_segment.get("same_purchase_value")),
                    "no_new_purchase_value": _as_bool(raw_segment.get("no_new_purchase_value")),
                    "boundary_quality": quality,
                    "trim_reason": _text(raw.get("trim_reason")),
                    "lineage_status": "resolved_frozen_source_word_exact",
                    "selection_authority": "AI_P0_5A_1_boundary_quality",
                })
        original_word_ids = [int(item["word_id"]) for item in original_words]
        if not reason and effective_decision == "KEEP":
            child = selected_children[0]
            if (
                int(child["final_start_word_id"]) != original_word_ids[0]
                or int(child["final_end_word_id"]) != original_word_ids[-1]
            ):
                reason = "boundary_keep_must_preserve_full_original_range"
        # The model occasionally labels a semantically valid unchanged range
        # as TRIM_AND_KEEP.  Exact source words—not that descriptive label—are
        # the invariant.  Treat it as a no-op boundary receipt rather than
        # permanently killing an otherwise valid Beat.
        if not reason and effective_decision == "MICRO_EXPAND_AND_KEEP":
            child = selected_children[0]
            if not child.get("micro_expanded"):
                reason = "micro_expand_not_applied"
        if reason:
            record = _boundary_rejected_record(beat, decision=decision, reason=reason, raw_decision=raw)
            rejected.append(record)
            audits.append(record)
            continue
        saved = round(max(0.0, _number(beat.get("duration_seconds")) - sum(
            _number(child.get("duration_seconds")) for child in selected_children
        )), 3)
        audit = {
            "beat_id": beat_id,
            "decision": effective_decision,
            "model_decision": decision,
            "decision_normalized_from_multi_segment_trim": effective_decision != decision,
            "original_start": beat.get("start"),
            "original_end": beat.get("end"),
            "original_duration_seconds": beat.get("duration_seconds"),
            "original_text": _text(beat.get("text")),
            "current_start_is_earliest_natural": _as_bool(raw.get("current_start_is_earliest_natural")),
            "current_end_is_latest_necessary": _as_bool(raw.get("current_end_is_latest_necessary")),
            "trim_reason": _text(raw.get("trim_reason")),
            "purchase_value": _text(beat.get("purchase_value")),
            "sub_outcome": _text(beat.get("sub_outcome")),
            "evidence_function": _text(beat.get("evidence_function")),
            "split_children": selected_children,
            "seconds_saved": saved,
            "lineage_status": "resolved_frozen_source_word_exact",
        }
        audits.append(audit)
        final_beats.extend(selected_children)
        if effective_decision == "TRIM_AND_KEEP" or saved > 0:
            trim_cases.append(audit)
        if effective_decision == "SPLIT":
            split_cases.append(audit)
    raw_seconds = round(sum(_number(item.get("duration_seconds")) for item in raw_beats), 3)
    final_seconds = round(sum(_number(item.get("duration_seconds")) for item in final_beats), 3)
    accepted_audits = [item for item in audits if isinstance(item.get("split_children"), list)]
    accepted_decision_counts = {
        name: sum(1 for item in accepted_audits if item.get("decision") == name)
        for name in _BOUNDARY_DECISIONS
    }
    explicit_ai_reject_count = sum(
        1 for item in rejected
        if item.get("decision") == "REJECT" and _text(item.get("reject_reason")) != ""
    )
    result = {
        "status": "publishable_inventory_completed",
        "raw_beats": len(raw_beats),
        "raw_seconds": raw_seconds,
        "boundary_decision_audit": audits,
        "publishable_beat_inventory": final_beats,
        "boundary_rejected_beats": rejected,
        "boundary_statistics": {
            "keep_count": accepted_decision_counts["KEEP"],
            "trim_and_keep_count": accepted_decision_counts["TRIM_AND_KEEP"],
            "micro_expand_count": accepted_decision_counts["MICRO_EXPAND_AND_KEEP"],
            "split_count": accepted_decision_counts["SPLIT"],
            "reject_count": len(rejected),
            "explicit_ai_reject_count": explicit_ai_reject_count,
            "contract_reject_count": len(rejected) - explicit_ai_reject_count,
            "final_beats": len(final_beats),
            "final_usable_seconds": final_seconds,
            "preferred_2_to_5_second_beat_count": sum(
                MICRO_BEAT_PREFERRED_MIN_SECONDS <= _number(item.get("duration_seconds")) <= MICRO_BEAT_PREFERRED_MAX_SECONDS
                for item in final_beats
            ),
            "five_to_8_second_beat_count": sum(
                MICRO_BEAT_PREFERRED_MAX_SECONDS < _number(item.get("duration_seconds")) <= MICRO_BEAT_MAX_SECONDS
                for item in final_beats
            ),
            "short_complete_exception_count": sum(
                _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS for item in final_beats
            ),
            "short_beats_1_to_2_seconds_count": sum(
                MICRO_BEAT_SHORT_BEAT_MIN_SECONDS <= _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS
                for item in final_beats
            ),
            "sub_one_second_beat_count": sum(
                MICRO_BEAT_HARD_MIN_SECONDS <= _number(item.get("duration_seconds")) < MICRO_BEAT_SHORT_BEAT_MIN_SECONDS
                for item in final_beats
            ),
            "word_trimmed_count": sum(1 for item in trim_cases if _number(item.get("seconds_saved")) > 0),
            "word_trimmed_seconds_saved": round(sum(_number(item.get("seconds_saved")) for item in trim_cases), 3),
        },
        "top_20_trimmed_cases": sorted(
            trim_cases,
            key=lambda item: (-_number(item.get("seconds_saved")), _text(item.get("beat_id"))),
        )[:20],
        "top_20_rejected_false_positives": sorted(
            rejected,
            key=lambda item: (-_number(item.get("original_duration_seconds")), _text(item.get("beat_id"))),
        )[:20],
        "split_cases": split_cases,
        "boundary_quality_summary": (
            dict(data.get("boundary_quality_summary"))
            if isinstance(data.get("boundary_quality_summary"), Mapping) else {}
        ),
        "contract": {
            "stage": P05_MICRO_BEAT_BOUNDARY_QUALITY_STAGE,
            "frozen_raw_beat_candidates_only": True,
            "complete_srt_rescanned_for_discovery": False,
            "commercial_annotations_frozen": True,
            "semantic_boundary_authority": "AI",
            "program_authority": "word_lineage_continuity_hard_floor_duration_frozen_safety_no_semantic_auto_repair",
            "short_beat_contract": {
                "preferred_seconds": [MICRO_BEAT_PREFERRED_MIN_SECONDS, MICRO_BEAT_PREFERRED_MAX_SECONDS],
                "hard_min_seconds": MICRO_BEAT_HARD_MIN_SECONDS,
                "hard_max_seconds": MICRO_BEAT_MAX_SECONDS,
                "short_beat_reason_is_advisory": True,
                "micro_expand_neighbor_limit": MICRO_BEAT_BOUNDARY_NEIGHBOR_SUBTITLES,
            },
            "arc_assembly_performed": False,
            "dense_composition_performed": False,
            "m3_invoked": False,
        },
    }
    return result


def build_micro_beat_publishable_adjudication_prompt(
    *,
    boundary_beat_inventory: Sequence[Mapping[str, Any]],
    calibration_expectations: Mapping[str, Mapping[str, Any]] | None = None,
) -> str:
    """Build the single P0.5A.2 publishability judgment over frozen Beats."""
    rows = []
    for item in boundary_beat_inventory:
        beat_id = _text(item.get("beat_id"))
        row = {
            "beat_id": beat_id,
            "source_beat_id": _text(item.get("source_beat_id")),
            "duration_seconds": item.get("duration_seconds"),
            "short_beat": bool(item.get("short_beat")),
            "micro_expanded": bool(item.get("micro_expanded")),
            "final_text": _text(item.get("text")),
            "commercial_theme": _text(item.get("commercial_theme")),
            "purchase_value": _text(item.get("purchase_value")),
            "evidence_function": _text(item.get("evidence_function")),
        }
        if calibration_expectations and isinstance(calibration_expectations.get(beat_id), Mapping):
            expected = calibration_expectations[beat_id]
            row["human_calibration_fixture"] = {
                "expected_status": _text(expected.get("expected_status")),
                "expected_narrative_priority": _text(expected.get("expected_narrative_priority")),
                "rationale": _text(expected.get("note")),
            }
        rows.append(row)
    schema = {
        "publishable_beat_adjudications": [{
            "beat_id": "B001",
            "publishability_status": "publishable_clean",
            "semantic_subject_resolved": True,
            "semantic_predicate_resolved": True,
            "commercial_result_resolved": True,
            "no_dangling_dependency": True,
            "visual_dependency": "none",
            "role_permissions": ["hook", "core", "proof", "support"],
            "context_requirement": "standalone",
            "narrative_priority": "high",
            "reason": "独立口语信息完整",
        }],
        "summary": {
            "all_boundary_beats_reviewed": True,
            "one_principal_adjudication_only": True,
            "reason": "",
        },
    }
    return "\n\n".join((
        "你只做 P0.5A.2 Publishable Beat Adjudication Calibration。输入都是已经完成词级边界裁剪、lineage 和安全校验的冻结 Micro Beat。不能重扫 SRT、不能重新裁边界、不能增加/删除词、不能重写、不能找新 Beat、不能组 Arc，也不能按商业标签自动选入故事。",
        "这是每条 Beat 的一次主判，不是多轮只可否决门。你必须为每个 beat_id 返回且只返回一个 publishability_status：publishable_clean、publishable_visual 或 reject。商业优先级另用 narrative_priority=high/medium/low 表达；publishable 不等于应该进入最终商品故事。",
        "publishable_clean：观众只听这句也能自然理解，商业对象/动作或机制/结果已经落地；它是自然真人口语，不要求书面作文语法。句末是逗号、SRT 在此断行、口语省略主语，都不能单独成为 reject 理由。比如“整个把你的肉全部藏在了这个马甲一样的形状里，”“你的两边拜拜肉全部藏在了这个大网纱里面，”“它的肩是全部做这种花边工艺的，把你的肩往里挖，”都有明确的商品机制和结果，应按语义判断，不得因逗号或口语式表达误杀。",
        "publishable_visual：这句本身有明确商业价值和可理解的核心意思，但需要当前视频画面才完全落地，例如“从这儿到这儿”、指向展示、揉搓/转身演示。它保留进视频 Beat 库，visual_dependency 必须为 required；不能因为依赖画面就判成废话。视觉依赖本身不等于 dangling dependency。",
        "reject：只用于真正不可发布的情况，例如 ASR 明显错乱、真正半句或未完成谓语、没有画面也没有可恢复意义的直播回应/悬空指代、直播昵称互动、个人尺码问答、价格/促单/安全不确定内容。不要根据你猜测的原意替 ASR 改字：原话中出现“各肢窝”“斜方系肩”“抗起球是的”“都就相当于是”“超爽足节”这类明显错词或错序，应 reject；“这个面料是”“自带3到5厘米的销售”“你看”这类没有独立商业信息的残句也应 reject。针对某一位观众的孕期/肚子月份/身材回应，即使大意可懂，也不作为可发布商品 Beat。",
        "本次必须按字幕原字逐字判，不得把直播口头杂音静默补成正常话：出现“是的/嗯哦/好牛”等回应词嵌进谓语导致语序断裂，或把不明词自行理解成“显瘦机制”，都属于 ASR/直播残留，应 reject。举例：把“抗起球是的，随便揉搓”解释为抗起球、把“直角尖的感觉”解释为版型、把“胸围不大边”解释为胸围不显大，都是禁止的脑补。",
        "以下同样必须 reject，不能因 Journey context 或 narrative_priority 洗白：针对个人的体重/身高/孕期月份建议；下一件/明天新款等其它商品预告；“自己看/你看”开头且依赖展示或夹入直播回应、无法独听闭合的句子；以“门襟的这根带子”“是的，就很干爽”之类未收束口语收尾的句子。若画面能补足明确商业信息，可只判 publishable_visual；但个人尺码回应、其它商品预告、ASR 错乱无论有无画面都 reject。",
        "不要用结果合理来掩盖直播残留：对象不明的“我就怕这种他这么瘦”、无落点的“应该知道……”，以及孤立的“是的，就很干爽”都必须检查是否有可单独成立的主语、谓语和商业结果；没有就 reject。相反，带完整后半句结果的自然口语可以保留。对于“搓搓自己感受”“你看用料”“从这儿到这儿”等展示动作，应优先考虑 publishable_visual，而非偷改成 clean 或直接 reject。",
        "narrative_priority 只描述未来 Arc 的候选价值，不能借此把可发布句子判为 reject，也不能把低购买价值句子拔高。比如“我宁可劝你别买”可以是 publishable_clean，但它不是商品叙事主句，应为 low。",
        "若某行附有 human_calibration_fixture，它是本轮人工确认的 Golden Beat 验收准则：必须按其 expected_status 输出，并在提供 expected_narrative_priority 时按该优先级输出。它只校准本轮判断标准，不会新增、替换或自动选入任何候选；其余字段仍需如实填写。",
        "短 Beat 规则：2–5 秒只是偏好，0.8–2 秒不因时长 reject。若它在已建立的商品/Journey 语境中带来明确的新 proof、support、expanded result 或 mechanism（如“后背也显薄”“颈线会拉长”“全松紧”“不起球”），可判 publishable_clean + context_requirement=journey_context_ok。短 Beat 不是 Hook；不得因为其短就把它拔高为独立开场。真正碎片如“就是这个”“所以它”“从这里”仍 reject。短句的放宽只适用于清楚的新购买认知，绝不适用于残句、个人回应、其它商品或 ASR 乱码。",
        "必须返回 role_permissions：从 hook/core/proof/support 中选择这条真实 Beat 可承担的角色。hook 仅限 publishable_clean、context_requirement=standalone、独立强成立的 2 秒及以上句；不得因商业价值高而给短句或视觉句 hook。0.8–2 秒 clean Beat 通常只可 proof/support。publishable_visual 只能 proof/support，绝不能 hook/core；其 context_requirement 必为 visual_required。其余 clean Beat 可为 standalone 或 journey_context_ok：后者可 core/proof/support，但不能 hook。程序只验证这些权限与状态的一致性，不会按权限自动挑选候选。",
        "字段含义：semantic_subject_resolved、semantic_predicate_resolved、commercial_result_resolved 只按语义判断；no_dangling_dependency 指不存在无法由成片视频解决的上下文依赖。对于 publishable_clean，四个语义字段均为 true 且 visual_dependency=none；对于 publishable_visual，前三项和 no_dangling_dependency 均为 true 且 visual_dependency=required；reject 可将不成立的字段设为 false。",
        "每个 beat_id 必须恰好一条。reason 使用4–24个汉字，说明语义或拒绝原因。返回严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "待校准冻结 Beat：\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def _is_declared_bool(value: Any) -> bool:
    return isinstance(value, bool)


def apply_micro_beat_publishable_adjudication(
    *, boundary_result: Mapping[str, Any], data: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one AI-owned three-state publishability decision without semantic repair."""
    base_beats = _as_mapping_list(boundary_result.get("publishable_beat_inventory"))
    evaluations = _as_mapping_list(data.get("publishable_beat_adjudications"))
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in evaluations:
        beat_id = _text(item.get("beat_id"))
        if not beat_id:
            continue
        if beat_id in by_id:
            duplicate_ids.add(beat_id)
        by_id[beat_id] = item

    clean: list[dict[str, Any]] = []
    visual: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for beat in base_beats:
        beat_id = _text(beat.get("beat_id"))
        evaluation = by_id.get(beat_id)
        status = _text((evaluation or {}).get("publishability_status"))
        priority = _text((evaluation or {}).get("narrative_priority"))
        visual_dependency = _text((evaluation or {}).get("visual_dependency"))
        role_permissions = tuple(_text(item).lower() for item in (evaluation or {}).get("role_permissions") or () if _text(item))
        context_requirement = _text((evaluation or {}).get("context_requirement"))
        duration = _number(beat.get("duration_seconds"))
        required_flags = (
            "semantic_subject_resolved", "semantic_predicate_resolved",
            "commercial_result_resolved", "no_dangling_dependency",
        )
        reason = ""
        if beat_id in duplicate_ids:
            reason = "publishable_adjudication_duplicate"
        elif evaluation is None:
            reason = "publishable_adjudication_missing"
        elif status not in _MICRO_BEAT_PUBLISHABILITY_STATUSES:
            reason = "publishability_status_invalid"
        elif priority not in _MICRO_BEAT_NARRATIVE_PRIORITIES:
            reason = "narrative_priority_invalid"
        elif status != "reject" and (not role_permissions or len(set(role_permissions)) != len(role_permissions)):
            reason = "role_permissions_missing_or_duplicate"
        elif status != "reject" and any(item not in _MICRO_BEAT_ROLE_PERMISSIONS for item in role_permissions):
            reason = "role_permissions_invalid"
        elif status != "reject" and context_requirement not in _MICRO_BEAT_CONTEXT_REQUIREMENTS:
            reason = "context_requirement_invalid"
        elif any(not _is_declared_bool(evaluation.get(flag)) for flag in required_flags):
            reason = "semantic_resolution_flags_invalid"
        elif status == "publishable_clean" and (
            visual_dependency != "none" or not all(evaluation.get(flag) is True for flag in required_flags)
        ):
            reason = "clean_status_contract_invalid"
        elif status == "publishable_visual" and (
            visual_dependency != "required" or not all(evaluation.get(flag) is True for flag in required_flags)
        ):
            reason = "visual_status_contract_invalid"
        elif status == "publishable_clean" and context_requirement == "visual_required":
            reason = "clean_context_requirement_invalid"
        elif status == "publishable_visual" and (
            context_requirement != "visual_required" or bool(set(role_permissions).intersection({"hook", "core"}))
        ):
            reason = "visual_role_permissions_invalid"
        elif "hook" in role_permissions and (
            status != "publishable_clean"
            or context_requirement != "standalone"
            or duration < MICRO_BEAT_PREFERRED_MIN_SECONDS - 1e-6
        ):
            reason = "hook_role_permission_invalid"
        elif duration < MICRO_BEAT_PREFERRED_MIN_SECONDS - 1e-6 and bool(
            set(role_permissions).intersection({"hook", "core"})
        ):
            reason = "short_beat_role_permission_invalid"
        elif status == "reject":
            reason = _text(evaluation.get("reason")) or "AI_publishability_reject"

        if not reason:
            checked = dict(beat)
            checked.update({
                "publishability_status": status,
                "visual_dependency": visual_dependency,
                "role_permissions": list(role_permissions),
                "context_requirement": context_requirement,
                "audio_only_eligible": status == "publishable_clean",
                "hook_eligible": (
                    status == "publishable_clean"
                    and "hook" in role_permissions
                    and context_requirement == "standalone"
                    and duration >= MICRO_BEAT_PREFERRED_MIN_SECONDS - 1e-6
                ),
                "arc_eligible": True,
                "narrative_priority": priority,
                "p0_5a2_publishable_adjudication": {
                    "publishability_status": status,
                    "semantic_subject_resolved": evaluation["semantic_subject_resolved"],
                    "semantic_predicate_resolved": evaluation["semantic_predicate_resolved"],
                    "commercial_result_resolved": evaluation["commercial_result_resolved"],
                    "no_dangling_dependency": evaluation["no_dangling_dependency"],
                    "visual_dependency": visual_dependency,
                    "role_permissions": list(role_permissions),
                    "context_requirement": context_requirement,
                    "narrative_priority": priority,
                    "reason": _text(evaluation.get("reason")),
                },
            })
            (clean if status == "publishable_clean" else visual).append(checked)
            continue

        rejected.append({
            "beat_id": beat_id,
            "source_beat_id": _text(beat.get("source_beat_id")),
            "decision": _text(beat.get("boundary_decision")),
            "original_text": _text(beat.get("text")),
            "original_duration_seconds": beat.get("duration_seconds"),
            "reject_reason": reason,
            "ai_publishable_adjudication": dict(evaluation or {}),
            "lineage_status": _text(beat.get("lineage_status")),
        })

    retained = clean + visual
    result = dict(boundary_result)
    result.update({
        "status": "publishable_inventory_calibrated",
        "publishable_beat_inventory": retained,
        "publishable_clean_beat_inventory": clean,
        "publishable_visual_beat_inventory": visual,
        "p0_5a2_publishable_adjudication_rejected_beats": rejected,
        "publishability_adjudication_summary": (
            dict(data.get("summary")) if isinstance(data.get("summary"), Mapping) else {}
        ),
    })
    stats = dict(boundary_result.get("boundary_statistics") or {})
    stats.update({
        "final_beats": len(retained),
        "final_usable_seconds": round(sum(_number(item.get("duration_seconds")) for item in retained), 3),
        "publishable_clean_count": len(clean),
        "publishable_clean_seconds": round(sum(_number(item.get("duration_seconds")) for item in clean), 3),
        "publishable_visual_count": len(visual),
        "publishable_visual_seconds": round(sum(_number(item.get("duration_seconds")) for item in visual), 3),
        "p0_5a2_publishable_adjudication_reject_count": len(rejected),
        "p0_5a2_source_boundary_beat_count": len(base_beats),
        "short_beats_1_to_2_seconds_count": sum(
            MICRO_BEAT_SHORT_BEAT_MIN_SECONDS <= _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS
            for item in retained
        ),
        "micro_expanded_count": sum(bool(item.get("micro_expanded")) for item in retained),
    })
    result["boundary_statistics"] = stats
    result["contract"] = dict(boundary_result.get("contract") or {}) | {
        "stage": P05_MICRO_BEAT_ADJUDICATION_CALIBRATION_STAGE,
        "frozen_boundary_snapshot_only": True,
        "complete_srt_rescanned_for_discovery": False,
        "word_boundary_recomputed": False,
        "principal_ai_publishability_adjudication_count": 1,
        "publishability_semantic_authority": "AI_single_principal_adjudication",
        "program_authority": "status_schema_lineage_duration_safety_no_semantic_auto_selection",
        "narrative_priority_is_selection": False,
        "arc_assembly_performed": False,
        "dense_composition_performed": False,
        "m3_invoked": False,
    }
    return result


def adjudicate_micro_beat_publishability(
    *,
    boundary_result: Mapping[str, Any],
    api_key: str,
    base_url: str,
    model: str,
    response_hook: Callable[[str, str], None] | None = None,
    calibration_expectations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run exactly one principal three-state adjudication over frozen boundary Beats."""
    base_beats = _as_mapping_list(boundary_result.get("publishable_beat_inventory"))
    if not base_beats:
        empty = dict(boundary_result)
        empty.update({
            "status": "publishable_inventory_unavailable",
            "errors": ["frozen_boundary_beat_inventory_empty"],
        })
        return empty
    batches = [
        base_beats[index:index + MICRO_BEAT_PUBLISHABLE_ADJUDICATION_BATCH_SIZE]
        for index in range(0, len(base_beats), MICRO_BEAT_PUBLISHABLE_ADJUDICATION_BATCH_SIZE)
    ]
    evaluations: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for index, beat_batch in enumerate(batches, start=1):
        batch_id = f"PA{index:02d}"
        response = _post_lite_request(
            api_key=api_key,
            base_url=base_url,
            model=model,
            prompt=build_micro_beat_publishable_adjudication_prompt(
                boundary_beat_inventory=beat_batch,
                calibration_expectations=calibration_expectations,
            ),
            stage=P05_MICRO_BEAT_ADJUDICATION_CALIBRATION_STAGE,
            max_tokens=4800,
        )
        content = _text(response.get("choices", [{}])[0].get("message", {}).get("content"))
        if response_hook and content:
            response_hook(batch_id, content)
        try:
            data = _extract_json(content) if content else None
        except (RuntimeError, ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            failed = dict(boundary_result)
            failed.update({
                "status": "publishable_adjudication_response_invalid",
                "errors": [f"micro_beat_publishable_adjudication_response_invalid:{batch_id}"],
                "publishability_adjudication_batches": audit + [{
                    "batch_id": batch_id,
                    "beat_ids": [_text(item.get("beat_id")) for item in beat_batch],
                    "status": "response_invalid",
                }],
            })
            return failed
        evaluations.extend(_as_mapping_list(data.get("publishable_beat_adjudications")))
        audit.append({
            "batch_id": batch_id,
            "beat_ids": [_text(item.get("beat_id")) for item in beat_batch],
            "status": "reviewed",
            "raw_response_present": bool(content),
        })
    result = apply_micro_beat_publishable_adjudication(
        boundary_result=boundary_result,
        data={"publishable_beat_adjudications": evaluations},
    )
    result["publishability_adjudication_batches"] = audit
    return result


def build_micro_beat_boundary_adjudication_prompt(
    *, publishable_beat_inventory: Sequence[Mapping[str, Any]],
) -> str:
    """A final P0.2 pass that can reject a Boundary decision, never repair it."""
    rows = [{
        "beat_id": _text(item.get("beat_id")),
        "source_beat_id": _text(item.get("source_beat_id")),
        "duration_seconds": item.get("duration_seconds"),
        "final_text": _text(item.get("text")),
    } for item in publishable_beat_inventory]
    schema = {
        "final_utterance_adjudications": [{
            "beat_id": "B001",
            "publishable": True,
            "reason": "独立完整自然",
        }],
        "summary": {"all_final_beats_reviewed": True, "reason": ""},
    }
    return "\n\n".join((
        "你只做 P0.5A.1 已完成词级边界后的最终口播复核。这是冻结 P0.2 Final Utterance Quality 的纯否决门：不能增加、删除词、裁剪、替换、重写、重排、找新 Beat 或做 Arc。你只能对每个给定 beat_id 回答 publishable=true 或 false。",
        "判断方式：假设观众在没有前后直播、没有画面解释的情况下，只听这一句。它必须是可以直接发布的真人口播：起止自然、对象和结论明确、句子闭合、没有直播回应残留、没有悬空指代、没有 ASR 怪句。商业价值、时长、购买标签都不构成通过理由。宁可 false，也不能让一句“似乎大概能懂”的残句通过。",
        "以下一律 false：名词短语或未完成谓语（如“32支超爽的一个竹节麻纱线”“这个面料是”）；残留或病句（如“一拉开160斤，轻轻松松”“热热啥呀”“都就相当于是”“不挑鞋子的，不挑的，随便穿”）；依赖现场展示/对象不明（如“手放这都能看到”“把肩从这儿延到这儿”）；没有前文就无所指的回应或因果（如孤立的“是的，就很干爽”“因为他要做里衬的”）；直播式个人身高/体重/孕期回应（公开完整尺码表可通过，但“你个子高高的，165斤应该OK”不可）；ASR 异常词（如“各肢窝”“斜方系肩”）；逗号后未收束的口播。自然口语可以保留，不要把“你看/其实/真的”等词机械判错；核心是该句独听时信息是否真正落地。",
        "每个 beat_id 必须恰好一条。reason 使用4–20个汉字。返回严格 JSON，不要 Markdown。结构：\n" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
        "待复核最终口播：\n" + json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
    ))


def apply_micro_beat_boundary_adjudication(
    *, boundary_result: Mapping[str, Any], data: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply an AI-only P0.2 veto; it never substitutes a semantic Beat."""
    base_beats = _as_mapping_list(boundary_result.get("publishable_beat_inventory"))
    evaluations = _as_mapping_list(data.get("final_utterance_adjudications"))
    by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in evaluations:
        beat_id = _text(item.get("beat_id"))
        if not beat_id:
            continue
        if beat_id in by_id:
            duplicate_ids.add(beat_id)
        by_id[beat_id] = item
    retained: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for beat in base_beats:
        beat_id = _text(beat.get("beat_id"))
        evaluation = by_id.get(beat_id)
        if beat_id in duplicate_ids:
            reason = "final_utterance_adjudication_duplicate"
        elif evaluation is None:
            reason = "final_utterance_adjudication_missing"
        elif not _as_bool(evaluation.get("publishable")):
            reason = _text(evaluation.get("reason")) or "AI_final_utterance_not_publishable"
        else:
            checked = dict(beat)
            checked["p0_2_final_utterance_adjudication"] = {
                "publishable": True,
                "reason": _text(evaluation.get("reason")),
            }
            retained.append(checked)
            continue
        rejected.append({
            "beat_id": beat_id,
            "source_beat_id": _text(beat.get("source_beat_id")),
            "decision": _text(beat.get("boundary_decision")),
            "original_text": _text(beat.get("text")),
            "original_duration_seconds": beat.get("duration_seconds"),
            "reject_reason": reason,
            "ai_final_utterance_adjudication": dict(evaluation or {}),
            "lineage_status": _text(beat.get("lineage_status")),
        })
    result = dict(boundary_result)
    all_rejected = _as_mapping_list(boundary_result.get("boundary_rejected_beats")) + rejected
    previous_adjudication_rejected = _as_mapping_list(
        boundary_result.get("p0_2_final_utterance_adjudication_rejected_beats")
    )
    result.update({
        "publishable_beat_inventory": retained,
        "boundary_rejected_beats": all_rejected,
        "p0_2_final_utterance_adjudication_rejected_beats": previous_adjudication_rejected + rejected,
        "boundary_quality_summary": {
            "boundary": dict(boundary_result.get("boundary_quality_summary") or {}),
            "p0_2_final_utterance_adjudication": (
                dict(data.get("summary")) if isinstance(data.get("summary"), Mapping) else {}
            ),
        },
    })
    stats = dict(boundary_result.get("boundary_statistics") or {})
    stats.update({
        "final_beats": len(retained),
        "final_usable_seconds": round(sum(_number(item.get("duration_seconds")) for item in retained), 3),
        "preferred_2_to_5_second_beat_count": sum(
            MICRO_BEAT_PREFERRED_MIN_SECONDS <= _number(item.get("duration_seconds")) <= MICRO_BEAT_PREFERRED_MAX_SECONDS
            for item in retained
        ),
        "five_to_8_second_beat_count": sum(
            MICRO_BEAT_PREFERRED_MAX_SECONDS < _number(item.get("duration_seconds")) <= MICRO_BEAT_MAX_SECONDS
            for item in retained
        ),
        "short_complete_exception_count": sum(
            _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS for item in retained
        ),
        "reject_count": len(all_rejected),
        "p0_2_final_utterance_adjudication_reject_count": len(previous_adjudication_rejected) + len(rejected),
    })
    result["boundary_statistics"] = stats
    retained_ids = {_text(item.get("beat_id")) for item in retained}
    result["retained_top_20_trimmed_cases"] = [
        item for item in _as_mapping_list(boundary_result.get("top_20_trimmed_cases"))
        if any(_text(child.get("beat_id")) in retained_ids for child in _as_mapping_list(item.get("split_children")))
    ][:20]
    result["top_20_rejected_false_positives"] = sorted(
        all_rejected,
        key=lambda item: (-_number(item.get("original_duration_seconds")), _text(item.get("beat_id"))),
    )[:20]
    result["contract"] = dict(boundary_result.get("contract") or {}) | {
        "p0_2_final_utterance_adjudication_applied": True,
        "final_utterance_adjudication_authority": "AI_P0_2_final_utterance_quality_veto_only",
    }
    return result


def adjudicate_micro_beat_final_utterances(
    *,
    boundary_result: Mapping[str, Any],
    api_key: str,
    base_url: str,
    model: str,
    response_hook: Callable[[str, str], None] | None = None,
    pass_index: int = 1,
) -> dict[str, Any]:
    """Run only the frozen P0.2 veto over fixed, already word-bound Beats."""
    base_beats = _as_mapping_list(boundary_result.get("publishable_beat_inventory"))
    if not base_beats:
        empty = dict(boundary_result)
        empty["contract"] = dict(empty.get("contract") or {}) | {
            "p0_2_final_utterance_adjudication_applied": False,
            "arc_assembly_performed": False,
            "dense_composition_performed": False,
            "m3_invoked": False,
        }
        return empty
    batches = [
        base_beats[index:index + MICRO_BEAT_BOUNDARY_ADJUDICATION_BATCH_SIZE]
        for index in range(0, len(base_beats), MICRO_BEAT_BOUNDARY_ADJUDICATION_BATCH_SIZE)
    ]
    evaluations: list[dict[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for index, beat_batch in enumerate(batches, start=1):
        batch_id = f"BA{pass_index}_{index:02d}"
        response = _post_lite_request(
            api_key=api_key,
            base_url=base_url,
            model=model,
            prompt=build_micro_beat_boundary_adjudication_prompt(publishable_beat_inventory=beat_batch),
            stage=P05_MICRO_BEAT_BOUNDARY_QUALITY_STAGE,
            max_tokens=3600,
        )
        content = _text(response.get("choices", [{}])[0].get("message", {}).get("content"))
        if response_hook and content:
            response_hook(batch_id, content)
        try:
            data = _extract_json(content) if content else None
        except (RuntimeError, ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            failed = dict(boundary_result)
            failed.update({
                "status": "publishable_inventory_adjudication_response_invalid",
                "errors": [f"micro_beat_final_utterance_adjudication_response_invalid:{batch_id}"],
                "final_utterance_adjudication_batches": audit + [{
                    "batch_id": batch_id,
                    "beat_ids": [item["beat_id"] for item in beat_batch],
                    "status": "response_invalid",
                }],
            })
            failed["contract"] = dict(failed.get("contract") or {}) | {
                "p0_2_final_utterance_adjudication_applied": False,
                "arc_assembly_performed": False,
                "dense_composition_performed": False,
                "m3_invoked": False,
            }
            return failed
        evaluations.extend(_as_mapping_list(data.get("final_utterance_adjudications")))
        audit.append({
            "batch_id": batch_id,
            "pass_index": pass_index,
            "beat_ids": [item["beat_id"] for item in beat_batch],
            "status": "reviewed",
            "raw_response_present": bool(content),
        })
    result = apply_micro_beat_boundary_adjudication(
        boundary_result=boundary_result,
        data={"final_utterance_adjudications": evaluations},
    )
    result["final_utterance_adjudication_batches"] = _as_mapping_list(
        boundary_result.get("final_utterance_adjudication_batches")
    ) + audit
    return result


def refine_micro_beat_boundaries(
    *,
    frozen_inventory: Sequence[Mapping[str, Any]],
    source_rows: Sequence[Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    response_hook: Callable[[str, str], None] | None = None,
    adjudication_response_hook: Callable[[str, str], None] | None = None,
    apply_legacy_final_utterance_adjudication: bool = True,
) -> dict[str, Any]:
    """Run P0.5A.1 only over a fixed inventory; no source rediscovery occurs."""
    raw_beats = _as_mapping_list(frozen_inventory)
    source_rows_by_id = {int(row["subtitle_id"]): dict(row) for row in source_rows}
    if not raw_beats or not source_rows_by_id:
        return {
            "status": "publishable_inventory_unavailable",
            "errors": ["frozen_inventory_or_source_rows_unavailable"],
            "publishable_beat_inventory": [],
        }
    batches = [
        raw_beats[index:index + MICRO_BEAT_BOUNDARY_QUALITY_BATCH_SIZE]
        for index in range(0, len(raw_beats), MICRO_BEAT_BOUNDARY_QUALITY_BATCH_SIZE)
    ]
    combined_decisions: list[dict[str, Any]] = []
    batch_audit: list[dict[str, Any]] = []
    for index, beat_batch in enumerate(batches, start=1):
        batch_id = f"BQ{index:02d}"
        response = _post_lite_request(
            api_key=api_key,
            base_url=base_url,
            model=model,
            prompt=build_micro_beat_boundary_quality_prompt(
                beat_inventory=beat_batch,
                source_rows_by_id=source_rows_by_id,
            ),
            stage=P05_MICRO_BEAT_BOUNDARY_QUALITY_STAGE,
            max_tokens=7200,
        )
        content = _text(response.get("choices", [{}])[0].get("message", {}).get("content"))
        if response_hook and content:
            response_hook(batch_id, content)
        try:
            data = _extract_json(content) if content else None
        except (RuntimeError, ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            return {
                "status": "publishable_inventory_quality_response_invalid",
                "errors": [f"micro_beat_boundary_quality_response_empty_or_invalid:{batch_id}"],
                "boundary_quality_batches": batch_audit + [{
                    "batch_id": batch_id,
                    "beat_ids": [item["beat_id"] for item in beat_batch],
                    "status": "response_invalid",
                }],
                "publishable_beat_inventory": [],
                "contract": {
                    "stage": P05_MICRO_BEAT_BOUNDARY_QUALITY_STAGE,
                    "arc_assembly_performed": False,
                    "dense_composition_performed": False,
                    "m3_invoked": False,
                },
            }
        parsed = parse_micro_beat_boundary_quality(
            frozen_inventory=beat_batch,
            source_rows=source_rows,
            data=data,
        )
        combined_decisions.extend(_as_mapping_list(data.get("boundary_decisions")))
        batch_audit.append({
            "batch_id": batch_id,
            "beat_ids": [item["beat_id"] for item in beat_batch],
            "status": "reviewed",
            "raw_response_present": bool(content),
            "publishable_beat_count": len(parsed["publishable_beat_inventory"]),
            "rejected_beat_count": len(parsed["boundary_rejected_beats"]),
        })
    result = parse_micro_beat_boundary_quality(
        frozen_inventory=raw_beats,
        source_rows=source_rows,
        data={"boundary_decisions": combined_decisions},
    )
    result["boundary_quality_batches"] = batch_audit
    result["contract"] = dict(result["contract"]) | {
        "boundary_quality_batch_size": MICRO_BEAT_BOUNDARY_QUALITY_BATCH_SIZE,
        "boundary_quality_applied": True,
    }
    # P0.5A.2 replaces the old P0.2 four-pass veto cascade with one
    # calibrated, three-state principal adjudication.  Keep the historical
    # default for the standalone P0.5A.1 runner, but let Narrative Mode stop
    # here so it can feed this frozen boundary inventory to P0.5A.2.
    if not apply_legacy_final_utterance_adjudication:
        result["contract"] = dict(result["contract"]) | {
            "p0_2_final_utterance_adjudication_applied": False,
            "legacy_final_utterance_veto_bypassed_for_p0_5a2": True,
        }
        return result
    adjudicated = result
    for pass_index in range(1, MICRO_BEAT_FINAL_UTTERANCE_ADJUDICATION_PASSES + 1):
        adjudicated = adjudicate_micro_beat_final_utterances(
            boundary_result=adjudicated,
            api_key=api_key,
            base_url=base_url,
            model=model,
            response_hook=adjudication_response_hook,
            pass_index=pass_index,
        )
        if adjudicated.get("status") != "publishable_inventory_completed":
            return adjudicated
    adjudicated["contract"] = dict(adjudicated.get("contract") or {}) | {
        "required_ai_final_utterance_adjudication_passes": MICRO_BEAT_FINAL_UTTERANCE_ADJUDICATION_PASSES,
    }
    return adjudicated


def mine_micro_beat_inventory(
    *,
    source_units: Sequence[Mapping[str, Any]],
    hard_safe_subtitle_ids: Sequence[int],
    word_timeline: SemanticSrtWordTimeline,
    api_key: str,
    base_url: str,
    model: str,
    response_hook: Callable[[str, str], None] | None = None,
    quality_response_hook: Callable[[str, str], None] | None = None,
    apply_static_p02_quality: bool = True,
    apply_legacy_p02_quality: bool = True,
) -> dict[str, Any]:
    """Mine every deterministic source window, then P0.2-audit every Beat."""
    source_rows = build_micro_beat_source_rows(
        source_units=source_units,
        hard_safe_subtitle_ids=hard_safe_subtitle_ids,
        word_timeline=word_timeline,
    )
    if not source_rows:
        return {
            "status": "inventory_unavailable",
            "errors": ["source_rows_unavailable"],
            "beat_inventory": [],
        }
    source_batches = build_micro_beat_source_batches(source_rows)
    mined_beats: list[dict[str, Any]] = []
    contract_rejected: list[dict[str, Any]] = []
    ai_rejected: list[dict[str, Any]] = []
    mining_quality: list[dict[str, Any]] = []
    mining_batches: list[dict[str, Any]] = []
    next_beat_id = 1
    for batch in source_batches:
        batch_id = _text(batch["batch_id"])
        response = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model,
            prompt=build_micro_beat_inventory_prompt(
                source_rows=batch["source_rows"], batch_id=batch_id,
                total_batch_count=len(source_batches),
            ),
            stage=P05_MICRO_BEAT_INVENTORY_STAGE, max_tokens=9000,
        )
        content = _text(response.get("choices", [{}])[0].get("message", {}).get("content"))
        if response_hook and content:
            response_hook(batch_id, content)
        try:
            data = _extract_json(content) if content else None
        except (RuntimeError, ValueError, TypeError):
            data = None
        if not isinstance(data, Mapping):
            mining_batches.append({
                "batch_id": batch_id, "start": batch["start"], "end": batch["end"],
                "source_subtitle_count": len(batch["source_rows"]),
                "status": "response_invalid",
            })
            return {
                "status": "inventory_response_invalid",
                "errors": [f"micro_beat_inventory_response_empty_or_invalid:{batch_id}"],
                "total_srt_duration_seconds": round(float(source_rows[-1]["end"]) - float(source_rows[0]["start"]), 3),
                "total_source_subtitles": len(source_rows),
                "mining_batches": mining_batches,
                "beat_inventory": [],
            }
        batch_result = parse_micro_beat_inventory(
            data=data,
            source_rows=batch["source_rows"],
            beat_id_start=next_beat_id,
            apply_static_p02_quality=apply_static_p02_quality,
        )
        next_beat_id += len(batch_result["beat_inventory"])
        mined_beats.extend(batch_result["beat_inventory"])
        contract_rejected.extend(batch_result["contract_rejected_declared_beats"])
        ai_rejected.extend(batch_result["ai_rejected_important_segments"])
        mining_quality.append({
            "batch_id": batch_id,
            **dict(batch_result.get("inventory_quality") or {}),
        })
        mining_batches.append({
            "batch_id": batch_id, "start": batch["start"], "end": batch["end"],
            "source_subtitle_count": len(batch["source_rows"]),
            "status": "mined",
            "declared_accepted_beat_count": len(batch_result["beat_inventory"]),
            "contract_rejected_declared_beat_count": len(batch_result["contract_rejected_declared_beats"]),
            "raw_response_present": bool(content),
        })
    result = {
        "status": "inventory_mined_pending_p0_2_quality",
        "total_srt_duration_seconds": round(float(source_rows[-1]["end"]) - float(source_rows[0]["start"]), 3),
        "total_source_subtitles": len(source_rows),
        "total_hard_safe_materializable_subtitles": sum(
            bool(item.get("hard_safe")) and bool(item.get("materializable")) for item in source_rows
        ),
        "total_micro_beats": len(mined_beats),
        "total_usable_beat_seconds": round(sum(_number(item.get("duration_seconds")) for item in mined_beats), 3),
        "preferred_2_to_5_second_beat_count": sum(
            MICRO_BEAT_PREFERRED_MIN_SECONDS <= _number(item.get("duration_seconds")) <= MICRO_BEAT_PREFERRED_MAX_SECONDS
            for item in mined_beats
        ),
        "short_complete_exception_count": sum(
            _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS for item in mined_beats
        ),
        "beat_inventory": mined_beats,
        "contract_rejected_declared_beats": contract_rejected,
        "ai_rejected_important_segments": ai_rejected,
        "inventory_quality": {"mining_batches": mining_quality},
        "mining_batches": mining_batches,
        "contract": {
            "stage": P05_MICRO_BEAT_INVENTORY_STAGE,
            "complete_srt_visible_to_ai": True,
            "source_batching": "deterministic_time_windows_no_semantic_preselection",
            "source_batch_seconds": MICRO_BEAT_SOURCE_BATCH_SECONDS,
            "old_candidate_pool_used_as_selector": False,
            "source_safety_derived_directly_not_from_candidate_ledger": True,
            "arc_assembly_performed": False,
            "dense_composition_performed": False,
            "m3_invoked": False,
            "min_preferred_seconds": MICRO_BEAT_PREFERRED_MIN_SECONDS,
            "max_seconds": MICRO_BEAT_MAX_SECONDS,
            "source_selection_authority": "AI",
            "program_authority": "source_boundaries_safety_word_lineage_duration_P0_2_static_quality_only",
        },
    }
    if not mined_beats:
        result["status"] = "inventory_completed"
        result["contract"] = dict(result["contract"]) | {"p0_2_micro_beat_quality_applied": False}
        return result
    if not apply_legacy_p02_quality:
        result.update({
            "status": "inventory_completed_pending_boundary_quality",
            "mined_before_p0_2_quality_count": len(mined_beats),
            "mined_before_p0_2_quality_seconds": round(sum(_number(item.get("duration_seconds")) for item in mined_beats), 3),
        })
        result["contract"] = dict(result["contract"]) | {
            "p0_2_static_quality_applied": bool(apply_static_p02_quality),
            "p0_2_micro_beat_quality_applied": False,
            "legacy_p0_2_quality_bypassed_for_p0_5a2": True,
        }
        return result
    quality_batches = [
        mined_beats[index:index + MICRO_BEAT_QUALITY_BATCH_SIZE]
        for index in range(0, len(mined_beats), MICRO_BEAT_QUALITY_BATCH_SIZE)
    ]
    final_beats: list[dict[str, Any]] = []
    quality_rejected: list[dict[str, Any]] = []
    quality_batches_audit: list[dict[str, Any]] = []
    quality_notes: list[dict[str, Any]] = []
    for index, beat_batch in enumerate(quality_batches, start=1):
        batch_id = f"Q{index:02d}"
        quality_response = _post_lite_request(
            api_key=api_key, base_url=base_url, model=model,
            prompt=build_micro_beat_quality_prompt(beat_inventory=beat_batch),
            stage=P05_MICRO_BEAT_QUALITY_STAGE, max_tokens=6200,
        )
        quality_content = _text(quality_response.get("choices", [{}])[0].get("message", {}).get("content"))
        if quality_response_hook and quality_content:
            quality_response_hook(batch_id, quality_content)
        try:
            quality_data = _extract_json(quality_content) if quality_content else None
        except (RuntimeError, ValueError, TypeError):
            quality_data = None
        if not isinstance(quality_data, Mapping):
            result.update({
                "status": "inventory_quality_response_invalid",
                "errors": [f"micro_beat_p0_2_quality_response_empty_or_invalid:{batch_id}"],
                "p0_2_quality_rejected_beats": quality_rejected,
                "quality_batches": quality_batches_audit + [{
                    "batch_id": batch_id, "beat_ids": [item["beat_id"] for item in beat_batch],
                    "status": "response_invalid",
                }],
            })
            result["contract"] = dict(result["contract"]) | {"p0_2_micro_beat_quality_applied": False}
            return result
        checked = apply_micro_beat_quality(
            inventory={"beat_inventory": beat_batch, "contract": result["contract"]}, data=quality_data,
        )
        final_beats.extend(checked["beat_inventory"])
        quality_rejected.extend(checked["p0_2_quality_rejected_beats"])
        quality_batches_audit.append({
            "batch_id": batch_id, "beat_ids": [item["beat_id"] for item in beat_batch],
            "status": "reviewed",
            "retained_count": len(checked["beat_inventory"]),
            "rejected_count": len(checked["p0_2_quality_rejected_beats"]),
            "raw_response_present": bool(quality_content),
        })
        quality_notes.append({
            "batch_id": batch_id,
            **dict(_as_mapping_list([quality_data.get("inventory_quality")])[0] if isinstance(quality_data.get("inventory_quality"), Mapping) else {}),
        })
    result.update({
        "status": "inventory_completed",
        "mined_before_p0_2_quality_count": len(mined_beats),
        "mined_before_p0_2_quality_seconds": round(sum(_number(item.get("duration_seconds")) for item in mined_beats), 3),
        "total_micro_beats": len(final_beats),
        "total_usable_beat_seconds": round(sum(_number(item.get("duration_seconds")) for item in final_beats), 3),
        "preferred_2_to_5_second_beat_count": sum(
            MICRO_BEAT_PREFERRED_MIN_SECONDS <= _number(item.get("duration_seconds")) <= MICRO_BEAT_PREFERRED_MAX_SECONDS
            for item in final_beats
        ),
        "short_complete_exception_count": sum(
            _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS for item in final_beats
        ),
        "beat_inventory": final_beats,
        "p0_2_quality_rejected_beats": quality_rejected,
        "quality_batches": quality_batches_audit,
        "inventory_quality": {
            "mining_batches": mining_quality,
            "p0_2_final_utterance_quality_batches": quality_notes,
        },
    })
    result["contract"] = dict(result["contract"]) | {
        "p0_2_micro_beat_quality_applied": True,
        "quality_selection_authority": "AI_P0_2_final_utterance_quality",
        "quality_batch_size": MICRO_BEAT_QUALITY_BATCH_SIZE,
    }
    return result


def build_narrative_mode_beat_inventory(
    *,
    source_units: Sequence[Mapping[str, Any]],
    hard_safe_subtitle_ids: Sequence[int],
    word_timeline: SemanticSrtWordTimeline,
    api_key: str,
    base_url: str,
    model: str,
    discovery_response_hook: Callable[[str, str], None] | None = None,
    boundary_response_hook: Callable[[str, str], None] | None = None,
    adjudication_response_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Build P0.5A.2's canonical Beat inventory for Narrative Mode.

    This is deliberately a source-inventory operation, not a Director, Arc or
    selection operation.  The historical P0.2 static and four-pass veto
    paths remain intact for their old runners, but Narrative Mode must use the
    calibrated P0.5A.2 three-state decision as its final utterance contract.
    """
    source_rows = build_micro_beat_source_rows(
        source_units=source_units,
        hard_safe_subtitle_ids=hard_safe_subtitle_ids,
        word_timeline=word_timeline,
    )
    mined = mine_micro_beat_inventory(
        source_units=source_units,
        hard_safe_subtitle_ids=hard_safe_subtitle_ids,
        word_timeline=word_timeline,
        api_key=api_key,
        base_url=base_url,
        model=model,
        response_hook=discovery_response_hook,
        apply_static_p02_quality=False,
        apply_legacy_p02_quality=False,
    )
    if not mined.get("beat_inventory"):
        result = dict(mined)
        result["contract"] = dict(result.get("contract") or {}) | {
            "narrative_mode_p0_5a2_inventory": True,
            "arc_assembly_performed": False,
            "m3_invoked": False,
        }
        return result
    boundary = refine_micro_beat_boundaries(
        frozen_inventory=_as_mapping_list(mined.get("beat_inventory")),
        source_rows=source_rows,
        api_key=api_key,
        base_url=base_url,
        model=model,
        response_hook=boundary_response_hook,
        apply_legacy_final_utterance_adjudication=False,
    )
    if boundary.get("status") != "publishable_inventory_completed":
        result = dict(boundary)
        result["contract"] = dict(result.get("contract") or {}) | {
            "narrative_mode_p0_5a2_inventory": True,
            "arc_assembly_performed": False,
            "m3_invoked": False,
        }
        return result
    calibrated = adjudicate_micro_beat_publishability(
        boundary_result=boundary,
        api_key=api_key,
        base_url=base_url,
        model=model,
        response_hook=adjudication_response_hook,
    )
    calibrated["source_inventory"] = {
        "total_source_subtitles": len(source_rows),
        "total_hard_safe_materializable_subtitles": sum(
            bool(item.get("hard_safe")) and bool(item.get("materializable"))
            for item in source_rows
        ),
        "mined_before_boundary_count": len(_as_mapping_list(mined.get("beat_inventory"))),
        "mined_before_boundary_seconds": round(sum(
            _number(item.get("duration_seconds")) for item in _as_mapping_list(mined.get("beat_inventory"))
        ), 3),
    }
    calibrated["contract"] = dict(calibrated.get("contract") or {}) | {
        "narrative_mode_p0_5a2_inventory": True,
        "p0_2_static_quality_bypassed": True,
        "legacy_final_utterance_veto_bypassed": True,
        "arc_assembly_performed": False,
        "dense_composition_performed": False,
        "m3_invoked": False,
    }
    return calibrated


def replay_short_beat_contract_rejects(
    *,
    previous_inventory: Mapping[str, Any],
    reconstructed_raw_inventory: Mapping[str, Any],
    source_rows: Sequence[Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    response_hook: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Re-review only prior *program* Boundary rejects under P0.5A.3.

    This is intentionally not another source discovery pass. Explicit AI
    semantic rejects remain frozen; the returned boundary result contains only
    the historic false-negative candidates that deserve the new short/expand
    contract.
    """
    old_rejections = _as_mapping_list(previous_inventory.get("boundary_rejected_beats"))
    contract_rejections = [
        item for item in old_rejections
        if _text(item.get("decision")).upper() != "REJECT"
    ]
    raw_by_id = {
        _text(item.get("beat_id")): item
        for item in _as_mapping_list(reconstructed_raw_inventory.get("beat_inventory"))
    }
    review_ids = [_text(item.get("beat_id")) for item in contract_rejections]
    layout_drift = bool((reconstructed_raw_inventory.get("contract") or {}).get("source_batch_layout_drift_replayed"))
    raw_beats = [] if layout_drift else [raw_by_id[beat_id] for beat_id in review_ids if beat_id in raw_by_id]
    missing = list(review_ids) if layout_drift else sorted(set(review_ids) - set(raw_by_id))
    replay_source_rows = [dict(item) for item in source_rows]
    reconstruction_mode = "frozen_raw_inventory_exact_ids"
    if missing:
        # A historic source-safety/configuration change can alter the old
        # mining transport batch and therefore its generated Beat ordinal.
        # The Boundary receipt still contains the original source time span,
        # which is a stronger freeze for this calibration than allocating new
        # IDs from a fresh discovery parse. Rebuild only these 51 anchors from
        # that receipt; explicit AI rejects are still excluded.
        by_subtitle_id = {int(item["subtitle_id"]): item for item in replay_source_rows}
        reconstructed_from_receipts: list[dict[str, Any]] = []
        restored_source_ids: set[int] = set()
        for record in contract_rejections:
            start = _number(record.get("original_start"), -1.0)
            end = _number(record.get("original_end"), -1.0)
            if start < 0 or end <= start:
                continue
            rows = [
                row for row in replay_source_rows
                if _number(row.get("end")) > start + 1e-6 and _number(row.get("start")) < end - 1e-6
            ]
            if not rows or any(not bool(row.get("materializable")) for row in rows):
                continue
            # These rows were demonstrably admitted into the original frozen
            # Boundary stage. Retain that historical hard-safety fact only for
            # the old raw span, not for any newly explored neighbour.
            for row in rows:
                restored_source_ids.add(int(row["subtitle_id"]))
            reconstructed_from_receipts.append({
                "beat_id": _text(record.get("beat_id")),
                "start": start,
                "end": end,
                "duration_seconds": round(end - start, 3),
                "subtitle_ids": [int(row["subtitle_id"]) for row in rows],
                "start_word_offset": None,
                "end_word_offset": None,
                "text": _text(record.get("original_text")),
                "commercial_theme": _text(record.get("purchase_value")),
                "purchase_value": _text(record.get("purchase_value")),
                "sub_outcome": _text(record.get("sub_outcome")),
                "evidence_function": _text(record.get("evidence_function")) or "other",
                "standalone_quality": "frozen_boundary_contract_replay",
                "source_context_required": False,
                "why_this_is_a_new_beat": "frozen_contract_reject_replay",
                "short_beat": _number(record.get("original_duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS,
                "short_beat_reason": "",
                "lineage_status": "reconstructed_from_frozen_boundary_receipt",
                "word_lineage": [
                    dict(row.get("word_lineage") or {}) | {
                        "subtitle_id": int(row["subtitle_id"]),
                        "start_word_offset": 0,
                        "end_word_offset": len(row.get("word_tokens") or ()) - 1,
                    }
                    for row in rows
                ],
                "selection_authority": "frozen_P0_5A2_boundary_contract_receipt",
            })
        for subtitle_id in restored_source_ids:
            if subtitle_id in by_subtitle_id:
                by_subtitle_id[subtitle_id]["hard_safe"] = True
                by_subtitle_id[subtitle_id]["safety_block_reason"] = ""
                by_subtitle_id[subtitle_id]["historical_frozen_boundary_safe"] = True
        raw_beats = reconstructed_from_receipts
        missing = sorted(set(review_ids) - {_text(item.get("beat_id")) for item in raw_beats})
        reconstruction_mode = "frozen_boundary_receipt_original_time_span"
    if missing:
        return {
            "status": "short_recall_replay_unavailable",
            "errors": ["frozen_contract_reject_raw_beat_missing:" + ",".join(missing)],
            "frozen_contract_rejects": contract_rejections,
            "publishable_beat_inventory": [],
        }
    replayed = refine_micro_beat_boundaries(
        frozen_inventory=raw_beats,
        source_rows=replay_source_rows,
        api_key=api_key,
        base_url=base_url,
        model=model,
        response_hook=response_hook,
        apply_legacy_final_utterance_adjudication=False,
    )
    reason_groups = {
        "duration_policy_false_negatives": 0,
        "short_exception_contract_false_negatives": 0,
        "lineage_or_program_contract_false_negatives": 0,
        "other_program_contract_false_negatives": 0,
    }
    for item in contract_rejections:
        reason = _text(item.get("reject_reason"))
        if "duration" in reason:
            reason_groups["duration_policy_false_negatives"] += 1
        elif "short_exception" in reason or "short_complete" in reason:
            reason_groups["short_exception_contract_false_negatives"] += 1
        elif any(marker in reason for marker in ("lineage", "word", "source_words")):
            reason_groups["lineage_or_program_contract_false_negatives"] += 1
        else:
            reason_groups["other_program_contract_false_negatives"] += 1
    result = dict(replayed)
    result["p0_5a3_short_recall_replay"] = {
        "stage": P05_MICRO_BEAT_SHORT_RECALL_CALIBRATION_STAGE,
        "complete_srt_rescanned_for_discovery": False,
        "frozen_contract_rejects_count": len(contract_rejections),
        "re_reviewed_count": len(raw_beats),
        "raw_reconstruction_mode": reconstruction_mode,
        "frozen_explicit_ai_rejects_count": sum(
            _text(item.get("decision")).upper() == "REJECT" for item in old_rejections
        ),
        "old_contract_reject_categories": reason_groups,
        "recovered_before_publishability_count": len(_as_mapping_list(replayed.get("publishable_beat_inventory"))),
        "still_rejected_count": len(_as_mapping_list(replayed.get("boundary_rejected_beats"))),
    }
    result["contract"] = dict(result.get("contract") or {}) | {
        "p0_5a3_short_recall_replay": True,
        "explicit_ai_rejects_replayed": False,
        "program_contract_rejects_replayed": True,
    }
    return result


def prepare_narrative_mode_beat_execution(
    *,
    publishable_inventory: Mapping[str, Any],
    word_timeline: SemanticSrtWordTimeline,
) -> dict[str, Any]:
    """Adapt already-approved P0.5A.2 Beat boundaries into M2/M3 inputs.

    The adapter has no semantic authority: it assigns stable per-run IDs and
    proves the exact timed source-word slice that M3 will later materialize.
    Visual-only Beats stay available to the Director for non-opening support,
    while only clean audio-independent Beats receive the immutable hook
    permission.
    """
    from story_planner import PlanningCandidate

    beats = _as_mapping_list(publishable_inventory.get("publishable_beat_inventory"))
    candidates: list[PlanningCandidate] = []
    candidate_words: dict[int, tuple[dict[str, Any], ...]] = {}
    execution_ledger: list[dict[str, Any]] = []
    beat_candidate_map: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    for index, beat in enumerate(beats, start=1):
        beat_id = _text(beat.get("beat_id"))
        status = _text(beat.get("publishability_status"))
        start_word_id = _optional_int(beat.get("final_start_word_id"))
        end_word_id = _optional_int(beat.get("final_end_word_id"))
        if (
            not beat_id or status not in {"publishable_clean", "publishable_visual"}
            or start_word_id is None or end_word_id is None or end_word_id < start_word_id
            or end_word_id >= len(word_timeline.words)
        ):
            rejected.append({"beat_id": beat_id, "reason": "p0_5a2_boundary_word_lineage_missing"})
            continue
        words = tuple(dict(item) for item in word_timeline.words[start_word_id:end_word_id + 1])
        source_text = "".join(_text(item.get("text")) for item in words)
        if not words or _normalize_spoken_source_text(source_text) != _normalize_spoken_source_text(beat.get("text")):
            rejected.append({"beat_id": beat_id, "reason": "p0_5a2_boundary_text_not_exact_timeline_words"})
            continue
        candidate_id = 950_000_000 + index
        while candidate_id in used_ids:
            candidate_id += 1
        used_ids.add(candidate_id)
        audio_only = bool(beat.get("audio_only_eligible")) and status == "publishable_clean"
        declared_roles = tuple(
            role for role in (_text(item).lower() for item in beat.get("role_permissions") or ())
            if role in _MICRO_BEAT_ROLE_PERMISSIONS
        )
        hook_eligible = bool(beat.get("hook_eligible")) and "hook" in declared_roles and audio_only
        candidate = PlanningCandidate(
            candidate_id=candidate_id,
            source_id=f"P05_MICRO_BEAT:{beat_id}",
            start=round(_number(words[0].get("start")), 3),
            end=round(_number(words[-1].get("end")), 3),
            text=_text(beat.get("text")),
            origin_subtitle_ids=_as_int_tuple(beat.get("subtitle_ids")),
            hook_eligible=hook_eligible and _number(beat.get("duration_seconds")) <= MICRO_BEAT_MAX_SECONDS,
            role_permissions=("product", *declared_roles) if declared_roles else ("product",),
            subject_relation="main",
            story_block_id=f"P05:{beat_id}",
            continuity_group_id=f"P05:{beat_id}",
            asset_tiers=(),
        )
        candidates.append(candidate)
        candidate_words[candidate_id] = words
        execution_ledger.append({
            "candidate_id": candidate_id,
            "beat_id": beat_id,
            "text": candidate.text,
            "start": candidate.start,
            "end": candidate.end,
            "subtitle_ids": list(candidate.origin_subtitle_ids),
            "final_start_word_id": start_word_id,
            "final_end_word_id": end_word_id,
            "lineage_status": "p0_5a2_source_boundary_word_exact",
            "publishability_status": status,
            "visual_dependency": _text(beat.get("visual_dependency")),
            "role_permissions": list(declared_roles),
            "context_requirement": _text(beat.get("context_requirement")),
            "short_beat": bool(beat.get("short_beat")),
        })
        beat_candidate_map.append({
            "beat_id": beat_id,
            "candidate_id": candidate_id,
            "publishability_status": status,
            "visual_dependency": _text(beat.get("visual_dependency")),
            "audio_only_eligible": audio_only,
            "hook_eligible": candidate.hook_eligible,
            "role_permissions": list(declared_roles),
            "context_requirement": _text(beat.get("context_requirement")),
            "short_beat": bool(beat.get("short_beat")),
            "narrative_priority": _text(beat.get("narrative_priority")),
            "purchase_value": _text(beat.get("purchase_value")),
            "sub_outcome": _text(beat.get("sub_outcome")),
            "evidence_function": _text(beat.get("evidence_function")),
        })
    return {
        "candidates": tuple(candidates),
        "candidate_words": candidate_words,
        "execution_ledger": execution_ledger,
        "beat_candidate_map": beat_candidate_map,
        "binding_rejections": rejected,
        "contract": {
            "selection_authority": "AI_director_then_AI_beat_casting",
            "program_authority": "p0_5a3_status_role_permission_word_lineage_timing_only",
            "visual_beat_opening_permission": "forbidden",
            "m3_input_is_exact_p0_5a2_word_boundary": True,
        },
    }
