# -*- coding: utf-8 -*-
"""P0.5C: render the first Director-chain plan without changing M3.

The runner consumes the frozen P0.5A.3 actor inventory and the AI-selected
P0.5A.4 H006 Hook.  A temporary casting view removes only final-utterance
hard blocks already defined by P0.2; it never adds ordinary beats, changes the
inventory JSON, decides semantic replacements, or changes M3.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_clipper import load_settings  # noqa: E402
from clip_selector import bind_candidate_words_by_origin, materialize_narrative_plan  # noqa: E402
from commerce_planner_lite import (  # noqa: E402
    _quality_final_utterance_reject_reason,
    _narrative_mode_contract,
    _narrative_mode_plan,
    _parse_narrative_mode_whole_video_audit,
    _post_lite_request,
    build_narrative_mode_whole_video_audit_prompt,
    plan_commerce_lite_narrative_mode_llm,
)
from hook_opening_recall import hook_candidate_as_opening_overlay  # noqa: E402
from micro_beat_inventory import prepare_narrative_mode_beat_execution  # noqa: E402
from run_m2_story_goldens import _load_srt  # noqa: E402
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source, _load_frozen_m1_strategy  # noqa: E402
from run_m3_selector_render import _duration, _tool, render_source_ranges, write_output_timeline_srt  # noqa: E402
from semantic_word_binder import build_semantic_srt_word_timeline  # noqa: E402


def _text(value: Any) -> str:
    return str(value or "").strip()


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _hook_from_report(report: Mapping[str, Any], hook_id: str) -> dict[str, Any]:
    for row in _rows(dict(report.get("hook_recall") or {}).get("hook_candidates")):
        if _text(row.get("hook_id")) == hook_id:
            return row
    raise RuntimeError(f"P0.5A.4 报告没有可用 Hook：{hook_id}")


def _p02_eligible_actor_rows(actor_pool: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    allowed: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in _rows(actor_pool.get("publishable_beat_inventory")):
        reason = _quality_final_utterance_reject_reason(_text(row.get("text")))
        if reason:
            excluded.append({"beat_id": _text(row.get("beat_id")), "reason": reason})
        else:
            allowed.append(row)
    return allowed, excluded


def _build_candidates(inventory: Mapping[str, Any], timeline: Any) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    execution = prepare_narrative_mode_beat_execution(
        publishable_inventory=inventory, word_timeline=timeline,
    )
    planning_by_id = {row.candidate_id: row for row in execution["candidates"]}
    source_by_beat = {
        _text(row.get("beat_id")): row
        for row in _rows(inventory.get("publishable_beat_inventory"))
    }
    mapped: list[dict[str, Any]] = []
    for row in execution.get("beat_candidate_map") or ():
        candidate_id = int(row.get("candidate_id") or 0)
        planning = planning_by_id.get(candidate_id)
        if planning is None:
            continue
        mapped.append({
            **source_by_beat.get(_text(row.get("beat_id")), {}), **dict(row),
            "candidate_id": candidate_id,
            "duration_seconds": planning.duration,
            "hook_eligible": planning.hook_eligible,
            "planning_candidate": planning,
        })
    return execution, tuple(mapped)


def _legal_ai_opening_package(
    *, p05a4: Mapping[str, Any], hook_id: str, beat_candidates: tuple[dict[str, Any], ...],
) -> dict[str, Any] | None:
    """Use an already AI-selected P0.5A.4 package only when P0.2 accepts it.

    The function intentionally does not score or choose sentences.  H006 has
    one remaining legal package after the P0.2 static contract excludes the
    package containing the known ``35厘米`` ASR error.
    """
    by_beat = {_text(row.get("beat_id")): row for row in beat_candidates}
    legal: list[dict[str, Any]] = []
    for package in _rows(dict(p05a4.get("opening_packages") or {}).get("opening_packages")):
        if _text(package.get("hook_id")) != hook_id or _text(package.get("quality")) not in {"strong", "medium"}:
            continue
        payoff_rows = [by_beat.get(_text(beat_id)) for beat_id in package.get("payoff_beat_ids") or ()]
        if not payoff_rows or any(row is None for row in payoff_rows):
            continue
        if any(_quality_final_utterance_reject_reason(_text(row.get("text"))) for row in payoff_rows if row):
            continue
        legal.append({**package, "_payoff_rows": payoff_rows})
    if len(legal) > 1:
        raise RuntimeError("P0.5C 有多个 P0.2 合法 Opening Package；不由程序做语义选择。")
    return legal[0] if legal else None


def _apply_ai_package_and_reaudit(
    *, base_audit: Mapping[str, Any], base_plan: Any, package: Mapping[str, Any],
    beat_candidates: tuple[dict[str, Any], ...], strategy: Any, director_contract: Mapping[str, Any],
    target_duration: float, settings: Mapping[str, Any], response_path: Path,
) -> tuple[dict[str, Any], Any]:
    """Honor the AI's P0.5A.4 Hook+payoff package, then reuse Whole Video Audit.

    This corrects only the integration gap where Narrative Casting was allowed
    to replace a legal package's C2 with two different mechanism Beats.  The
    selected Hook and payoff remain decisions from the earlier AI package; the
    rest of the casts remain decisions from the just-completed AI Casting.
    """
    casts = [dict(row) for row in dict(base_audit.get("beat_casting") or {}).get("casts") or ()]
    if len(casts) < 2:
        return dict(base_audit), base_plan
    by_beat = {_text(row.get("beat_id")): row for row in beat_candidates}
    hook_row = next((row for row in beat_candidates if bool(row.get("hook_eligible"))), None)
    payoff_rows = [dict(row) for row in package.get("_payoff_rows") or ()]
    if hook_row is None or not payoff_rows:
        return dict(base_audit), base_plan
    casts[0]["candidate_ids"] = [int(hook_row["candidate_id"])]
    casts[1]["candidate_ids"] = [int(row["candidate_id"]) for row in payoff_rows]
    casts[1]["purchase_outcome"] = _text(package.get("opening_promise")) or casts[1].get("purchase_outcome", "")
    casts[1]["why_it_advances"] = _text(package.get("why_viewer_keeps_watching")) or casts[1].get("why_it_advances", "")
    opening = {
        "hook_candidate_id": int(hook_row["candidate_id"]),
        "payoff_candidate_ids": [int(row["candidate_id"]) for row in payoff_rows],
        "promise": _text(package.get("opening_promise")),
        "payoff_relation": _text(package.get("payoff_relation")),
        "connection_reason": _text(package.get("why_viewer_keeps_watching")),
        "hook_integrity_reason": "AI P0.5A.4 已审 Hook + immediate payoff package",
    }
    narrative_contract = _narrative_mode_contract(director_contract)
    plan = _narrative_mode_plan(
        strategy=strategy, target_duration=target_duration,
        base_contract={**dict(base_plan.selection_contract or {}), "p0_5a4_package_enforced": _text(package.get("opening_id"))},
        narrative_contract=narrative_contract, journey=dict(base_audit.get("director_journey") or {}),
        casts=casts, opening=opening, beat_candidates=beat_candidates,
    )
    content = _text(_post_lite_request(
        api_key=_text(settings.get("api_key")), base_url=_text(settings.get("base_url")) or "https://api.deepseek.com",
        model=_text(settings.get("model")) or "deepseek-v4-flash",
        prompt=build_narrative_mode_whole_video_audit_prompt(
            journey=dict(base_audit.get("director_journey") or {}), casts=casts, plan=plan,
        ), stage="P0_5C_package_fidelity_whole_video_audit", max_tokens=2600,
    ).get("choices", [{}])[0].get("message", {}).get("content"))
    response_path.write_text(content, encoding="utf-8")
    parsed = _parse_narrative_mode_whole_video_audit(
        json.loads(content) if content.startswith("{") else {}, total_seconds=plan.total_seconds,
    )
    assessment = dict(plan.duration_assessment or {}) | {
        "status": "journey_complete" if parsed["passed"] else "journey_incomplete",
        "reason": "Whole Video Audit 通过，允许 M3 忠实物化。" if parsed["passed"] else "Whole Video Audit 要求重新由 AI Beat Casting 处理；程序不自动换句。",
        "narrative_mode_whole_video_audit": parsed,
        "m3_render_gate": {"passed": bool(parsed["passed"]), "reason": "whole_video_audit_passed" if parsed["passed"] else "whole_video_audit_needs_recast"},
    }
    audit = dict(base_audit) | {
        "beat_casting": {"casts": casts, "opening_package": opening, "errors": []},
        "whole_video_audit": parsed,
        "p0_5c_package_fidelity": {"opening_id": _text(package.get("opening_id")), "authority": "AI_P0_5A4_opening_package"},
    }
    return audit, replace(plan, status="journey_complete" if parsed["passed"] else "journey_incomplete", plan_valid=bool(plan.plan_valid and parsed["passed"]), duration_assessment=assessment)


def _write_selector_artifacts(*, plan: Any, source_srt: Path, output_dir: Path) -> tuple[dict[str, Any], Path]:
    _, subtitles = _load_srt(str(source_srt))
    sidecar_path = source_srt.with_suffix(".words.json")
    sidecar_segments = []
    if sidecar_path.is_file():
        sidecar_segments = list(json.loads(sidecar_path.read_text(encoding="utf-8-sig")).get("segments") or ())
    binding = bind_candidate_words_by_origin(plan.selected_candidates, subtitles, sidecar_segments)
    selector = materialize_narrative_plan(plan, binding.words_by_candidate)
    planned = round(float(plan.total_seconds), 3)
    materialized = round(sum(item.duration for item in selector.ranges), 3)
    tolerance = round(max(0.6, planned * 0.03), 3)
    alignment = "aligned" if selector.status == "ok" and abs(materialized - planned) <= tolerance else "replan_required"
    manifest = {
        "case_id": "p05c_caramel_h006",
        "source_srt": str(source_srt),
        "word_sidecar": str(sidecar_path),
        "word_sidecar_available": sidecar_path.is_file(),
        "word_timeline_status": "verified" if binding.words_by_candidate else "not_available",
        "explicit_word_bindings": sorted(binding.words_by_candidate),
        "unbound_reasons": list(binding.unbound_reasons),
        "semantic_replan_required": bool(selector.blocked),
        "duration_flow": {
            "target_duration": round(float(plan.target_duration), 3),
            "planned_duration": planned,
            "materialized_duration": materialized,
            "materialization_delta": round(materialized - planned, 3),
            "alignment_tolerance": tolerance,
            "duration_alignment_status": alignment,
            "duration_replan_required": alignment != "aligned",
        },
        "selector_result": selector.to_dict(),
    }
    path = output_dir / "selector_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if selector.status != "ok" or alignment != "aligned":
        raise RuntimeError(f"M3 词级物化拒绝：status={selector.status}; alignment={alignment}; blocked={len(selector.blocked)}")
    subtitle_path = output_dir / "selector_subtitles.srt"
    write_output_timeline_srt(manifest["selector_result"]["ranges"], subtitle_path)
    return manifest, path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--p05a4-report", required=True)
    parser.add_argument("--actor-pool", required=True)
    parser.add_argument("--out-dir", default="workspace/p05c_first_directed_render")
    parser.add_argument("--target-duration", type=float, default=45.0)
    args = parser.parse_args()

    output_dir = (ROOT / args.out_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_report_path = Path(args.source_report).resolve()
    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    case = dict((source_report.get("cases") or {}).get("jccc_deep_roast_hoodie") or {})
    source_srt = Path(_text(case.get("source_srt"))).resolve()
    director_contract = dict(case.get("director_strategy_contract") or {})
    if not source_srt.is_file() or not director_contract:
        raise RuntimeError("P0.5C 缺少冻结焦糖 source_srt 或 Director contract")
    settings = load_settings()
    if not _text(settings.get("api_key")):
        raise RuntimeError("未找到 AI API Key，无法执行 AI Beat Casting")

    p05a4 = json.loads(Path(args.p05a4_report).resolve().read_text(encoding="utf-8"))
    hook = _hook_from_report(p05a4, "H006")
    actor_pool = json.loads(Path(args.actor_pool).resolve().read_text(encoding="utf-8"))
    allowed, excluded = _p02_eligible_actor_rows(actor_pool)
    overlay = hook_candidate_as_opening_overlay(hook)
    casting_inventory = {**actor_pool, "publishable_beat_inventory": allowed + [overlay]}

    ledger = _asset_ledger_for_source("jccc_deep_roast_hoodie", source_srt=str(source_srt))
    timeline = build_semantic_srt_word_timeline(str(ledger["source_srt"]))
    execution, beat_candidates = _build_candidates(casting_inventory, timeline)
    if not any(bool(row.get("hook_eligible")) for row in execution["beat_candidate_map"]):
        raise RuntimeError("H006 未进入临时 Casting 视图")
    responses: dict[str, str] = {}

    def record(name: str, value: str) -> None:
        path = output_dir / f"p05c.{name}.response.txt"
        path.write_text(value, encoding="utf-8")
        responses[name] = path.name

    audit, plan = plan_commerce_lite_narrative_mode_llm(
        strategy=_load_frozen_m1_strategy(str(source_report_path)), beat_candidates=beat_candidates,
        target_duration=float(args.target_duration), safe_candidates=execution["candidates"],
        selection_contract={
            "p0_5c_h006_opening_overlay": True,
            "p0_5a3_actor_pool_read_only": True,
            "p0_2_hard_blocks_excluded_from_casting_view": True,
            "strong_clip_top12_used_before_journey": False,
        },
        api_key=_text(settings.get("api_key")), base_url=_text(settings.get("base_url")) or "https://api.deepseek.com",
        model=_text(settings.get("model")) or "deepseek-v4-flash", director_strategy_contract=director_contract,
        journey_response_hook=lambda value: record("journey", value),
        casting_response_hook=lambda value: record("casting", value),
        whole_video_audit_response_hook=lambda value: record("whole_video_audit", value),
    )
    initial_whole_audit = dict(audit.get("whole_video_audit") or {})
    package = _legal_ai_opening_package(p05a4=p05a4, hook_id="H006", beat_candidates=beat_candidates)
    if (
        package is not None
        and not bool(initial_whole_audit.get("passed"))
        and "first_10_second_progression_insufficient" in "|".join(initial_whole_audit.get("issues") or ())
    ):
        audit, plan = _apply_ai_package_and_reaudit(
            base_audit=audit, base_plan=plan, package=package, beat_candidates=beat_candidates,
            strategy=_load_frozen_m1_strategy(str(source_report_path)), director_contract=director_contract,
            target_duration=float(args.target_duration), settings=settings,
            response_path=output_dir / "p05c.package_fidelity_whole_video_audit.response.txt",
        )
    selected_hard_blocks = [
        {"candidate_id": item.candidate_id, "reason": _quality_final_utterance_reject_reason(item.text)}
        for item in plan.selected_candidates if _quality_final_utterance_reject_reason(item.text)
    ]
    audit_data = dict(audit.get("whole_video_audit") or {})
    if not plan.plan_valid or selected_hard_blocks or not bool(audit_data.get("passed")):
        report = {
            "stage": "P0.5C First Director-chain Render", "status": "blocked_before_m3",
            "plan": plan.to_dict(), "narrative_audit": audit,
            "p0_2_casting_excluded": excluded, "selected_hard_blocks": selected_hard_blocks,
            "legal_ai_opening_package": {key: value for key, value in (package or {}).items() if key != "_payoff_rows"},
            "responses": responses, "m3_modified": False, "rendered": False,
        }
        (output_dir / "p0_5c_execution_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise RuntimeError("P0.5C 重选仍未通过 Whole Video / P0.2；拒绝调用 M3 渲染。")

    try:
        manifest, manifest_path = _write_selector_artifacts(plan=plan, source_srt=source_srt, output_dir=output_dir)
    except RuntimeError as error:
        report = {
            "stage": "P0.5C First Director-chain Render", "status": "blocked_at_m3_materialization",
            "opening_hook": {"hook_id": "H006", "text": hook.get("text"), "duration": hook.get("duration")},
            "p0_2_casting_excluded": excluded,
            "legal_ai_opening_package": {key: value for key, value in (package or {}).items() if key != "_payoff_rows"},
            "plan": plan.to_dict(), "narrative_audit": audit,
            "selector_manifest": str(output_dir / "selector_manifest.json"),
            "m3_materialization_error": str(error), "responses": responses,
            "m3_modified": False, "rendered": False,
        }
        (output_dir / "p0_5c_execution_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    source_video = source_srt.with_suffix(".mp4")
    if not source_video.is_file():
        raise RuntimeError(f"找不到同源视频：{source_video}")
    render_dir = output_dir / "m3_render"
    render_dir.mkdir(exist_ok=True)
    ranges = list(dict(manifest["selector_result"]).get("ranges") or ())
    preview = render_dir / "selector_preview.mp4"
    render_source_ranges(source_video, ranges, preview)
    caption_srt = render_dir / "selector_preview_subtitles.srt"
    write_output_timeline_srt(ranges, caption_srt)
    ffmpeg = _tool("ffmpeg")
    captioned = render_dir / "selector_preview_captioned.mp4"
    import subprocess
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-i", str(preview), "-i", str(caption_srt),
        "-map", "0", "-map", "1:0", "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=chi", str(captioned),
    ], check=True)
    rendered_duration = round(_duration(_tool("ffprobe"), preview), 3)
    report = {
        "stage": "P0.5C First Director-chain Render", "status": "rendered",
        "opening_hook": {"hook_id": "H006", "text": hook.get("text"), "duration": hook.get("duration")},
        "p0_2_casting_excluded": excluded, "legal_ai_opening_package": {key: value for key, value in (package or {}).items() if key != "_payoff_rows"},
        "plan": plan.to_dict(), "narrative_audit": audit,
        "selector_manifest": str(manifest_path), "rendered_video": str(preview),
        "captioned_video": str(captioned), "rendered_duration": rendered_duration,
        "responses": responses, "m3_modified": False, "m3_word_level_materialization": "passed",
    }
    (output_dir / "p0_5c_execution_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": "rendered", "report": str(output_dir / "p0_5c_execution_report.json"),
        "video": str(preview), "captioned_video": str(captioned), "duration": rendered_duration,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
