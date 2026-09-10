# -*- coding: utf-8 -*-
"""Pre-Shadow M1 -> M2 -> M3 runner, isolated from every user-facing route.

It deliberately reuses the real source SRT, managed ASR cache, formal frozen
candidate builder and word-timing assets.  It does not import an M1/M2/M3 plan
into ``ai_clipper.py``, alter old selection, create a preview task, or publish
anything.  Rendering is opt-in diagnostic evidence only; a rendered B clip is
never a commercial-quality approval.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
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
from ai_director_experiment import (  # noqa: E402
    M2_PLANNER_MODE_HEAVY_DIRECTOR,
    M2_PLANNER_MODE_LEGACY,
    M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT,
    controlled_experiment_enabled,
    controlled_planner_mode,
    experimental_output_policy,
    planner_mode_version,
)
from run_m3_golden_source_identity import M3_GOLDEN_SOURCES, assess_source_identity  # noqa: E402
from run_m3_new_golden_plan_fidelity import _run_case  # noqa: E402
from run_m3_selector_render import render_source_ranges, write_output_timeline_srt  # noqa: E402
from story_blind_eval import EvaluationVariant, build_blind_packet, empty_rating_sheet  # noqa: E402


PRE_SHADOW_VERSION = "m123-pre-shadow-source-runner-v1"
PLANNER_MODE_MANIFEST_VERSION = "m123-planner-mode-manifest-v1"


def _planner_mode_flags(planner_mode: str) -> tuple[bool, bool]:
    """Map a persisted controlled planner choice to the existing M2 variants."""
    mode = str(planner_mode or "").strip()
    if mode == M2_PLANNER_MODE_LEGACY:
        return False, False
    if mode == M2_PLANNER_MODE_HEAVY_DIRECTOR:
        return True, False
    if mode == M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT:
        return False, True
    raise ValueError(f"unsupported controlled planner mode: {mode}")


def _write_planner_manifest(
    case_dir: Path,
    *,
    run_id: str,
    planner_mode: str,
    controlled_experiment: bool,
    source_info: Mapping[str, Any],
) -> Path:
    """Persist planner provenance without changing a candidate or render route."""
    manifest = {
        "version": PLANNER_MODE_MANIFEST_VERSION,
        "run_id": run_id,
        "planner_mode": planner_mode,
        "planner_version": planner_mode_version(planner_mode),
        "controlled_experiment": bool(controlled_experiment),
        "source": {
            "case_id": str(source_info.get("case_id") or ""),
            "source_srt": str(source_info.get("source_srt") or ""),
            "source_video": str(source_info.get("source_video") or ""),
        },
        "scope": "m2_composition_only",
        "unchanged_components": ["M1", "hard_safe_candidates", "candidate_ledger", "semantic_binder", "M3"],
        "opening_policy": "human_review_required_non_blocking_for_workspace_diagnostic_only",
        "user_preview_allowed": False,
        "formal_export_allowed": False,
        "publication_allowed": False,
    }
    path = case_dir / "run_manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _pid_is_running(value: Any) -> bool:
    try:
        pid = int(value)
        if pid <= 0:
            return False
        os.kill(pid, 0)
    except (TypeError, ValueError, OSError):
        return False
    return True


@contextmanager
def _case_run_lock(case_dir: Path):
    """Forbid concurrent runs of the same case/run id; recover only dead locks."""
    path = case_dir / ".active.lock"
    payload = {"pid": os.getpid(), "created_at": dt.datetime.now().isoformat(timespec="seconds")}
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            previous = _load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            previous = {}
        if _pid_is_running(previous.get("pid")):
            raise RuntimeError(f"同一 Pre-Shadow 案例仍在运行：pid={previous.get('pid')}")
        path.unlink(missing_ok=True)
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        yield
    finally:
        path.unlink(missing_ok=True)


def _case_summary_from_status(case_id: str, case_dir: Path) -> dict[str, Any] | None:
    path = case_dir / "status.json"
    if not path.is_file():
        return None
    try:
        status = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    render = status.get("render") if isinstance(status.get("render"), Mapping) else {}
    return {
        "case_id": case_id,
        "directory": str(case_dir),
        "status": str(status.get("status") or "blocked"),
        "reasons": list(status.get("reasons") or ()),
        "render": dict(render),
    }


def write_run_summary(
    output_root: Path,
    run_id: str,
    *,
    target_duration: float,
    render_requested: bool,
    controlled_experiment: bool = False,
    planner_mode: str = M2_PLANNER_MODE_LEGACY,
) -> Path:
    """Rebuild a complete run index from durable per-case status files only."""
    cases: list[dict[str, Any]] = []
    for case_id in M3_GOLDEN_SOURCES:
        item = _case_summary_from_status(case_id, output_root / case_id / run_id)
        if item is not None:
            cases.append(item)
    summary = {
        "version": PRE_SHADOW_VERSION,
        "mode": (
            "source_identical_m123_controlled_experiment_no_user_preview_or_formal_output"
            if controlled_experiment else "source_identical_m123_pre_shadow_no_user_preview_or_output"
        ),
        "formal_paths_changed": False,
        "controlled_experiment": bool(controlled_experiment),
        "planner_mode": planner_mode,
        "planner_version": planner_mode_version(planner_mode),
        "run_id": run_id,
        "target_duration": target_duration,
        "render_requested": bool(render_requested),
        "cases": cases,
        "all_ready_for_shadow": False,
    }
    path = output_root / f"pre_shadow_run_{run_id}.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _legacy_ranges(log_path: Path, source_video: Path) -> tuple[list[dict[str, Any]], str]:
    """Read an existing old-path run; never regenerate or alter the legacy cut."""
    if not log_path.is_file():
        return [], "legacy_run_log_missing"
    try:
        payload = _load_json(log_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return [], "legacy_run_log_unreadable"
    logged_source = Path(str(payload.get("视频") or "")).resolve()
    if logged_source != source_video.resolve():
        return [], "legacy_run_log_source_mismatch"
    audit = payload.get("选片审计")
    clips = audit.get("final_clips") if isinstance(audit, Mapping) else None
    if not isinstance(clips, list):
        return [], "legacy_run_log_missing_final_clips"
    ranges: list[dict[str, Any]] = []
    for order, clip in enumerate(clips, 1):
        if not isinstance(clip, Mapping):
            return [], "legacy_run_log_invalid_clip"
        try:
            start, end = float(clip.get("start")), float(clip.get("end"))
        except (TypeError, ValueError):
            return [], "legacy_run_log_invalid_range"
        text = str(clip.get("text") or "").strip()
        if not text or end <= start:
            return [], "legacy_run_log_invalid_clip"
        ranges.append({"order": order, "start": start, "end": end, "text": text})
    return ranges, "available"


def _quality_review(
    case: Mapping[str, Any],
    *,
    legacy_status: str,
    controlled_experiment: bool = False,
) -> dict[str, Any]:
    plan = dict(case.get("m2_plan") or {})
    selector = dict(case.get("m3_selection_result") or {})
    fidelity = dict(case.get("m3_plan_fidelity_audit") or {})
    consumption = dict(case.get("m2_story_consumption_audit") or {})
    binder = dict(case.get("semantic_binder") or {})
    technical_ok = bool(
        plan.get("plan_valid")
        and selector.get("status") == "ok"
        and fidelity.get("passed")
        and consumption.get("passed")
        and binder.get("coverage") == 1.0
        and not binder.get("ambiguous")
        and not binder.get("unmatched")
    )
    reasons: list[str] = []
    if not technical_ok:
        reasons.append("m123_technical_contract_failed")
    duration = dict(plan.get("duration_assessment") or {})
    duration_status = str(duration.get("status") or plan.get("status") or "unreported")
    if duration_status in {"insufficient_for_target", "insufficient_material"}:
        reasons.append("insufficient_for_target")
    # LLM Opening Review is evidence, never a commercial-quality approval.
    reasons.append("opening_quality_human_review_required")
    if legacy_status != "available":
        reasons.append("legacy_comparison_unavailable")
    experimental_policy = experimental_output_policy(technical_ok=technical_ok) if controlled_experiment else {}
    return {
        "technical_status": "passed" if technical_ok else "blocked",
        "m1_story_discovery": "reported" if case.get("selected_m1_hero") else "blocked",
        "m2_story_consumption": "passed" if consumption.get("passed") else "blocked",
        "m3_plan_fidelity": "passed" if fidelity.get("passed") else "blocked",
        "duration_status": duration_status,
        "story_quality": "experimental" if controlled_experiment else "unreviewed",
        "opening_quality": "human_review_required",
        "opening_quality_detail": {
            "automatic_review": (case.get("m2_plan") or {}).get("opening_quality_review"),
            "human_status": "pending",
            "automatic_review_is_not_approval": True,
        },
        "legacy_comparison_status": legacy_status,
        "commercial_quality_pending": True,
        "commercial_ready": False,
        "diagnostic_render_allowed": technical_ok,
        "experimental_output_policy": experimental_policy,
        "status": "ready_for_human_review" if controlled_experiment and technical_ok else "blocked",
        "reasons": reasons,
    }


def _write_blind_packet(
    *,
    case_id: str,
    case_dir: Path,
    legacy_ranges: list[Mapping[str, Any]],
    selector_ranges: list[Mapping[str, Any]],
    legacy_video: Path | None,
    new_video: Path | None,
    run_id: str,
) -> str:
    if not legacy_ranges or not selector_ranges or not legacy_video or not new_video:
        return "blind_eval_unavailable"
    try:
        packet, answer_key = build_blind_packet({case_id: (
            EvaluationVariant.from_mapping({"variant_id": f"legacy:{case_id}", "clips": legacy_ranges}),
            EvaluationVariant.from_mapping({"variant_id": f"m123:{case_id}", "clips": selector_ranges}),
        )}, seed=run_id)
    except ValueError:
        return "blind_eval_unavailable"
    public_dir = case_dir / "blind_eval_packet"
    private_dir = case_dir / "blind_eval_private"
    public_dir.mkdir(parents=True, exist_ok=True)
    private_dir.mkdir(parents=True, exist_ok=True)
    labels = {
        str(item.get("label") or ""): str(item.get("variant_id") or "")
        for item in ((answer_key.get("cases") or [{}])[0].get("variants") or ())
        if isinstance(item, Mapping)
    }
    for label, variant_id in labels.items():
        source = legacy_video if variant_id == f"legacy:{case_id}" else new_video
        shutil.copy2(source, public_dir / f"{label}.mp4")
    (public_dir / "packet.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
    (public_dir / "rating_sheet.json").write_text(
        json.dumps(empty_rating_sheet(packet), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (private_dir / "answer_key.json").write_text(json.dumps(answer_key, ensure_ascii=False, indent=2), encoding="utf-8")
    return "ready_for_human_blind_eval"


def _render(case_dir: Path, source_video: Path, ranges: list[Mapping[str, Any]], label: str) -> Path:
    video = case_dir / f"{label}.mp4"
    subtitles = case_dir / f"{label}.srt"
    render_source_ranges(source_video, ranges, video)
    write_output_timeline_srt(ranges, subtitles)
    return video


def _evaluation_ranges(ranges: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Adapt M3 lineage names to the blind evaluator without losing provenance."""
    return [
        {
            "candidate_id": item.get("candidate_id", item.get("parent_candidate_id")),
            "start": item.get("start"),
            "end": item.get("end"),
            "text": item.get("text"),
        }
        for item in ranges
        if isinstance(item, Mapping)
    ]


