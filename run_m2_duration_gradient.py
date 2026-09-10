# -*- coding: utf-8 -*-
"""Run one unchanged M1 story through duration-aware M2 planning.

This is an offline prototype verifier.  It does not import into preview,
smart-render, mix-render, or any release path.  Every target reuses the same
M1 Commercial Story and the same hard-safe candidate contract so the report
isolates narrative-depth decisions from story discovery.
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
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_clipper import load_settings  # noqa: E402
from commercial_analyzer import parse_strategy_result  # noqa: E402
from content_policy import default_content_policy  # noqa: E402
from run_m2_story_goldens import CASES, _freeze_safe_candidates, _load_srt  # noqa: E402
from story_planner import (  # noqa: E402
    _duration_depth_mode,
    duration_plan_needs_refinement,
    finalize_duration_budget_after_retry,
    plan_narrative_llm,
    refine_duration_narrative_llm,
    replan_narrative_llm,
)


def _targets(raw: str) -> tuple[float, ...]:
    values: list[float] = []
    for item in str(raw or "").split(","):
        try:
            value = float(item.strip())
        except ValueError:
            continue
        if value > 0 and value not in values:
            values.append(value)
    if not values:
        raise ValueError("至少提供一个大于 0 的时长，例如 30,60,90,120。")
    return tuple(values)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES), default="jccc")
    parser.add_argument("--targets", default="30,60,90,120")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replan-attempts", type=int, default=1)
    parser.add_argument("--duration-refine-attempts", type=int, default=1)
    parser.add_argument("--out-dir", default="workspace/m2_duration_gradient")
    args = parser.parse_args()

    targets = _targets(args.targets)
    case = CASES[args.case]
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = load_settings()
    api_key = str(settings.get("api_key") or "").strip()
    base_url = str(settings.get("base_url") or "").strip()
    model = str(settings.get("model") or "deepseek-v4-flash").strip()
    if not args.dry_run and (not api_key or not base_url):
        raise RuntimeError("缺少当前 AI 配置；不能执行真实 Duration-Aware M2 验证。")

    srt_text, subtitles = _load_srt(str(case["srt"]))
    # M1 is intentionally parsed once at a neutral duration.  Each target below
    # directs the same story rather than rediscovering a different story.
    raw_dir = ROOT / "workspace" / "m1_story_goldens_raw"
    strategy_result = parse_strategy_result(
        (raw_dir / f"{args.case}.txt").read_text(encoding="utf-8"),
        product=str(case["product"]),
        subtitles=subtitles,
        target_duration=60.0,
        content_contract=None,
    )
    strategy = next((item for item in strategy_result.strategies if item.strategy_id == case["story_id"]), None)
    if strategy is None:
        raise RuntimeError(f"{args.case}: 未找到 M1 story {case['story_id']}")
    policy = default_content_policy()
    safe_candidates, ledger = _freeze_safe_candidates(
        srt_text,
        subtitles,
        main_product=str(case["main_product"]),
        content_policy=policy,
    )
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    report: dict[str, Any] = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "dry_run" if args.dry_run else "live_model",
        "case_id": args.case,
        "source_srt": str(case["srt"]),
        "strategy": strategy.to_dict(),
        "m1_story_reused_across_targets": True,
        "candidate_count": len(safe_candidates),
        "safe_candidates": [candidate.payload() for candidate in safe_candidates],
        "candidate_ledger": ledger.to_dict(),
        "duration_plans": {},
    }

    for target in targets:
        target_key = f"{target:g}"
        contract = {
            "contract_version": "m2-duration-aware-gradient-v1",
            "target_duration": target,
            "main_product": case["main_product"],
            "product": case["product"],
            "content_policy": policy,
        }
        entry: dict[str, Any] = {
            "target_duration": target,
            "depth_mode": _duration_depth_mode(target),
            "selection_contract": contract,
        }
        if args.dry_run:
            report["duration_plans"][target_key] = entry
            continue

        response_path = output_dir / f"{args.case}_{target_key}_{stamp}.response.txt"
        plan = plan_narrative_llm(
            strategy=strategy,
            target_duration=target,
            safe_candidates=safe_candidates,
            selection_contract=contract,
            api_key=api_key,
            base_url=base_url,
            model=model,
            raw_response_hook=lambda raw, path=response_path: path.write_text(raw, encoding="utf-8"),
        )
        entry["initial_response_path"] = str(response_path)
        entry["initial_plan"] = plan.to_dict()
        structural_replans: list[dict[str, Any]] = []
        for attempt in range(1, max(0, int(args.replan_attempts)) + 1):
            if plan.plan_valid:
                break
            previous = plan
            replan_path = output_dir / f"{args.case}_{target_key}_{stamp}.replan{attempt}.response.txt"
            plan = replan_narrative_llm(
                strategy=strategy,
                invalid_plan=previous,
                safe_candidates=safe_candidates,
                selection_contract=contract,
                api_key=api_key,
                base_url=base_url,
                model=model,
                raw_response_hook=lambda raw, path=replan_path: path.write_text(raw, encoding="utf-8"),
            )
            structural_replans.append({
                "attempt": attempt,
                "response_path": str(replan_path),
                "input_replan_request": previous.replan_request.to_dict() if previous.replan_request else None,
                "plan": plan.to_dict(),
            })
        duration_refinements: list[dict[str, Any]] = []
        for attempt in range(1, max(0, int(args.duration_refine_attempts)) + 1):
            if not duration_plan_needs_refinement(plan):
                break
            previous = plan
            refine_path = output_dir / f"{args.case}_{target_key}_{stamp}.duration_refine{attempt}.response.txt"
            scout_path = output_dir / f"{args.case}_{target_key}_{stamp}.duration_scout{attempt}.response.txt"
            plan = refine_duration_narrative_llm(
                strategy=strategy,
                current_plan=previous,
                safe_candidates=safe_candidates,
                selection_contract=contract,
                api_key=api_key,
                base_url=base_url,
                model=model,
                raw_response_hook=lambda raw, path=refine_path: path.write_text(raw, encoding="utf-8"),
                duration_expansion_scout_hook=lambda raw, path=scout_path: path.write_text(raw, encoding="utf-8"),
            )
            duration_refinements.append({
                "attempt": attempt,
                "response_path": str(refine_path),
                "duration_expansion_scout_response_path": str(scout_path) if scout_path.exists() else None,
                "duration_expansion_scout": (
                    plan.duration_expansion_scout.to_dict() if plan.duration_expansion_scout else None
                ),
                "input_plan": previous.to_dict(),
                "plan": plan.to_dict(),
            })
        plan = finalize_duration_budget_after_retry(plan)
        entry["structural_replans"] = structural_replans
        entry["duration_refinements"] = duration_refinements
        entry["plan"] = plan.to_dict()
        entry["planned_duration"] = plan.total_seconds
        entry["duration_status"] = plan.status
        entry["director_duration_plan"] = plan.duration_plan.to_dict() if plan.duration_plan else None
        report["duration_plans"][target_key] = entry
        print(
            f"[M2 duration] {args.case} target={target:g}s depth={entry['depth_mode']} "
            f"planned={plan.total_seconds:.1f}s status={plan.status} valid={plan.plan_valid}"
        )

    report_path = output_dir / f"m2_duration_gradient_{args.case}_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M2 duration] 报告：{report_path}")


if __name__ == "__main__":
    main()
