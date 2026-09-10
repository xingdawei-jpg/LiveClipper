# -*- coding: utf-8 -*-
"""Five-source heavy-Director versus Lite-composition experiment.

This runner deliberately freezes the previously produced heavy C route's M1
Hero for each same source.  It therefore measures only whether a compact
Ledger/M1 tag projection plus one ranking call can approach the heavy route's
commercial composition at materially lower marginal token cost.

No user preview, formal output, legacy selection, or publication path is
called.  Videos are rendered only into ``workspace`` for anonymous review.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import random
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_director_experiment import controlled_experiment_enabled  # noqa: E402
from commercial_analyzer import Strategy  # noqa: E402
from commerce_lite_execution_adapter import narrative_plan_from_mapping  # noqa: E402
from run_five_source_director_experiment import FIVE_SOURCE_GOLDENS, _validate_source  # noqa: E402
from run_m123_pre_shadow import run_pre_shadow_case  # noqa: E402


LITE_EXPERIMENT_VERSION = "five-source-commerce-lite-v1"
HEAVY_RUN_ID_DEFAULT = "20260823_five_source_v1"


def _write_json(path: Path, value: Mapping[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def _heavy_case_dir(case_id: str, heavy_run_id: str) -> Path:
    return ROOT / "workspace" / "five_source_director_experiment" / "C" / case_id / heavy_run_id


def _frozen_heavy_strategy(case_id: str, heavy_run_id: str) -> tuple[Strategy, Path]:
    path = _heavy_case_dir(case_id, heavy_run_id) / "m1_story_brief.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少同源重型 C 的冻结 M1 Brief：{path}")
    return Strategy.from_dict(_load_json(path), 1), path


def _existing_heavy_video(case_id: str, heavy_run_id: str) -> Path | None:
    path = _heavy_case_dir(case_id, heavy_run_id) / "render.mp4"
    return path if path.is_file() else None


def _lite_case_dir(case_id: str, lite_run_id: str) -> Path:
    return ROOT / "workspace" / "five_source_commerce_lite_experiment" / case_id / lite_run_id


def _known_tokens(case_dir: Path) -> int | None:
    path = case_dir / "cost_report.json"
    if not path.is_file():
        return None
    try:
        return int((_load_json(path).get("tokens") or {}).get("known_total_tokens"))
    except (TypeError, ValueError):
        return None


def _render_path(item: Mapping[str, Any]) -> Path | None:
    render = item.get("render") if isinstance(item.get("render"), Mapping) else {}
    path = Path(str(render.get("new_video") or ""))
    return path if path.is_file() else None


def _blind_packet(
    *,
    root: Path,
    run_id: str,
    lite_results: Mapping[str, Mapping[str, Any]],
    heavy_run_id: str,
) -> str:
    """Expose A/B/C video labels only; the mapping remains private."""
    staged: list[tuple[str, str, Path]] = []
    cases: list[dict[str, Any]] = []
    answer_cases: list[dict[str, Any]] = []
    for index, (case_id, spec) in enumerate(FIVE_SOURCE_GOLDENS.items(), 1):
        legacy = Path(spec["legacy_video"])
        heavy = _existing_heavy_video(case_id, heavy_run_id)
        lite = _render_path(lite_results.get(case_id, {}))
        if not legacy.is_file() or heavy is None or lite is None:
            return "unavailable_missing_same_source_A_heavy_or_lite_video"
        values = [("legacy", legacy), ("heavy_director", heavy), ("commerce_lite", lite)]
        seed = int(hashlib.sha256(f"{run_id}:{case_id}".encode("utf-8")).hexdigest()[:16], 16)
        random.Random(seed).shuffle(values)
        public_case_id = f"case_{index:02d}"
        public_variants: list[dict[str, str]] = []
        private_variants: list[dict[str, str]] = []
        for label, (variant, video) in zip(("A", "B", "C"), values):
            staged.append((public_case_id, label, video))
            public_variants.append({"label": label, "video": f"{public_case_id}/{label}.mp4"})
            private_variants.append({"label": label, "variant_id": variant, "source_video": str(video)})
        cases.append({"case_id": public_case_id, "variants": public_variants})
        answer_cases.append({"case_id": public_case_id, "source_case_id": case_id, "variants": private_variants})

    public_dir, private_dir = root / "blind_eval" / run_id / "public", root / "blind_eval" / run_id / "private"
    for case_id, label, video in staged:
        target = public_dir / case_id / f"{label}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, target)
    packet = {
        "version": "commerce-lite-video-blind-eval-v1",
        "mode": "anonymous_same_source_legacy_heavy_lite_human_review_only",
        "score_scale": {"min": 1, "max": 5},
        "criteria": [
            {"id": "first_second_interest", "label": "第一秒是否想继续看"},
            {"id": "purchase_reason_45s", "label": "约45秒内是否建立购买理由"},
            {"id": "editor_quality", "label": "是否像优秀剪辑师完成的商业片"},
        ],
        "cases": cases,
        "publication_allowed": False,
    }
    rating = {
        "packet_version": packet["version"],
        "scores": [
            {"case_id": item["case_id"], "variant_scores": [
                {"label": variant["label"], "first_second_interest": None,
                 "purchase_reason_45s": None, "editor_quality": None, "publish_choice": None, "note": ""}
                for variant in item["variants"]
            ]}
            for item in cases
        ],
    }
    _write_json(public_dir / "packet.json", packet)
    _write_json(public_dir / "rating_sheet.json", rating)
    _write_json(private_dir / "answer_key.json", {
        "version": packet["version"], "private": True, "cases": answer_cases,
    })
    return "ready_for_human_blind_eval"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run five same-source Commerce Planner Lite evidence.")
    parser.add_argument("--case", choices=tuple(FIVE_SOURCE_GOLDENS) + ("all",), default="all")
    parser.add_argument("--target-duration", type=float, default=45.0)
    parser.add_argument("--heavy-run-id", default=HEAVY_RUN_ID_DEFAULT)
    parser.add_argument(
        "--replay-lite-run-id", default="",
        help="Replay a saved Lite Plan through only the Execution Adapter; no model call is made.",
    )
    parser.add_argument("--out-dir", default="workspace/five_source_commerce_lite_experiment")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    from ai_clipper import load_settings  # noqa: E402
    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行 Commerce Planner Lite 实验。")
    if not controlled_experiment_enabled(settings):
        raise RuntimeError("实验需要已保存 ai_director_mode=experimental；不会在 legacy/production 模式运行。")

    run_id = str(args.run_id or "").strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (ROOT / args.out_dir).resolve()
    case_ids = tuple(FIVE_SOURCE_GOLDENS) if args.case == "all" else (args.case,)
    report: dict[str, Any] = {
        "version": LITE_EXPERIMENT_VERSION,
        "mode": (
            "source_only_replay_existing_lite_plan_through_execution_adapter_no_model_call_or_user_output"
            if str(args.replay_lite_run_id).strip()
            else "source_only_frozen_same_source_m1_then_single_commerce_lite_ranking_no_user_preview_or_formal_output"
        ),
        "run_id": run_id,
        "heavy_reference_run_id": str(args.heavy_run_id),
        "target_duration": float(args.target_duration),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "formal_paths_changed": False,
        "cases": {},
    }
    results: dict[str, Mapping[str, Any]] = {}
    for case_id in case_ids:
        spec = FIVE_SOURCE_GOLDENS[case_id]
        validation = _validate_source(spec)
        case_report: dict[str, Any] = {"source_validation": validation}
        report["cases"][case_id] = case_report
        if not validation["ready"]:
            case_report["status"] = "blocked_source_identity_or_word_lineage"
            print(f"[Commerce Lite] {case_id}: blocked before ranker call")
            continue
        try:
            strategy, brief_path = _frozen_heavy_strategy(case_id, str(args.heavy_run_id))
        except (OSError, ValueError, FileNotFoundError) as error:
            case_report["status"] = "blocked_frozen_m1_missing"
            case_report["error_type"] = type(error).__name__
            print(f"[Commerce Lite] {case_id}: missing frozen M1")
            continue
        replay_plan = None
        replay_cost = None
        replay_path = None
        if str(args.replay_lite_run_id).strip():
            replay_dir = _lite_case_dir(case_id, str(args.replay_lite_run_id).strip())
            replay_path = replay_dir / "m2_plan.json"
            if not replay_path.is_file():
                case_report["status"] = "blocked_saved_lite_plan_missing"
                print(f"[Commerce Lite] {case_id}: missing saved Lite Plan")
                continue
            replay_plan = narrative_plan_from_mapping(_load_json(replay_path), strategy=strategy)
            replay_cost = _known_tokens(replay_dir)
        item = run_pre_shadow_case(
            case_id,
            settings=settings,
            output_root=output_root,
            run_id=run_id,
            target_duration=float(args.target_duration),
            render=True,
            controlled_experiment=True,
            commerce_lite=True,
            m1_strategy_override=strategy,
            commerce_lite_replay_plan=replay_plan,
            source_definition=spec,
        )
        results[case_id] = item
        output_dir = Path(str(item["directory"]))
        heavy_dir = _heavy_case_dir(case_id, str(args.heavy_run_id))
        heavy_tokens, adapter_tokens = _known_tokens(heavy_dir), _known_tokens(output_dir)
        lite_tokens = replay_cost if replay_plan is not None else adapter_tokens
        case_report.update({
            "frozen_m1_brief": str(brief_path),
            "replayed_lite_plan": str(replay_path) if replay_path else "",
            "result": item,
            "heavy_known_tokens": heavy_tokens,
            "lite_ranking_known_tokens": lite_tokens,
            "execution_adapter_known_tokens": adapter_tokens,
            "lite_to_heavy_token_ratio": (
                round(lite_tokens / heavy_tokens, 4)
                if heavy_tokens and lite_tokens is not None else None
            ),
        })
        print(f"[Commerce Lite] {case_id}: {item['status']}")
    report["blind_eval"] = {
        "status": _blind_packet(
            root=output_root, run_id=run_id, lite_results=results, heavy_run_id=str(args.heavy_run_id),
        ) if set(case_ids) == set(FIVE_SOURCE_GOLDENS) else "pending_full_five_source_lite_run",
        "mapping_visibility": "private_answer_key_only",
    }
    ratios = [
        item.get("lite_to_heavy_token_ratio")
        for item in report["cases"].values()
        if isinstance(item.get("lite_to_heavy_token_ratio"), float)
    ]
    report["summary"] = {
        "source_ready": sum(1 for item in report["cases"].values() if item["source_validation"]["ready"]),
        "expected_sources": len(case_ids),
        "frozen_m1_reused": len(results),
        "average_lite_to_heavy_token_ratio": round(sum(ratios) / len(ratios), 4) if ratios else None,
        "commercial_quality_approved": False,
        "shadow_ready": False,
    }
    path = output_root / f"commerce_lite_experiment_{run_id}.json"
    _write_json(path, report)
    print(f"[Commerce Lite] report={path}")


if __name__ == "__main__":
    main()
