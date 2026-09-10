# -*- coding: utf-8 -*-
"""Run the five-source A/B/C Commerce Director experiment in workspace only.

``A`` is a completed legacy smart-cut video that is copied only into a later
anonymous review packet.  ``B`` is the current M1 -> M2 -> M3 source runner;
``C`` adds the candidate-free M2.5 Commerce Director coverage contract.  This
file intentionally has no web route, preview task, or formal export call.
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
from run_m123_pre_shadow import run_pre_shadow_case  # noqa: E402
from run_m3_golden_source_identity import assess_source_identity  # noqa: E402
from semantic_word_binder import build_semantic_srt_word_timeline  # noqa: E402


EXPERIMENT_VERSION = "five-source-commerce-director-ab-c-v1"

# These product strings label the test case only.  They are never injected as
# style, audience, or selling-point facts; M1 must still ground stories in the
# hard-safe subtitles and read-only Commercial Asset Ledger.
FIVE_SOURCE_GOLDENS: dict[str, dict[str, str]] = {
    "jccc_caramel_rum": {
        "label": "JCCC 焦糖朗姆针织罩衫套装",
        "product": "焦糖朗姆针织罩衫套装",
        "srt": r"C:\工作\JCCC的穿搭影记(2.25春上新）\单品素材\JCCC影子【焦糖朗姆】宽松透气针织罩衫遮肉显瘦出片套装女JE6NZZ11\焦糖.srt",
        "legacy_video": r"C:\工作\output\xx\8.23\焦糖_20260823_152919.mp4",
    },
    "jccc_paris_trench": {
        "label": "JCCC 慢调巴黎短款风衣",
        "product": "慢调巴黎短款风衣",
        "srt": r"C:\工作\JCCC的穿搭影记(2.25春上新）\新建文件夹\新建文件夹\JCCC影子_慢调巴黎_米色100棉漏斗领廓形短款风衣外套JE6OSW10\JCCC影子_慢调巴黎_米色100棉漏斗领廓形短款风衣外套JE6OSW10_1_1.srt",
        "legacy_video": r"C:\工作\output\xx\8.23\JCCC影子_慢调巴黎_米色100棉漏斗领廓形短款风衣外套JE6OSW10_1_1_20260823_152011.mp4",
    },
    "shanjie_qingqiu": {
        "label": "珊姐纱韵清秋",
        "product": "纱韵清秋",
        "srt": r"C:\工作\珊姐\8月14日直播\纱韵-清秋（2）.srt",
        "legacy_video": r"C:\工作\output\xx\8.23\纱韵-清秋_2__20260823_153544.mp4",
    },
    "xiaoxian_utility_jacket": {
        "label": "小贤连帽工装夹克",
        "product": "连帽工装夹克",
        "srt": r"C:\工作\小贤\8-22单品\【AYOBE】小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套\【AYOBE】小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套 8.22.srt",
        "legacy_video": r"C:\工作\output\xx\8.23\AYOBE_小贤 8月22日1000新品 废土松弛风酷感连帽工装夹克外套 8_22_20260823_154119.mp4",
    },
    "xiaoxian_pleated_skirt": {
        "label": "小贤复古褶皱半裙",
        "product": "复古褶皱半裙",
        "srt": r"C:\工作\小贤\8-22单品\【AYOBE】小贤 8月22日1000新品 结构层次氛围感复古气质褶皱半裙\【AYOBE】小贤 8月22日1000新品 结构层次氛围感复古气质褶皱半裙 8.22.srt",
        "legacy_video": r"C:\工作\output\xx\8.23\AYOBE_小贤 8月22日1000新品 结构层次氛围感复古气质褶皱半裙 8_22_20260823_160226.mp4",
    },
}


def _write_json(path: Path, value: Mapping[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _validate_source(spec: Mapping[str, str]) -> dict[str, Any]:
    """Fail before any model call unless the same source has exact word lineage."""
    identity = assess_source_identity(spec["srt"])
    timeline = build_semantic_srt_word_timeline(spec["srt"])
    binding = timeline.report()
    ready = bool(
        identity.get("source_identity_verified")
        and binding.get("coverage") == 1.0
        and binding.get("ambiguous") == 0
        and binding.get("unmatched") == 0
    )
    return {"ready": ready, "identity": identity, "semantic_word_binding": binding}


def _render_path(item: Mapping[str, Any]) -> Path | None:
    raw = ((item.get("render") or {}).get("new_video") if isinstance(item.get("render"), Mapping) else "")
    path = Path(str(raw or ""))
    return path if path.is_file() else None


def _make_video_blind_packet(
    *,
    output_root: Path,
    run_id: str,
    results: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> str:
    """Create public A/B/C videos and keep the randomized mapping private."""
    packet_cases: list[dict[str, Any]] = []
    answer_cases: list[dict[str, Any]] = []
    staged: list[tuple[str, str, Path]] = []
    for case_id, spec in FIVE_SOURCE_GOLDENS.items():
        legacy = Path(spec["legacy_video"])
        current = _render_path(results.get(case_id, {}).get("B", {}))
        director = _render_path(results.get(case_id, {}).get("C", {}))
        if not legacy.is_file() or current is None or director is None:
            return "unavailable_missing_same_source_video"
        variants = [("legacy", legacy), ("current_m2", current), ("commerce_director", director)]
        seed = int(hashlib.sha256(f"{run_id}:{case_id}".encode("utf-8")).hexdigest()[:16], 16)
        random.Random(seed).shuffle(variants)
        labels = ("A", "B", "C")
        public_variants: list[dict[str, str]] = []
        private_variants: list[dict[str, str]] = []
        for label, (variant_id, video) in zip(labels, variants):
            staged.append((case_id, label, video))
            public_variants.append({"label": label, "video": f"{case_id}/{label}.mp4"})
            private_variants.append({"label": label, "variant_id": variant_id, "source_video": str(video)})
        packet_cases.append({"case_id": case_id, "label": spec["label"], "variants": public_variants})
        answer_cases.append({"case_id": case_id, "variants": private_variants})

    root = output_root / "blind_eval" / run_id
    public_dir, private_dir = root / "public", root / "private"
    for case_id, label, source in staged:
        target = public_dir / case_id / f"{label}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    packet = {
        "version": "commerce-director-video-blind-eval-v1",
        "mode": "anonymous_same_source_A_B_C_human_review_only",
        "score_scale": {"min": 1, "max": 5},
        "criteria": [
            {"id": "first_second_interest", "label": "第一秒是否想继续看"},
            {"id": "purchase_reason_45s", "label": "约45秒内是否建立购买理由"},
            {"id": "editor_quality", "label": "是否像优秀剪辑师完成的商业片"},
        ],
        "cases": packet_cases,
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
            for item in packet_cases
        ],
    }
    answer_key = {"version": packet["version"], "private": True, "cases": answer_cases}
    _write_json(public_dir / "packet.json", packet)
    _write_json(public_dir / "rating_sheet.json", rating)
    _write_json(private_dir / "answer_key.json", answer_key)
    return "ready_for_human_blind_eval"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run isolated five-source legacy A / current B / director C experiment.")
    parser.add_argument("--case", choices=tuple(FIVE_SOURCE_GOLDENS) + ("all",), default="all")
    parser.add_argument("--variant", choices=("b", "c", "both"), default="both")
    parser.add_argument("--target-duration", type=float, default=45.0)
    parser.add_argument("--out-dir", default="workspace/five_source_director_experiment")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    from ai_clipper import load_settings  # noqa: E402
    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行五素材导演实验。")
    if not controlled_experiment_enabled(settings):
        raise RuntimeError("实验需要已保存 ai_director_mode=experimental；不会在 legacy/production 模式运行。")

    run_id = str(args.run_id or "").strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (ROOT / args.out_dir).resolve()
    case_ids = tuple(FIVE_SOURCE_GOLDENS) if args.case == "all" else (args.case,)
    variants = ("B", "C") if args.variant == "both" else (args.variant.upper(),)
    report: dict[str, Any] = {
        "version": EXPERIMENT_VERSION,
        "mode": "source_only_same_source_A_legacy_vs_B_current_m2_vs_C_m2_5_commerce_director_no_user_preview_or_formal_output",
        "run_id": run_id,
        "target_duration": float(args.target_duration),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "formal_paths_changed": False,
        "cases": {},
    }
    for case_id in case_ids:
        spec = FIVE_SOURCE_GOLDENS[case_id]
        validation = _validate_source(spec)
        case_report: dict[str, Any] = {"source_validation": validation, "variants": {}}
        report["cases"][case_id] = case_report
        if not validation["ready"]:
            case_report["status"] = "blocked_source_identity_or_word_lineage"
            print(f"[Five Source] {case_id}: blocked before model call")
            continue
        for variant in variants:
            item = run_pre_shadow_case(
                case_id,
                settings=settings,
                output_root=output_root / variant,
                run_id=run_id,
                target_duration=float(args.target_duration),
                render=True,
                controlled_experiment=True,
                commerce_director=(variant == "C"),
                source_definition=spec,
            )
            case_report["variants"][variant] = item
            print(f"[Five Source] {case_id} {variant}: {item['status']}")
    result_index = {
        case_id: dict(value.get("variants") or {})
        for case_id, value in report["cases"].items()
        if isinstance(value, Mapping)
    }
    report["blind_eval"] = {
        "status": _make_video_blind_packet(output_root=output_root, run_id=run_id, results=result_index)
        if set(case_ids) == set(FIVE_SOURCE_GOLDENS) and set(variants) == {"B", "C"}
        else "pending_full_five_source_B_C_run",
        "legacy_task_id": "smart-cut-1787469611-b2ead8",
        "mapping_visibility": "private_answer_key_only",
    }
    report["summary"] = {
        "source_ready": sum(1 for item in report["cases"].values() if item["source_validation"]["ready"]),
        "expected_sources": len(case_ids),
        "variant_runs": sum(len(item.get("variants") or {}) for item in report["cases"].values()),
        "commercial_quality_approved": False,
        "shadow_ready": False,
    }
    path = output_root / f"five_source_director_experiment_{run_id}.json"
    _write_json(path, report)
    print(f"[Five Source] report={path}")


if __name__ == "__main__":
    main()
