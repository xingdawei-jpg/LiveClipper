"""Quality routing for local ASR without changing the cutter timing contract.

SenseVoice remains the primary recognizer.  This module only identifies
high-confidence transcript risks and, when the user has already enabled the
word-timed Volcengine ASR provider, asks a caller supplied recognizer to retry
the affected *audio window*.  A failed or imprecise retry never replaces the
local result.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable


QUALITY_SCHEMA = "liveclipper.local-asr-quality.v1"
_MAX_RETRY_WINDOWS = 6
_MAX_RETRY_SECONDS = 72.0
_WINDOW_TOLERANCE_SECONDS = 0.05
_MIN_RETRY_WINDOW_SECONDS = 1.0

_KNOWN_ASR_ERROR_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"切个割"), "known_asr_phrase"),
    (re.compile(r"(?:料子|面料)非(?=$|[，。！？!?])"), "known_asr_phrase"),
    (re.compile(r"风吹过来整(?:[。！？!?]|$)"), "known_asr_phrase"),
    (re.compile(r"普通面料做椅"), "known_asr_phrase"),
    (re.compile(r"下0天"), "known_asr_zero_day"),
    # Concrete high-risk live-ASR residues observed in the caramel source.
    # These are audit/retry triggers only: a finding never rewrites a word by
    # itself and never turns a guessed phrase into a publishable sentence.
    (re.compile(r"人间一定是直角"), "known_asr_semantic_confusion"),
    (re.compile(r"(?:是那个)?35厘米|自带3(?:到|-)?5厘米的销售"), "known_asr_number_unit"),
    (re.compile(r"A类母婴店|就是你小宝宝"), "known_asr_listener_reference"),
    (re.compile(r"像100斤葡萄"), "known_asr_or_delivery_anomaly"),
)
_UNFINISHED_TAIL_RE = re.compile(r"(?:的|是|和|与|而且|但是|因为|所以|如果|然后)$")
_ABNORMAL_REPEAT_RE = re.compile(r"(.)(?:\1){3,}")


def quality_report_path(srt_path: str | Path) -> Path:
    path = Path(srt_path)
    return path.with_suffix(".asr_quality.json")


def _as_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _segment_copy(segment: dict[str, Any]) -> dict[str, Any]:
    copy = dict(segment)
    copy["words"] = [dict(word) for word in list(segment.get("words") or []) if isinstance(word, dict)]
    return copy


def _confidence_values(segment: dict[str, Any]) -> list[float]:
    values: list[float] = []
    for word in list(segment.get("words") or []):
        if not isinstance(word, dict):
            continue
        confidence = _as_float(word.get("confidence"))
        if confidence is None:
            continue
        if confidence > 1.0:
            confidence /= 100.0
        if 0.0 <= confidence <= 1.0:
            values.append(confidence)
    return values


def assess_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return auditable risk findings without editing subtitle text or timings."""
    findings: list[dict[str, Any]] = []
    for index, raw_segment in enumerate(segments or []):
        if not isinstance(raw_segment, dict):
            continue
        start = _as_float(raw_segment.get("start"))
        end = _as_float(raw_segment.get("end"))
        text = str(raw_segment.get("text") or "").strip()
        if start is None or end is None or end <= start or not text:
            continue

        reasons: list[str] = []
        score = 0
        for pattern, reason in _KNOWN_ASR_ERROR_PATTERNS:
            if pattern.search(text):
                reasons.append(reason)
                score += 4
                break
        if _ABNORMAL_REPEAT_RE.search(text):
            reasons.append("abnormal_repetition")
            score += 2
        confidences = _confidence_values(raw_segment)
        if len(confidences) >= 3 and sum(confidences) / len(confidences) < 0.56:
            reasons.append("low_word_confidence")
            score += 2
        compact = re.sub(r"[。！？!?，,；;：:\s]", "", text)
        has_terminal_punctuation = bool(re.search(r"[。！？!?]$", text))
        if len(compact) >= 5 and not has_terminal_punctuation and _UNFINISHED_TAIL_RE.search(compact):
            reasons.append("unfinished_tail")
            score += 2

        if reasons:
            findings.append({
                "index": index,
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
                "reasons": reasons,
                "risk_score": score,
            })
    return findings


def _select_retry_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    selected_seconds = 0.0
    for finding in sorted(findings, key=lambda item: (-int(item["risk_score"]), float(item["start"]))):
        duration = max(0.0, float(finding["duration"]))
        if duration < _MIN_RETRY_WINDOW_SECONDS:
            continue
        if len(selected) >= _MAX_RETRY_WINDOWS or selected_seconds + duration > _MAX_RETRY_SECONDS:
            continue
        selected.append(dict(finding))
        selected_seconds += duration
    return selected


