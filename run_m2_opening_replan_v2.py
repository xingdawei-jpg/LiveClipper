# -*- coding: utf-8 -*-
"""Run only M2 Opening Replan v2 against an already frozen source experiment.

This runner deliberately reuses the M1 brief and initial M2 plan emitted by
Pre-Shadow.  It rebuilds the same read-only ledger/candidate/word-lineage
facts, sends one small C1/C2-only M2 request, then asks the unchanged M3
selector whether the proposed opening can be materialized.  It never calls a
user preview or render path, and it never re-runs M1 or the full M2 director.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_cost_ledger import ai_cost_ledger_scope, generate_ai_cost_reports  # noqa: E402
from clip_selector import (  # noqa: E402
    SelectorBlocked,
    assess_candidate_materializability,
    audit_materialization_fidelity,
    materialize_narrative_plan,
)
from commercial_analyzer import Strategy  # noqa: E402
from run_m3_golden_source_identity import M3_GOLDEN_SOURCES, assess_source_identity  # noqa: E402
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source, _chapter_trace  # noqa: E402
from semantic_word_binder import bind_candidates_by_semantic_srt, build_semantic_srt_word_timeline  # noqa: E402
from story_planner import (  # noqa: E402
    CommercialStoryBrief,
    NarrativePlan,
    PlanningCandidate,
    _parse_beats,
    _parse_depth_expansion,
    _parse_duration_assessment,
    _parse_duration_plan,
    _parse_opening_package,
    _parse_story_consumption,
    audit_story_consumption,
    build_executable_evidence_view,
    review_opening_quality_llm,
    validate_narrative_plan,
)
from run_m2_story_consumption_validation import _planning_candidates  # noqa: E402


VERSION = "m2-opening-replan-v2-source-runner-v1"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object required: {path}")
    return payload


def _pid_is_running(value: Any) -> bool:
    try:
        pid = int(value)
        return pid > 0 and os.kill(pid, 0) is None
    except (TypeError, ValueError, OSError):
        return False


@contextmanager
def _case_run_lock(case_dir: Path):
    """Prevent accidental duplicate paid Opening calls for one case/run id."""
    path = case_dir / ".active.lock"
    payload = {"pid": os.getpid(), "created_at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        previous = {}
        try:
            previous = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        if _pid_is_running(previous.get("pid")):
            raise RuntimeError(f"同一 Opening Replan v2 案例仍在运行：pid={previous.get('pid')}")
        path.unlink(missing_ok=True)
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        yield
    finally:
        path.unlink(missing_ok=True)


def _plan_from_payload(raw: Mapping[str, Any], strategy: Strategy) -> NarrativePlan:
    """Recover an auditable frozen plan; no local chapter repair is performed."""
    target = float(raw.get("target_duration") or 45.0)
    return NarrativePlan(
        strategy_id=str(raw.get("strategy_id") or strategy.strategy_id),
        thesis=str(raw.get("thesis") or strategy.thesis),
        target_duration=target,
        beats=_parse_beats(raw),
        status=str(raw.get("status") or "insufficient_material"),
        recommended_duration=float(raw.get("recommended_duration") or 0.0),
        issues=tuple(str(item) for item in (raw.get("issues") or ()) if str(item)),
        removed_beats=(),
        plan_valid=bool(raw.get("plan_valid")),
        story_brief=CommercialStoryBrief.from_strategy(strategy),
        opening_package=_parse_opening_package(raw.get("opening_package")),
        selection_contract=dict(raw.get("selection_contract") or {}),
        selected_candidates=tuple(
            PlanningCandidate.from_mapping(item)
            for item in (raw.get("selected_candidates") or ())
            if isinstance(item, Mapping)
        ),
        duration_assessment=_parse_duration_assessment(raw.get("duration_assessment")),
        duration_plan=_parse_duration_plan(raw.get("duration_plan"), target_duration=target),
        depth_expansion=_parse_depth_expansion(raw.get("depth_expansion"), target_duration=target),
        # The duration scout belongs to the original whole-story run.  Opening
        # Replan v2 must not revive or invoke it, because C3+ and duration
        # policy are frozen for this localized experiment.
        duration_expansion_scout=None,
        story_consumption=_parse_story_consumption(raw.get("story_consumption")),
    )


def _rebuild_executable_context(case_id: str) -> tuple[tuple[PlanningCandidate, ...], dict[int, dict[str, Any]], Any, list[dict[str, Any]]]:
    """Recreate frozen source facts without invoking M1/M2 or changing the ledger."""
    ledger_context = _asset_ledger_for_source(case_id)
    all_candidates = _planning_candidates(list(ledger_context["assets"]))
    timeline = build_semantic_srt_word_timeline(ledger_context["source_srt"])
    bound, unbound = bind_candidates_by_semantic_srt(all_candidates, timeline)
    candidates: list[PlanningCandidate] = []
    facts: dict[int, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    for candidate in all_candidates:
        if candidate.candidate_id not in bound:
            detail = "candidate_word_lineage_unbound"
            facts[candidate.candidate_id] = {
                "materializable": False, "materialization_issue": detail,
                "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            }
            blocked.append({"candidate_id": candidate.candidate_id, "code": detail})
            continue
        checked = assess_candidate_materializability(candidate, bound[candidate.candidate_id])
        if isinstance(checked, SelectorBlocked):
            payload = checked.to_dict()
            facts[candidate.candidate_id] = {
                "materializable": False,
                "materialization_issue": f"{payload['code']}:{payload['detail']}",
                "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
            }
            blocked.append(payload)
            continue
        candidates.append(candidate)
        facts[candidate.candidate_id] = {
            "materializable": True, "materialization_issue": "",
            "origin_subtitle_ids": list(candidate.origin_subtitle_ids),
        }
    if not candidates:
        raise ValueError(f"{case_id}: no materializable candidates")
    return tuple(candidates), facts, timeline, [{"binder_rejections": list(unbound), "materializability_blocked": blocked}]


def _opening_text(plan: NarrativePlan, candidates: tuple[PlanningCandidate, ...]) -> dict[str, Any]:
    by_id = {candidate.candidate_id: candidate for candidate in candidates}
    opening = plan.opening_package
    hook_ids = tuple(opening.hook_candidate_ids) if opening else ()
    payoff_ids = tuple(opening.payoff_candidate_ids) if opening else ()
    def render(ids: tuple[int, ...]) -> dict[str, Any]:
        items = [by_id[item] for item in ids if item in by_id]
        return {
            "candidate_ids": list(ids),
            "text": "".join(item.text for item in items),
            "duration_seconds": round(sum(item.duration for item in items), 3),
        }
    hook, payoff = render(hook_ids), render(payoff_ids)
    return {
        "hook": hook,
        "payoff": payoff,
        "opening_unit_seconds": round(hook["duration_seconds"] + payoff["duration_seconds"], 3),
    }


def run_case(
    case_id: str,
    *,
    input_root: Path,
    source_run_id: str,
    output_root: Path,
    run_id: str,
    settings: Mapping[str, Any],
) -> dict[str, Any]:
    input_dir = input_root / case_id / source_run_id
    plan_path = input_dir / "m2_plan.json"
    brief_path = input_dir / "m1_story_brief.json"
    if not plan_path.is_file() or not brief_path.is_file():
        raise FileNotFoundError(f"缺少冻结输入：{plan_path} / {brief_path}")
    case_dir = output_root / case_id / run_id
    case_dir.mkdir(parents=True, exist_ok=True)
    task_id = f"opening_replan_v2:{case_id}:{run_id}"
    with _case_run_lock(case_dir):
        original_payload = _load_json(plan_path)
        strategy = Strategy.from_dict(_load_json(brief_path), 1)
        original = _plan_from_payload(original_payload, strategy)
        candidates, materialization_facts, timeline, preflight = _rebuild_executable_context(case_id)
        before = _opening_text(original, candidates)
        raw_path = case_dir / "m2_opening_replan_v2.response.txt"
        with ai_cost_ledger_scope(task_id=task_id, session_id=run_id):
            replanned = review_opening_quality_llm(
                strategy=strategy,
                plan=original,
                safe_candidates=candidates,
                executable_evidence=materialization_facts,
                api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
                raw_response_hook=lambda text: raw_path.write_text(text, encoding="utf-8"),
            )
        after = _opening_text(replanned, candidates)
        consumption = audit_story_consumption(replanned, strategy, candidates)
        selected_words, word_rejections = bind_candidates_by_semantic_srt(replanned.selected_candidates, timeline)
        selector = materialize_narrative_plan(replanned, selected_words)
        fidelity = audit_materialization_fidelity(replanned, selector, list(_asset_ledger_for_source(case_id)["assets"]), require_word_boundaries=True)
        technical_ok = bool(replanned.plan_valid and consumption["passed"] and selector.status == "ok" and fidelity["passed"] and not word_rejections)
        report, _ = generate_ai_cost_reports(session_id=run_id, task_id=task_id)
        review = replanned.opening_quality_review.to_dict() if replanned.opening_quality_review else {}
        artifact = {
            "version": VERSION,
            "mode": "source_only_opening_replan_no_user_preview_or_output",
            "formal_paths_changed": False,
            "case_id": case_id,
            "source_run": {"run_id": source_run_id, "directory": str(input_dir)},
            "original_opening": before,
            "replanned_opening": after,
            "director_decision": review,
            "opening_gate": {
                "technical_materialization": "passed" if technical_ok else "blocked",
                "commercial_quality": "human_review_required",
                "automatic_director_verdict_is_not_approval": True,
            },
            "m3_plan_fidelity": fidelity,
            "m2_story_consumption": consumption,
            "candidate_preflight": preflight,
            "word_binding_rejections": list(word_rejections),
            "chapter_lineage": _chapter_trace(replanned, selector, timeline),
        }
        (case_dir / "opening_replan_v2.json").write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
        (case_dir / "m2_plan.json").write_text(json.dumps(replanned.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        (case_dir / "m3_selector.json").write_text(json.dumps({"selector_result": selector.to_dict(), "plan_fidelity": fidelity}, ensure_ascii=False, indent=2), encoding="utf-8")
        (case_dir / "cost_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        status = {
            "status": "human_review_required" if technical_ok else "blocked",
            "technical_status": "passed" if technical_ok else "blocked",
            "reasons": ["opening_quality_human_review_required"] if technical_ok else ["opening_replan_or_m3_contract_failed"],
            "source_only": True,
            "rendered": False,
            "user_preview_changed": False,
        }
        (case_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"case_id": case_id, "directory": str(case_dir), **status}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one small source-only M2 Opening Replan v2 per frozen case.")
    parser.add_argument("--case", choices=tuple(M3_GOLDEN_SOURCES) + ("all",), default="all")
    parser.add_argument("--source-run-id", default="20260821_205135")
    parser.add_argument("--input-root", default="workspace/pre_shadow")
    parser.add_argument("--out-dir", default="workspace/m2_opening_replan_v2")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    from ai_clipper import load_settings  # noqa: E402
    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行真实 M2 Opening Replan v2。")
    run_id = str(args.run_id or "").strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    case_ids = tuple(M3_GOLDEN_SOURCES) if args.case == "all" else (args.case,)
    output_root = (ROOT / args.out_dir).resolve()
    results = [
        run_case(
            case_id, input_root=(ROOT / args.input_root).resolve(), source_run_id=args.source_run_id,
            output_root=output_root, run_id=run_id, settings=settings,
        )
        for case_id in case_ids
    ]
    summary = {
        "version": VERSION,
        "mode": "source_only_opening_replan_no_user_preview_or_output",
        "formal_paths_changed": False,
        "source_run_id": args.source_run_id,
        "run_id": run_id,
        "cases": results,
    }
    path = output_root / f"opening_replan_v2_run_{run_id}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Opening Replan v2] summary={path}")


if __name__ == "__main__":
    main()
