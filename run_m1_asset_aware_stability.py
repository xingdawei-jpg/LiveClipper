# -*- coding: utf-8 -*-
"""Repeat the Asset-Aware M1 golden discovery without entering M2/M3.

This is an evidence runner, not a story selector.  ``story_priority`` is only
audited as M1's declared importance: the runner never uses it to choose clips,
rank candidates, or change a story.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for path in (str(ROOT), str(APP)):
    if path not in sys.path:
        sys.path.insert(0, path)

from commercial_analyzer import analyze_commercial_story, assess_story_commercial_change  # noqa: E402
from run_commercial_asset_ledger_audit import build_asset_ledger_case  # noqa: E402
from run_m1_asset_aware_goldens import _hard_safe_subtitles  # noqa: E402
from run_m1_story_goldens import CASES  # noqa: E402


def _commercial_change_directions(
    raw: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Sequence[Sequence[str]]]]:
    """Accept one legacy contract or every approved Hero direction for a case."""
    if any(key in raw for key in ("problem", "solution", "outcome")):
        return {"expected": raw}  # Backward-compatible deterministic test input.
    return {
        str(direction): contract
        for direction, contract in raw.items()
        if isinstance(contract, Mapping)
    }


def assess_stability_run(
    *,
    result: Any,
    commercial_change: Mapping[str, Any],
) -> dict[str, Any]:
    """Assess one M1 response without altering it.

    A Hero direction is stable only when an expected commercial-change story is
    explicitly marked ``high``.  A different high-priority story is recorded
    as drift rather than silently treated as an acceptable alternative.
    """
    stories = tuple(getattr(result, "strategies", ()) or ())
    directions = _commercial_change_directions(commercial_change)
    assessments = {
        direction: {
            story.strategy_id: assess_story_commercial_change(story, contract)
            for story in stories
        }
        for direction, contract in directions.items()
    }
    expected_hero_ids_by_direction = {
        direction: [sid for sid, assessment in direction_assessments.items() if assessment["passed"]]
        for direction, direction_assessments in assessments.items()
    }
    expected_hero_ids = sorted({sid for ids in expected_hero_ids_by_direction.values() for sid in ids})
    high_ids = [story.strategy_id for story in stories if story.story_priority == "high"]
    high_hero_ids = [sid for sid in expected_hero_ids if sid in high_ids]
    high_priority_drift_ids = [sid for sid in high_ids if sid not in expected_hero_ids]
    boundary_exclusions = {
        story.strategy_id: list(story.excluded_assets_reason)
        for story in stories if story.excluded_assets_reason
    }
    valid_story_ids = [
        story.strategy_id for story in stories
        if story.story_validity == "recommended" and not story.excluded_assets_reason
    ]
    return {
        "has_valid_story": bool(valid_story_ids),
        "valid_story_ids": valid_story_ids,
        "expected_hero_ids": expected_hero_ids,
        "expected_hero_ids_by_direction": expected_hero_ids_by_direction,
        "high_priority_hero_ids": high_hero_ids,
        "high_priority_drift_ids": high_priority_drift_ids,
        "asset_boundary_clean": not boundary_exclusions,
        "asset_boundary_exclusions": boundary_exclusions,
        "story_priorities": {story.strategy_id: story.story_priority for story in stories},
        "commercial_change_assessments": assessments,
    }


def summarize_stability(runs_by_case: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    all_runs = [item for runs in runs_by_case.values() for item in runs]
    total = len(all_runs)
    valid = sum(bool(item.get("has_valid_story")) for item in all_runs)
    hero = sum(bool(item.get("high_priority_hero_ids")) for item in all_runs)
    clean = sum(bool(item.get("asset_boundary_clean")) for item in all_runs)
    drifts = sum(len(item.get("high_priority_drift_ids") or ()) for item in all_runs)
    return {
        "total_runs": total,
        "valid_story_runs": valid,
        "high_priority_expected_hero_runs": hero,
        "asset_boundary_clean_runs": clean,
        "high_priority_drift_count": drifts,
        "thresholds": {
            "valid_story_runs": total,
            "high_priority_expected_hero_runs": 12,
            "asset_boundary_clean_runs": total,
            "high_priority_drift_count": 0,
        },
        "m2_source_only_prerequisite_passed": bool(total)
        and valid == total
        and hero >= 12
        and clean == total
        and drifts == 0,
    }


def summarize_direction_coverage(runs_by_case: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Show every approved direction separately; an aggregate pass cannot hide an omission."""
    report: dict[str, Any] = {}
    for case_id, runs in runs_by_case.items():
        directions: dict[str, dict[str, int]] = {}
        for run in runs:
            expected_by_direction = run.get("expected_hero_ids_by_direction") or {}
            high_ids = set(run.get("high_priority_hero_ids") or ())
            for direction, ids in expected_by_direction.items():
                row = directions.setdefault(str(direction), {"any_match_runs": 0, "high_match_runs": 0})
                if ids:
                    row["any_match_runs"] += 1
                if any(str(strategy_id) in high_ids for strategy_id in ids):
                    row["high_match_runs"] += 1
        report[case_id] = {
            direction: {**counts, "total_case_runs": len(runs)}
            for direction, counts in sorted(directions.items())
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--out-dir", default="workspace/m1_asset_aware_stability")
    parser.add_argument("--target-duration", type=float, default=60.0)
    parser.add_argument("--from-report", default="")
    args = parser.parse_args()
    if args.runs < 1:
        raise ValueError("--runs 必须至少为 1")

    fixture = json.loads((ROOT / "tests" / "fixtures" / "commercial_story_semantic_goldens.json").read_text(encoding="utf-8"))
    selected = tuple(CASES) if args.case == "all" else (args.case,)
    case_inputs: dict[str, dict[str, Any]] = {}
    for case_id in selected:
        context = build_asset_ledger_case(case_id)
        case_inputs[case_id] = {
            "assets": list(context["assets"]),
            "subtitles": _hard_safe_subtitles(list(context["assets"])),
            "commercial_change": {
                golden_id: fixture[golden_id]["commercial_change"]
                for golden_id in CASES[case_id]["goldens"]
            },
            "hard_safe_candidate_count": context["hard_safe_candidate_count"],
            "story_permission_counts": context["story_permission_counts"],
        }

    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    runs_by_case: dict[str, list[dict[str, Any]]] = {case_id: [] for case_id in selected}
    if args.from_report:
        previous = json.loads((ROOT / args.from_report).read_text(encoding="utf-8"))
        for case_id in selected:
            for saved in (previous.get("cases", {}).get(case_id, {}) or {}).get("runs", ()):
                raw_result = saved.get("result") or {}
                from commercial_analyzer import parse_strategy_result  # noqa: E402
                result = parse_strategy_result(
                    json.dumps({"strategies": raw_result.get("strategies") or ()}, ensure_ascii=False),
                    product=CASES[case_id]["product"],
                    subtitles=case_inputs[case_id]["subtitles"],
                    target_duration=args.target_duration,
                    commercial_assets=case_inputs[case_id]["assets"],
                )
                runs_by_case[case_id].append({
                    "attempt": int(saved.get("attempt") or 0),
                    "raw_response": str(saved.get("raw_response") or ""),
                    "result": result.to_dict(),
                    **assess_stability_run(result=result, commercial_change=case_inputs[case_id]["commercial_change"]),
                })
    else:
        from ai_clipper import load_settings  # noqa: E402
        settings = load_settings()
        if not str(settings.get("api_key") or "").strip():
            raise RuntimeError("未找到 AI API Key，不能运行真实 M1 稳定性验证。")
        for attempt in range(1, args.runs + 1):
            for case_id in selected:
                current = case_inputs[case_id]
                raw_path = output_dir / f"{case_id}_run{attempt}_{dt.datetime.now():%Y%m%d_%H%M%S}.response.txt"
                result = analyze_commercial_story(
                    api_key=str(settings["api_key"]),
                    base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                    model=str(settings.get("model") or "deepseek-v4-flash"),
                    product=CASES[case_id]["product"],
                    subtitles=current["subtitles"],
                    target_duration=args.target_duration,
                    commercial_assets=current["assets"],
                    raw_response_hook=lambda value, path=raw_path: path.write_text(value, encoding="utf-8"),
                )
                assessment = assess_stability_run(result=result, commercial_change=current["commercial_change"])
                runs_by_case[case_id].append({
                    "attempt": attempt,
                    "raw_response": raw_path.name,
                    "result": result.to_dict(),
                    **assessment,
                })
                print(
                    f"[M1 Stability] {case_id} run={attempt}/{args.runs} "
                    f"valid={assessment['has_valid_story']} hero={assessment['high_priority_hero_ids']} "
                    f"drift={assessment['high_priority_drift_ids']} clean={assessment['asset_boundary_clean']}"
                )

    report = {
        "version": "m1-asset-aware-stability-v1",
        "mode": "reassess_saved_live_responses" if args.from_report else "live_model",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "runs_per_case": args.runs,
        "m1_m2_m3_formal_paths_changed": False,
        "cases": {
            case_id: {
                "hard_safe_candidate_count": case_inputs[case_id]["hard_safe_candidate_count"],
                "story_permission_counts": case_inputs[case_id]["story_permission_counts"],
                "m1_facts_boundary": "hard_safe_raw_subtitles_only",
                "runs": runs,
            }
            for case_id, runs in runs_by_case.items()
        },
        "summary": summarize_stability(runs_by_case),
        "direction_coverage": summarize_direction_coverage(runs_by_case),
    }
    path = output_dir / f"m1_asset_aware_stability_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    print(
        f"[M1 Stability] valid={summary['valid_story_runs']}/{summary['total_runs']} "
        f"hero={summary['high_priority_expected_hero_runs']}/{summary['total_runs']} "
        f"drifts={summary['high_priority_drift_count']} report={path}"
    )
    raise SystemExit(0 if summary["m2_source_only_prerequisite_passed"] else 2)


if __name__ == "__main__":
    main()
