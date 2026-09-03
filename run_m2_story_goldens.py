# -*- coding: utf-8 -*-
"""Explicit M2 prototype verification against three real M1-commercial stories.

This tool is intentionally outside the production selection route.  It creates
the current hard-safe candidate pool with the real freezer, asks M2 to direct a
chapter plan, and saves the exact prompt/response/validated plan for review.

M1 evidence is linked to frozen candidates through the CandidateLedger's
explicit semantic ancestors.  It never reconstructs ancestry by timestamp
containment, so a missing bridge fails the verification instead of silently
guessing one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_clipper import (  # noqa: E402
    _attach_safe_inventory_context,
    _director_candidate_source,
    _director_safe_candidate_inventory,
    _freeze_director_candidates,
    _hook_role_ineligibility_reason,
    _build_ai_srt_entry_index,
    load_settings,
)
from commercial_analyzer import parse_strategy_result  # noqa: E402
from candidate_ledger import CandidateLedger  # noqa: E402
from content_policy import default_content_policy  # noqa: E402
from srt_parser import _time_to_seconds, open_srt  # noqa: E402
from story_planner import (  # noqa: E402
    PlanningCandidate,
    build_planner_prompt,
    duration_plan_needs_refinement,
    finalize_duration_budget_after_retry,
    plan_narrative_llm,
    refine_duration_narrative_llm,
    replan_narrative_llm,
)


CASES = {
    "jccc": {
        "product": "焦糖朗姆针织罩衫套装",
        "main_product": "上衣",
        "story_id": "S1",
        "srt": r"C:\工作\JCCC的穿搭影记(2.25春上新）\单品素材\JCCC影子【焦糖朗姆】宽松透气针织罩衫遮肉显瘦出片套装女JE6NZZ11\5.srt",
    },
    "jianzhi": {
        "product": "简致衬衫",
        "main_product": "上衣",
        "story_id": "S1",
        "srt": r"C:\工作\珊姐\8月14日直播\简致（1）.srt",
    },
    "hanxi": {
        "product": "韩系学院风连衣裙",
        "main_product": "裙子",
        "story_id": "S1",
        "srt": r"C:\工作\小贤\单品素材\7-22单品\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙_1.srt",
    },
}


def _load_srt(path: str) -> tuple[str, list[dict[str, object]]]:
    rows, _encoding = open_srt(path)
    subtitles: list[dict[str, object]] = []
    parts: list[str] = []
    for row in rows:
        start = round(_time_to_seconds(row.start), 3)
        end = round(_time_to_seconds(row.end), 3)
        text = str(row.text or "").strip()
        subtitles.append({"id": int(row.index), "start": start, "end": end, "text": text})
        parts.append(
            f"{int(row.index)}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n"
        )
    return "\n".join(parts), subtitles


def _srt_time(seconds: float) -> str:
    milliseconds = int(round(max(0.0, float(seconds)) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _freeze_safe_candidates(
    srt_text: str,
    subtitles: list[dict[str, object]],
    *,
    main_product: str,
    content_policy: dict[str, object],
) -> tuple[tuple[PlanningCandidate, ...], CandidateLedger]:
    """Use the same frozen -> hard-safety inventory as the live director.

    Freezing alone preserves source boundaries but does not apply every final
    policy matcher.  M2 must receive the later hard-safe inventory, otherwise
    a blocked price or CTA line could be mislabelled as safe in this prototype.
    """
    ledger = CandidateLedger()
    ledger.seed(
        "semantic_input",
        [
            {
                "candidate_id": int(item["id"]),
                "start": float(item["start"]),
                "end": float(item["end"]),
                "text": str(item["text"]),
            }
            for item in subtitles
        ],
    )
    frozen_srt = _freeze_director_candidates(
        srt_text,
        word_timings=None,
        content_policy=content_policy,
        main_product=main_product,
        candidate_ledger=ledger,
    )
    entries = _build_ai_srt_entry_index(frozen_srt)
    inventory = _attach_safe_inventory_context(
        _director_safe_candidate_inventory(entries, content_policy=content_policy),
        entries,
    )
    origin_by_candidate = ledger.frozen_candidate_origins()
    candidates: list[PlanningCandidate] = []
    for item in inventory:
        candidate_id = int(item.get("srt_index") or 0)
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        text = str(item.get("text") or "").strip()
        text = str(text or "").strip()
        if candidate_id <= 0 or not text or end <= start:
            continue
        origin_subtitle_ids = origin_by_candidate.get(candidate_id)
        if not origin_subtitle_ids:
            raise RuntimeError(
                f"冻结候选 {candidate_id} 缺少 CandidateLedger 显式语义血缘，拒绝按时间戳猜测"
            )
        candidates.append(PlanningCandidate(
            candidate_id=candidate_id,
            source_id=str(item.get("source") or _director_candidate_source(text) or "SINGLE"),
            start=start,
            end=end,
            text=text,
            origin_subtitle_ids=origin_subtitle_ids,
            hook_eligible=not bool(_hook_role_ineligibility_reason(text, content_policy=content_policy)),
            role_permissions=tuple(item.get("role_permissions") or ("hook", "product")),
            subject_relation=str(item.get("subject_relation") or "main"),
            story_block_id=str(item.get("story_block_id") or ""),
            continuity_group_id=str(item.get("continuity_group_id") or ""),
        ))
    return tuple(candidates), ledger


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--target-duration", type=float, default=40.0)
    parser.add_argument("--dry-run", action="store_true", help="只构建并保存完整 M2 输入，不调用模型")
    parser.add_argument("--replan-attempts", type=int, default=1, help="每个结构化 replan 最多交回导演的次数")
    parser.add_argument(
        "--duration-refine-attempts",
        type=int,
        default=1,
        help="合法但与导演时长预算不一致的计划最多交回导演重规划次数；程序不自行删片或补片",
    )
    parser.add_argument("--out-dir", default="workspace/m2_story_goldens")
    args = parser.parse_args()

    settings = load_settings()
    api_key = str(settings.get("api_key") or "").strip()
    base_url = str(settings.get("base_url") or "https://api.deepseek.com").strip()
    model = str(settings.get("model") or "deepseek-v4-flash").strip()
    if not args.dry_run and not api_key:
        raise SystemExit("未找到 AI API Key，无法调用 M2。")

    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = ROOT / "workspace" / "m1_story_goldens_raw"
    policy = default_content_policy()
    contract = {
        "contract_version": "m2-prototype-1",
        "target_duration": float(args.target_duration),
        "content_policy": policy,
        "candidate_lineage": "candidate_ledger_explicit_semantic_ancestors_v1",
    }
    selected = CASES.keys() if args.case == "all" else (args.case,)
    report: dict[str, object] = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "mode": "dry_run" if args.dry_run else "live_model",
        "model": model if not args.dry_run else "not_called",
        "candidate_lineage": "candidate_ledger_explicit_semantic_ancestors_v1",
        "cases": {},
    }

    for case_id in selected:
        case = CASES[case_id]
        srt_text, subtitles = _load_srt(str(case["srt"]))
        strategy_result = parse_strategy_result(
            (raw_dir / f"{case_id}.txt").read_text(encoding="utf-8"),
            product=str(case["product"]),
            subtitles=subtitles,
            target_duration=float(args.target_duration),
            content_contract=None,
        )
        strategy = next((item for item in strategy_result.strategies if item.strategy_id == case["story_id"]), None)
        if strategy is None:
            raise RuntimeError(f"{case_id}: 未找到 M1 story {case['story_id']}")
        safe_candidates, candidate_ledger = _freeze_safe_candidates(
            srt_text,
            subtitles,
            main_product=str(case["main_product"]),
            content_policy=policy,
        )
        case_contract = {**contract, "main_product": case["main_product"], "product": case["product"]}
        prompt = build_planner_prompt(strategy, args.target_duration, safe_candidates, case_contract)
        stamp = f"{case_id}_{dt.datetime.now():%Y%m%d_%H%M%S}"
        prompt_path = output_dir / f"{stamp}.prompt.txt"
        prompt_path.write_text(prompt, encoding="utf-8")
        print(f"[M2] {case_id}: M1={strategy.strategy_id}, hard-safe={len(safe_candidates)}, prompt={prompt_path.name}")
        case_report: dict[str, object] = {
            "source_srt": case["srt"],
            "strategy": strategy.to_dict(),
            "selection_contract": case_contract,
            "candidate_count": len(safe_candidates),
            "safe_candidates": [item.payload() for item in safe_candidates],
            "candidate_ledger": candidate_ledger.to_dict(),
            "prompt_path": str(prompt_path),
        }
        if not args.dry_run:
            raw_path = output_dir / f"{stamp}.response.txt"
            plan = plan_narrative_llm(
                strategy=strategy,
                target_duration=args.target_duration,
                safe_candidates=safe_candidates,
                selection_contract=case_contract,
                api_key=api_key,
                base_url=base_url,
                model=model,
                raw_response_hook=lambda raw, path=raw_path: path.write_text(raw, encoding="utf-8"),
            )
            case_report["response_path"] = str(raw_path)
            case_report["initial_plan"] = plan.to_dict()
            replans = []
            for attempt in range(1, max(0, int(args.replan_attempts)) + 1):
                if plan.plan_valid:
                    break
                replan_path = output_dir / f"{stamp}.replan{attempt}.response.txt"
                previous = plan
                plan = replan_narrative_llm(
                    strategy=strategy,
                    invalid_plan=previous,
                    safe_candidates=safe_candidates,
                    selection_contract=case_contract,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    raw_response_hook=lambda raw, path=replan_path: path.write_text(raw, encoding="utf-8"),
                )
                replans.append({
                    "attempt": attempt,
                    "response_path": str(replan_path),
                    "input_replan_request": previous.replan_request.to_dict() if previous.replan_request else None,
                    "plan": plan.to_dict(),
                })
            duration_refinements = []
            for attempt in range(1, max(0, int(args.duration_refine_attempts)) + 1):
                if not duration_plan_needs_refinement(plan):
                    break
                refinement_path = output_dir / f"{stamp}.duration_refine{attempt}.response.txt"
                previous = plan
                plan = refine_duration_narrative_llm(
                    strategy=strategy,
                    current_plan=previous,
                    safe_candidates=safe_candidates,
                    selection_contract=case_contract,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    raw_response_hook=lambda raw, path=refinement_path: path.write_text(raw, encoding="utf-8"),
                )
                duration_refinements.append({
                    "attempt": attempt,
                    "response_path": str(refinement_path),
                    "input_plan": previous.to_dict(),
                    "plan": plan.to_dict(),
                })
            plan = finalize_duration_budget_after_retry(plan)
            case_report["replans"] = replans
            case_report["duration_refinements"] = duration_refinements
            case_report["plan"] = plan.to_dict()
            print(
                f"[M2] {case_id}: chapters={len(plan.beats)} actual={plan.total_seconds}s "
                f"status={plan.status} valid={plan.plan_valid}"
            )
        report["cases"][case_id] = case_report

    report_path = output_dir / f"m2_story_goldens_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M2] 报告：{report_path}")


if __name__ == "__main__":
    main()
