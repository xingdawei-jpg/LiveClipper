from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Mapping, Sequence


CONTRACT_VERSION = "selection-v1"
SHORTAGE_GRACE_SECONDS = 5.0


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(0.001, parsed)


@dataclass(frozen=True)
class DurationContract:
    final_target: float
    final_min: float
    final_max: float
    speed_factor: float
    source_target: float
    source_min: float
    source_max: float
    acceptance_margin: float = 0.75
    version: str = CONTRACT_VERSION

    @classmethod
    def create(
        cls,
        final_target: Any,
        speed_factor: Any = 1.0,
        *,
        tolerance: Any | None = None,
        acceptance_margin: Any = 0.75,
    ) -> "DurationContract":
        target = _positive_float(final_target, 60.0)
        speed = _positive_float(speed_factor, 1.0)
        if tolerance is None:
            final_tolerance = max(5.0, target / 6.0)
        else:
            final_tolerance = _positive_float(tolerance, max(5.0, target / 6.0))
        margin = max(0.0, float(acceptance_margin or 0.0))
        final_min = max(1.0, target - final_tolerance)
        final_max = target + final_tolerance
        return cls(
            final_target=target,
            final_min=final_min,
            final_max=final_max,
            speed_factor=speed,
            source_target=target * speed,
            source_min=final_min * speed,
            source_max=final_max * speed,
            acceptance_margin=margin,
        )

    @classmethod
    def coerce(
        cls,
        value: "DurationContract | Mapping[str, Any] | None",
        *,
        final_target: Any = 60,
        speed_factor: Any = 1.0,
    ) -> "DurationContract":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            target = value.get("final_target", final_target)
            speed = value.get("speed_factor", speed_factor)
            contract = cls.create(
                target,
                speed,
                tolerance=value.get("tolerance"),
                acceptance_margin=value.get("acceptance_margin", 0.75),
            )
            required = (
                "final_min",
                "final_max",
                "source_target",
                "source_min",
                "source_max",
            )
            if all(key in value for key in required):
                try:
                    return cls(
                        final_target=_positive_float(value.get("final_target"), contract.final_target),
                        final_min=_positive_float(value.get("final_min"), contract.final_min),
                        final_max=_positive_float(value.get("final_max"), contract.final_max),
                        speed_factor=_positive_float(value.get("speed_factor"), contract.speed_factor),
                        source_target=_positive_float(value.get("source_target"), contract.source_target),
                        source_min=_positive_float(value.get("source_min"), contract.source_min),
                        source_max=_positive_float(value.get("source_max"), contract.source_max),
                        acceptance_margin=max(0.0, float(value.get("acceptance_margin", 0.75) or 0.0)),
                        version=str(value.get("version") or CONTRACT_VERSION),
                    )
                except (TypeError, ValueError):
                    return contract
            return contract
        return cls.create(final_target, speed_factor)

    @property
    def ai_target_seconds(self) -> int:
        return max(1, int(round(self.source_target)))

    def status(self, source_total: Any, *, shortage_grace_seconds: Any = 0.0) -> dict[str, Any]:
        total = max(0.0, float(source_total or 0.0))
        projected_final = total / self.speed_factor
        try:
            shortage_grace = max(0.0, float(shortage_grace_seconds or 0.0))
        except (TypeError, ValueError):
            shortage_grace = 0.0
        relaxed_final_min = max(1.0, self.final_min - shortage_grace)
        normal_accepted_min = max(0.0, self.final_min - self.acceptance_margin)
        accepted_min = max(0.0, relaxed_final_min - self.acceptance_margin)
        accepted_max = self.final_max + self.acceptance_margin
        accepted = accepted_min <= projected_final <= accepted_max
        return {
            "source_total": total,
            "projected_final": projected_final,
            "target": self.final_target,
            "low": self.final_min,
            "high": self.final_max,
            "relaxed_low": relaxed_final_min,
            "relaxed_source_low": relaxed_final_min * self.speed_factor,
            "shortage_grace_seconds": shortage_grace,
            "source_target": self.source_target,
            "source_low": self.source_min,
            "source_high": self.source_max,
            "gap": max(0.0, self.source_min - total),
            "excess": max(0.0, total - self.source_max),
            "short": projected_final < accepted_min,
            "long": projected_final > accepted_max,
            "accepted": accepted,
            "used_shortage_grace": bool(shortage_grace and projected_final < normal_accepted_min and accepted),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "final_target": self.final_target,
            "final_min": self.final_min,
            "final_max": self.final_max,
            "tolerance": max(0.0, self.final_max - self.final_target),
            "speed_factor": self.speed_factor,
            "source_target": self.source_target,
            "source_min": self.source_min,
            "source_max": self.source_max,
            "acceptance_margin": self.acceptance_margin,
        }


class SelectionStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL_INSUFFICIENT = "partial_insufficient"
    INSUFFICIENT_SAFE_MATERIAL = "insufficient_safe_material"
    AI_INVALID = "ai_invalid"
    SAFETY_BLOCKED = "safety_blocked"
    RENDER_FAILED = "render_failed"
    CANCELLED = "cancelled"
    SYSTEM_ERROR = "system_error"


@dataclass(frozen=True)
class SelectionRequest:
    source_ids: tuple[str, ...]
    category: str
    focus: str
    merge_mode: bool
    duration_contract: DurationContract
    controls_digest: str
    version: str = CONTRACT_VERSION

    @classmethod
    def create(
        cls,
        *,
        source_ids: Sequence[str] = (),
        category: Any = "",
        focus: Any = "",
        merge_mode: bool = False,
        final_target: Any = 60,
        speed_factor: Any = 1.0,
        duration_contract: DurationContract | Mapping[str, Any] | None = None,
        controls: Mapping[str, Any] | None = None,
    ) -> "SelectionRequest":
        contract = DurationContract.coerce(
            duration_contract,
            final_target=final_target,
            speed_factor=speed_factor,
        )
        controls_raw = json.dumps(dict(controls or {}), ensure_ascii=False, sort_keys=True, default=str)
        return cls(
            source_ids=tuple(str(item) for item in source_ids if str(item)),
            category=str(category or ""),
            focus=str(focus or ""),
            merge_mode=bool(merge_mode),
            duration_contract=contract,
            controls_digest=hashlib.sha256(controls_raw.encode("utf-8")).hexdigest()[:20],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source_ids": list(self.source_ids),
            "category": self.category,
            "focus": self.focus,
            "merge_mode": self.merge_mode,
            "duration_contract": self.duration_contract.to_dict(),
            "controls_digest": self.controls_digest,
        }


@dataclass(frozen=True)
class SelectionCandidate:
    candidate_id: int
    source_id: str
    start: float
    end: float
    text: str
    hook_eligible: bool
    story_block_id: str = ""
    continuity_group_id: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def payload(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "hook_eligible": self.hook_eligible,
            "story_block_id": self.story_block_id,
            "continuity_group_id": self.continuity_group_id,
        }


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[SelectionCandidate, ...]
    version: str = CONTRACT_VERSION

    @classmethod
    def from_candidates(cls, candidates: Iterable[SelectionCandidate]) -> "CandidateSet":
        return cls(tuple(candidates))

    @property
    def digest(self) -> str:
        payload = [candidate.payload() for candidate in self.candidates]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def as_index_entries(self) -> list[tuple[float, float, str]]:
        return [(item.start, item.end, item.text) for item in self.candidates]

    def hook_candidate_ids(self) -> set[int]:
        return {item.candidate_id for item in self.candidates if item.hook_eligible}

    def summary(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "digest": self.digest,
            "candidate_count": len(self.candidates),
            "hook_candidate_count": len(self.hook_candidate_ids()),
        }

