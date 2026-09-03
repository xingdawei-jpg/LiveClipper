"""Offline M3 materializer for approved commercial-story candidates.

The Selector owns exact playable boundaries, never commercial direction.  It
may shorten only the edges of an M2-approved candidate, and only when the
remaining spoken words form one complete expression at source word boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from candidate_quality import leading_fragment_trim
from story_planner import NarrativePlan, PlanningCandidate


CLIP_SELECTOR_VERSION = "m3-clip-selector-prototype-v1"
_VOCAL_FILLERS = frozenset({"嗯", "啊", "呃", "哦", "诶"})
_STRONG_PUNCTUATION = frozenset("。！？!?")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _clean_words(raw_words: Sequence[Mapping[str, Any]] | None) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for raw in raw_words or ():
        if not isinstance(raw, Mapping):
            continue
        text = str(raw.get("text") or "").strip()
        start = _number(raw.get("start"), -1.0)
        end = _number(raw.get("end"), -1.0)
        if not text or start < 0 or end <= start:
            continue
        result.append({"text": text, "start": start, "end": end})
    return tuple(result)


def _plain_text(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or ""))


def _punctuation_after_words(
    text: str,
    words: Sequence[Mapping[str, Any]],
) -> dict[int, str]:
    """Reuse only punctuation already aligned to these exact spoken words."""
    plain = "".join(_plain_text(word.get("text")) for word in words)
    source = str(text or "")
    if not plain or _plain_text(source) != plain:
        return {}
    punctuation: dict[int, str] = {}
    offset = 0
    for index, word in enumerate(words):
        offset += len(_plain_text(word.get("text")))
        cursor = 0
        spoken = 0
        marker = ""
        while cursor < len(source) and spoken < offset:
            char = source[cursor]
            if _plain_text(char):
                spoken += len(_plain_text(char))
            cursor += 1
        while cursor < len(source) and not _plain_text(source[cursor]):
            marker += source[cursor]
            cursor += 1
        if marker:
            punctuation[index] = marker
    return punctuation


def _render_words(
    words: Sequence[Mapping[str, Any]],
    punctuation: Mapping[int, str],
    start_index: int,
    end_index: int,
) -> str:
    parts: list[str] = []
    for index in range(start_index, end_index + 1):
        parts.append(str(words[index].get("text") or ""))
        marker = str(punctuation.get(index) or "")
        if marker:
            parts.append(marker)
    return "".join(parts).strip()


def _standalone_boundary_reason(text: str) -> str:
    """Use the existing candidate-boundary contract without changing it."""
    from ai_clipper import _director_standalone_boundary_reason

    return str(_director_standalone_boundary_reason(text) or "")


def _playable_expression_boundary_reason(text: str) -> str:
    """Reject a few observable ASR tails before an offline range is emitted.

    This is deliberately a playback validator, not a story rule.  It only
    covers malformed endings that cannot be a complete Chinese expression even
    when the older freezer did not recognize them.  M3 must return a blocker
    here rather than trying to join another candidate or invent an ending.
    """
    inherited = _standalone_boundary_reason(text)
    if inherited:
        return inherited
    compact = re.sub(r"[\s，。！？!?、；;：:]+$", "", str(text or ""))
    if re.search(r"(?:那种|一个|这样|这种).{0,18}的精$", compact):
        return "词尾残缺"
    if re.search(r"(?:显得|看起来|穿上).{0,24}之外(?:，|,)?", compact) and "除了" not in compact:
        return "转折承接未保留"
    if re.search(r"(?:就一件){1,3}就穿的$", compact):
        return "句子残缺"
    return ""


def _leading_trim_index(words: Sequence[Mapping[str, Any]]) -> tuple[int, str]:
    index = 0
    while index < len(words) and str(words[index].get("text") or "").strip() in _VOCAL_FILLERS:
        index += 1
    if index:
        return index, "leading_vocal_trim"

    edge = leading_fragment_trim(words)
    if not edge:
        return 0, ""
    boundary = _number(edge.get("boundary"), -1.0)
    for position, word in enumerate(words):
        if float(word["start"]) >= boundary - 1e-6:
            return position, "leading_fragment_trim"
    return 0, ""


@dataclass(frozen=True)
class SelectedRange:
    chapter_id: str
    parent_candidate_id: int
    source_id: str
    start: float
    end: float
    text: str
    origin_subtitle_ids: tuple[int, ...]
    boundary_kind: str
    removed_prefix_text: str = ""

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end - self.start), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "parent_candidate_id": self.parent_candidate_id,
            "source_id": self.source_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": self.duration,
            "text": self.text,
            "origin_subtitle_ids": list(self.origin_subtitle_ids),
            "boundary_kind": self.boundary_kind,
            "removed_prefix_text": self.removed_prefix_text,
        }


@dataclass(frozen=True)
class SelectorBlocked:
    chapter_id: str
    parent_candidate_id: int
    code: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "parent_candidate_id": self.parent_candidate_id,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ClipSelectionResult:
    status: str
    ranges: tuple[SelectedRange, ...]
    blocked: tuple[SelectorBlocked, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CLIP_SELECTOR_VERSION,
            "status": self.status,
            "ranges": [item.to_dict() for item in self.ranges],
            "selector_blocked": [item.to_dict() for item in self.blocked],
        }


@dataclass(frozen=True)
class CandidateWordBinding:
    """Explicit candidate-to-word bridge; never reconstructed by time overlap."""

    words_by_candidate: Mapping[int, tuple[dict[str, Any], ...]]
    unbound_reasons: tuple[str, ...]


def audit_materialization_fidelity(
    plan: NarrativePlan,
    result: ClipSelectionResult,
    ledger_assets: Sequence[Mapping[str, Any]] | None = None,
    *,
    require_word_boundaries: bool = False,
) -> dict[str, Any]:
    """Audit that M3 only materialized the exact M2 plan.

    This is deliberately a lineage and boundary audit, not another director.
    It never judges which commercial theme is better and does not inspect video
    pixels.  ``ledger_assets`` therefore proves only that every range has an
    unchanged hard-safe/Commercial Asset Ledger source; it is not a product
    recognition result.
    """
    expected_pairs = [
        (beat.chapter_id, candidate_id)
        for beat in plan.beats
        for candidate_id in beat.candidate_ids
    ]
    selected_by_id = {candidate.candidate_id: candidate for candidate in plan.selected_candidates}
    materialized_pairs = [
        (item.chapter_id, item.parent_candidate_id) for item in result.ranges
    ]
    ledger_by_id: dict[int, Mapping[str, Any]] = {}
    for asset in ledger_assets or ():
        try:
            candidate_id = int(asset.get("candidate_id") or 0)
        except (AttributeError, TypeError, ValueError):
            candidate_id = 0
        if candidate_id > 0:
            ledger_by_id[candidate_id] = asset

    issues: list[str] = []
    if not plan.plan_valid:
        issues.append("m2_plan_invalid")
    if result.blocked:
        issues.append("selector_blocked")
    if materialized_pairs != expected_pairs:
        issues.append("chapter_or_candidate_order_changed")

    off_plan = [
        item.parent_candidate_id for item in result.ranges
        if (item.chapter_id, item.parent_candidate_id) not in expected_pairs
        or item.parent_candidate_id not in selected_by_id
    ]
    if off_plan:
        issues.append("materialized_candidate_not_in_m2_plan")

    ledger_identity_errors: list[int] = []
    if ledger_assets is not None:
        for candidate_id in {candidate_id for _, candidate_id in expected_pairs}:
            candidate = selected_by_id.get(candidate_id)
            asset = ledger_by_id.get(candidate_id)
            if candidate is None or asset is None:
                ledger_identity_errors.append(candidate_id)
                continue
            if (
                _plain_text(candidate.text) != _plain_text(asset.get("text"))
                or abs(candidate.start - _number(asset.get("start"), candidate.start)) > 1e-3
                or abs(candidate.end - _number(asset.get("end"), candidate.end)) > 1e-3
            ):
                ledger_identity_errors.append(candidate_id)
    if ledger_identity_errors:
        issues.append("candidate_ledger_identity_mismatch")

    subtitle_errors: list[int] = []
    non_word_exact: list[int] = []
    for item in result.ranges:
        candidate = selected_by_id.get(item.parent_candidate_id)
        if candidate is None:
            subtitle_errors.append(item.parent_candidate_id)
            continue
        reconstructed = f"{item.removed_prefix_text}{item.text}"
        if not item.text or _plain_text(reconstructed) != _plain_text(candidate.text):
            subtitle_errors.append(item.parent_candidate_id)
        if item.boundary_kind == "whole_candidate_no_word_timing":
            non_word_exact.append(item.parent_candidate_id)
    if subtitle_errors:
        issues.append("subtitle_text_not_a_lossless_candidate_expression")
    if require_word_boundaries and (non_word_exact or len(result.ranges) != len(expected_pairs)):
        issues.append("verified_word_boundaries_required")

    return {
        "passed": not issues,
        "issues": issues,
        "expected_chapter_candidate_pairs": [list(item) for item in expected_pairs],
        "materialized_chapter_candidate_pairs": [list(item) for item in materialized_pairs],
        "selector_blocked": [item.to_dict() for item in result.blocked],
        "off_plan_candidate_ids": sorted(set(off_plan)),
        "candidate_ledger_identity_error_ids": sorted(set(ledger_identity_errors)),
        "subtitle_text_error_ids": sorted(set(subtitle_errors)),
        "non_word_exact_candidate_ids": sorted(set(non_word_exact)),
        "no_story_rewrite": not off_plan,
        "complete_plan_materialized": not result.blocked and materialized_pairs == expected_pairs,
        "ledger_lineage_only_not_visual_product_recognition": True,
    }


def bind_candidate_words_by_origin(
    candidates: Sequence[PlanningCandidate],
    source_subtitles: Sequence[Mapping[str, Any]],
    sidecar_segments: Sequence[Mapping[str, Any]],
) -> CandidateWordBinding:
    """Bind words through verified source subtitle identity, never timestamps.

    Older sidecars do not persist their subtitle IDs. They can still be used
    only when their ordered source text exactly agrees with the SRT rows.  Any
    mismatch means no edge-level materialization for the affected run.
    """
    subtitles = [item for item in source_subtitles or () if isinstance(item, Mapping)]
    segments = [item for item in sidecar_segments or () if isinstance(item, Mapping)]
    if len(subtitles) != len(segments):
        return CandidateWordBinding({}, (
            f"sidecar_subtitle_count_mismatch:{len(segments)}!={len(subtitles)}",
        ))
    words_by_subtitle: dict[int, tuple[dict[str, Any], ...]] = {}
    for ordinal, (subtitle, segment) in enumerate(zip(subtitles, segments), 1):
        try:
            subtitle_id = int(subtitle.get("id") or 0)
        except (TypeError, ValueError):
            subtitle_id = 0
        if subtitle_id <= 0:
            return CandidateWordBinding({}, (f"invalid_source_subtitle_id:{ordinal}",))
        if _plain_text(subtitle.get("text")) != _plain_text(segment.get("text")):
            return CandidateWordBinding({}, (f"sidecar_text_mismatch_at_source:{subtitle_id}",))
        words = _clean_words(segment.get("words"))
        if not words:
            return CandidateWordBinding({}, (f"sidecar_words_missing_at_source:{subtitle_id}",))
        words_by_subtitle[subtitle_id] = words

    bound: dict[int, tuple[dict[str, Any], ...]] = {}
    reasons: list[str] = []
    for candidate in candidates or ():
        origin_ids = tuple(int(value) for value in candidate.origin_subtitle_ids if int(value) > 0)
        if not origin_ids:
            reasons.append(f"candidate_origin_missing:{candidate.candidate_id}")
            continue
        words: list[dict[str, Any]] = []
        for subtitle_id in origin_ids:
            source_words = words_by_subtitle.get(subtitle_id)
            if not source_words:
                reasons.append(f"candidate_origin_words_missing:{candidate.candidate_id}:{subtitle_id}")
                words = []
                break
            words.extend(dict(item) for item in source_words)
        if not words:
            continue
        if _plain_text(candidate.text) != "".join(_plain_text(item["text"]) for item in words):
            reasons.append(f"candidate_text_words_mismatch:{candidate.candidate_id}")
            continue
        bound[candidate.candidate_id] = tuple(words)
    return CandidateWordBinding(bound, tuple(reasons))


def _materialize_candidate(
    chapter_id: str,
    candidate: PlanningCandidate,
    words: Sequence[Mapping[str, Any]] | None,
) -> SelectedRange | SelectorBlocked:
    """Use only an approved candidate and its explicitly bound timed words."""
    clean_words = _clean_words(words)
    whole_reason = _playable_expression_boundary_reason(candidate.text)
    if not clean_words:
        if whole_reason:
            return SelectorBlocked(
                chapter_id, candidate.candidate_id, "candidate_not_complete", whole_reason
            )
        return SelectedRange(
            chapter_id, candidate.candidate_id, candidate.source_id,
            candidate.start, candidate.end, candidate.text,
            candidate.origin_subtitle_ids, "whole_candidate_no_word_timing",
        )

    punctuation = _punctuation_after_words(candidate.text, clean_words)
    start_index, trim_kind = _leading_trim_index(clean_words)
    if start_index >= len(clean_words):
        return SelectorBlocked(
            chapter_id, candidate.candidate_id, "only_edge_filler", "candidate contains no spoken expression after edge trim"
        )

    # A candidate with multiple standalone sentences has several possible
    # meanings. The prototype refuses to pick an arbitrary middle sentence;
    # that would silently direct the story. Preserve the whole candidate if it
    # is valid, otherwise return a structured request for M2/M3 clarification.
    sentence_ends = [
        index for index, marker in punctuation.items()
        if any(char in _STRONG_PUNCTUATION for char in marker) and index >= start_index
    ]
    internal_sentence_end = any(index < len(clean_words) - 1 for index in sentence_ends)
    if internal_sentence_end and not trim_kind:
        if whole_reason:
            return SelectorBlocked(
                chapter_id, candidate.candidate_id, "ambiguous_complete_subunits", whole_reason
            )
        return SelectedRange(
            chapter_id, candidate.candidate_id, candidate.source_id,
            float(clean_words[0]["start"]), float(clean_words[-1]["end"]), candidate.text,
            candidate.origin_subtitle_ids, "whole_candidate_multiple_complete_units",
        )

    text = _render_words(clean_words, punctuation, start_index, len(clean_words) - 1)
    reason = _playable_expression_boundary_reason(text)
    if reason:
        return SelectorBlocked(
            chapter_id, candidate.candidate_id, "trim_would_break_semantic_unit", reason
        )
    return SelectedRange(
        chapter_id, candidate.candidate_id, candidate.source_id,
        float(clean_words[start_index]["start"]), float(clean_words[-1]["end"]),
        text, candidate.origin_subtitle_ids, trim_kind or "word_exact_whole_expression",
        _render_words(clean_words, punctuation, 0, start_index - 1) if start_index else "",
    )


def assess_candidate_materializability(
    candidate: PlanningCandidate,
    words: Sequence[Mapping[str, Any]] | None,
) -> SelectedRange | SelectorBlocked:
    """Check one approved candidate's playback boundary without directing it.

    M2 adapters may use this as a hard-safe completeness precondition before
    planning.  It does not delete Ledger assets, select replacements, or make
    any commercial decision; it simply exposes the same residual/fragment
    blocker M3 would later return.
    """
    return _materialize_candidate("", candidate, words)


def materialize_narrative_plan(
    plan: NarrativePlan,
    candidate_words: Mapping[int, Sequence[Mapping[str, Any]]] | None = None,
) -> ClipSelectionResult:
    """Materialize an already-valid plan without selecting or reordering content.

    ``candidate_words`` is keyed by M2-approved candidate ID. It must be
    explicitly supplied from Candidate Ledger/word timing lineage; this module
    never finds words by time containment.
    """
    if not plan.plan_valid:
        return ClipSelectionResult(
            "selector_blocked", (),
            (SelectorBlocked("", 0, "narrative_plan_invalid", "M2 plan is not valid"),),
        )
    by_id = {candidate.candidate_id: candidate for candidate in plan.selected_candidates}
    ranges: list[SelectedRange] = []
    blocked: list[SelectorBlocked] = []
    used_ids: set[int] = set()
    for beat in plan.beats:
        for candidate_id in beat.candidate_ids:
            candidate = by_id.get(candidate_id)
            if candidate is None:
                blocked.append(SelectorBlocked(
                    beat.chapter_id, candidate_id, "candidate_not_approved_by_m2",
                    "beat references a candidate absent from M2 selected_candidates",
                ))
                continue
            if candidate_id in used_ids:
                blocked.append(SelectorBlocked(
                    beat.chapter_id, candidate_id, "candidate_reused_by_plan",
                    "Selector cannot duplicate an approved candidate into another beat",
                ))
                continue
            item = _materialize_candidate(
                beat.chapter_id,
                candidate,
                (candidate_words or {}).get(candidate_id),
            )
            if isinstance(item, SelectorBlocked):
                blocked.append(item)
            else:
                ranges.append(item)
                used_ids.add(candidate_id)
    return ClipSelectionResult(
        "selector_blocked" if blocked else "ok", tuple(ranges), tuple(blocked)
    )
