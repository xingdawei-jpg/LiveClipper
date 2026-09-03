# -*- coding: utf-8 -*-
"""Verify M1 Commercial Story Discovery with a read-only Asset Ledger input.

This remains a source-only prototype.  It neither invokes M2/M3 nor changes a
preview/render path.  ``--from-raw`` replays existing M1 responses through the
new asset-boundary audit; a live invocation is explicit.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for path in (str(ROOT), str(APP)):
    if path not in sys.path:
        sys.path.insert(0, path)

from commercial_analyzer import (  # noqa: E402
    analyze_commercial_story,
    assess_story_commercial_change,
    matches_story_semantic_signature,
    parse_strategy_result,
)
from run_commercial_asset_ledger_audit import build_asset_ledger_case  # noqa: E402
from run_m1_story_goldens import CASES  # noqa: E402


def _asset_usage(result, assets: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {int(item["candidate_id"]): item for item in assets}
    stories = []
    for strategy in result.strategies:
        pools = {}
        for name, evidence in (
            ("core_assets", strategy.core_evidence_pool),
            ("supporting_assets", strategy.supporting_evidence_pool),
            ("bridge_assets", strategy.bridge_candidates),
        ):
            pools[name] = [{
                "role": item.role,
                "subtitle_ids": list(item.subtitle_ids),
                "asset_roles": [
                    str((by_id.get(subtitle_id) or {}).get("asset_role") or "missing")
                    for subtitle_id in item.subtitle_ids
                ],
                "permissions": [
                    str((by_id.get(subtitle_id) or {}).get("story_permission") or "missing")
                    for subtitle_id in item.subtitle_ids
                ],
            } for item in evidence]
        stories.append({
            "strategy_id": strategy.strategy_id,
            "story_priority": strategy.story_priority,
            "thesis": strategy.thesis,
            **pools,
            "excluded_assets_reason": list(strategy.excluded_assets_reason),
        })
    return {"stories": stories}


def _hard_safe_subtitles(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep M1's raw facts exactly inside the read-only Ledger boundary."""
    return [{
        "id": int(item["candidate_id"]),
        "start": float(item.get("start") or 0),
        "end": float(item.get("end") or 0),
        "text": str(item.get("text") or ""),
    } for item in assets]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--from-raw", action="store_true")
    parser.add_argument("--raw-dir", default="workspace/m1_story_goldens_raw")
    parser.add_argument("--out-dir", default="workspace/m1_asset_aware_goldens")
    parser.add_argument("--target-duration", type=float, default=60.0)
    args = parser.parse_args()

    settings: dict[str, Any] = {}
    if not args.from_raw:
        from ai_clipper import load_settings  # noqa: E402
        settings = load_settings()
        if not str(settings.get("api_key") or "").strip():
            raise RuntimeError("未找到 AI API Key，不能运行真实 Asset-Aware M1 验证。")
    raw_dir = ROOT / args.raw_dir
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    fixture = json.loads((ROOT / "tests" / "fixtures" / "commercial_story_semantic_goldens.json").read_text(encoding="utf-8"))
    selected = tuple(CASES) if args.case == "all" else (args.case,)
    report: dict[str, Any] = {
        "version": "m1-asset-aware-golden-v1",
        "mode": "replay_existing_m1_response" if args.from_raw else "live_model",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "m1_m2_m3_formal_paths_changed": False,
        "cases": {},
    }
    passed = 0
    expected = 0
    for case_id in selected:
        case = CASES[case_id]
        ledger_context = build_asset_ledger_case(case_id)
        assets = list(ledger_context["assets"])
        subtitles = _hard_safe_subtitles(assets)
        if args.from_raw:
            raw = (raw_dir / f"{case_id}.txt").read_text(encoding="utf-8")
            result = parse_strategy_result(
                raw,
                product=case["product"],
                subtitles=subtitles,
                target_duration=args.target_duration,
                content_contract=None,
                commercial_assets=assets,
            )
        else:
            raw_path = output_dir / f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}.response.txt"
            result = analyze_commercial_story(
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                product=case["product"],
                subtitles=subtitles,
                target_duration=args.target_duration,
                commercial_assets=assets,
                raw_response_hook=lambda value, path=raw_path: path.write_text(value, encoding="utf-8"),
            )
        goldens = {}
        for golden_id in case["goldens"]:
            expected += 1
            golden = fixture[golden_id]
            if "commercial_change" in golden:
                assessments = {
                    item.strategy_id: assess_story_commercial_change(item, golden["commercial_change"])
                    for item in result.strategies
                }
                matched = [strategy_id for strategy_id, assessment in assessments.items() if assessment["passed"]]
                goldens[golden_id] = {
                    "passed": bool(matched),
                    "matched_by": matched,
                    "commercial_change_assessments": assessments,
                }
            else:
                matched = [
                    item.strategy_id for item in result.strategies
                    if matches_story_semantic_signature(item, golden["signature"])
                ]
                goldens[golden_id] = {"passed": bool(matched), "matched_by": matched}
            passed += int(bool(matched))
        report["cases"][case_id] = {
            "asset_ledger": {
                "hard_safe_candidate_count": ledger_context["hard_safe_candidate_count"],
                "story_permission_counts": ledger_context["story_permission_counts"],
                "m1_facts_boundary": "hard_safe_raw_subtitles_only",
            },
            "goldens": goldens,
            "result": result.to_dict(),
            "asset_usage": _asset_usage(result, assets),
        }
        print(f"[M1 Asset-Aware] {case_id}: stories={len(result.strategies)} goldens={goldens}")
    report["summary"] = {"passed": passed, "expected": expected, "all_passed": passed == expected}
    path = output_dir / f"m1_asset_aware_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M1 Asset-Aware] {passed}/{expected} report={path}")
    raise SystemExit(0 if passed == expected else 2)


if __name__ == "__main__":
    main()
