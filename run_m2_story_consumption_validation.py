# -*- coding: utf-8 -*-
"""Validate that source-only M2 executes an approved M1 Hero Story.

No preview, render, M3 selector, or production route is called.  The full
hard-safe pool remains visible; this runner audits the returned Plan JSON only.
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

from commercial_analyzer import parse_strategy_result  # noqa: E402
from run_commercial_asset_ledger_audit import build_asset_ledger_case  # noqa: E402
from run_m1_asset_aware_goldens import _hard_safe_subtitles  # noqa: E402
from run_m1_story_goldens import CASES  # noqa: E402
from story_planner import PlanningCandidate, audit_story_consumption, plan_narrative_llm  # noqa: E402


HERO_GOLDENS = {
    "jccc": "jccc_shoulder_narrowing",
    "jianzhi": "jianzhi_standalone_not_office",
    "hanxi": "hanxi_short_skirt_safety",
}


def _planning_candidates(assets: list[dict[str, Any]]) -> tuple[PlanningCandidate, ...]:
    """Adapt the product-agnostic hard-safe Ledger pool without filtering it."""
    return tuple(
        PlanningCandidate(
            candidate_id=int(item["candidate_id"]),
            source_id=str(item.get("source") or "SRT"),
            start=float(item.get("start") or 0.0),
            end=float(item.get("end") or 0.0),
            text=str(item.get("text") or ""),
            origin_subtitle_ids=(int(item["candidate_id"]),),
            hook_eligible=float(item.get("end") or 0.0) - float(item.get("start") or 0.0) <= 5.0,
        )
        for item in assets
    )


def _approved_high_strategy(
    *,
    case_id: str,
    stable_case: dict[str, Any],
    subtitles: list[dict[str, Any]],
    assets: list[dict[str, Any]],
):
    for saved_run in stable_case.get("runs") or ():
        approved = ((saved_run.get("expected_hero_ids_by_direction") or {}).get(HERO_GOLDENS[case_id]) or [])
        if not approved:
            continue
        parsed = parse_strategy_result(
            json.dumps({"strategies": (saved_run.get("result") or {}).get("strategies") or ()}, ensure_ascii=False),
            product=CASES[case_id]["product"],
            subtitles=subtitles,
            commercial_assets=assets,
            target_duration=40.0,
        )
        by_id = {strategy.strategy_id: strategy for strategy in parsed.strategies}
        for strategy_id in approved:
            strategy = by_id.get(strategy_id)
            if strategy and strategy.story_priority == "high":
                return strategy, int(saved_run.get("attempt") or 0)
    raise RuntimeError(f"{case_id}: 稳定性报告中没有已批准的 high Hero")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--m1-report", required=True)
    parser.add_argument("--out-dir", default="workspace/m2_story_consumption_validation")
    parser.add_argument("--target-duration", type=float, default=40.0)
    args = parser.parse_args()

    from ai_clipper import load_settings  # noqa: E402
    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行真实 M2 消费验证。")
    m1_report = json.loads((ROOT / args.m1_report).read_text(encoding="utf-8"))
    selected = tuple(CASES) if args.case == "all" else (args.case,)
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "version": "m2-story-consumption-validation-v1",
        "mode": "live_model_plan_json_only",
        "m1_m2_m3_formal_paths_changed": False,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": {},
    }

    for case_id in selected:
        context = build_asset_ledger_case(case_id)
        assets = list(context["assets"])
        subtitles = _hard_safe_subtitles(assets)
        strategy, m1_attempt = _approved_high_strategy(
            case_id=case_id,
            stable_case=(m1_report.get("cases") or {}).get(case_id) or {},
            subtitles=subtitles,
            assets=assets,
        )
        candidates = _planning_candidates(assets)
        stamp = f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}"
        raw_path = output_dir / f"{stamp}.response.txt"
        contract = {
            "contract_version": "m2-story-consumption-validation-v1",
            "m1_hero_strategy_id": strategy.strategy_id,
            "m1_story_priority": strategy.story_priority,
            "m1_consumption_only": True,
            "m1_consumption_validation_require_supporting_bridge": True,
            "target_duration": float(args.target_duration),
        }
        plan = plan_narrative_llm(
            strategy=strategy,
            target_duration=args.target_duration,
            safe_candidates=candidates,
            selection_contract=contract,
            api_key=str(settings["api_key"]),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            raw_response_hook=lambda raw, path=raw_path: path.write_text(raw, encoding="utf-8"),
        )
        consumption = audit_story_consumption(plan, strategy, candidates)
        passed = bool(plan.plan_valid and consumption["passed"])
        report["cases"][case_id] = {
            "m1_stability_attempt": m1_attempt,
            "hero_golden": HERO_GOLDENS[case_id],
            "strategy": strategy.to_dict(),
            "hard_safe_candidate_count": len(candidates),
            "plan_response": raw_path.name,
            "plan": plan.to_dict(),
            "story_consumption_audit": consumption,
            "passed": passed,
        }
        print(
            f"[M2 Consumption] {case_id}: hero={strategy.strategy_id}/{strategy.story_priority} "
            f"plan_valid={plan.plan_valid} consumption={consumption['passed']}"
        )

    passed_count = sum(bool(case.get("passed")) for case in report["cases"].values())
    report["summary"] = {
        "passed": passed_count,
        "expected": len(selected),
        "all_passed": passed_count == len(selected),
    }
    path = output_dir / f"m2_story_consumption_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M2 Consumption] {passed_count}/{len(selected)} report={path}")
    raise SystemExit(0 if report["summary"]["all_passed"] else 2)


if __name__ == "__main__":
    main()
