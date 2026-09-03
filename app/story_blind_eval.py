"""Blind evaluation utilities for commercial-story planning.

This module deliberately does not score or select clips.  It prepares an
anonymous review packet so a human can judge whether one ordered playlist
feels like a commercial short video, without knowing whether it came from a
manual edit, the legacy director, or M2.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import random
from typing import Any, Iterable, Mapping, Sequence


BLIND_EVALUATION_VERSION = "commercial-story-blind-eval-v1"

# Keep the criteria about viewer experience, not whether a JSON schema happened
# to contain all chapter roles.  Scores are intentionally coarse: 0 poor, 1
# usable, 2 strong.
BLIND_EVALUATION_CRITERIA: tuple[dict[str, str], ...] = (
    {"id": "hook", "label": "开头是否真能抓人"},
    {"id": "payoff", "label": "承诺是否及时兑现"},
    {"id": "progression", "label": "故事是否有推进"},
    {"id": "focus", "label": "内容是否丰富但不散"},
    {"id": "purchase_reason", "label": "购买理由是否越来越清晰"},
    {"id": "naturalness", "label": "是否自然，不像字幕拼接"},
)
_CRITERIA_IDS = frozenset(item["id"] for item in BLIND_EVALUATION_CRITERIA)
BLIND_EVALUATION_DIAGNOSTICS: tuple[dict[str, str], ...] = (
    {"id": "hook_weak", "label": "Hook 弱"},
    {"id": "payoff_weak", "label": "兑现弱"},
    {"id": "middle_repetitive", "label": "中段重复"},
    {"id": "transition_jump", "label": "转场跳"},
    {"id": "too_livestream", "label": "直播感强"},
    {"id": "content_thin", "label": "内容太薄"},
    {"id": "close_weak", "label": "收尾差"},
    {"id": "cut_sentence", "label": "剪辑残句"},
)
_DIAGNOSTIC_IDS = frozenset(item["id"] for item in BLIND_EVALUATION_DIAGNOSTICS)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@dataclass(frozen=True)
class EvaluationClip:
    """One already-selected immutable candidate, in intended playback order."""

    candidate_id: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return round(max(0.0, self.end - self.start), 3)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EvaluationClip":
        try:
            candidate_id = int(raw.get("candidate_id") or 0)
        except (TypeError, ValueError):
            candidate_id = 0
        start = _number(raw.get("start"))
        end = _number(raw.get("end"), start)
        return cls(
            candidate_id=candidate_id,
            start=start,
            end=max(start, end),
            text=str(raw.get("text") or "").strip(),
        )

    def public_payload(self, order: int) -> dict[str, Any]:
        # Do not reveal candidate IDs, source IDs, roles, story assets, or the
        # generating path.  They bias a reviewer before they assess the cut.
        return {
            "order": int(order),
            "duration": self.duration,
            "text": self.text,
        }


@dataclass(frozen=True)
class EvaluationVariant:
    """Private identity plus an ordered playlist for one comparable variant."""

    variant_id: str
    clips: tuple[EvaluationClip, ...]

    @property
    def duration(self) -> float:
        return round(sum(item.duration for item in self.clips), 3)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "EvaluationVariant":
        variant_id = str(raw.get("variant_id") or "").strip()
        clips = tuple(
            EvaluationClip.from_mapping(item)
            for item in (raw.get("clips") or ())
            if isinstance(item, Mapping)
        )
        if not variant_id:
            raise ValueError("blind variant requires variant_id")
        if not clips:
            raise ValueError(f"blind variant {variant_id} has no clips")
        if any(not item.text or item.duration <= 0 for item in clips):
            raise ValueError(f"blind variant {variant_id} has invalid clip")
        return cls(variant_id=variant_id, clips=clips)

    @classmethod
    def from_m2_plan(cls, variant_id: str, plan: Mapping[str, Any]) -> "EvaluationVariant":
        return cls.from_mapping({
            "variant_id": variant_id,
            "clips": plan.get("selected_candidates") or (),
        })


def build_blind_packet(
    cases: Mapping[str, Sequence[EvaluationVariant | Mapping[str, Any]]],
    *,
    seed: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create public anonymous packet and separate private answer key.

    A case must contain two or more genuinely comparable playlists.  We refuse
    to create a fake comparison from one M2 result alone; absence of an
    approved manual or historical comparison is an explicit evaluation gap.
    """
    if not str(seed or "").strip():
        raise ValueError("blind evaluation requires a non-empty seed")

    public_cases: list[dict[str, Any]] = []
    private_cases: list[dict[str, Any]] = []
    for case_id in sorted(str(value).strip() for value in cases if str(value).strip()):
        raw_variants = cases[case_id]
        variants = tuple(
            item if isinstance(item, EvaluationVariant) else EvaluationVariant.from_mapping(item)
            for item in (raw_variants or ())
        )
        if len(variants) < 2:
            raise ValueError(f"blind case {case_id} needs at least two variants")
        if len({item.variant_id for item in variants}) != len(variants):
            raise ValueError(f"blind case {case_id} has duplicate variant_id")

        shuffled = list(variants)
        digest = hashlib.sha256(f"{seed}|{case_id}".encode("utf-8")).digest()
        random.Random(digest).shuffle(shuffled)
        labels = tuple(chr(ord("A") + index) for index in range(len(shuffled)))
        public_variants = []
        key_variants = []
        for label, variant in zip(labels, shuffled):
            public_variants.append({
                "label": label,
                "duration": variant.duration,
                "playlist": [item.public_payload(index) for index, item in enumerate(variant.clips, 1)],
            })
            key_variants.append({"label": label, "variant_id": variant.variant_id})
        public_cases.append({"case_id": case_id, "variants": public_variants})
        private_cases.append({"case_id": case_id, "variants": key_variants})

    public_packet = {
        "version": BLIND_EVALUATION_VERSION,
        "criteria": list(BLIND_EVALUATION_CRITERIA),
        "diagnostic_issues": list(BLIND_EVALUATION_DIAGNOSTICS),
        "score_scale": {"0": "差", "1": "可用", "2": "强"},
        "cases": public_cases,
    }
    private_key = {
        "version": BLIND_EVALUATION_VERSION,
        "cases": private_cases,
    }
    return public_packet, private_key


