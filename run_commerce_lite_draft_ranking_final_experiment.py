"""Offline-only Commerce Lite Draft -> Ranking -> Final experiment."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for item in (str(ROOT), str(APP)):
    if item not in sys.path:
        sys.path.insert(0, item)

from run_m3_new_golden_plan_fidelity import _run_case  # noqa: E402


DEFAULT_VIDEO = r"C:\工作\JCCC的穿搭影记(2.25春上新）\单品素材\JCCC影子【焦糖朗姆】宽松透气针织罩衫遮肉显瘦出片套装女JE6NZZ11\焦糖.mp4"
DEFAULT_SRT = r"C:\工作\JCCC的穿搭影记(2.25春上新）\单品素材\JCCC影子【焦糖朗姆】宽松透气针织罩衫遮肉显瘦出片套装女JE6NZZ11\焦糖.srt"
DEFAULT_PRODUCT = "焦糖朗姆宽松透气针织罩衫遮肉显瘦出片套装"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", default=DEFAULT_VIDEO)
    parser.add_argument("--srt", default=DEFAULT_SRT)
    parser.add_argument("--product", default=DEFAULT_PRODUCT)
    parser.add_argument("--target-duration", type=float, default=60.0)
    parser.add_argument("--out-dir", default="workspace/commerce_lite_draft_ranking_final_experiment")
    args = parser.parse_args()

    from ai_clipper import load_settings  # noqa: E402
    from ai_cost_ledger import ai_cost_ledger_scope, generate_ai_cost_reports  # noqa: E402

    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行真实 Draft → Ranking → Final 实验。")
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    case_id = "caramel_lite_draft_ranking_final"
    session_id = f"draft-ranking-final-{dt.datetime.now():%Y%m%d%H%M%S}"
    with ai_cost_ledger_scope(task_id=f"commerce_lite_draft_ranking_final:{case_id}", session_id=session_id):
        result = _run_case(
            case_id,
            settings=settings,
            output_dir=output_dir,
            target_duration=args.target_duration,
            strategy_id="",
            m2_replan_attempts=0,
            opening_quality_review=False,
            commerce_lite_draft_rank_final=True,
            source_definition={
                "label": "焦糖朗姆 Draft→Ranking→Final",
                "product": args.product,
                "video": args.video,
                "srt": args.srt,
            },
        )
    cost_report, _ = generate_ai_cost_reports(output_dir=output_dir, session_id=session_id)
    ranking = (result.get("commerce_lite") or {}).get("commercial_ranking") or {}
    report = {
        "version": "commerce-lite-draft-ranking-final-experiment-v1",
        "mode": "offline_source_only_no_preview_no_render_no_export",
        "formal_paths_changed": False,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "case": result,
        "cost_report": cost_report,
        "summary": {
            "draft_step_count": len((result.get("commerce_lite") or {}).get("draft", {}).get("buying_path", [])),
            "retained_value_count": len(ranking.get("retained_values", [])),
            "final_plan_valid": bool((result.get("m2_plan") or {}).get("plan_valid")),
            "m3_fidelity_passed": bool((result.get("m3_plan_fidelity_audit") or {}).get("passed")),
            "rendered_video": False,
        },
    }
    path = output_dir / f"commerce_lite_draft_ranking_final_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Commerce Lite Draft Ranking Final] report={path}")
    print(json.dumps(report["summary"], ensure_ascii=False))
    raise SystemExit(0 if report["summary"]["final_plan_valid"] else 2)


if __name__ == "__main__":
    main()
