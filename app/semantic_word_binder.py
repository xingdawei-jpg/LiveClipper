"""Strict lineage between semantic SRT rows and a managed word timeline.

Visible SRT subtitles may be resegmented for reading and directing while the
sidecar retains provider-level timed tokens.  This module reconnects them by
monotonic text/token sequence only.  It never uses timestamp containment,
semantic similarity, or a nearest-neighbour fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping, Sequence

from asr_cache import inspect_cache
from srt_parser import open_srt
from volcengine_asr import load_word_timing_sidecar


_CN_NUMBER_RE = re.compile(r"[零〇一二两三四五六七八九十百千万亿]+")
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10_000, "亿": 100_000_000}


def _basic_text(value: object) -> str:
    """Remove presentation-only differences, but retain lexical content."""
    return "".join(
        char for char in str(value or "")
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def _cn_number_to_arabic(value: str) -> str:
    if not value:
        return ""
    if all(char in _CN_DIGITS for char in value):
        return "".join(str(_CN_DIGITS[char]) for char in value)
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            continue
        unit = _CN_UNITS.get(char)
        if unit is None:
            return value
        if unit >= 10_000:
            section = (section + number) * unit
            total += section
            section = 0
        else:
            section += (number or 1) * unit
        number = 0
    return str(total + section + number)


def _canonical_text(value: object) -> str:
    """Allow only formatting/case/full-width and unambiguous number forms."""
    normalized = unicodedata.normalize("NFKC", _basic_text(value)).lower()
    return _CN_NUMBER_RE.sub(lambda match: _cn_number_to_arabic(match.group(0)), normalized)


def _clean_words(segments: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    words: list[dict[str, Any]] = []
    last_start = -1.0
    last_end = -1.0
    for segment in segments:
        for raw in segment.get("words") or ():
            if not isinstance(raw, Mapping):
                continue
            text = str(raw.get("text") or "").strip()
            normalized = _basic_text(text)
            try:
                start = float(raw.get("start"))
                end = float(raw.get("end"))
            except (TypeError, ValueError):
                continue
            if not normalized or end < start:
                continue
            if start + 1e-6 < last_start or end + 1e-6 < last_end:
                raise ValueError("word_timeline_not_monotonic")
            words.append({"text": text, "start": start, "end": end})
            last_start = start
            last_end = end
    return tuple(words)


@dataclass(frozen=True)
class SemanticSrtWordSpan:
    subtitle_id: int
    text: str
    status: str
    alignment_kind: str
    alignment_confidence: float
    word_start_index: int | None
    word_end_index: int | None
    word_start_time: float | None
    word_end_time: float | None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtitle_id": self.subtitle_id,
            "text": self.text,
            "status": self.status,
            "alignment_kind": self.alignment_kind,
            "alignment_confidence": self.alignment_confidence,
            "word_start_index": self.word_start_index,
            "word_end_index": self.word_end_index,
            "word_start_time": self.word_start_time,
            "word_end_time": self.word_end_time,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SemanticSrtWordTimeline:
    source_srt: str
    source_video: str
    word_sidecar: str
    source_identity: Mapping[str, Any]
    words: tuple[dict[str, Any], ...]
    spans: tuple[SemanticSrtWordSpan, ...]
    validation_issues: tuple[str, ...]

    @property
    def by_subtitle_id(self) -> dict[int, SemanticSrtWordSpan]:
        return {item.subtitle_id: item for item in self.spans}

    def report(self) -> dict[str, Any]:
        exact = sum(item.status == "bound" and item.alignment_kind == "exact" for item in self.spans)
        normalized = sum(item.status == "bound" and item.alignment_kind == "normalized" for item in self.spans)
        ambiguous = sum(item.status == "ambiguous" for item in self.spans)
        unmatched = sum(item.status == "unmatched" for item in self.spans)
        total = len(self.spans)
        bound = exact + normalized
        return {
            "source_srt": self.source_srt,
            "source_video": self.source_video,
            "word_sidecar": self.word_sidecar,
            "source_identity": dict(self.source_identity),
            "total_srt_segments": total,
            "exact_aligned": exact,
            "normalized_aligned": normalized,
            "ambiguous": ambiguous,
            "unmatched": unmatched,
            "coverage": round(bound / total, 6) if total else 0.0,
            "word_count": len(self.words),
            "unclaimed_word_count": _unclaimed_word_count(self.spans, len(self.words)),
            "validation_issues": list(self.validation_issues),
            "spans": [item.to_dict() for item in self.spans],
        }


def _unclaimed_word_count(spans: Sequence[SemanticSrtWordSpan], word_count: int) -> int:
    claimed: set[int] = set()
    for span in spans:
        if span.status == "bound" and span.word_start_index is not None and span.word_end_index is not None:
            claimed.update(range(span.word_start_index, span.word_end_index + 1))
    return max(0, word_count - len(claimed))


def _unbound_span(subtitle_id: int, text: str, *, status: str, detail: str) -> SemanticSrtWordSpan:
    return SemanticSrtWordSpan(subtitle_id, text, status, "", 0.0, None, None, None, None, detail)


def build_semantic_srt_word_timeline(
    srt_path: str | Path,
    *,
    source_video_path: str | Path | None = None,
    require_managed_identity: bool = True,
) -> SemanticSrtWordTimeline:
    """Bind every semantic SRT row to one unique, contiguous timed word span.

    The semantic sidecar view is reconstructed through the existing, immutable
    provider-word + semantic-segmentation path.  Matching then consumes that
    word stream once from left to right.  A mismatch halts the chain instead of
    searching later text or using timestamps to recover a plausible span.
    """
    srt = Path(srt_path).resolve()
    video = Path(source_video_path).resolve() if source_video_path else srt.with_suffix(".mp4")
    sidecar = srt.with_suffix(".words.json")
    identity = inspect_cache(video, srt) if video.is_file() else {
        "valid": False,
        "managed": srt.with_suffix(".asr-cache.json").is_file(),
        "reason": "source_video_missing",
    }
    rows, _encoding = open_srt(str(srt))
    row_payload = [(int(row.index), str(row.text or "").strip()) for row in rows]
    if require_managed_identity and not (
        identity.get("valid") and identity.get("managed") and identity.get("timing_precision") == "word"
    ):
        spans = tuple(
            _unbound_span(subtitle_id, text, status="unmatched", detail="managed_word_identity_not_verified")
            for subtitle_id, text in row_payload
        )
        return SemanticSrtWordTimeline(str(srt), str(video), str(sidecar), identity, (), spans, ("managed_word_identity_not_verified",))

    try:
        semantic_segments = load_word_timing_sidecar(str(srt), semantic=True)
        words = _clean_words(semantic_segments)
    except Exception as exc:
        spans = tuple(
            _unbound_span(subtitle_id, text, status="unmatched", detail="semantic_word_timeline_unavailable")
            for subtitle_id, text in row_payload
        )
        return SemanticSrtWordTimeline(
            str(srt), str(video), str(sidecar), identity, (), spans,
            (f"semantic_word_timeline_unavailable:{type(exc).__name__}",),
        )
    if not words:
        spans = tuple(
            _unbound_span(subtitle_id, text, status="unmatched", detail="semantic_word_timeline_empty")
            for subtitle_id, text in row_payload
        )
        return SemanticSrtWordTimeline(str(srt), str(video), str(sidecar), identity, (), spans, ("semantic_word_timeline_empty",))

    spans: list[SemanticSrtWordSpan] = []
    cursor = 0
    halted = False
    for subtitle_id, text in row_payload:
        if halted:
            spans.append(_unbound_span(subtitle_id, text, status="unmatched", detail="blocked_after_previous_unmatched"))
            continue
        basic_target = _basic_text(text)
        canonical_target = _canonical_text(text)
        if not canonical_target:
            spans.append(_unbound_span(subtitle_id, text, status="unmatched", detail="empty_normalized_srt_text"))
            halted = True
            continue
        raw = ""
        candidates: list[tuple[int, str]] = []
        for end_index in range(cursor, len(words)):
            raw += _basic_text(words[end_index]["text"])
            canonical = _canonical_text(raw)
            if raw == basic_target:
                candidates.append((end_index, "exact"))
            elif canonical == canonical_target:
                candidates.append((end_index, "normalized"))
            # Canonical Chinese-number conversion is not prefix-stable while
            # a number is still being read (``万`` -> ``10000`` but ``万一``
            # -> ``10001``).  The unconverted stream is therefore also a
            # valid strict-prefix proof; it is still lexical equality, never
            # a search for similar text later on the timeline.
            if basic_target.startswith(raw) or canonical_target.startswith(canonical):
                continue
            break
        if len(candidates) != 1:
            status = "ambiguous" if len(candidates) > 1 else "unmatched"
            detail = "multiple_contiguous_word_spans" if candidates else "text_sequence_mismatch"
            spans.append(_unbound_span(subtitle_id, text, status=status, detail=detail))
            halted = True
            continue
        end_index, kind = candidates[0]
        spans.append(SemanticSrtWordSpan(
            subtitle_id=subtitle_id,
            text=text,
            status="bound",
            alignment_kind=kind,
            alignment_confidence=1.0 if kind == "exact" else 0.98,
            word_start_index=cursor,
            word_end_index=end_index,
            word_start_time=round(float(words[cursor]["start"]), 3),
            word_end_time=round(float(words[end_index]["end"]), 3),
        ))
        cursor = end_index + 1

    issues = _validate_spans(spans, words)
    return SemanticSrtWordTimeline(str(srt), str(video), str(sidecar), identity, words, tuple(spans), tuple(issues))


def _validate_spans(spans: Sequence[SemanticSrtWordSpan], words: Sequence[Mapping[str, Any]]) -> list[str]:
    issues: list[str] = []
    prior_end = -1
    prior_time = -1.0
    for span in spans:
        if span.status != "bound":
            continue
        if span.word_start_index is None or span.word_end_index is None:
            issues.append(f"bound_span_missing_indices:{span.subtitle_id}")
            continue
        if span.word_start_index > span.word_end_index or span.word_start_index <= prior_end:
            issues.append(f"overlapping_or_nonmonotonic_span:{span.subtitle_id}")
        if span.word_end_index >= len(words):
            issues.append(f"span_outside_word_timeline:{span.subtitle_id}")
            continue
        rendered = "".join(str(words[index].get("text") or "") for index in range(span.word_start_index, span.word_end_index + 1))
        if _canonical_text(rendered) != _canonical_text(span.text):
            issues.append(f"span_text_not_lossless:{span.subtitle_id}")
        if span.word_start_time is None or span.word_end_time is None or span.word_start_time < prior_time:
            issues.append(f"span_time_not_monotonic:{span.subtitle_id}")
        prior_end = span.word_end_index
        prior_time = float(span.word_end_time or prior_time)
    return issues


def bind_candidates_by_semantic_srt(
    candidates: Sequence[Any],
    timeline: SemanticSrtWordTimeline,
) -> tuple[dict[int, tuple[dict[str, Any], ...]], tuple[str, ...]]:
    """Trace M2 candidate origins through verified SRT spans to timed words."""
    by_subtitle = timeline.by_subtitle_id
    bound: dict[int, tuple[dict[str, Any], ...]] = {}
    reasons: list[str] = []
    for candidate in candidates:
        try:
            candidate_id = int(candidate.candidate_id)
            origin_ids = tuple(int(value) for value in candidate.origin_subtitle_ids)
            candidate_text = str(candidate.text or "")
        except (AttributeError, TypeError, ValueError):
            reasons.append("candidate_identity_invalid")
            continue
        spans = [by_subtitle.get(item) for item in origin_ids]
        if not origin_ids or any(item is None or item.status != "bound" for item in spans):
            reasons.append(f"candidate_origin_unbound:{candidate_id}")
            continue
        resolved = [item for item in spans if item is not None]
        if any(
            previous.word_end_index is None or current.word_start_index is None
            or previous.word_end_index + 1 != current.word_start_index
            for previous, current in zip(resolved, resolved[1:])
        ):
            reasons.append(f"candidate_origin_not_contiguous:{candidate_id}")
            continue
        words: list[dict[str, Any]] = []
        for span in resolved:
            assert span.word_start_index is not None and span.word_end_index is not None
            words.extend(dict(item) for item in timeline.words[span.word_start_index:span.word_end_index + 1])
        rendered = "".join(str(item["text"]) for item in words)
        if _canonical_text(rendered) != _canonical_text(candidate_text):
            reasons.append(f"candidate_text_span_mismatch:{candidate_id}")
            continue
        bound[candidate_id] = tuple(words)
    return bound, tuple(reasons)