def run_pre_shadow_case(
    case_id: str,
    *,
    settings: Mapping[str, Any],
    output_root: Path,
    run_id: str,
    target_duration: float,
    render: bool,
    legacy_run_log: Path | None = None,
    controlled_experiment: bool = False,
    planner_mode: str = M2_PLANNER_MODE_LEGACY,
    commerce_director: bool = False,
    commerce_lite: bool = False,
    m1_strategy_override: Any | None = None,
    commerce_lite_replay_plan: Any | None = None,
    source_definition: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one source-identical M1/M2/M3 experiment and always emit status."""
    case_dir = output_root / case_id / run_id
    case_dir.mkdir(parents=True, exist_ok=True)
    source = dict(source_definition or M3_GOLDEN_SOURCES[case_id])
    source_srt = Path(str(source["srt"])).resolve()
    identity = assess_source_identity(source_srt)
    source_video = Path(str(identity.get("source_video") or ""))
    source_info = {"case_id": case_id, "label": str(source.get("label") or case_id), **identity}
    (case_dir / "source_info.json").write_text(json.dumps(source_info, ensure_ascii=False, indent=2), encoding="utf-8")
    # Preserve the existing source-only callers that passed the implementation
    # flags before Planner Mode existed.  A non-legacy explicit mode, however,
    # must still agree with the requested implementation.
    if commerce_director and commerce_lite:
        raise ValueError("heavy and Lite planner flags cannot both be enabled")
    inferred_mode = (
        M2_PLANNER_MODE_HEAVY_DIRECTOR if commerce_director
        else M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT if commerce_lite
        else M2_PLANNER_MODE_LEGACY
    )
    if planner_mode == M2_PLANNER_MODE_LEGACY:
        planner_mode = inferred_mode
    elif planner_mode != inferred_mode:
        raise ValueError("planner mode and planner implementation flags disagree")
    _write_planner_manifest(
        case_dir,
        run_id=run_id,
        planner_mode=planner_mode,
        controlled_experiment=controlled_experiment,
        source_info=source_info,
    )
    task_id = f"pre_shadow:{case_id}:{run_id}"
    legacy, legacy_status = _legacy_ranges(legacy_run_log, source_video) if legacy_run_log else ([], "legacy_run_log_not_supplied")
    try:
        with _case_run_lock(case_dir):
            with ai_cost_ledger_scope(task_id=task_id, session_id=run_id):
                case = _run_case(
                    case_id,
                    settings=settings,
                    output_dir=case_dir,
                    target_duration=target_duration,
                    strategy_id="",
                    m2_replan_attempts=1,
                    # In controlled output experiments, Opening is a human-review
                    # risk flag rather than another automatic replan loop.
                    opening_quality_review=not controlled_experiment,
                    commerce_director=commerce_director,
                    commerce_lite=commerce_lite,
                    m1_strategy_override=m1_strategy_override,
                    commerce_lite_replay_plan=commerce_lite_replay_plan,
                    source_definition=source,
                )
    except Exception as error:
        report, _ = generate_ai_cost_reports(session_id=run_id, task_id=task_id)
        (case_dir / "cost_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        status = {
            "status": "blocked", "reasons": ["pipeline_exception"], "error_type": type(error).__name__,
            "planner_mode": planner_mode, "planner_version": planner_mode_version(planner_mode),
        }
        (case_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"case_id": case_id, "directory": str(case_dir), **status}

    (case_dir / "m1_story_brief.json").write_text(
        json.dumps(case.get("selected_m1_hero") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (case_dir / "m2_plan.json").write_text(
        json.dumps(case.get("m2_plan") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # Keep the deterministic M1->M2 consumption verdict beside the plan.  This
    # is audit evidence only: it neither changes the plan nor attempts a local
    # correction when the director selected an invalid commercial theme.
    (case_dir / "m2_story_consumption_audit.json").write_text(
        json.dumps(case.get("m2_story_consumption_audit") or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (case_dir / "m3_selector.json").write_text(
        json.dumps({
            "selector_result": case.get("m3_selection_result"),
            "plan_fidelity": case.get("m3_plan_fidelity_audit"),
            "chapter_lineage": case.get("chapter_lineage"),
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    commerce = dict(case.get("commerce_director") or {})
    if commerce.get("enabled"):
        (case_dir / "commerce_story_plan.json").write_text(
            json.dumps(commerce.get("commerce_story_plan") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (case_dir / "commerce_coverage_audit.json").write_text(
            json.dumps(commerce.get("coverage_audit") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (case_dir / "commerce_evidence_cards.json").write_text(
            json.dumps(commerce.get("evidence_cards") or [], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    commerce_lite_result = dict(case.get("commerce_lite") or {})
    if commerce_lite_result.get("enabled"):
        (case_dir / "commerce_lite_tags.json").write_text(
            json.dumps(commerce_lite_result.get("tags") or [], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (case_dir / "commerce_lite_summary.json").write_text(
            json.dumps({
                key: value for key, value in commerce_lite_result.items() if key != "tags"
            }, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (case_dir / "commerce_lite_raw_plan.json").write_text(
            json.dumps(commerce_lite_result.get("raw_plan") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (case_dir / "commerce_lite_execution_adapter.json").write_text(
            json.dumps(commerce_lite_result.get("execution_adapter") or {}, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if legacy:
        (case_dir / "legacy_selection.json").write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    review = _quality_review(
        case,
        legacy_status=legacy_status,
        controlled_experiment=controlled_experiment,
    )
    (case_dir / "quality_review.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
    report, _ = generate_ai_cost_reports(session_id=run_id, task_id=task_id)
    (case_dir / "cost_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    rendered: dict[str, Any] = {"requested": render, "new_video": "", "legacy_video": "", "blind_eval_status": "not_requested"}
    selector_ranges = list((case.get("m3_selection_result") or {}).get("ranges") or ())
    if render and review["diagnostic_render_allowed"] and source_video.is_file():
        new_video = _render(case_dir, source_video, selector_ranges, "render")
        rendered["new_video"] = str(new_video)
        legacy_video: Path | None = None
        if legacy:
            legacy_video = _render(case_dir, source_video, legacy, "legacy_render")
            rendered["legacy_video"] = str(legacy_video)
        rendered["blind_eval_status"] = _write_blind_packet(
            case_id=case_id, case_dir=case_dir, legacy_ranges=legacy,
            selector_ranges=_evaluation_ranges(selector_ranges),
            legacy_video=legacy_video, new_video=new_video, run_id=run_id,
        )
    elif render:
        rendered["blind_eval_status"] = "render_blocked_by_technical_contract"
    (case_dir / "status.json").write_text(
        json.dumps({
            **review,
            "planner_mode": planner_mode,
            "planner_version": planner_mode_version(planner_mode),
            "render": rendered,
        }, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {
        "case_id": case_id,
        "directory": str(case_dir),
        "status": review["status"],
        "reasons": review["reasons"],
        "planner_mode": planner_mode,
        "planner_version": planner_mode_version(planner_mode),
        "render": rendered,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated M1->M2->M3 Pre-Shadow evidence.")
    parser.add_argument("--case", choices=tuple(M3_GOLDEN_SOURCES) + ("all",), default="all")
    parser.add_argument("--target-duration", type=float, default=45.0)
    parser.add_argument("--render", action="store_true", help="Render diagnostic workspace clips only.")
    parser.add_argument(
        "--controlled-experiment", action="store_true",
        help="Require persisted ai_director_mode=experimental; writes B only to workspace for human review.",
    )
    parser.add_argument("--out-dir", default="workspace/pre_shadow")
    parser.add_argument("--run-id", default="", help="Resume the same isolated experiment id after an interruption.")
    parser.add_argument("--summarize-only", action="store_true", help="Rebuild a run index from status files; never calls AI.")
    parser.add_argument(
        "--legacy-run-log", action="append", default=[], metavar="CASE=PATH",
        help="Optional existing old-path run log for the same source; never regenerates legacy output.",
    )
    args = parser.parse_args()
    legacy_logs: dict[str, Path] = {}
    for raw in args.legacy_run_log:
        case_id, separator, value = str(raw).partition("=")
        if not separator or case_id not in M3_GOLDEN_SOURCES or not value.strip():
            raise ValueError("--legacy-run-log 必须是 CASE=PATH，CASE 必须是当前素材 ID。")
        legacy_logs[case_id] = Path(value.strip()).resolve()
    run_id = str(args.run_id or "").strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (ROOT / args.out_dir).resolve()
    if args.summarize_only:
        path = write_run_summary(
            output_root, run_id, target_duration=args.target_duration, render_requested=bool(args.render),
            controlled_experiment=bool(args.controlled_experiment),
        )
        print(f"[Pre-Shadow] summary={path}")
        return
    from ai_clipper import load_settings  # noqa: E402

    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行真实 M1→M2→M3 Pre-Shadow。")
    if args.controlled_experiment and not controlled_experiment_enabled(settings):
        raise RuntimeError(
            "Controlled experiment requires persisted ai_director_mode=experimental; "
            "legacy and production modes are intentionally refused."
        )
    planner_mode = controlled_planner_mode(settings)
    if planner_mode != M2_PLANNER_MODE_LEGACY and not args.controlled_experiment:
        raise RuntimeError("Heavy/Lite Planner 仅能通过 --controlled-experiment 在隔离工作区运行。")
    commerce_director, commerce_lite = _planner_mode_flags(planner_mode)
    case_ids = tuple(M3_GOLDEN_SOURCES) if args.case == "all" else (args.case,)
    for case_id in case_ids:
        item = run_pre_shadow_case(
            case_id, settings=settings, output_root=output_root, run_id=run_id,
            target_duration=args.target_duration,
            render=bool(args.render or args.controlled_experiment),
            legacy_run_log=legacy_logs.get(case_id),
            controlled_experiment=bool(args.controlled_experiment),
            planner_mode=planner_mode,
            commerce_director=commerce_director,
            commerce_lite=commerce_lite,
        )
        print(f"[Pre-Shadow] {case_id}: {item['status']} ({', '.join(item['reasons'])})")
    report_path = write_run_summary(
        output_root, run_id, target_duration=args.target_duration,
        render_requested=bool(args.render or args.controlled_experiment),
        controlled_experiment=bool(args.controlled_experiment),
        planner_mode=planner_mode,
    )
    print(f"[Pre-Shadow] report={report_path}")


if __name__ == "__main__":
    main()
