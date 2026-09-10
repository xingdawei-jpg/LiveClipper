# -*- coding: utf-8 -*-
"""P0.5A.4 Hook Recall + Opening Package Quality focused caramel regression.

Only the Hook source scan is new.  P0.5A.3 remains a read-only payoff pool;
Director Strategy, Journey/Casting implementation, Whole Video Audit and M3
are not changed.  When a package is legal, the runner makes an ephemeral
opening-only overlay and executes the existing Narrative Mode routine without
materializing or rendering M3.
"""

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

from ai_clipper import load_settings  # noqa: E402
from commerce_planner_lite import plan_commerce_lite_narrative_mode_llm  # noqa: E402
from hook_opening_recall import (  # noqa: E402
    assemble_opening_packages,
    hook_candidate_as_opening_overlay,
    recall_hooks_from_complete_source,
)
from micro_beat_inventory import build_micro_beat_source_rows, prepare_narrative_mode_beat_execution  # noqa: E402
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source, _load_frozen_m1_strategy  # noqa: E402
from semantic_word_binder import build_semantic_srt_word_timeline  # noqa: E402


def _text(value: Any) -> str:
    return str(value or "").strip()


def _rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _build_narrative_mode_beat_candidates(
    *, inventory: Mapping[str, Any], timeline: Any,
) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """Reuse the existing P0.5A.3 adapter without mutating its JSON file."""
    execution = prepare_narrative_mode_beat_execution(
        publishable_inventory=inventory, word_timeline=timeline,
    )
    candidate_by_id = {candidate.candidate_id: candidate for candidate in execution["candidates"]}
    raw_by_beat_id = {
        _text(item.get("beat_id")): dict(item)
        for item in inventory.get("publishable_beat_inventory") or () if isinstance(item, Mapping)
    }
    candidates: list[dict[str, Any]] = []
    for mapping in execution.get("beat_candidate_map") or ():
        candidate_id = int(mapping.get("candidate_id") or 0)
        candidate = candidate_by_id.get(candidate_id)
        if candidate is None:
            continue
        raw = raw_by_beat_id.get(_text(mapping.get("beat_id")), {})
        candidates.append({
            **raw, **dict(mapping), "candidate_id": candidate_id,
            "duration_seconds": candidate.duration, "hook_eligible": candidate.hook_eligible,
            "planning_candidate": candidate,
        })
    return execution, tuple(candidates)


