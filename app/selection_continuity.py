from __future__ import annotations

import os
from typing import Any, Iterable


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _source_key(clip: dict[str, Any]) -> str:
    source = str(clip.get("source") or "").strip().strip('"')
    if source:
        return os.path.normcase(os.path.abspath(source))
    return str(clip.get("source_marker") or "unknown").strip().upper()


def annotate_continuity_groups(
    clips: Iterable[dict[str, Any]],
    *,
    max_gap_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    """Annotate the current order without selecting, deleting, or reordering clips."""
    result = [clip for clip in clips if isinstance(clip, dict)]
    group_number = 0
    group_positions: dict[str, int] = {}
    previous: dict[str, Any] | None = None

    for clip in result:
        source_key = _source_key(clip)
        start = _number(clip.get("start"))
        end = max(start, _number(clip.get("end"), start))
        reason = "first_clip"
        continuous = False
        gap = 0.0

        if previous is not None:
            previous_source = str(previous.get("_continuity_source") or "")
            previous_start = _number(previous.get("start"))
            previous_end = max(previous_start, _number(previous.get("end"), previous_start))
            gap = start - previous_end
            if source_key != previous_source:
                reason = "source_change"
            elif start + 0.05 < previous_start:
                reason = "time_reverse"
            elif gap < -0.05:
                reason = "source_overlap"
            elif gap <= max_gap_seconds:
                reason = "same_source_continuation"
                continuous = True
            else:
                reason = "source_time_gap"

        if not continuous:
            group_number += 1
        group_key = f"continuity-{group_number}"
        position = group_positions.get(group_key, 0)
        group_positions[group_key] = position + 1
        clip["continuity_group"] = group_key
        clip["continuity_position"] = position
        clip["continuity_break_before"] = not continuous and previous is not None
        clip["transition_reason"] = reason
        clip["transition_gap_seconds"] = round(gap, 3)
        clip["_continuity_source"] = source_key
        previous = clip

    group_sizes: dict[str, int] = {}
    for clip in result:
        key = str(clip.get("continuity_group") or "")
        group_sizes[key] = group_sizes.get(key, 0) + 1
    for clip in result:
        key = str(clip.get("continuity_group") or "")
        clip["continuity_size"] = group_sizes.get(key, 1)
        clip.pop("_continuity_source", None)
    return result

