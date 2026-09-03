"""Materialize one AI-authored director packet without a second semantic call.

The director chooses the commercial story and subtitle IDs exactly once in the
M1 response.  This module has deliberately narrow authority: it resolves those
IDs against the complete safe executable candidate pool, reports quality
warnings, and builds the existing M2/M3 contract.  It never scores, selects,
reorders, appends, removes, replaces, or semantically repairs a beat.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Mapping, Sequence

from commercial_analyzer import (
    DIRECTOR_ANSWER_ROLES,
    DIRECTOR_COVERAGE,
    DIRECTOR_PURCHASE_QUESTIONS,
    DirectorBeat,
    Strategy,
    director_target_duration_range,
    final_utterance_surface_issue,
)
from story_planner import (
    CommercialStoryBrief,
    NarrativeBeat,
    NarrativePlan,
    OpeningPackage,
    PlanningCandidate,
    ReplanRequest,
    StoryConsumption,
)


def _candidate_for_subtitle(
    candidates: Sequence[PlanningCandidate],
) -> dict[int, PlanningCandidate]:
    """Keep source lookup deterministic while preserving AI playback order."""
    lookup: dict[int, PlanningCandidate] = {}
    for candidate in candidates:
        for subtitle_id in candidate.origin_subtitle_ids:
            # A duplicate lineage is not semantic ambiguity.  Preserve the
            # first complete safe candidate in stable pool order; normal data
            # has one candidate per source subtitle.
            lookup.setdefault(int(subtitle_id), candidate)
        lookup.setdefault(int(candidate.candidate_id), candidate)
    return lookup


_DEFAULT_OPENING_QUESTION_IDS = ("Q1",)
_DEFAULT_OPENING_ANSWER_ROLES = ("result", "proof")


def _normalized_values(raw: Any, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = (raw,)
    values = tuple(str(value or "").strip() for value in (raw or ()) if str(value or "").strip())
    return values or fallback


def _opening_scope(
    director: Mapping[str, Any], sequence: Sequence[DirectorBeat],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Read the Director-declared opening scope without inventing a mode."""
    raw_scope = director.get("opening_scope")
    scope = dict(raw_scope) if isinstance(raw_scope, Mapping) else {}
    # A model occasionally omits the display-only ``opening_scope`` object
    # while still declaring the first beat's question and answer role.  That
    # declaration is already its own semantic decision, so expose it as the
    # narrow scope rather than forcing a scene story back to global Q1.  This
    # never substitutes or ranks a source sentence.
    first = sequence[0] if sequence else None
    fallback_questions = (
        (str(first.purchase_question_id or "").strip().upper(),)
        if first and str(first.purchase_question_id or "").strip() else _DEFAULT_OPENING_QUESTION_IDS
    )
    fallback_roles = (
        (str(first.answer_role or first.role or "").strip().lower(),)
        if first and str(first.answer_role or first.role or "").strip() else _DEFAULT_OPENING_ANSWER_ROLES
    )
    question_ids = tuple(value.upper() for value in _normalized_values(
        scope.get("allowed_purchase_question_ids"), fallback_questions,
    ))
    answer_roles = tuple(value.lower() for value in _normalized_values(
        scope.get("allowed_answer_roles"), fallback_roles,
    ))
    return question_ids, answer_roles


