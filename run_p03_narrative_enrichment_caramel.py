# -*- coding: utf-8 -*-
"""P0.4 Chapter Packet regression for the two approved caramel P0.2 cuts.

This runner intentionally replays the approved P0.2 M2 receipts through the
current production validator, then makes one live M2 Chapter Packet request.
P0.3 may run only if that same M2 Packet decision explicitly requests its
single-candidate fallback.  This prevents a new random Strong Clip Ranking
answer from changing a previously approved opening or Purchase Journey Quality
decision.  It is not a new planner and it never calls M1, renders video, or
gives M3 any semantic editing authority.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping
from unittest.mock import patch


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import commerce_planner_lite  # noqa: E402
from ai_clipper import load_settings  # noqa: E402
from clip_selector import (  # noqa: E402
    SelectorBlocked,
    assess_candidate_materializability,
    audit_materialization_fidelity,
    materialize_narrative_plan,
)
from commerce_lite_execution_adapter import align_lite_execution_metadata  # noqa: E402
from commerce_planner_lite import build_commerce_lite_tags, plan_commerce_lite_strong_clip_llm  # noqa: E402
from content_policy import default_content_policy  # noqa: E402
from run_m2_story_consumption_validation import _planning_candidates  # noqa: E402
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source, _load_frozen_m1_strategy  # noqa: E402
from semantic_word_binder import bind_candidates_by_semantic_srt, build_semantic_srt_word_timeline  # noqa: E402
from story_planner import audit_story_consumption  # noqa: E402


CASE_ID = "jccc_deep_roast_hoodie"
BASELINES = {
    "pain_point": ROOT / "workspace" / "p02_final_utterance_quality_caramel" / "pain_point" / "m3_new_golden_plan_fidelity_20260827_201643.json",
    "scene_immersion": ROOT / "workspace" / "p02_final_utterance_quality_caramel" / "scene_immersion" / "m3_new_golden_plan_fidelity_20260828_012943.json",
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read_json(path: Path) -> dict[str, Any]:
    return _mapping(json.loads(path.read_text(encoding="utf-8")))


def _response(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"content": content}}]}


def _case_payload(report_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    report = _read_json(report_path)
    cases = _mapping(report.get("cases"))
    case = _mapping(cases.get(CASE_ID))
    if not case:
        raise ValueError(f"P0.2 基线缺少焦糖案例：{report_path}")
    return report, case


def _approved_p02_responses(*, baseline_path: Path, case: Mapping[str, Any]) -> dict[str, Any]:
    attempts = list(case.get("m2_attempts") or ())
    attempt = _mapping(attempts[0] if attempts else {})
    parent = baseline_path.parent
    names = {
        "ranking": str(attempt.get("strong_clip_ranking_response") or ""),
        "composition": str(attempt.get("purchase_cognition_response") or ""),
        "recall": str(attempt.get("purchase_journey_targeted_recall_response") or ""),
        "quality": str(attempt.get("purchase_journey_quality_response") or ""),
    }
    missing = [key for key, name in names.items() if not name or not (parent / name).is_file()]
    if missing:
        raise FileNotFoundError(f"P0.2 基线缺少可回放回执：{','.join(missing)}")
    quality = _read_json(parent / names["quality"])
    local = list(quality.get("local_quality_responses") or ())
    order = _mapping(quality.get("quality_order_response"))
    if not local or not order:
        raise ValueError("P0.2 Quality 回执不完整，不能以它伪造基线")
    return {
        "ranking": (parent / names["ranking"]).read_text(encoding="utf-8"),
        "composition": (parent / names["composition"]).read_text(encoding="utf-8"),
        "recall": (parent / names["recall"]).read_text(encoding="utf-8"),
        "local": [_mapping(item) for item in local],
        "order": order,
        "source_files": names,
    }


def _materializable_inputs(*, source_srt: str, strategy: Any) -> tuple[list[dict[str, Any]], tuple[Any, ...], dict[int, dict[str, Any]], dict[int, Any], Any, list[dict[str, Any]]]:
    """Rebuild the immutable safe executable pool and semantic word binding."""
    ledger_context = _asset_ledger_for_source(CASE_ID, source_srt=source_srt)
    assets = list(ledger_context["assets"])
    timeline = build_semantic_srt_word_timeline(source_srt)
    all_candidates = _planning_candidates(assets)
    all_words, _ = bind_candidates_by_semantic_srt(all_candidates, timeline)
    candidates = []
    executable_evidence: dict[int, dict[str, Any]] = {}
    for candidate in all_candidates:
        words = all_words.get(candidate.candidate_id)
        if words is None:
            executable_evidence[candidate.candidate_id] = {
                "materializable": False,
                "materialization_issue": "candidate_word_lineage_unbound",
                "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            }
            continue
        checked = assess_candidate_materializability(candidate, words)
        if isinstance(checked, SelectorBlocked):
            executable_evidence[candidate.candidate_id] = {
                "materializable": False,
                "materialization_issue": f"{checked.code}:{checked.detail}",
                "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            }
            continue
        candidates.append(candidate)
        executable_evidence[candidate.candidate_id] = {
            "materializable": True,
            "materialization_issue": "",
            "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
        }
    if not candidates:
        raise ValueError("焦糖没有可物化的 safe candidate，不能运行 P0.4")
    tags = build_commerce_lite_tags(
        strategy=strategy,
        safe_candidates=tuple(candidates),
        ledger_assets=assets,
        executable_evidence=executable_evidence,
    )
    return assets, tuple(candidates), executable_evidence, all_words, tags, list(ledger_context.get("source_context_units") or ())


def _replay_p02_then_live_enrichment(
    *, replay: Mapping[str, Any], api_key: str, base_url: str, model: str,
) -> tuple[Any, dict[str, int]]:
    """Return approved P0.2 decisions by stage; Packet/P0.3 fallback are live."""
    local_rows = list(replay["local"])
    local_index = 0
    calls: dict[str, int] = {}
    original_request = commerce_planner_lite._post_lite_request

    def request(*, stage: str, **kwargs: Any) -> dict[str, Any]:
        nonlocal local_index
        calls[stage] = calls.get(stage, 0) + 1
        if stage == commerce_planner_lite.COMMERCE_STRONG_CLIP_RANKING_STAGE:
            return _response(str(replay["ranking"]))
        if stage == commerce_planner_lite.COMMERCE_NARRATIVE_COMPOSITION_STAGE:
            return _response(str(replay["composition"]))
        if stage == commerce_planner_lite.COMMERCE_PURCHASE_JOURNEY_RECALL_STAGE:
            return _response(str(replay["recall"]))
        if stage == commerce_planner_lite.COMMERCE_PURCHASE_QUESTION_LOCAL_QUALITY_STAGE:
            if local_index >= len(local_rows):
                raise RuntimeError("当前 P0.2 Quality 要求额外重答；P0.4 回放不得伪造新的 P0.2 决策")
            row = local_rows[local_index]
            local_index += 1
            return _response(json.dumps(row, ensure_ascii=False))
        if stage == commerce_planner_lite.COMMERCE_PURCHASE_JOURNEY_QUALITY_STAGE:
            return _response(json.dumps(replay["order"], ensure_ascii=False))
        if stage in {
            commerce_planner_lite.COMMERCE_CHAPTER_PACKET_STAGE,
            commerce_planner_lite.COMMERCE_NARRATIVE_ENRICHMENT_STAGE,
        }:
            result = original_request(
                api_key=api_key,
                base_url=base_url,
                model=model,
                prompt=str(kwargs["prompt"]),
                stage=stage,
                max_tokens=int(kwargs.get("max_tokens") or 6200),
            )
            return result
        raise RuntimeError(f"P0.4 基线回放遇到未授权的新 M2 阶段：{stage}")

    return request, calls


def _run_archetype(*, archetype: str, output_root: Path) -> Path:
    baseline_path = BASELINES[archetype]
    _, case = _case_payload(baseline_path)
    strategy = _load_frozen_m1_strategy(str(baseline_path))
    source_srt = str(case.get("source_srt") or "")
    if not source_srt or not Path(source_srt).is_file():
        raise FileNotFoundError(f"P0.2 基线源字幕不可用：{source_srt}")
    director_contract = _mapping(case.get("director_strategy_contract"))
    if str(director_contract.get("narrative_archetype") or "") != archetype:
        raise ValueError(f"P0.2 基线 Archetype 不匹配：{archetype}")
    baseline_plan = _mapping(case.get("m2_plan"))
    baseline_assessment = _mapping(baseline_plan.get("duration_assessment"))
    if str(baseline_assessment.get("status") or "") != "natural_complete_below_target":
        raise ValueError("P0.4 只接收已自然完成且低于软目标的 P0.2 基线")
    quality_gate = _mapping(_mapping(baseline_assessment.get("commerce_purchase_journey_quality")).get("m3_render_gate"))
    if not bool(quality_gate.get("passed")):
        raise ValueError("P0.2 基线 Quality 未通过，禁止进入 P0.4")

    assets, candidates, evidence, _, tags, source_context_units = _materializable_inputs(source_srt=source_srt, strategy=strategy)
    replay = _approved_p02_responses(baseline_path=baseline_path, case=case)
    settings = _mapping(load_settings())
    if not str(settings.get("api_key") or ""):
        raise RuntimeError("未找到 AI API Key，无法进行一次真实 P0.4 Packet 探索")
    target_duration = float(baseline_plan.get("target_duration") or 60.0)
    selection_contract = _mapping(baseline_plan.get("selection_contract"))
    output_dir = output_root / archetype
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    packet_path = output_dir / f"{CASE_ID}_{stamp}.m2.chapter_packet.response.txt"
    enrichment_path = output_dir / f"{CASE_ID}_{stamp}.m2.narrative_enrichment.response.txt"
    replay_request, replay_calls = _replay_p02_then_live_enrichment(
        replay=replay,
        api_key=str(settings["api_key"]),
        base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
        model=str(settings.get("model") or "deepseek-v4-flash"),
    )
    # All P0.2 decisions are the approved recorded response.  The only live
    # decision is M2_chapter_packet_builder, plus an explicitly requested P0.3
    # fallback if Packet M2 declares one.
    with patch.object(commerce_planner_lite, "_post_lite_request", side_effect=replay_request):
        _, plan = plan_commerce_lite_strong_clip_llm(
            strategy=strategy,
            tags=tags,
            target_duration=target_duration,
            safe_candidates=candidates,
            selection_contract=selection_contract,
            executable_evidence=evidence,
            api_key=str(settings["api_key"]),
            base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
            model=str(settings.get("model") or "deepseek-v4-flash"),
            director_strategy_contract=director_contract,
            chapter_packet_response_hook=lambda value: packet_path.write_text(value, encoding="utf-8"),
            narrative_enrichment_response_hook=lambda value: enrichment_path.write_text(value, encoding="utf-8"),
            source_context_units=source_context_units,
        )

    plan, execution_adapter = align_lite_execution_metadata(
        plan=plan,
        strategy=strategy,
        safe_candidates=candidates,
        executable_evidence=evidence,
    )
    selected_words, word_rejections = bind_candidates_by_semantic_srt(plan.selected_candidates, build_semantic_srt_word_timeline(source_srt))
    selector = materialize_narrative_plan(plan, selected_words)
    fidelity = audit_materialization_fidelity(plan, selector, assets, require_word_boundaries=True)
    consumption = audit_story_consumption(plan, strategy, candidates)
    final_assessment = _mapping(plan.duration_assessment)
    final_quality = _mapping(_mapping(final_assessment.get("commerce_purchase_journey_quality")).get("m3_render_gate"))
    chapter_packets = _mapping(final_assessment.get("commerce_chapter_packet_builder"))
    enrichment = _mapping(final_assessment.get("commerce_narrative_enrichment"))
    result = {
        "version": "p0_4_chapter_packet_caramel_baseline_replay_v1",
        "mode": "approved_p0_2_replay_then_one_live_m2_chapter_packet_then_optional_p0_3_fallback_then_unchanged_m3",
        "case_id": CASE_ID,
        "archetype": archetype,
        "source_srt": source_srt,
        "baseline_report": str(baseline_path),
        "baseline_actual_seconds": float(baseline_plan.get("total_seconds") or 0.0),
        "baseline_candidate_ids": [
            candidate_id for beat in list(baseline_plan.get("beats") or ())
            for candidate_id in list(_mapping(beat).get("candidate_ids") or ())
        ],
        "baseline_p0_2_quality_gate": quality_gate,
        "p0_2_replay_source_files": replay["source_files"],
        "m2_stage_call_counts": replay_calls,
        "live_chapter_packet_response": packet_path.name if packet_path.is_file() else "empty_or_invalid_response",
        "live_enrichment_response": enrichment_path.name if enrichment_path.is_file() else "not_triggered",
        "m2_plan": plan.to_dict(),
        "chapter_packet_builder": chapter_packets,
        "enrichment": enrichment,
        "execution_metadata_alignment": execution_adapter.to_dict(),
        "m2_story_consumption_audit": consumption,
        "m3_materialization": selector.to_dict(),
        "m3_fidelity_audit": fidelity,
        "m3_word_rejections": list(word_rejections),
        "passed": bool(plan.plan_valid and final_quality.get("passed") and consumption.get("passed") and fidelity.get("passed") and not word_rejections),
        "p0_4_status": str(chapter_packets.get("status") or "not_triggered"),
        "p0_3_status": str(enrichment.get("status") or "not_triggered"),
    }
    report_path = output_dir / f"p0_4_chapter_packet_{archetype}_{stamp}.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one live P0.4 packet decision against each approved caramel P0.2 cut.")
    parser.add_argument("--archetype", choices=("pain_point", "scene_immersion", "both"), default="both")
    parser.add_argument("--output-dir", default=str(ROOT / "workspace" / "p04_chapter_packet_caramel" / "approved_p02_replay"))
    args = parser.parse_args()
    archetypes = ("pain_point", "scene_immersion") if args.archetype == "both" else (args.archetype,)
    paths = [_run_archetype(archetype=item, output_root=Path(args.output_dir)) for item in archetypes]
    for path in paths:
        report = _read_json(path)
        print(json.dumps({
            "report": str(path),
            "archetype": report.get("archetype"),
            "p0_4_status": report.get("p0_4_status"),
            "p0_3_status": report.get("p0_3_status"),
            "seconds": _mapping(report.get("m2_plan")).get("total_seconds"),
            "passed": report.get("passed"),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
