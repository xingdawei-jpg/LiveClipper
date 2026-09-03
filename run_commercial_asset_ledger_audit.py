# -*- coding: utf-8 -*-
"""Create source-only Commercial Asset Ledger reports for the three goldens."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_clipper import _build_ai_srt_entry_index, _director_safe_candidate_inventory  # noqa: E402
from candidate_ledger import CandidateLedger  # noqa: E402
from commercial_analyzer import parse_strategy_result  # noqa: E402
from commercial_asset_audit import classify_commercial_assets, independent_sale_messages  # noqa: E402
from content_policy import default_content_policy  # noqa: E402
from run_m2_story_goldens import CASES, _freeze_safe_candidates, _load_srt  # noqa: E402


def _asset_row(asset, inventory_by_id: dict[int, dict[str, Any]]) -> dict[str, Any]:
    item = inventory_by_id.get(asset.candidate_id, {})
    return {
        **asset.to_dict(),
        "source": str(item.get("source") or ""),
        "start": item.get("start"),
        "end": item.get("end"),
        "text": str(item.get("text") or ""),
    }


def _summary(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(item.get(key) or "") for item in rows).items()))


def build_asset_ledger_case(case_id: str) -> dict[str, Any]:
    """Build the read-only Hard-safe -> Commercial Asset Ledger input for M1.

    This deliberately stops before M2's existing frozen-candidate compatibility
    check, so an M1-only experiment does not invoke an M2 helper at all.
    """
    case = CASES[case_id]
    srt_text, subtitles = _load_srt(str(case["srt"]))
    policy = default_content_policy()
    ledger = CandidateLedger()
    ledger.seed("semantic_input", subtitles)
    # This is the product-agnostic hard-safety inventory.  It intentionally
    # does not call the exact-product or subject-relation filters used by the
    # live selector path.
    inventory = _director_safe_candidate_inventory(
        _build_ai_srt_entry_index(srt_text), content_policy=policy,
    )
    ledger.transition(
        "hard_safe_candidate_inventory",
        inventory,
        reason_code="product_agnostic_hard_safety",
    )
    assets = classify_commercial_assets(inventory)
    ledger.annotate_commercial_assets("commercial_asset_annotation", assets)
    inventory_by_id = {int(item.get("srt_index") or 0): dict(item) for item in inventory}
    rows = [_asset_row(asset, inventory_by_id) for asset in assets]
    return {
        "case": case,
        "srt_text": srt_text,
        "subtitles": subtitles,
        "assets": rows,
        "candidate_ledger": ledger.to_dict(),
        "hard_safe_candidate_count": len(rows),
        "asset_role_counts": _summary(rows, "asset_role"),
        "story_permission_counts": _summary(rows, "story_permission"),
        "raw_transaction_messages_excluded_by_hard_safety": list(independent_sale_messages(subtitles)),
    }


def build_case_report(case_id: str) -> dict[str, Any]:
    """Build the historic Ledger audit report, including its M2 compatibility note."""
    ledger_context = build_asset_ledger_case(case_id)
    case = ledger_context["case"]
    srt_text = ledger_context["srt_text"]
    subtitles = ledger_context["subtitles"]
    rows = ledger_context["assets"]

    raw_dir = ROOT / "workspace" / "m1_story_goldens_raw"
    strategy_result = parse_strategy_result(
        (raw_dir / f"{case_id}.txt").read_text(encoding="utf-8"),
        product=str(case["product"]),
        subtitles=subtitles,
        target_duration=60.0,
        content_contract=None,
    )
    story = next(item for item in strategy_result.strategies if item.strategy_id == case["story_id"])
    evidence_ids = sorted({
        int(subtitle_id)
        for item in (*story.core_evidence_pool, *story.supporting_evidence_pool, *story.bridge_candidates)
        for subtitle_id in item.subtitle_ids
    })
    assets_by_id = {row["candidate_id"]: row for row in rows}
    coverage = [{
        "subtitle_id": subtitle_id,
        "present_in_product_agnostic_hard_safe_inventory": subtitle_id in assets_by_id,
        "story_permission": (assets_by_id.get(subtitle_id) or {}).get("story_permission"),
        "asset_role": (assets_by_id.get(subtitle_id) or {}).get("asset_role"),
    } for subtitle_id in evidence_ids]
    unavailable = [item for item in rows if item["story_permission"] == "unavailable"]
    m2_candidates, _m2_ledger = _freeze_safe_candidates(
        srt_text,
        subtitles,
        main_product=str(case["main_product"]),
        content_policy=policy,
    )
    m2_coverage = [{
        "subtitle_id": subtitle_id,
        "present_in_existing_m2_frozen_pool": any(
            subtitle_id in candidate.origin_subtitle_ids for candidate in m2_candidates
        ),
        "existing_m2_candidate_ids": [
            candidate.candidate_id for candidate in m2_candidates
            if subtitle_id in candidate.origin_subtitle_ids
        ],
    } for subtitle_id in evidence_ids]
    return {
        "version": "commercial-asset-ledger-audit-v1",
        "case_id": case_id,
        "product": case["product"],
        "main_product_hint": case["main_product"],
        "scope": {
            "m1_m2_m3_changed": False,
            "candidate_pool_filtered_by_asset_annotation": False,
            "annotation_basis": "ASR-only conservative commercial-use labels; no visual product recognition",
        },
        "hard_safe_candidate_count": ledger_context["hard_safe_candidate_count"],
        "asset_role_counts": ledger_context["asset_role_counts"],
        "story_permission_counts": ledger_context["story_permission_counts"],
        "assets": rows,
        "unavailable_assets": unavailable,
        "raw_transaction_messages_excluded_by_hard_safety": ledger_context["raw_transaction_messages_excluded_by_hard_safety"],
        "m1_story": story.to_dict(),
        "m1_evidence_asset_coverage": coverage,
        "m2_compatibility": {
            "status": "unchanged_source_prototype",
            "reason": "本轮未修改 M2 输入、Prompt、Hook、时长或候选排序；Ledger 注释尚未接入 M2。",
            "story_evidence_in_hard_safe_inventory": sum(
                1 for item in coverage if item["present_in_product_agnostic_hard_safe_inventory"]
            ),
            "story_evidence_total": len(coverage),
            "story_evidence_in_existing_m2_frozen_pool": sum(
                1 for item in m2_coverage if item["present_in_existing_m2_frozen_pool"]
            ),
            "existing_m2_frozen_candidate_count": len(m2_candidates),
            "existing_m2_evidence_coverage": m2_coverage,
        },
        "candidate_ledger": ledger_context["candidate_ledger"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--out-dir", default="workspace/commercial_asset_ledger_audit")
    args = parser.parse_args()
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    case_ids = tuple(CASES) if args.case == "all" else (args.case,)
    for case_id in case_ids:
        report = build_case_report(case_id)
        path = output_dir / f"commercial_asset_ledger_{case_id}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"[Commercial Asset Ledger] {case_id}: hard-safe={report['hard_safe_candidate_count']} "
            f"permissions={report['story_permission_counts']} report={path}"
        )


if __name__ == "__main__":
    main()