def _packet_relation_issues(
    packet: DirectorBeat,
    *,
    index: int,
    prior_question_ids: set[str],
    seen_claims: set[tuple[str, str, str]],
    opening_question_ids: tuple[str, ...],
    opening_answer_roles: tuple[str, ...],
) -> list[str]:
    """Validate only the Director-declared relationship, never its meaning.

    The AI still chooses the sentence and writes the buyer-value labels in the
    same response.  This verifier merely makes a malformed packet visible
    instead of silently turning a list of claims into a completed film.
    """
    issues: list[str] = []
    label = packet.beat_id or str(index)
    question_id = str(packet.purchase_question_id or "").strip().upper()
    answer_role = str(packet.answer_role or "").strip().lower()
    supports_question_id = str(packet.supports_question_id or "").strip().upper()
    outcome = str(packet.purchase_outcome or "").strip()
    coverage = str(packet.coverage or "").strip().lower()
    if question_id not in DIRECTOR_PURCHASE_QUESTIONS:
        issues.append(f"single_pass_director_purchase_question_invalid:{label}")
    if not str(packet.purchase_question or "").strip():
        issues.append(f"single_pass_director_purchase_question_missing:{label}")
    if answer_role not in DIRECTOR_ANSWER_ROLES:
        issues.append(f"single_pass_director_answer_role_invalid:{label}")
    if coverage not in DIRECTOR_COVERAGE:
        issues.append(f"single_pass_director_coverage_invalid:{label}")
    if not outcome:
        issues.append(f"single_pass_director_purchase_outcome_missing:{label}")
    if supports_question_id:
        if supports_question_id not in DIRECTOR_PURCHASE_QUESTIONS:
            issues.append(f"single_pass_director_supports_question_invalid:{label}")
        elif supports_question_id == question_id:
            # A chapter may have a result followed by a different-role proof
            # for that *same already-established* purchase question.  This is
            # the formal version of “same claim, additional proof”, not a
            # semantic expansion by code.  Only the AI supplied both the
            # question relation and the exact source beat; we merely avoid
            # treating an internal chapter proof as an impossible reference.
            if question_id not in prior_question_ids:
                issues.append(f"single_pass_director_supports_question_not_established:{label}:{question_id}")
        elif supports_question_id not in prior_question_ids:
            issues.append(f"single_pass_director_supports_question_not_established:{label}:{supports_question_id}")
    if index == 1 and (question_id not in opening_question_ids or answer_role not in opening_answer_roles):
        issues.append(f"single_pass_director_opening_outside_scope:{label}")
    triple = (question_id, answer_role, outcome)
    if question_id and answer_role and outcome and triple in seen_claims:
        issues.append(f"single_pass_director_repeated_purchase_value:{label}")
    return issues


def _opening_surface_issue(text: str) -> str:
    """Report obvious live delivery lead-ins without changing the opening.

    This does not choose an alternative, re-rank, or reject anything.  It only
    makes an AI-selected source sentence such as ``的确…你看…`` visible to the
    editor as an opening-quality warning.
    """
    normalized = str(text or "").strip()
    for prefix in (
        "好的", "那个", "其实", "然后", "因为", "所以", "的确", "就的确", "嗯", "你看",
    ):
        if normalized.startswith(prefix):
            return "live_or_dependent_opening_leadin"
    # Keep a useful demonstrated comparison available as a body beat, but it
    # cannot carry the first spoken promise without the picture already doing
    # the explanatory work.  This is a role restriction, never a deletion or
    # a program-selected substitute.
    if re.search(r"(?:从|在)(?:这儿|这里).{0,20}(?:到|变成)(?:这儿|这里)", normalized):
        return "visual_dependent_opening"
    if re.match(r"^(?:后背|侧面|正面|上身).{0,18}(?:看|都|显)", normalized):
        return "visual_dependent_opening"
    return ""


def _verbatim_key(value: Any) -> str:
    """Compare copied source speech without treating layout spaces as edits."""
    return re.sub(r"\s+", "", str(value or "").strip())