@dataclass(frozen=True)
class SelectionManifestItem:
    order: int
    role: str
    start: float
    end: float
    text_digest: str
    source_id: str = ""

    @property
    def clip_id(self) -> str:
        raw = f"{self.source_id}|{self.start:.3f}|{self.end:.3f}|{self.text_digest}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def payload(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "order": self.order,
            "role": self.role,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(max(0.0, self.end - self.start), 3),
            "text_digest": self.text_digest,
            "source_id": self.source_id,
        }


@dataclass(frozen=True)
class SelectionManifest:
    items: tuple[SelectionManifestItem, ...]
    candidate_digest: str
    duration_contract: DurationContract
    version: str = CONTRACT_VERSION

    @classmethod
    def from_clips(
        cls,
        clips: Sequence[Any],
        *,
        candidate_digest: str,
        duration_contract: DurationContract | Mapping[str, Any],
    ) -> "SelectionManifest":
        contract = DurationContract.coerce(duration_contract)
        items: list[SelectionManifestItem] = []
        for order, clip in enumerate(clips or (), 1):
            if isinstance(clip, Mapping):
                role = str(clip.get("type") or clip.get("clip_type") or "product")
                text = str(clip.get("text") or "")
                start = float(clip.get("start") or 0.0)
                end = float(clip.get("end") or start)
                source = str(clip.get("source") or clip.get("source_id") or "")
            else:
                values = list(clip or ())
                role = str(values[0] if len(values) > 0 else "product")
                text = str(values[1] if len(values) > 1 else "")
                start = float(values[2] if len(values) > 2 else 0.0)
                end = float(values[3] if len(values) > 3 else start)
                source = str(values[7] if len(values) > 7 and values[7] is not None else "")
            normalized_text = "".join(text.split())
            items.append(SelectionManifestItem(
                order=order,
                role=role,
                start=start,
                end=end,
                text_digest=hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()[:20],
                source_id=source,
            ))
        return cls(tuple(items), str(candidate_digest or ""), contract)

    @property
    def digest(self) -> str:
        payload = {
            "version": self.version,
            "candidate_digest": self.candidate_digest,
            "duration_contract": self.duration_contract.to_dict(),
            "items": [item.payload() for item in self.items],
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        source_total = sum(max(0.0, item.end - item.start) for item in self.items)
        return {
            "version": self.version,
            "digest": self.digest,
            "candidate_digest": self.candidate_digest,
            "selected_count": len(self.items),
            "source_duration": source_total,
            "projected_final_duration": source_total / self.duration_contract.speed_factor,
            "duration_contract": self.duration_contract.to_dict(),
            "items": [item.payload() for item in self.items],
        }
@dataclass(frozen=True)
class SelectionResult:
    status: SelectionStatus
    manifest: SelectionManifest | None = None
    failure_code: str = ""
    message: str = ""
    details: Mapping[str, Any] | None = None
    version: str = CONTRACT_VERSION

    @classmethod
    def success(cls, manifest: SelectionManifest) -> "SelectionResult":
        return cls(status=SelectionStatus.SUCCESS, manifest=manifest)

    @classmethod
    def partial_insufficient(
        cls,
        manifest: SelectionManifest,
        *,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> "SelectionResult":
        return cls(
            status=SelectionStatus.PARTIAL_INSUFFICIENT,
            manifest=manifest,
            failure_code="insufficient_content",
            message=str(message or ""),
            details=dict(details or {}),
        )

    @classmethod
    def insufficient_safe_material(
        cls,
        *,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> "SelectionResult":
        """Represent a hard precondition failure, never an exportable success."""
        return cls(
            status=SelectionStatus.INSUFFICIENT_SAFE_MATERIAL,
            failure_code="insufficient_safe_material",
            message=str(message or ""),
            details=dict(details or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status.value,
            "ok": self.status == SelectionStatus.SUCCESS,
            "failure_code": self.failure_code,
            "message": self.message,
            "details": dict(self.details or {}),
            "manifest": self.manifest.to_dict() if self.manifest else {},
        }