def cloud_retry_eligibility(settings: dict[str, Any] | None, mode: str | None = None) -> tuple[bool, str]:
    """Respect the existing cloud-ASR privacy/configuration choice."""
    configured_mode = str(mode or os.environ.get("LIVECLIPPER_LOCAL_ASR_REVIEW_MODE", "auto")).strip().lower()
    if configured_mode not in {"off", "audit", "auto", "on"}:
        configured_mode = "auto"
    if configured_mode in {"off", "audit"}:
        return False, configured_mode

    cfg = dict(settings or {})
    has_volc_credentials = bool(
        str(cfg.get("volc_tos_ak") or "").strip()
        and str(cfg.get("volc_tos_sk") or "").strip()
        and (
            str(cfg.get("volc_api_key") or "").strip()
            or (
                str(cfg.get("volc_app_id") or "").strip()
                and str(cfg.get("volc_access_token") or "").strip()
            )
        )
    )
    if not has_volc_credentials:
        return False, "volcengine_not_configured"
    if configured_mode == "on":
        return True, "enabled"
    if not bool(cfg.get("local_asr_quality_retry_enabled")):
        return False, "quality_retry_disabled"
    return True, "enabled"


def _normalize_replacement(
    replacement: list[dict[str, Any]] | None,
    finding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Only accept complete, word-timed results within the requested window."""
    lower = float(finding["start"]) - _WINDOW_TOLERANCE_SECONDS
    upper = float(finding["end"]) + _WINDOW_TOLERANCE_SECONDS
    normalized: list[dict[str, Any]] = []
    for raw_segment in list(replacement or []):
        if not isinstance(raw_segment, dict):
            return []
        text = str(raw_segment.get("text") or "").strip()
        words = [dict(word) for word in list(raw_segment.get("words") or []) if isinstance(word, dict)]
        if not text or not words:
            return []
        clean_words = []
        for word in words:
            word_text = str(word.get("text") or "").strip()
            start = _as_float(word.get("start"))
            end = _as_float(word.get("end"))
            if not word_text or start is None or end is None or end <= start:
                return []
            if start < lower or end > upper:
                return []
            clean_words.append({
                **word,
                "text": word_text,
                "start": round(max(float(finding["start"]), start), 3),
                "end": round(min(float(finding["end"]), end), 3),
            })
        start = _as_float(raw_segment.get("start"))
        end = _as_float(raw_segment.get("end"))
        if start is None:
            start = clean_words[0]["start"]
        if end is None:
            end = clean_words[-1]["end"]
        start = max(float(finding["start"]), start)
        end = min(float(finding["end"]), end)
        if end <= start:
            return []
        normalized.append({
            **raw_segment,
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
            "words": clean_words,
            "asr_source": "cloud_retry",
        })
    normalized.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    return normalized


def review_segments(
    segments: list[dict[str, Any]],
    *,
    retry_enabled: bool,
    retry_callback: Callable[[dict[str, Any]], list[dict[str, Any]] | None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Diagnose local output and replace only validated retry windows."""
    original = [_segment_copy(segment) for segment in segments or [] if isinstance(segment, dict)]
    initial_findings = assess_segments(original)
    selected = _select_retry_findings(initial_findings) if retry_enabled and retry_callback else []
    replacements: dict[int, list[dict[str, Any]]] = {}
    outcomes: list[dict[str, Any]] = []
    for finding in selected:
        replacement = None
        try:
            replacement = retry_callback(dict(finding)) if retry_callback else None
        except Exception:
            replacement = None
        safe_replacement = _normalize_replacement(replacement, finding)
        if safe_replacement:
            replacements[int(finding["index"])] = safe_replacement
            outcome = "replaced"
        else:
            outcome = "kept_local"
        outcomes.append({
            "start": finding["start"],
            "end": finding["end"],
            "reasons": list(finding["reasons"]),
            "outcome": outcome,
        })

    reviewed: list[dict[str, Any]] = []
    for index, segment in enumerate(original):
        reviewed.extend(replacements.get(index, [segment]))
    reviewed.sort(key=lambda item: (float(item.get("start") or 0.0), float(item.get("end") or 0.0)))
    final_findings = assess_segments(reviewed)
    report = {
        "schema": QUALITY_SCHEMA,
        "created_at": int(time.time()),
        "segment_count": len(original),
        "initial": {
            "flagged_count": len(initial_findings),
            "flagged_seconds": round(sum(float(item["duration"]) for item in initial_findings), 3),
        },
        "retry": {
            "requested_count": len(selected),
            "requested_seconds": round(sum(float(item["duration"]) for item in selected), 3),
            "replaced_count": len(replacements),
            "outcomes": outcomes,
        },
        "final": {
            "segment_count": len(reviewed),
            "flagged_count": len(final_findings),
            "flagged_seconds": round(sum(float(item["duration"]) for item in final_findings), 3),
        },
    }
    return reviewed, report


def write_quality_report(srt_path: str | Path, report: dict[str, Any]) -> Path | None:
    """Atomically persist diagnostics beside the SRT; a report never blocks ASR."""
    target = quality_report_path(srt_path)
    temp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(temp, target)
        return target
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
