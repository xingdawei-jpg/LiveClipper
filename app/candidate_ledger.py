"""Audit-only lineage and commercial-asset annotations for AI candidates.

The ledger deliberately has no selection policy.  It observes candidate
snapshots and membership decisions so a later audit can distinguish a merge
from a filter, and a review annotation from a real exclusion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


CANDIDATE_LEDGER_VERSION = "commercial-asset-ledger-v1"
PRODUCT_FOCUSES = frozenset(("same_product", "related_product", "unknown"))
ASSET_ROLES = frozenset((
    "product_proof", "design_explanation", "wearing_effect", "styling_scene",
    "lifestyle_scene", "trust_signal", "unknown",
))
STORY_PERMISSIONS = frozenset(("main_story", "supporting_story", "unavailable"))
EVIDENCE_SOURCES = frozenset(("timeline", "asr", "product_signal", "manual", "other"))


def _as_float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _source_from_text(text: str) -> str:
    match = re.search(r"\[V\d+\]", text, flags=re.IGNORECASE)
    return match.group(0).upper() if match else ""


def _origin_subtitle_ids(value: Any) -> tuple[int, ...]:
    """Normalize explicit immutable semantic ancestors for audit use only."""
    if isinstance(value, dict):
        value = value.get("origin_subtitle_ids")
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[int] = []
    for item in value:
        try:
            subtitle_id = int(item)
        except (TypeError, ValueError):
            continue
        if subtitle_id > 0 and subtitle_id not in result:
            result.append(subtitle_id)
    return tuple(sorted(result))


def _enum(value: Any, allowed: frozenset[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


@dataclass(frozen=True)
class SubjectContext:
    """A non-blocking statement of how an asset relates to the current story."""

    product_focus: str = "unknown"
    confidence: str = "low"

    @classmethod
    def from_mapping(cls, raw: Any) -> "SubjectContext":
        data = raw if isinstance(raw, dict) else {}
        return cls(
            product_focus=_enum(data.get("product_focus"), PRODUCT_FOCUSES, "unknown"),
            confidence=_enum(data.get("confidence"), frozenset(("high", "medium", "low")), "low"),
        )

    def to_dict(self) -> dict[str, str]:
        return {"product_focus": self.product_focus, "confidence": self.confidence}


@dataclass(frozen=True)
class CandidateAsset:
    """Read-only commercial-use annotation; never a candidate filter or sorter."""

    candidate_id: int
    subject_context: SubjectContext
    asset_role: str
    story_permission: str
    evidence_source: str
    reason: str

    @classmethod
    def from_mapping(cls, raw: Any) -> "CandidateAsset":
        if not isinstance(raw, dict):
            raise ValueError("commercial asset must be a mapping")
        try:
            candidate_id = int(raw.get("candidate_id") or raw.get("srt_index") or 0)
        except (TypeError, ValueError):
            candidate_id = 0
        if candidate_id <= 0:
            raise ValueError("commercial asset requires candidate_id")
        return cls(
            candidate_id=candidate_id,
            subject_context=SubjectContext.from_mapping(raw.get("subject_context")),
            asset_role=_enum(raw.get("asset_role"), ASSET_ROLES, "unknown"),
            story_permission=_enum(raw.get("story_permission"), STORY_PERMISSIONS, "supporting_story"),
            evidence_source=_enum(raw.get("evidence_source"), EVIDENCE_SOURCES, "other"),
            reason=str(raw.get("reason") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "subject_context": self.subject_context.to_dict(),
            "asset_role": self.asset_role,
            "story_permission": self.story_permission,
            "evidence_source": self.evidence_source,
            "reason": self.reason,
        }


def _item_payload(item: Any, *, external_id: int = 0) -> dict[str, Any] | None:
    origin_subtitle_ids: tuple[int, ...] = ()
    if isinstance(item, dict):
        text = str(item.get("text") or "").strip()
        start = _as_float(item.get("start"))
        end = _as_float(item.get("end"))
        source = str(item.get("source") or "").strip().upper()
        try:
            external_id = int(item.get("candidate_id") or item.get("srt_index") or external_id or 0)
        except (TypeError, ValueError):
            pass
        origin_subtitle_ids = _origin_subtitle_ids(item.get("origin_subtitle_ids"))
    elif isinstance(item, (list, tuple)) and len(item) >= 3:
        # SRT entries are ``(start, end, text)``; candidate clips are
        # ``(type, text, start, end, ...)``.  The ledger must observe both.
        if len(item) == 3:
            start = _as_float(item[0])
            end = _as_float(item[1])
            text = str(item[2] or "").strip()
        else:
            text = str(item[1] or "").strip()
            start = _as_float(item[2])
            end = _as_float(item[3])
        source = ""
        for extra in reversed(item[6:]):
            origin_subtitle_ids = _origin_subtitle_ids(extra)
            if origin_subtitle_ids:
                break
    else:
        text = str(getattr(item, "text", "") or "").strip()
        start = _as_float(getattr(item, "start", 0.0))
        end = _as_float(getattr(item, "end", 0.0))
        source = str(getattr(item, "source_id", "") or "").strip().upper()
        try:
            external_id = int(getattr(item, "candidate_id", external_id) or external_id or 0)
        except (TypeError, ValueError):
            pass
        origin_subtitle_ids = _origin_subtitle_ids(
            getattr(item, "origin_subtitle_ids", ())
        )
    if not text or end <= start:
        return None
    source = source or _source_from_text(text)
    return {
        "external_id": external_id if external_id > 0 else None,
        "source": source,
        "start": start,
        "end": end,
        "text": text,
        "text_key": _clean_text(re.sub(r"^\s*\[V\d+\]\s*", "", text, flags=re.IGNORECASE)),
        "origin_subtitle_ids": origin_subtitle_ids,
    }


class CandidateLedger:
    """Keep an append-only audit trail without changing selection behaviour."""

    def __init__(self) -> None:
        self._next_node = 1
        self._active: dict[str, dict[str, Any]] = {}
        self._nodes: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._stages: list[dict[str, Any]] = []
        self._commercial_assets: dict[int, CandidateAsset] = {}

    def seed(self, stage: str, items: Iterable[Any]) -> None:
        self._active = {}
        for position, item in enumerate(items or (), 1):
            payload = _item_payload(item, external_id=position)
            if payload is None:
                continue
            node_id = self._new_node_id()
            node = {"node_id": node_id, "parents": [], **payload}
            self._active[node_id] = node
            self._nodes[node_id] = node
            self._events.append(self._event(stage, "CREATE", node_id, "asr_semantic_input"))
        self._record_stage(stage, input_count=0, reason_code="asr_semantic_input")

    def transition(
        self,
        stage: str,
        items: Iterable[Any],
        *,
        reason_code: str,
    ) -> None:
        """Record a transformation snapshot and its best-effort lineage.

        Merges from the candidate freezer are exact in normal operation because
        the merged time span contains its inputs.  Later filter snapshots may
        use an overlap match; the match method is included for audit honesty.
        """
        before = dict(self._active)
        after_items = [
            payload
            for position, item in enumerate(items or (), 1)
            for payload in (_item_payload(item, external_id=position),)
            if payload is not None
        ]
        next_active: dict[str, dict[str, Any]] = {}
        matched_parent_ids: set[str] = set()
        for payload in after_items:
            parents, match_method = self._match_parents(payload, before)
            matched_parent_ids.update(parents)
            node_id = self._new_node_id()
            action = "CREATE"
            if len(parents) > 1:
                action = "MERGE"
            elif len(parents) == 1:
                parent = before[parents[0]]
                action = "PASS" if self._same_payload(parent, payload) else "TRIM"
            node = {
                "node_id": node_id,
                "parents": parents,
                "match_method": match_method,
                **payload,
            }
            next_active[node_id] = node
            self._nodes[node_id] = node
            self._events.append(self._event(stage, action, node_id, reason_code, parents=parents, match_method=match_method))
        for node_id in before:
            if node_id not in matched_parent_ids:
                self._events.append(self._event(stage, "DROP", node_id, reason_code))
        self._active = next_active
        self._record_stage(stage, input_count=len(before), reason_code=reason_code)

    def mark_membership(
        self,
        stage: str,
        candidate_ids: Iterable[Any],
        *,
        action: str,
        reason_code: str,
        excluded_action: str | None = None,
        excluded_reason_code: str | None = None,
        universe_ids: Iterable[Any] | None = None,
    ) -> None:
        """Record a decision against immutable frozen candidate IDs."""
        selected = {
            int(value)
            for value in (candidate_ids or ())
            if str(value).strip().isdigit() and int(value) > 0
        }
        universe = (
            {
                int(value)
                for value in universe_ids
                if str(value).strip().isdigit() and int(value) > 0
            }
            if universe_ids is not None else None
        )
        for node_id, node in self._active.items():
            candidate_id = node.get("external_id")
            if universe is not None and candidate_id not in universe:
                continue
            if candidate_id in selected:
                self._events.append(self._event(stage, action, node_id, reason_code))
            elif excluded_action:
                self._events.append(
                    self._event(stage, excluded_action, node_id, excluded_reason_code or reason_code)
                )
        self._record_stage(stage, input_count=len(self._active), reason_code=reason_code, selected_count=len(selected))

    def frozen_candidate_origins(self) -> dict[int, tuple[int, ...]]:
        """Return the final candidate-to-source mapping without inferring overlap.

        The freezer provides these ancestors explicitly.  This accessor must
        never fall back to timestamp containment: an absent mapping is an
        audit failure, not a reason to manufacture lineage downstream.
        """
        result: dict[int, tuple[int, ...]] = {}
        for node in self._active.values():
            try:
                candidate_id = int(node.get("external_id") or 0)
            except (TypeError, ValueError):
                continue
            origin_ids = _origin_subtitle_ids(node.get("origin_subtitle_ids"))
            if candidate_id > 0 and origin_ids:
                result[candidate_id] = origin_ids
        return dict(sorted(result.items()))

    def annotate_commercial_assets(
        self,
        stage: str,
        assets: Iterable[CandidateAsset | dict[str, Any]],
        *,
        reason_code: str = "commercial_asset_annotation",
    ) -> None:
        """Attach read-only asset uses to the current immutable candidates.

        Annotation never changes the active set.  An unknown candidate is an
        audit error rather than an invitation to recreate it by text or time.
        """
        active_by_candidate: dict[int, str] = {}
        for node_id, node in self._active.items():
            try:
                candidate_id = int(node.get("external_id") or 0)
            except (TypeError, ValueError):
                continue
            if candidate_id > 0:
                active_by_candidate[candidate_id] = node_id
        count = 0
        for raw in assets or ():
            asset = raw if isinstance(raw, CandidateAsset) else CandidateAsset.from_mapping(raw)
            node_id = active_by_candidate.get(asset.candidate_id)
            if not node_id:
                raise ValueError(
                    f"commercial asset candidate_id={asset.candidate_id} is absent from the active ledger"
                )
            self._commercial_assets[asset.candidate_id] = asset
            self._events.append(self._event(stage, "ANNOTATE", node_id, reason_code))
            count += 1
        self._record_stage(
            stage,
            input_count=len(self._active),
            reason_code=reason_code,
            selected_count=count,
        )

    def commercial_assets(self) -> dict[int, CandidateAsset]:
        return dict(sorted(self._commercial_assets.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CANDIDATE_LEDGER_VERSION,
            "stage_count": len(self._stages),
            "event_count": len(self._events),
            "stages": list(self._stages),
            "nodes": [self._public_node(node) for node in self._nodes.values()],
            "events": list(self._events),
            "commercial_assets": [asset.to_dict() for asset in self.commercial_assets().values()],
        }

    def _new_node_id(self) -> str:
        node_id = f"L{self._next_node:04d}"
        self._next_node += 1
        return node_id

    def _record_stage(self, stage: str, *, input_count: int, reason_code: str, selected_count: int | None = None) -> None:
        item = {
            "stage": str(stage),
            "input_count": int(input_count),
            "active_count": len(self._active),
            "reason_code": str(reason_code),
        }
        if selected_count is not None:
            item["selected_count"] = int(selected_count)
        self._stages.append(item)

    @staticmethod
    def _same_payload(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return (
            left.get("source") == right.get("source")
            and left.get("start") == right.get("start")
            and left.get("end") == right.get("end")
            and left.get("text_key") == right.get("text_key")
        )

    def _match_parents(self, payload: dict[str, Any], before: dict[str, dict[str, Any]]) -> tuple[list[str], str]:
        exact = [
            node_id for node_id, node in before.items()
            if self._same_payload(node, payload)
        ]
        if exact:
            return exact[:1], "exact"

        parents: list[str] = []
        for node_id, node in before.items():
            if payload["source"] and node.get("source") and payload["source"] != node.get("source"):
                continue
            overlap = max(0.0, min(payload["end"], node["end"]) - max(payload["start"], node["start"]))
            node_duration = max(0.001, node["end"] - node["start"])
            text_related = (
                node["text_key"] in payload["text_key"]
                or payload["text_key"] in node["text_key"]
            )
            if overlap / node_duration >= 0.72 or (text_related and overlap > 0.04):
                parents.append(node_id)
        if parents:
            return parents, "overlap"
        return [], "unmatched"

    @staticmethod
    def _event(stage: str, action: str, node_id: str, reason_code: str, **extra: Any) -> dict[str, Any]:
        return {
            "stage": str(stage),
            "action": str(action),
            "node_id": str(node_id),
            "reason_code": str(reason_code),
            **extra,
        }

    @staticmethod
    def _public_node(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "node_id": str(node.get("node_id") or ""),
            "parent_node_ids": list(node.get("parents") or ()),
            "candidate_id": node.get("external_id"),
            "source": str(node.get("source") or ""),
            "start": node.get("start"),
            "end": node.get("end"),
            "text": str(node.get("text") or ""),
            "match_method": str(node.get("match_method") or ""),
            "origin_subtitle_ids": list(_origin_subtitle_ids(node.get("origin_subtitle_ids"))),
        }