def build_single_pass_director_plan(
    *,
    strategy: Strategy,
    safe_candidates: Sequence[PlanningCandidate],
    target_duration: float,
    selection_contract: Mapping[str, Any] | None,
    director_contract: Mapping[str, Any] | None,
) -> NarrativePlan:
    """Bind the one-call director sequence to real executable source clips."""
    contract = dict(selection_contract or {})
    director = dict(director_contract or {})
    sequence = tuple(strategy.director_sequence)
    candidate_by_subtitle = _candidate_for_subtitle(safe_candidates)
    selected: list[PlanningCandidate] = []
    beats: list[NarrativeBeat] = []
    # Only technically unrenderable source failures belong in ``issues``.
    # Director semantics and quality stay visible but may not delete/reorder a
    # paid one-call result or keep an editable preview out of M3.
    issues: list[str] = []
    quality_warnings: list[str] = []
    seen_candidate_ids: set[int] = set()
    seen_purchase_values: set[tuple[str, str, str]] = set()
    established_questions: set[str] = set()
    whole_video_audit_verification: dict[str, Any] = {}
    # Historical fixtures can still be read, but every new one-call packet is
    # required to carry the relationships the Director authored in that call.
    enforce_journey_contract = bool(director.get("single_ai_director_packet"))
    opening_question_ids, opening_answer_roles = _opening_scope(director, sequence)
    opening_fallbacks_used: list[dict[str, Any]] = []
    verified_chapter_parts: dict[str, list[str]] = {}

    def resolve_source_ids(
        subtitle_ids: Sequence[int], *, packet_label: str, opening: bool, verbatim: str = "",
    ) -> tuple[list[PlanningCandidate], list[str]]:
        """Resolve one AI-authored contiguous spoken span.

        Individual SRT rows are timing units.  Publishability and opening
        independence therefore apply to their concatenated span; evaluating
        every row alone recreates the fragment problem this contract avoids.
        """
        resolved_rows: list[PlanningCandidate] = []
        local_ids: set[int] = set()
        rejected: list[str] = []
        for subtitle_id in subtitle_ids:
            candidate = candidate_by_subtitle.get(int(subtitle_id))
            if candidate is None:
                rejected.append(
                    f"single_pass_director_source_unresolved:{packet_label}:subtitle_id={subtitle_id}"
                )
                continue
            if candidate.candidate_id in seen_candidate_ids:
                quality_warnings.append(
                    f"single_pass_director_candidate_reused:{packet_label}:candidate_id={candidate.candidate_id}"
                )
            if candidate.candidate_id in local_ids:
                # Multiple source subtitle IDs may legitimately resolve to one
                # already-merged safe candidate.  Play it once in that span.
                continue
            resolved_rows.append(candidate)
            local_ids.add(candidate.candidate_id)
        if rejected or not resolved_rows:
            return [], rejected
        for previous, current in zip(resolved_rows, resolved_rows[1:]):
            gap = float(current.start) - float(previous.end)
            if float(current.start) < float(previous.start):
                return [], [
                    f"single_pass_director_source_time_reversed:{packet_label}:"
                    f"candidate_id={previous.candidate_id}->{current.candidate_id}"
                ]
            if gap > 1.5:
                quality_warnings.append(
                    f"single_pass_director_source_span_gap_warning:{packet_label}:"
                    f"candidate_id={previous.candidate_id}->{current.candidate_id}:gap={gap:.3f}"
                )
        combined_text = " ".join(str(item.text or "").strip() for item in resolved_rows).strip()
        if _verbatim_key(verbatim):
            if _verbatim_key(verbatim) != _verbatim_key(combined_text):
                quality_warnings.append(
                    f"single_pass_director_verbatim_mismatch:{packet_label}"
                )
        combined_duration = float(resolved_rows[-1].end) - float(resolved_rows[0].start)
        if len(resolved_rows) > 1 and combined_duration > 12.0:
            quality_warnings.append(
                f"single_pass_director_source_span_long_warning:{packet_label}:"
                f"duration={combined_duration:.3f}"
            )
        surface_issue = final_utterance_surface_issue(combined_text)
        if surface_issue:
            quality_warnings.append(
                f"single_pass_director_final_utterance_warning:{packet_label}:"
                f"candidate_ids={','.join(str(item.candidate_id) for item in resolved_rows)}:{surface_issue}"
            )
        if opening:
            first = resolved_rows[0]
            if not first.hook_eligible:
                quality_warnings.append(
                    f"single_pass_director_hook_ineligible_warning:{packet_label}:candidate_id={first.candidate_id}"
                )
            opening_issue = _opening_surface_issue(combined_text)
            if opening_issue:
                quality_warnings.append(
                    f"single_pass_director_opening_quality_warning:{packet_label}:"
                    f"candidate_ids={','.join(str(item.candidate_id) for item in resolved_rows)}:{opening_issue}"
                )
        return resolved_rows, rejected

    # Opening alternatives are direction metadata only.  Programmatic
    # replacement caused a valid AI story to lose its premise and collapse to
    # a few disconnected lines, so the primary sequence is now immutable.
    chapter_by_beat_id = {
        str(beat.beat_id or "").strip(): chapter
        for chapter in strategy.director_chapter_packets
        for beat in chapter.beats
        if str(beat.beat_id or "").strip()
    }
    for index, packet in enumerate(sequence, 1):
        question_id = str(packet.purchase_question_id or "").strip().upper()
        answer_role = str(packet.answer_role or packet.role or "purchase_progress").strip().lower()
        outcome = str(packet.purchase_outcome or "").strip()
        coverage = str(packet.coverage or "recommended").strip().lower()
        if enforce_journey_contract:
            relation_issues = _packet_relation_issues(
                packet,
                index=index,
                prior_question_ids=established_questions,
                seen_claims=seen_purchase_values,
                opening_question_ids=opening_question_ids,
                opening_answer_roles=opening_answer_roles,
            )
            if relation_issues:
                quality_warnings.extend(relation_issues)
        packet_label = packet.beat_id or str(index)
        authored_chapter = chapter_by_beat_id.get(str(packet_label).strip())
        resolved: list[PlanningCandidate] = []
        if not packet.subtitle_ids:
            issues.append(f"single_pass_director_empty_subtitle_ids:{packet_label}")
            continue
        resolved, primary_resolution_issues = resolve_source_ids(
            packet.subtitle_ids, packet_label=packet_label, opening=index == 1,
            verbatim=str(getattr(packet, "verbatim", "") or ""),
        )
        issues.extend(primary_resolution_issues)
        if not resolved:
            continue
        for candidate in resolved:
            seen_candidate_ids.add(candidate.candidate_id)
            selected.append(candidate)
            if authored_chapter:
                verified_chapter_parts.setdefault(authored_chapter.chapter_id, []).append(
                    str(candidate.text or "").strip()
                )
        role = str(packet.role or answer_role or "purchase_progress").strip() or "purchase_progress"
        is_opening = index == 1
        # Relation data comes directly from the one AI packet.  The fallback
        # exists only for legacy non-single-pass callers; it never enriches a
        # new director result on the program's own judgment.
        mapped_question_id = question_id or f"D{index}"
        mapped_outcome = outcome or f"director_step_{index}"
        beats.append(NarrativeBeat(
            # Multiple source beats may deliberately form one complete
            # micro-narrative chapter.  Reusing the AI-authored chapter id is
            # grouping metadata only; playback order remains ``sequence``.
            chapter_id=(authored_chapter.chapter_id if authored_chapter else f"D{index}"),
            source_role="hook" if is_opening else role,
            narrative_role="hook" if is_opening else role,
            goal=str(
                (authored_chapter.title if authored_chapter else "")
                or packet.goal or "真实购买信息推进"
            ).strip(),
            candidate_evidence=tuple(candidate.candidate_id for candidate in resolved),
            required=coverage == "required",
            target_seconds=round(sum(candidate.duration for candidate in resolved), 3),
            selection_instruction="single_ai_director_packet_exact_source_order",
            selection_origin="single_ai_director_packet",
            transition_from_previous=str(packet.why_this_follows or "").strip(),
            value_dimension=mapped_question_id,
            # These fields record the Director's declared relationship; no
            # program derives a semantic outcome from the source text.
            purchase_value_dimension=(
                "same_claim_additional_proof" if packet.supports_question_id else "new_outcome"
            ),
            purchase_value_domain=mapped_question_id,
            purchase_value_outcomes=(mapped_outcome,),
            purchase_value_reason=str(packet.purchase_question or packet.goal or "真实购买信息推进").strip(),
            story_support="single_ai_director_packet_source_grounded",
            commerce_beat_id=mapped_question_id,
        ))
        if enforce_journey_contract:
            established_questions.add(question_id)
            seen_purchase_values.add((question_id, answer_role, outcome))

    selected_seconds = round(sum(candidate.duration for candidate in selected), 3)
    verified_readthrough = " ".join(str(candidate.text or "").strip() for candidate in selected).strip()
    verified_chapter_readthroughs: dict[str, str] = {}
    for chapter in strategy.director_chapter_packets:
        actual_readthrough = "｜".join(
            item for item in verified_chapter_parts.get(chapter.chapter_id, []) if item
        )
        verified_chapter_readthroughs[chapter.chapter_id] = actual_readthrough
        declared_readthrough = str(getattr(chapter, "chapter_readthrough", "") or "").strip()
        if declared_readthrough and _verbatim_key(declared_readthrough) != _verbatim_key(actual_readthrough):
            quality_warnings.append(
                f"single_pass_director_chapter_readthrough_mismatch:{chapter.chapter_id}"
            )
        if enforce_journey_contract and director.get("two_pass_director_packet"):
            if not declared_readthrough:
                quality_warnings.append(
                    f"single_pass_director_chapter_readthrough_missing:{chapter.chapter_id}"
                )
            if str(getattr(chapter, "continuity_status", "") or "").strip().lower() != "pass":
                quality_warnings.append(
                    f"single_pass_director_chapter_continuity_not_passed:{chapter.chapter_id}"
                )
    if (
        str(getattr(strategy, "director_readthrough", "") or "").strip()
        and _verbatim_key(strategy.director_readthrough) != _verbatim_key(verified_readthrough)
    ):
        quality_warnings.append("single_pass_director_final_readthrough_mismatch")
    journey_question_ids = tuple(dict.fromkeys(
        str(beat.commerce_beat_id or "").strip()
        for beat in beats
        if str(beat.commerce_beat_id or "").strip()
    ))
    has_opening = len(beats) >= 2
    if len(beats) < 2:
        quality_warnings.append("single_pass_director_requires_opening_and_payoff")
    elif enforce_journey_contract and len(journey_question_ids) < 2:
        quality_warnings.append("single_pass_director_purchase_journey_too_thin")
    declared_quality_tier = str(strategy.director_quality_tier or "standard").strip().lower()
    # Whole Video Audit is authored by the same AI call.  Program authority is
    # strictly limited to checking its declared counts against the already
    # selected source IDs and timestamps; it does not judge a sentence or add
    # an alternative.  A mismatch stays visible in the audit rather than
    # withholding a source-grounded review render.
    if strategy.director_chapter_packets:
        audit = dict(strategy.whole_video_audit or {})
        audit_status = str(audit.get("status") or "").strip().lower()
        try:
            audit_count = int(audit.get("selected_beat_count"))
        except (TypeError, ValueError):
            audit_count = -1
        try:
            audit_subtitle_count = int(audit.get("selected_subtitle_id_count"))
        except (TypeError, ValueError):
            audit_subtitle_count = -1
        try:
            audit_seconds = float(audit.get("estimated_source_seconds"))
        except (TypeError, ValueError):
            audit_seconds = -1.0
        audit_issues: list[str] = []
        verified_subtitle_count = len({
            int(subtitle_id)
            for packet in sequence
            for subtitle_id in packet.subtitle_ids
            if int(subtitle_id) > 0
        })
        # Historical responses may contain an AI-authored audit.  Verify it
        # read-only.  New compact responses omit self-scoring entirely.
        if audit:
            if "selected_beat_count" in audit and audit_count != len(sequence):
                audit_issues.append("beat_count_mismatch")
            if (
                "selected_subtitle_id_count" in audit
                and audit_subtitle_count != verified_subtitle_count
            ):
                audit_issues.append("subtitle_id_count_mismatch")
            if (
                "estimated_source_seconds" in audit
                and (audit_seconds < 0 or abs(audit_seconds - selected_seconds) > 1.5)
            ):
                audit_issues.append("duration_mismatch")
            if selected_seconds < 30.0 and audit_status not in {
                "short_source", "natural_complete_below_target", "source_material_limited",
            }:
                audit_issues.append("short_source_status_expected")
            if selected_seconds < 30.0 and declared_quality_tier != "basic":
                audit_issues.append("basic_tier_expected")
        core_question_beat_count = sum(
            1 for beat in beats
            if str(beat.commerce_beat_id or "").strip().upper() in {"Q1", "Q2"}
        )
        if core_question_beat_count > 3:
            # This is a read-only Whole Video Audit finding.  It never removes
            # a Director-selected source beat or blocks a review MP4; the UI
            # can expose it so a user may delete/revise the repetitive core.
            audit_issues.append("core_question_overconcentration")
        whole_video_audit_verification = {
            "mode": "program_read_only_verification",
            "ai_self_audit_present": bool(audit),
            "declared_status": audit_status,
            "declared_beat_count": audit.get("selected_beat_count"),
            "declared_subtitle_id_count": audit.get("selected_subtitle_id_count"),
            "declared_seconds": audit.get("estimated_source_seconds"),
            "verified_beat_count": len(sequence),
            "verified_subtitle_id_count": verified_subtitle_count,
            "verified_seconds": selected_seconds,
            "verified_q1_q2_beat_count": core_question_beat_count,
            "verified_chapter_readthroughs": dict(verified_chapter_readthroughs),
            "passed": not audit_issues,
            "issues": audit_issues,
            "verified_readthrough": verified_readthrough,
        }
        quality_warnings.extend(
            f"single_pass_director_whole_video_audit:{item}"
            for item in audit_issues
        )
    # 30–120 seconds is the normal delivery range.  Target duration is soft:
    # a source-grounded shorter draft remains materializable for human review
    # and manual editing.  No program may withhold an MP4 merely because the
    # AI called a short story "strong" or mis-added its source timestamps.
    if beats and any(not candidate.hook_eligible for candidate in selected[:len(beats[0].candidate_ids)]):
        quality_warnings.append("single_pass_director_hook_ineligible")
    opening = None
    if has_opening:
        opening = OpeningPackage(
            promise=strategy.opening_promise or strategy.core_desire or strategy.core_commercial_idea or strategy.thesis,
            payoff_relation=str(beats[1].goal or "第二段真实口播承接开场承诺"),
            hook_candidate_ids=beats[0].candidate_ids,
            payoff_candidate_ids=beats[1].candidate_ids,
            selection_instruction="declared_by_single_ai_director_packet",
            hook_promise=strategy.opening_promise or strategy.core_desire or strategy.thesis,
            payoff_delivery=str(beats[1].goal or "真实口播即时兑现"),
            connection_reason=str(getattr(sequence[1], "why_this_follows", "") or beats[1].goal),
            hook_integrity_reason="AI selected this complete source utterance as the opening package.",
        )

    final_brief = {
        "authority": "director_narrative_contract",
        "director_title": strategy.director_title or strategy.core_commercial_idea or strategy.thesis,
        "core_desire": strategy.core_desire or strategy.core_commercial_idea or strategy.thesis,
        "opening_promise": strategy.opening_promise or strategy.core_desire or strategy.thesis,
        "quality_tier": declared_quality_tier,
        "video_structure": {
            "id": strategy.video_structure_id or strategy.narrative_archetype or "director_defined",
            "name": strategy.video_structure_name or strategy.narrative_archetype or "导演自定义结构",
            "selection_reason": strategy.video_structure_reason,
        },
        "source": "single_ai_director_packet",
    }
    contract.update({
        "single_ai_director_packet": True,
        "m1_source_story": CommercialStoryBrief.from_strategy(strategy).payload(),
        "director_narrative_contract": {
            **director,
            "core_desire": final_brief["core_desire"],
            "opening_promise": final_brief["opening_promise"],
            "video_structure": dict(final_brief["video_structure"]),
            "director_sequence": [item.to_dict() for item in sequence],
            "chapter_packets": [item.to_dict() for item in strategy.director_chapter_packets],
            "opening_alternative_packages": [
                item.to_dict() for item in strategy.director_opening_alternatives
            ],
            "whole_video_audit": dict(strategy.whole_video_audit or {}),
            "product_scope": dict(strategy.product_scope or {}),
            "opening_selection": dict(strategy.opening_selection or {}),
            "final_readthrough": str(getattr(strategy, "director_readthrough", "") or ""),
        },
        "single_ai_director_journey": [item.to_dict() for item in sequence],
        "director_chapter_packets": [item.to_dict() for item in strategy.director_chapter_packets],
        "director_opening_alternative_packages": [
            item.to_dict() for item in strategy.director_opening_alternatives
        ],
        "director_video_structure": dict(final_brief["video_structure"]),
        "director_whole_video_audit": dict(strategy.whole_video_audit or {}),
        "director_whole_video_audit_verification": whole_video_audit_verification,
        "director_verified_readthrough": verified_readthrough,
        "director_verified_chapter_readthroughs": dict(verified_chapter_readthroughs),
        "director_selection_pool": "complete_safe_executable_candidates",
        "strong_clip_ranking_used_before_journey": False,
        "director_journey_question_ids": list(journey_question_ids),
        "director_opening_scope": {
            "allowed_purchase_question_ids": list(opening_question_ids),
            "allowed_answer_roles": list(opening_answer_roles),
        },
        "director_opening_fallbacks_used": opening_fallbacks_used,
        "director_semantic_mutation_disabled": True,
        "director_quality_warnings": list(dict.fromkeys(quality_warnings)),
        "director_hard_execution_issues": list(issues),
        "final_story_brief": final_brief,
        # This is not the historical Commerce Lite multi-pass plan.  Keeping
        # the flag false prevents legacy Q-completion/budget heuristics from
        # reinterpreting the AI-authored director order.
        "commerce_lite_purchase_value_progression": False,
        "m1_consumption_validation_require_supporting_bridge": False,
    })
    # A source-grounded AI plan is editable even when its story is short,
    # repetitive, or has imperfect relationship metadata.  Only technical
    # source failures may prevent M3.
    plan_valid = not issues and bool(beats)
    journey_complete = len(journey_question_ids) >= 3
    raw_duration_range = contract.get("target_duration_range")
    duration_range = (
        dict(raw_duration_range)
        if isinstance(raw_duration_range, Mapping)
        else director_target_duration_range(
            target_duration,
            contract.get("duration_tolerance"),
        )
    )
    requested_seconds = float(duration_range.get("requested_seconds") or target_duration or 45.0)
    preferred_low = float(duration_range.get("preferred_low") or max(30.0, requested_seconds * 0.80))
    preferred_high = float(duration_range.get("preferred_high") or min(120.0, requested_seconds * 1.10))
    duration_status = (
        "target_range_fulfilled"
        if plan_valid and journey_complete and preferred_low <= selected_seconds <= preferred_high
        else "natural_shortfall_below_requested_duration"
        if plan_valid and journey_complete and selected_seconds < preferred_low
        else "natural_complete_above_requested_duration"
        if plan_valid and journey_complete
        else "short_draft_below_normal_duration" if plan_valid
        else "director_packet_invalid"
    )
    duration_assessment = {
        "status": duration_status,
        "reason": (
            "AI 导演短句 Casting 已按真实可执行字幕物化；程序没有补句，实际时长按目标区间如实标记。"
            if journey_complete else
            "AI 导演短句 Casting 给出了可预览的短草案，但购买链仍偏薄；保留给人工比较和编辑，不伪装成完成目标时长的商品片。"
        ),
        "duration_note": (
            f"真实时长 {selected_seconds:.1f}s；本次目标 {requested_seconds:.0f}s，"
            f"验收区间 {preferred_low:.1f}–{preferred_high:.1f}s。预览仍允许，以便人工比较和手动编辑。"
        ),
        "actual_seconds": selected_seconds,
        "requested_seconds": requested_seconds,
        "preferred_low": preferred_low,
        "preferred_high": preferred_high,
        "m3_render_gate": {
            "passed": plan_valid,
            "reason": (
                "single_ai_director_packet_source_lineage_resolved"
                if plan_valid else "single_ai_director_packet_source_lineage_unresolved"
            ),
            "issues": list(issues),
        },
        "quality_warnings": list(dict.fromkeys(quality_warnings)),
    }
    consumption = StoryConsumption(
        hero_strategy_id=strategy.strategy_id,
        hero_priority=strategy.story_priority,
        hero_consistency_reason="同一次 AI 导演结果定义成片核心购买欲望与真实口播顺序。",
        supporting_chapter_ids=(),
        bridge_chapter_ids=(),
        no_rediscovery=True,
    )
    return NarrativePlan(
        strategy_id=strategy.strategy_id,
        thesis=strategy.core_desire or strategy.core_commercial_idea or strategy.thesis,
        target_duration=float(target_duration),
        beats=tuple(beats),
        status=(
            "director_ready" if plan_valid and duration_status == "target_range_fulfilled"
            else "director_short_draft" if plan_valid
            else "director_packet_invalid"
        ),
        recommended_duration=selected_seconds,
        issues=tuple(issues),
        removed_beats=(),
        plan_valid=plan_valid,
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=opening,
        selection_contract=contract,
        selected_candidates=tuple(selected),
        replan_request=(
            None if plan_valid else ReplanRequest(
                reason_codes=tuple(issues) or ("single_pass_director_packet_invalid",),
                detail=("The one AI director packet could not be bound to executable source.",),
            )
        ),
        duration_assessment=duration_assessment,
        story_consumption=consumption,
    )
