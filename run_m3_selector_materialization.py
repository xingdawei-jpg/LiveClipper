# -*- coding: utf-8 -*-
"""Materialize an already-recorded M2 plan without touching preview or render."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from clip_selector import bind_candidate_words_by_origin, materialize_narrative_plan  # noqa: E402
from run_m2_story_goldens import _load_srt  # noqa: E402
from story_planner import NarrativeBeat, NarrativePlan, PlanningCandidate, _parse_duration_plan  # noqa: E402


def _plan_from_payload(raw: Mapping[str, Any]) -> NarrativePlan:
    target_duration = float(raw.get("target_duration") or 0.0)
    beats = tuple(
        NarrativeBeat(
            source_role=str(item.get("source_role") or ""),
            narrative_role=str(item.get("narrative_role") or ""),
            goal=str(item.get("goal") or ""),
            candidate_evidence=tuple(int(value) for value in (item.get("candidate_ids") or ())),
            required=bool(item.get("required", True)),
            target_seconds=float(item.get("target_seconds") or 0.0),
            selection_instruction=str(item.get("selection_instruction") or ""),
            chapter_id=str(item.get("chapter_id") or ""),
            asset_tier=str(item.get("asset_tier") or ""),
            selection_origin=str(item.get("selection_origin") or ""),
            transition_from_previous=str(item.get("transition_from_previous") or ""),
        )
        for item in (raw.get("beats") or ())
        if isinstance(item, Mapping)
    )
    candidates = tuple(
        PlanningCandidate.from_mapping(item)
        for item in (raw.get("selected_candidates") or ())
        if isinstance(item, Mapping)
    )
    return NarrativePlan(
        strategy_id=str(raw.get("strategy_id") or ""),
        thesis=str(raw.get("thesis") or ""),
        target_duration=target_duration,
        beats=beats,
        status=str(raw.get("status") or ""),
        recommended_duration=float(raw.get("recommended_duration") or 0.0),
        issues=tuple(str(value) for value in (raw.get("issues") or ())),
        removed_beats=(),
        plan_valid=bool(raw.get("plan_valid")),
        selection_contract=dict(raw.get("selection_contract") or {}),
        selected_candidates=candidates,
        duration_assessment=dict(raw.get("duration_assessment") or {}),
        duration_plan=_parse_duration_plan(raw.get("duration_plan"), target_duration=max(1.0, target_duration)),
    )


def _srt_time(seconds: float) -> str:
    milliseconds = int(round(max(0.0, float(seconds)) * 1000.0))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("comparison_report", help="M2 comparison report or run_m2_duration_gradient.py report")
    parser.add_argument("--case", default="", help="case ID; required only when report has multiple cases")
    parser.add_argument("--duration-target", default="", help="M2 duration-gradient target, for example 30 or 120")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    report_path = Path(args.comparison_report).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    duration_plans = dict(report.get("duration_plans") or {})
    if duration_plans:
        target_key = str(args.duration_target or "").strip()
        if not target_key:
            if len(duration_plans) != 1:
                raise SystemExit("时长梯度报告含多个目标，请用 --duration-target 指定，例如 30。")
            target_key = next(iter(duration_plans))
        case = duration_plans.get(target_key)
        if not isinstance(case, Mapping):
            raise SystemExit(f"时长梯度报告未找到目标：{target_key}")
        case_id = f"{str(report.get('case_id') or 'case')}_{target_key}s"
        source_srt = str(report.get("source_srt") or "")
        plan = _plan_from_payload(dict(case.get("plan") or {}))
    else:
        cases = dict(report.get("cases") or {})
        case_id = str(args.case or "").strip()
        if not case_id:
            if len(cases) != 1:
                raise SystemExit("报告含多个案例，请用 --case 指定。")
            case_id = next(iter(cases))
        case = cases.get(case_id)
        if not isinstance(case, Mapping):
            raise SystemExit(f"未找到案例：{case_id}")
        source_srt = str(case.get("source_srt") or "")
        plan = _plan_from_payload(dict(case.get("m2_plan") or {}))
    if not source_srt:
        raise SystemExit("案例缺少 source_srt。")
    rows, subtitles = _load_srt(source_srt)
    del rows
    sidecar_path = Path(source_srt).with_suffix(".words.json")
    if sidecar_path.is_file():
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8-sig"))
        sidecar_segments = list(sidecar.get("segments") or [])
    else:
        sidecar_segments = []
    binding = bind_candidate_words_by_origin(
        plan.selected_candidates,
        subtitles,
        sidecar_segments,
    )
    result = materialize_narrative_plan(plan, binding.words_by_candidate)
    if binding.words_by_candidate:
        word_timeline_status = "verified"
    elif sidecar_path.is_file() and binding.unbound_reasons:
        word_timeline_status = "identity_mismatch"
    else:
        word_timeline_status = "not_available"
    output_dir = Path(args.out_dir) if args.out_dir else report_path.parent / "m3_selector"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    planned_duration = round(plan.total_seconds, 3)
    materialized_duration = round(sum(item.duration for item in result.ranges), 3)
    materialization_delta = round(materialized_duration - planned_duration, 3)
    alignment_tolerance = round(max(0.6, planned_duration * 0.03), 3)
    if result.status != "ok":
        duration_alignment_status = "materialization_blocked"
    elif abs(materialization_delta) > alignment_tolerance:
        duration_alignment_status = "replan_required"
    else:
        duration_alignment_status = "aligned"
    manifest = {
        "case_id": case_id,
        "source_srt": source_srt,
        "word_sidecar": str(sidecar_path),
        "word_sidecar_available": sidecar_path.is_file(),
        "word_timeline_status": word_timeline_status,
        "explicit_word_bindings": sorted(binding.words_by_candidate),
        "unbound_reasons": list(binding.unbound_reasons),
        "semantic_replan_required": bool(result.blocked),
        "duration_flow": {
            "target_duration": round(plan.target_duration, 3),
            "planned_duration": planned_duration,
            "materialized_duration": materialized_duration,
            "materialization_delta": materialization_delta,
            "alignment_tolerance": alignment_tolerance,
            "duration_alignment_status": duration_alignment_status,
            "duration_replan_required": duration_alignment_status == "replan_required",
            "director_duration_plan": plan.duration_plan.to_dict() if plan.duration_plan else None,
        },
        "selector_result": result.to_dict(),
    }
    (output_dir / "selector_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    subtitle_blocks = []
    for index, item in enumerate(result.ranges, 1):
        subtitle_blocks.append(
            f"{index}\n{_srt_time(item.start)} --> {_srt_time(item.end)}\n{item.text}\n"
        )
    (output_dir / "selector_subtitles.srt").write_text("\n".join(subtitle_blocks), encoding="utf-8")
    print(f"Selector manifest: {output_dir / 'selector_manifest.json'}")
    print(f"Selector subtitles: {output_dir / 'selector_subtitles.srt'}")
    print(
        f"status={result.status}, ranges={len(result.ranges)}, blocked={len(result.blocked)}, "
        f"planned={planned_duration:.3f}s materialized={materialized_duration:.3f}s "
        f"alignment={duration_alignment_status}"
    )


if __name__ == "__main__":
    main()
