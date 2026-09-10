# -*- coding: utf-8 -*-
"""Run the source-only M2.5 Commerce Director experiment (C path).

This is deliberately a workspace experiment: it requires the persisted
``experimental`` mode, never invokes an AI preview/export route, and emits a
commercial-coverage audit next to the actual M1/M2/M3 lineage.  It does not
claim a legacy A or current-M2 B exists unless a same-source artifact is later
bound for blind review.
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

from ai_director_experiment import controlled_experiment_enabled  # noqa: E402
from run_m123_pre_shadow import run_pre_shadow_case  # noqa: E402
from run_m3_golden_source_identity import M3_GOLDEN_SOURCES  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Run M2.5 Commerce Director source-only experiment.")
    parser.add_argument("--case", choices=tuple(M3_GOLDEN_SOURCES) + ("all",), default="all")
    parser.add_argument("--target-duration", type=float, default=45.0)
    parser.add_argument("--out-dir", default="workspace/commerce_director_experiment")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    from ai_clipper import load_settings  # noqa: E402
    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行 Commerce Director 实验。")
    if not controlled_experiment_enabled(settings):
        raise RuntimeError("Commerce Director 实验需要已保存 ai_director_mode=experimental；不会在 legacy/production 模式运行。")

    run_id = str(args.run_id or "").strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (ROOT / args.out_dir).resolve()
    case_ids = tuple(M3_GOLDEN_SOURCES) if args.case == "all" else (args.case,)
    report = {
        "version": "commerce-director-experiment-v1",
        "mode": "source_identical_m1_m2_5_m2_m3_controlled_experiment_no_user_preview_or_formal_output",
        "target_duration": float(args.target_duration),
        "run_id": run_id,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": [],
        "blind_eval": {
            "status": "pending_same_source_A_B_C_binding",
            "note": "C is rendered only for human review. A/B are never inferred from unrelated outputs.",
        },
    }
    for case_id in case_ids:
        item = run_pre_shadow_case(
            case_id,
            settings=settings,
            output_root=output_root,
            run_id=run_id,
            target_duration=float(args.target_duration),
            render=True,
            controlled_experiment=True,
            commerce_director=True,
        )
        case_dir = Path(str(item["directory"]))
        coverage_path = case_dir / "commerce_coverage_audit.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8")) if coverage_path.is_file() else {}
        report["cases"].append({
            **item,
            "coverage_status": coverage.get("status", "not_generated"),
            "coverage_missing_materialization": coverage.get("missing_materialization_beat_ids", []),
        })
        print(f"[Commerce Director] {case_id}: {item['status']} coverage={coverage.get('status', 'not_generated')}")
    report["summary"] = {
        "technical_runs": len(report["cases"]),
        "rendered_for_human_review": sum(1 for item in report["cases"] if item.get("render", {}).get("new_video")),
        "all_commercial_ready": False,
        "shadow_ready": False,
        "reason": "Director Experiment v1 is evidence collection; A/B/C blind evaluation is still pending.",
    }
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"commerce_director_experiment_{run_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Commerce Director] report={path}")


if __name__ == "__main__":
    main()
