from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


SELECTION_CONTEXT_VERSION = "selection-context-v1"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _source_id(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text or "SINGLE"


def _source_label(source_id: str) -> str:
    text = _source_id(source_id).strip("[]")
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in {"-", "_"})
    return cleaned or "SINGLE"


@dataclass(frozen=True)
class CandidateContext:
    candidate_id: int
    source_id: str
    start: float
    end: float
    story_block_id: str
    continuity_group_id: str

    def payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "story_block_id": self.story_block_id,
            "continuity_group_id": self.continuity_group_id,
        }


@dataclass(frozen=True)
class StoryBlockContext:
    story_block_id: str
    source_id: str
    candidate_ids: tuple[int, ...]
    start: float
    end: float
    continuity_group_ids: tuple[str, ...]

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def payload(self) -> dict[str, Any]:
        return {
            "story_block_id": self.story_block_id,
            "source_id": self.source_id,
            "candidate_ids": list(self.candidate_ids),
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "continuity_group_ids": list(self.continuity_group_ids),
        }


@dataclass(frozen=True)
class SelectionContext:
    candidates: tuple[CandidateContext, ...]
    story_blocks: tuple[StoryBlockContext, ...]
    version: str = SELECTION_CONTEXT_VERSION

    def candidate_map(self) -> dict[int, CandidateContext]:
        return {item.candidate_id: item for item in self.candidates}

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "candidate_count": len(self.candidates),
            "story_block_count": len(self.story_blocks),
            "continuity_group_count": len({
                item.continuity_group_id for item in self.candidates
            }),
            "source_count": len({item.source_id for item in self.candidates}),
        }


def _entry_values(raw: Any, index: int) -> tuple[int, str, float, float]:
    if isinstance(raw, dict):
        candidate_id = int(raw.get("candidate_id") or index)
        source_id = _source_id(raw.get("source_id") or raw.get("source"))
        start = _number(raw.get("start"))
        end = max(start, _number(raw.get("end"), start))
        return candidate_id, source_id, start, end

    candidate_id = int(getattr(raw, "candidate_id", index) or index)
    source_id = _source_id(getattr(raw, "source_id", ""))
    start = _number(getattr(raw, "start", 0.0))
    end = max(start, _number(getattr(raw, "end", start), start))
    return candidate_id, source_id, start, end


def build_selection_context(
    candidates: Sequence[Any] | Iterable[Any],
    *,
    continuity_gap_seconds: float = 2.2,
    story_gap_seconds: float = 12.0,
    story_max_candidates: int = 12,
    story_max_duration: float = 75.0,
) -> SelectionContext:
    """Create stable structural metadata without selecting or reordering candidates."""
    rows = [
        _entry_values(raw, index)
        for index, raw in enumerate(list(candidates or ()), 1)
    ]
    if not rows:
        return SelectionContext((), ())

    continuity_gap = max(0.0, float(continuity_gap_seconds or 0.0))
    story_gap = max(continuity_gap, float(story_gap_seconds or continuity_gap))
    max_candidates = max(1, int(story_max_candidates or 1))
    max_duration = max(1.0, float(story_max_duration or 1.0))

    candidate_contexts: list[CandidateContext] = []
    block_rows: list[list[CandidateContext]] = []
    current_block: list[CandidateContext] = []
    previous_source = ""
    previous_start = 0.0
    previous_end = 0.0
    continuity_number = 0
    story_number = 0
    current_continuity_id = ""
    current_story_id = ""

    for candidate_id, source_id, start, end in rows:
        gap = start - previous_end if candidate_contexts else 0.0
        source_changed = bool(candidate_contexts and source_id != previous_source)
        time_reversed = bool(candidate_contexts and start + 0.05 < previous_start)
        continuity_break = bool(
            not candidate_contexts
            or source_changed
            or time_reversed
            or gap < -0.05
            or gap > continuity_gap
        )

        block_start = current_block[0].start if current_block else start
        story_break = bool(
            not current_block
            or source_changed
            or time_reversed
            or gap < -0.05
            or gap > story_gap
            or len(current_block) >= max_candidates
            or end - block_start > max_duration
        )

        if story_break:
            if current_block:
                block_rows.append(current_block)
            story_number += 1
            current_story_id = f"SB-{_source_label(source_id)}-{story_number:03d}"
            current_block = []
            continuity_break = True

        if continuity_break:
            continuity_number += 1
            current_continuity_id = (
                f"CG-{_source_label(source_id)}-{continuity_number:03d}"
            )

        context = CandidateContext(
            candidate_id=candidate_id,
            source_id=source_id,
            start=start,
            end=end,
            story_block_id=current_story_id,
            continuity_group_id=current_continuity_id,
        )
        candidate_contexts.append(context)
        current_block.append(context)
        previous_source = source_id
        previous_start = start
        previous_end = end

    if current_block:
        block_rows.append(current_block)

    blocks = []
    for rows_in_block in block_rows:
        continuity_ids = tuple(dict.fromkeys(
            item.continuity_group_id for item in rows_in_block
        ))
        blocks.append(StoryBlockContext(
            story_block_id=rows_in_block[0].story_block_id,
            source_id=rows_in_block[0].source_id,
            candidate_ids=tuple(item.candidate_id for item in rows_in_block),
            start=min(item.start for item in rows_in_block),
            end=max(item.end for item in rows_in_block),
            continuity_group_ids=continuity_ids,
        ))

    return SelectionContext(tuple(candidate_contexts), tuple(blocks))
