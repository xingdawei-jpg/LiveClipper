# -*- coding: utf-8 -*-
"""Run a same-source Legacy vs M2 blind evaluation outside the product path."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from run_m2_story_goldens import CASES, ROOT, _freeze_safe_candidates, _load_srt

from ai_clipper import ai_analyze_clips, load_settings
from commercial_analyzer import parse_strategy_result
from content_policy import default_content_policy
from story_blind_eval import EvaluationVariant, build_blind_packet, empty_rating_sheet
from story_planner import plan_narrative_llm, replan_narrative_llm


def _legacy_variant(case_id: str, clips: Sequence[Sequence[Any]]) -> EvaluationVariant:
    payload = []
    for order, clip in enumerate(clips or (), 1):
        if not isinstance(clip, (tuple, list)) or len(clip) < 4:
            continue
        payload.append({
            "candidate_id": order,
            "start": float(clip[2]),
            "end": float(clip[3]),
            "text": str(clip[1] or "").strip(),
        })
    return EvaluationVariant.from_mapping({
        "variant_id": f"legacy:{case_id}",
        "clips": payload,
    })


def _comparison_variants(
    case_id: str,
    m2_plan: Mapping[str, Any],
    legacy_clips: Sequence[Sequence[Any]],
) -> tuple[EvaluationVariant, EvaluationVariant]:
    if not bool(m2_plan.get("plan_valid")):
        raise ValueError(f"{case_id}: M2 plan invalid; cannot construct a blind comparison")
    return (
        _legacy_variant(case_id, legacy_clips),
        EvaluationVariant.from_m2_plan(f"m2:{case_id}", m2_plan),
    )


def _call_legacy(
    *,
    srt_text: str,
    main_product: str,
    target_duration: float,
    content_policy: Mapping[str, Any],
) -> tuple[list[tuple[Any, ...]], list[str]]:
    """Call the current legacy director once with Review disabled for comparison."""
    logs: list[str] = []
    previous = os.environ.get("LIVECLIPPER_CONTENT_REVIEW_MODE")
    os.environ["LIVECLIPPER_CONTENT_REVIEW_MODE"] = "off"
    try:
        clips = ai_analyze_clips(
            srt_text,
            log_fn=logs.append,
            force_category=main_product,
            focus_hint="自动",
            target_duration=target_duration,
            ai_controls={
                "content_review_mode": "off",
                "content_policy": dict(content_policy),
            },
            record_history=False,
            allow_short_duration_output=True,
        )
    finally:
        if previous is None:
            os.environ.pop("LIVECLIPPER_CONTENT_REVIEW_MODE", None)
        else:
            os.environ["LIVECLIPPER_CONTENT_REVIEW_MODE"] = previous
    return [tuple(item) for item in (clips or ())], logs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--target-duration", type=float, default=40.0)
    parser.add_argument("--replan-attempts", type=int, default=1)
    parser.add_argument("--out-dir", default="workspace/m2_legacy_blind_eval")
    parser.add_argument("--seed", default="m2-legacy-2026-08-20")
    args = parser.parse_args()

    settings = load_settings()
    api_key = str(settings.get("api_key") or "").strip()
    if not api_key:
        raise SystemExit("未找到 AI API Key，无法运行 Legacy vs M2 对比。")
    base_url = str(settings.get("base_url") or "https://api.deepseek.com").strip()
    model = str(settings.get("model") or "deepseek-v4-flash").strip()
    policy = default_content_policy()
    raw_dir = ROOT / "workspace" / "m1_story_goldens_raw"
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    selected = CASES.keys() if args.case == "all" else (args.case,)
    public_cases: dict[str, tuple[EvaluationVariant, EvaluationVariant]] = {}
    report: dict[str, Any] = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "target_duration": float(args.target_duration),
        "model": model,
        "comparison": "same_source_same_content_contract_legacy_vs_m2",
        "cases": {},
    }
    for case_id in selected:
        case = CASES[case_id]
        srt_text, subtitles = _load_srt(str(case["srt"]))
        contract = {
            "contract_version": "m2-legacy-blind-eval-v1",
            "target_duration": float(args.target_duration),
            "content_policy": policy,
            "main_product": case["main_product"],
            "product": case["product"],
        }
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
        safe_candidates, ledger = _freeze_safe_candidates(
            srt_text,
            subtitles,
            main_product=str(case["main_product"]),
            content_policy=policy,
        )
        m2_raw_path = output_dir / f"{case_id}.m2.response.txt"
        m2_plan = plan_narrative_llm(
            strategy=strategy,
            target_duration=float(args.target_duration),
            safe_candidates=safe_candidates,
            selection_contract=contract,
            api_key=api_key,
            base_url=base_url,
            model=model,
            raw_response_hook=lambda raw, path=m2_raw_path: path.write_text(raw, encoding="utf-8"),
        )
        replans = []
        for attempt in range(1, max(0, int(args.replan_attempts)) + 1):
            if m2_plan.plan_valid:
                break
            previous = m2_plan
            replan_path = output_dir / f"{case_id}.m2.replan{attempt}.response.txt"
            m2_plan = replan_narrative_llm(
                strategy=strategy,
                invalid_plan=previous,
                safe_candidates=safe_candidates,
                selection_contract=contract,
                api_key=api_key,
                base_url=base_url,
                model=model,
                raw_response_hook=lambda raw, path=replan_path: path.write_text(raw, encoding="utf-8"),
            )
            replans.append({
                "attempt": attempt,
                "request": previous.replan_request.to_dict() if previous.replan_request else None,
                "plan": m2_plan.to_dict(),
                "response_path": str(replan_path),
            })

        legacy_clips, legacy_logs = _call_legacy(
            srt_text=srt_text,
            main_product=str(case["main_product"]),
            target_duration=float(args.target_duration),
            content_policy=policy,
        )
        m2_payload = m2_plan.to_dict()
        try:
            public_cases[case_id] = _comparison_variants(case_id, m2_payload, legacy_clips)
        except ValueError as error:
            report["cases"][case_id] = {
                "source_srt": case["srt"],
                "selection_contract": contract,
                "m2_plan": m2_payload,
                "legacy_clips": [list(item) for item in legacy_clips],
                "legacy_logs": legacy_logs,
                "candidate_ledger": ledger.to_dict(),
                "error": str(error),
            }
            continue
        report["cases"][case_id] = {
            "source_srt": case["srt"],
            "selection_contract": contract,
            "m2_plan": m2_payload,
            "m2_response_path": str(m2_raw_path),
            "m2_replans": replans,
            "legacy_clips": [list(item) for item in legacy_clips],
            "legacy_logs": legacy_logs,
            "candidate_ledger": ledger.to_dict(),
        }

    if not public_cases:
        report_path = output_dir / "comparison_failed.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(f"没有可用同源对比；详情：{report_path}")
    public_packet, private_key = build_blind_packet(public_cases, seed=args.seed)
    report_path = output_dir / "comparison_report.json"
    public_path = output_dir / "public_packet.json"
    private_path = output_dir / "private_answer_key.json"
    rating_path = output_dir / "rating_sheet.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    public_path.write_text(json.dumps(public_packet, ensure_ascii=False, indent=2), encoding="utf-8")
    private_path.write_text(json.dumps(private_key, ensure_ascii=False, indent=2), encoding="utf-8")
    rating_path.write_text(json.dumps(empty_rating_sheet(public_packet), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"匿名评审包：{public_path}")
    print(f"评分表：{rating_path}")
    print(f"私有映射：{private_path}")
    print(f"完整证据：{report_path}")


if __name__ == "__main__":
    main()
