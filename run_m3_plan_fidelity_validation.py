# -*- coding: utf-8 -*-
"""Validate source-only M3 materialization of an already-approved M2 plan.

This runner never calls M1/M2, preview, rendering, or a production route.  It
requires the exact source SRT's own word-timing sidecar; a neighbouring file or
timestamp-overlap guess is deliberately not a substitute for that lineage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from clip_selector import (  # noqa: E402
    audit_materialization_fidelity,
    bind_candidate_words_by_origin,
    materialize_narrative_plan,
)
from run_commercial_asset_ledger_audit import build_asset_ledger_case  # noqa: E402
from run_m2_story_goldens import CASES, _load_srt  # noqa: E402
from run_m3_selector_materialization import _plan_from_payload  # noqa: E402


FOCUS = {
    "jccc": "肩部视觉收窄 → 度假延展",
    "jianzhi": "单穿不职业 → 版型兑现",
    "hanxi": "露臀风险 → 加长2cm → 安心穿",
}


def _load_sidecar(path: Path) -> tuple[list[dict[str, Any]], str]:
    if not path.is_file():
        return [], "missing_exact_source_sidecar"
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return [], "invalid_exact_source_sidecar"
    segments = payload.get("segments") if isinstance(payload, Mapping) else None
    if not isinstance(segments, list):
        return [], "invalid_exact_source_sidecar"
    return [dict(item) for item in segments if isinstance(item, Mapping)], "available"


def validate_case(
    case_id: str,
    case_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Run M3 on a saved M2 plan and expose every fidelity precondition."""
    raw_plan = case_payload.get("plan")
    if not isinstance(raw_plan, Mapping):
        raise ValueError(f"{case_id}: M2 report has no plan payload")
    plan = _plan_from_payload(raw_plan)
    source_srt = Path(str(CASES[case_id]["srt"])).resolve()
    _srt_text, source_subtitles = _load_srt(str(source_srt))
    sidecar_path = source_srt.with_suffix(".words.json")
    sidecar_segments, sidecar_status = _load_sidecar(sidecar_path)
    binding = bind_candidate_words_by_origin(
        plan.selected_candidates,
        source_subtitles,
        sidecar_segments,
    )
    approved_ids = {candidate.candidate_id for candidate in plan.selected_candidates}
    bound_ids = set(binding.words_by_candidate)
    if sidecar_status != "available":
        word_lineage_status = sidecar_status
    elif binding.unbound_reasons:
        word_lineage_status = "rejected_identity_mismatch"
    elif bound_ids != approved_ids:
        word_lineage_status = "incomplete_candidate_word_binding"
    else:
        word_lineage_status = "verified"

    # Calling the materializer with an empty map is useful only to report a
    # source-expression blocker.  Its sentence-level output is never treated
    # as a validated word-level clip list below.
    result = materialize_narrative_plan(plan, binding.words_by_candidate)
    ledger_assets = list(build_asset_ledger_case(case_id)["assets"])
    fidelity = audit_materialization_fidelity(
        plan,
        result,
        ledger_assets,
        require_word_boundaries=True,
    )
    word_level_ready = word_lineage_status == "verified"
    passed = bool(word_level_ready and fidelity["passed"])
    subtitle_status = "verified_against_plan" if passed else "not_emitted_without_verified_word_lineage"

    return {
        "focus": FOCUS[case_id],
        "source_srt": str(source_srt),
        "exact_word_sidecar": str(sidecar_path),
        "word_lineage_status": word_lineage_status,
        "sidecar_segment_count": len(sidecar_segments),
        "source_subtitle_count": len(source_subtitles),
        "explicit_word_binding_candidate_ids": sorted(bound_ids),
        "word_binding_rejections": list(binding.unbound_reasons),
        "m2_plan_valid": plan.plan_valid,
        "m2_plan_total_seconds": plan.total_seconds,
        "m3_candidate_materialization": result.to_dict(),
        "plan_fidelity_audit": fidelity,
        "semantic_completeness": {
            "source_expression_status": "blocked" if result.blocked else "complete_candidate_expression",
            "word_level_status": "verified" if word_level_ready and not result.blocked else "not_verified",
        },
        "subtitle_plan_consistency": {
            "status": subtitle_status,
            "note": "本轮不渲染视频；只有已验证词级血缘的文本才会被视为可输出字幕。",
        },
        "cross_product_scope": (
            "仅验证候选没有离开 M2 批准集合和 Commercial Asset Ledger；"
            "Ledger 不做视觉商品识别，不能把此项表述为画面级商品识别证明。"
        ),
        "passed": passed,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument(
        "--m2-report",
        default="workspace/m2_story_consumption_validation_v4/m2_story_consumption_20260821_125621.json",
    )
    parser.add_argument("--out-dir", default="workspace/m3_plan_fidelity_validation")
    args = parser.parse_args()

    report_path = (ROOT / args.m2_report).resolve()
    m2_report = json.loads(report_path.read_text(encoding="utf-8"))
    cases = dict(m2_report.get("cases") or {})
    case_ids = tuple(CASES) if args.case == "all" else (args.case,)
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "version": "m3-plan-fidelity-validation-v1",
        "mode": "source_only_no_model_no_preview_no_render",
        "m1_m2_m3_formal_paths_changed": False,
        "m2_report": str(report_path),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": {},
    }
    for case_id in case_ids:
        case_payload = cases.get(case_id)
        if not isinstance(case_payload, Mapping):
            raise ValueError(f"M2 report missing case: {case_id}")
        result = validate_case(case_id, case_payload)
        report["cases"][case_id] = result
        print(
            f"[M3 Fidelity] {case_id}: word_lineage={result['word_lineage_status']} "
            f"fidelity={result['plan_fidelity_audit']['passed']} passed={result['passed']}"
        )
    passed_count = sum(bool(item["passed"]) for item in report["cases"].values())
    report["summary"] = {
        "passed": passed_count,
        "expected": len(case_ids),
        "all_passed": passed_count == len(case_ids),
        "shadow_ready": passed_count == len(case_ids),
    }
    output_path = output_dir / f"m3_plan_fidelity_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M3 Fidelity] {passed_count}/{len(case_ids)} report={output_path}")
    raise SystemExit(0 if report["summary"]["all_passed"] else 2)


if __name__ == "__main__":
    main()
