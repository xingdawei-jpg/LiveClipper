"""Read-only receipts for AI opening comparisons, never a Hook selector."""
from __future__ import annotations

from typing import Any, Mapping, Sequence


OPENING_RECEIPT_VERSION = "source-opening-v1"


def audit_opening_candidates(
    opening: Mapping[str, Any],
    *,
    source_rows: Sequence[Mapping[str, Any]],
    executed_ids: Sequence[int],
    product_context_rows: Sequence[Mapping[str, Any]] = (),
    hook_pool: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reconstruct quotes and check membership/order without judging semantics.

    A real ID, an AI product claim and an attractive opening are different
    kinds of evidence. In particular, matching garment *types* cannot prove
    two mentions refer to the same SKU. Keep that judgment visible for review.
    """
    if opening.get("receipt_version") != OPENING_RECEIPT_VERSION:
        return {"status": "not_recorded", "quality_status": "not_assessed", "candidates": []}
    by_id = {row["id"]: row for row in source_rows if type(row.get("id")) is int}
    evidence_by_id = {
        **{row["id"]: row for row in product_context_rows if type(row.get("id")) is int},
        **by_id,
    }
    pool_ids: set[int] = set()
    type_by_id: dict[int, str] = {}
    if hook_pool is not None:
        for item in hook_pool:
            if not isinstance(item, Mapping):
                continue
            hook_type = str(item.get("hook_type") or "").strip()
            pool_subtitle_ids = item.get("subtitle_ids")
            if not isinstance(pool_subtitle_ids, (list, tuple)):
                continue
            for pool_id in pool_subtitle_ids:
                if type(pool_id) is int:
                    pool_ids.add(pool_id)
                    if hook_type:
                        type_by_id[pool_id] = hook_type
    packages = opening.get("compared_packages")
    issues: list[str] = []
    candidates: list[dict[str, Any]] = []
    selected_hook_ids: list[int] = []
    candidate_types: list[str] = []
    if not isinstance(packages, list) or not packages:
        packages = []
        issues.append("missing_candidates")
    selected = []
    for rank, package in enumerate(packages, 1):
        if not isinstance(package, Mapping):
            issues.append(f"candidate_{rank}:invalid_receipt")
            continue
        errors = []
        groups = {}
        decision = package.get("decision")
        for key in ("subtitle_ids", "payoff_subtitle_ids", "product_evidence_ids"):
            ids = package.get(key)
            allowed = evidence_by_id if key == "product_evidence_ids" else by_id
            empty_rejection = decision == "rejected" and key != "subtitle_ids" and ids == []
            if not empty_rejection and (not isinstance(ids, list) or not ids or any(type(sid) is not int or sid not in allowed for sid in ids)):
                errors.append(f"{key}:missing_or_outside_safe_pool")
                groups[key] = []
            else:
                groups[key] = ids
        hook_ids = groups["subtitle_ids"]
        payoff_ids = groups["payoff_subtitle_ids"]
        joined = hook_ids + payoff_ids
        if len(joined) != len(set(joined)):
            errors.append("repeated_opening_id")
        if decision not in {"selected", "alternative", "rejected"}:
            errors.append("missing_decision")
        if not str(package.get("reason") or "").strip():
            errors.append("missing_comparison_reason")
        if decision in {"selected", "alternative"}:
            resolved_type = next((type_by_id[sid] for sid in hook_ids if sid in type_by_id), "")
            if resolved_type:
                candidate_types.append(resolved_type)
        if decision == "selected":
            selected.append(joined)
            selected_hook_ids[:] = list(hook_ids)
            if not joined or joined != list(executed_ids[:len(joined)]):
                errors.append("payoff_not_immediately_executed")
            if joined != opening.get("selected_subtitle_ids"):
                errors.append("selected_receipt_mismatch")
            if rank != 1:
                errors.append("selected_package_not_first")
        def source_units(ids, inventory=by_id):
            return [{key: inventory[sid].get(key) for key in ("id", "text", "start", "end")} for sid in ids]
        candidates.append({
            "ai_rank": rank,
            "ai_receipt": dict(package),
            "hook_source": source_units(hook_ids),
            "payoff_source": source_units(payoff_ids),
            "product_evidence_source": source_units(groups["product_evidence_ids"], evidence_by_id),
            "source_seconds": round(sum(float(by_id[sid].get("end") or 0) - float(by_id[sid].get("start") or 0) for sid in joined), 3),
            "issues": errors,
        })
        issues.extend(f"candidate_{rank}:{error}" for error in errors)
    if len(selected) != 1:
        issues.append("requires_one_selected_package")
    hook_pool_status = "not_injected" if hook_pool is None else ("empty" if not pool_ids else "injected")
    if hook_pool is not None:
        if not selected_hook_ids:
            issues.append("selected_hook_missing_for_pool_check")
        elif not pool_ids or any(sid not in pool_ids for sid in selected_hook_ids):
            issues.append("selected_hook_not_from_recall_pool")
        if len(candidate_types) >= 2 and len(set(candidate_types)) == 1:
            issues.append("opening_candidates_not_distinct_type")
    return {
        "version": OPENING_RECEIPT_VERSION,
        "status": "warning" if issues else "references_consistent",
        "issues": issues,
        "quality_status": "human_review_required",
        "product_identity_status": "human_review_required",
        "appeal_and_continuation_status": "human_review_required",
        "candidates": candidates,
        "program_selected_or_reordered": False,
        "hook_pool_status": hook_pool_status,
    }