def _run_readonly_narrative_regression(
    *, package: Mapping[str, Any], hook_by_id: Mapping[str, Mapping[str, Any]], actor_pool: Mapping[str, Any],
    timeline: Any, strategy: Any, director_contract: Mapping[str, Any], settings: Mapping[str, Any],
    target_duration: float, output_dir: Path,
) -> dict[str, Any]:
    hook = hook_by_id[_text(package.get("hook_id"))]
    overlay = hook_candidate_as_opening_overlay(hook)
    inventory = dict(actor_pool)
    inventory["publishable_beat_inventory"] = _rows(actor_pool.get("publishable_beat_inventory")) + [overlay]
    execution, beat_candidates = _build_narrative_mode_beat_candidates(inventory=inventory, timeline=timeline)
    actor_hook_count = sum(
        bool(item.get("hook_eligible"))
        for item in _rows(actor_pool.get("publishable_beat_inventory"))
    )
    overlay_mapping = next((item for item in execution.get("beat_candidate_map") or () if _text(item.get("beat_id")) == _text(overlay.get("beat_id"))), None)
    if actor_hook_count != 0 or overlay_mapping is None or not bool(overlay_mapping.get("hook_eligible")):
        raise RuntimeError("P0.5A.4 Opening overlay 合同失败：冻结 Actor Pool 被意外改变或 Hook 未进入开场权限")
    opening_id = _text(package.get("opening_id")) or _text(package.get("hook_id"))
    response_paths: dict[str, str] = {}

    def _write(kind: str, value: str) -> None:
        path = output_dir / f"{opening_id}.{kind}.response.txt"
        path.write_text(value, encoding="utf-8")
        response_paths[kind] = path.name

    audit, plan = plan_commerce_lite_narrative_mode_llm(
        strategy=strategy, beat_candidates=beat_candidates, target_duration=target_duration,
        safe_candidates=execution["candidates"],
        selection_contract={
            "p0_5a4_hook_opening_overlay_only": True,
            "p0_5a3_actor_pool_read_only": True,
            "m3_invoked": False,
        },
        api_key=_text(settings.get("api_key")),
        base_url=_text(settings.get("base_url")) or "https://api.deepseek.com",
        model=_text(settings.get("model")) or "deepseek-v4-flash",
        director_strategy_contract=director_contract,
        journey_response_hook=lambda value: _write("journey", value),
        casting_response_hook=lambda value: _write("casting", value),
        whole_video_audit_response_hook=lambda value: _write("whole_video_audit", value),
    )
    return {
        "opening_id": opening_id, "hook_id": hook.get("hook_id"),
        "frozen_actor_pool_hook_count": actor_hook_count,
        "opening_overlay_candidate_id": overlay_mapping.get("candidate_id"),
        "opening_package_receipt": dict(package),
        "narrative_mode_audit": audit, "plan": plan.to_dict(),
        "response_files": response_paths,
        "m3_modified": False, "m3_materialization_or_rendering_invoked": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="jccc_deep_roast_hoodie")
    parser.add_argument("--source-report", required=True, help="同源冻结报告；读取 source_srt、M1 与 Director 合同")
    parser.add_argument("--actor-pool", required=True, help="冻结 P0.5A.3 Actor Pool")
    parser.add_argument("--out-dir", default="workspace/p05a4_hook_recall_caramel")
    parser.add_argument("--target-duration", type=float, default=45.0)
    args = parser.parse_args()

    output_dir = (ROOT / args.out_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_report_path = Path(args.source_report).resolve()
    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    case = dict((source_report.get("cases") or {}).get(args.case) or {})
    source_srt = _text(case.get("source_srt"))
    if not source_srt:
        raise RuntimeError("冻结报告缺少同案例 source_srt")
    director_contract = dict(case.get("director_strategy_contract") or {})
    opening_promise = _text(director_contract.get("opening_promise"))
    if not director_contract or not opening_promise:
        raise RuntimeError("冻结报告缺少 Director Opening Contract")
    opening_scope = dict(director_contract.get("opening_scope") or {})
    allowed_opening_answer_roles = [
        _text(item).lower() for item in opening_scope.get("allowed_answer_roles") or () if _text(item)
    ]
    if not allowed_opening_answer_roles:
        raise RuntimeError("冻结 Director Opening Contract 缺少 allowed_answer_roles")
    actor_pool = json.loads(Path(args.actor_pool).resolve().read_text(encoding="utf-8"))
    if not _rows(actor_pool.get("publishable_beat_inventory")):
        raise RuntimeError("P0.5A.3 Actor Pool 为空")
    settings = load_settings()
    if not _text(settings.get("api_key")):
        raise RuntimeError("未找到 AI API Key，不能运行 P0.5A.4")

    ledger_context = _asset_ledger_for_source(args.case, source_srt=str(Path(source_srt).resolve()))
    timeline = build_semantic_srt_word_timeline(str(ledger_context["source_srt"]))
    source_rows = build_micro_beat_source_rows(
        source_units=ledger_context["source_context_units"],
        hard_safe_subtitle_ids=[int(item.get("candidate_id") or 0) for item in ledger_context["assets"]],
        word_timeline=timeline,
    )
    hook_response_paths: dict[str, str] = {}

    def _write_hook(batch_id: str, content: str) -> None:
        path = output_dir / f"p0_5a4.hook_recall.{batch_id}.response.txt"
        path.write_text(content, encoding="utf-8")
        hook_response_paths[batch_id] = path.name

    hooks = recall_hooks_from_complete_source(
        source_rows=source_rows, api_key=_text(settings.get("api_key")),
        base_url=_text(settings.get("base_url")) or "https://api.deepseek.com",
        model=_text(settings.get("model")) or "deepseek-v4-flash", opening_promise=opening_promise,
        allowed_opening_answer_roles=allowed_opening_answer_roles,
        response_hook=_write_hook,
    )
    result: dict[str, Any] = {
        "stage": "P0.5A.4 Hook Recall + Opening Package Quality",
        "source_srt": str(Path(source_srt).resolve()),
        "frozen_actor_pool": str(Path(args.actor_pool).resolve()),
        "hook_recall": hooks,
        "opening_packages": None,
        "narrative_mode_readonly_regressions": [],
        "contract": {
            "ordinary_actor_pool_modified": False, "director_journey_modified": False,
            "beat_casting_modified": False, "whole_video_audit_modified": False,
            "m3_modified": False, "strong_ranking_or_top12_used": False,
        },
    }
    if hooks.get("status") == "hook_recall_completed":
        package_path = output_dir / "p0_5a4.opening_packages.response.txt"
        packages = assemble_opening_packages(
            hook_recall=hooks, frozen_actor_pool=_rows(actor_pool.get("publishable_beat_inventory")),
            api_key=_text(settings.get("api_key")), base_url=_text(settings.get("base_url")) or "https://api.deepseek.com",
            model=_text(settings.get("model")) or "deepseek-v4-flash", opening_promise=opening_promise,
            response_hook=lambda content: package_path.write_text(content, encoding="utf-8"),
        )
        result["opening_packages"] = packages
        if packages.get("status") == "opening_packages_completed":
            strategy = _load_frozen_m1_strategy(str(source_report_path))
            hooks_by_id = {_text(item.get("hook_id")): item for item in hooks.get("hook_candidates") or () if isinstance(item, Mapping)}
            for package in packages.get("opening_packages") or ():
                if _text(package.get("quality")) not in {"strong", "medium"}:
                    continue
                result["narrative_mode_readonly_regressions"].append(_run_readonly_narrative_regression(
                    package=package, hook_by_id=hooks_by_id, actor_pool=actor_pool, timeline=timeline,
                    strategy=strategy, director_contract=director_contract, settings=settings,
                    target_duration=float(args.target_duration), output_dir=output_dir,
                ))
    result["hook_response_files"] = hook_response_paths
    output = output_dir / "p0_5a4_hook_opening_report.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {
        "report": str(output), "hook_status": hooks.get("status"),
        "hook_count": hooks.get("hook_candidate_count", 0),
        "package_status": (result.get("opening_packages") or {}).get("status"),
        "package_count": len((result.get("opening_packages") or {}).get("opening_packages") or ()),
        "readonly_regressions": len(result["narrative_mode_readonly_regressions"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