def empty_rating_sheet(public_packet: Mapping[str, Any]) -> dict[str, Any]:
    """Create the only file a blind reviewer needs to complete."""
    cases = []
    for raw_case in public_packet.get("cases") or ():
        if not isinstance(raw_case, Mapping):
            continue
        variants = []
        for raw_variant in raw_case.get("variants") or ():
            if not isinstance(raw_variant, Mapping):
                continue
            variants.append({
                "label": str(raw_variant.get("label") or ""),
                "scores": {criterion["id"]: None for criterion in BLIND_EVALUATION_CRITERIA},
                "max_issue": None,
                "comment": "",
            })
        cases.append({"case_id": str(raw_case.get("case_id") or ""), "variants": variants})
    return {"version": BLIND_EVALUATION_VERSION, "cases": cases}


def score_rating_sheet(
    rating_sheet: Mapping[str, Any],
    private_key: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate human ratings and reveal aggregate scores only after scoring."""
    key_cases = {
        str(case.get("case_id") or ""): {
            str(item.get("label") or ""): str(item.get("variant_id") or "")
            for item in (case.get("variants") or ())
            if isinstance(item, Mapping)
        }
        for case in (private_key.get("cases") or ())
        if isinstance(case, Mapping)
    }
    totals: dict[str, dict[str, Any]] = {}
    for raw_case in rating_sheet.get("cases") or ():
        if not isinstance(raw_case, Mapping):
            continue
        case_id = str(raw_case.get("case_id") or "")
        labels = key_cases.get(case_id)
        if not labels:
            raise ValueError(f"unknown blind evaluation case {case_id}")
        for raw_variant in raw_case.get("variants") or ():
            if not isinstance(raw_variant, Mapping):
                continue
            label = str(raw_variant.get("label") or "")
            variant_id = labels.get(label)
            if not variant_id:
                raise ValueError(f"unknown blind label {case_id}/{label}")
            scores = raw_variant.get("scores")
            if not isinstance(scores, Mapping) or set(scores) != _CRITERIA_IDS:
                raise ValueError(f"incomplete criteria for {case_id}/{label}")
            values: dict[str, int] = {}
            for criterion_id in _CRITERIA_IDS:
                try:
                    value = int(scores[criterion_id])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"invalid score for {case_id}/{label}/{criterion_id}") from exc
                if value not in (0, 1, 2):
                    raise ValueError(f"score must be 0, 1, or 2 for {case_id}/{label}/{criterion_id}")
                values[criterion_id] = value
            diagnostic = raw_variant.get("max_issue")
            diagnostic_id = str(diagnostic or "").strip()
            if diagnostic_id and diagnostic_id not in _DIAGNOSTIC_IDS:
                raise ValueError(f"unknown diagnostic for {case_id}/{label}: {diagnostic_id}")
            total = totals.setdefault(variant_id, {
                "variant_id": variant_id,
                "cases": 0,
                "total": 0,
                "criteria": {key: 0 for key in _CRITERIA_IDS},
                "diagnostics": {key: 0 for key in _DIAGNOSTIC_IDS},
            })
            total["cases"] += 1
            total["total"] += sum(values.values())
            for criterion_id, value in values.items():
                total["criteria"][criterion_id] += value
            if diagnostic_id:
                total["diagnostics"][diagnostic_id] += 1
    for value in totals.values():
        value["average_per_case"] = round(value["total"] / max(1, value["cases"]), 3)
    return {
        "version": BLIND_EVALUATION_VERSION,
        "criteria": list(BLIND_EVALUATION_CRITERIA),
        "diagnostic_issues": list(BLIND_EVALUATION_DIAGNOSTICS),
        "variants": sorted(totals.values(), key=lambda item: (-item["average_per_case"], item["variant_id"])),
    }
