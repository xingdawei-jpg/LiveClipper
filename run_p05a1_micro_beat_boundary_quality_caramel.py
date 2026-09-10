# -*- coding: utf-8 -*-
"""P0.5A.1: turn frozen caramel Raw Beats into a publishable Beat inventory."""

from __future__ import annotations

import argparse
import datetime as dt
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
from micro_beat_inventory import (  # noqa: E402
    build_micro_beat_source_rows,
    reconstruct_frozen_micro_beat_candidates,
    refine_micro_beat_boundaries,
)
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source  # noqa: E402
from semantic_word_binder import build_semantic_srt_word_timeline  # noqa: E402


CASE_ID = "jccc_deep_roast_hoodie"
P05A_BASELINE = (
    ROOT / "workspace" / "p05_micro_beat_inventory_caramel"
    / "p0_5a_micro_beat_inventory_20260828_194956.json"
)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _read_baseline(path: Path) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if int(report.get("mined_before_p0_2_quality_count") or 0) != 96:
        raise RuntimeError("P0.5A.1 只接受冻结的 96 条 Raw Beat Candidates。")
    if not isinstance(report.get("raw_ai_responses"), list):
        raise RuntimeError("冻结 P0.5A 报告缺少原始 AI Inventory 回放。")
    return report


def _source_rows(baseline: Mapping[str, Any]) -> tuple[str, tuple[dict[str, Any], ...]]:
    source_srt = str(baseline.get("source_srt") or "")
    if not source_srt or not Path(source_srt).is_file():
        raise FileNotFoundError("未找到冻结 P0.5A 指向的完整焦糖 SRT。")
    ledger = _asset_ledger_for_source(CASE_ID, source_srt=source_srt)
    hard_safe_ids = [int(item.get("candidate_id") or 0) for item in ledger.get("assets") or ()]
    rows = build_micro_beat_source_rows(
        source_units=list(ledger.get("source_context_units") or ()),
        hard_safe_subtitle_ids=hard_safe_ids,
        word_timeline=build_semantic_srt_word_timeline(source_srt),
    )
    return source_srt, rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run frozen P0.5A caramel Micro-Beat Boundary Quality only."
    )
    parser.add_argument("--baseline", default=str(P05A_BASELINE))
    parser.add_argument("--output-dir", default=str(ROOT / "workspace" / "p05a1_micro_beat_boundary_quality_caramel"))
    args = parser.parse_args()
    baseline_path = Path(args.baseline)
    baseline = _read_baseline(baseline_path)
    source_srt, source_rows = _source_rows(baseline)
    raw_response_paths = [baseline_path.parent / str(name) for name in baseline["raw_ai_responses"]]
    if any(not path.is_file() for path in raw_response_paths):
        missing = next(str(path) for path in raw_response_paths if not path.is_file())
        raise FileNotFoundError(f"冻结 P0.5A 原始响应缺失：{missing}")
    reconstructed = reconstruct_frozen_micro_beat_candidates(
        raw_inventory_responses=[path.read_text(encoding="utf-8") for path in raw_response_paths],
        source_rows=source_rows,
    )
    if reconstructed.get("status") != "frozen_inventory_reconstructed":
        raise RuntimeError("冻结 Raw Beat Inventory 无法按原始 lineage 回放。")
    expected_count = int(baseline.get("mined_before_p0_2_quality_count") or 0)
    expected_seconds = round(float(baseline.get("mined_before_p0_2_quality_seconds") or 0.0), 3)
    if (
        int(reconstructed.get("raw_beat_count") or 0) != expected_count
        or round(float(reconstructed.get("raw_beat_seconds") or 0.0), 3) != expected_seconds
    ):
        raise RuntimeError("P0.5A.1 冻结回放与 96 条 Raw Beat 基线不一致，已停止。")
    settings = _mapping(load_settings())
    if not str(settings.get("api_key") or ""):
        raise RuntimeError("未找到 AI API Key，无法进行 P0.5A.1 Boundary Quality。")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    response_paths: list[Path] = []
    adjudication_response_paths: list[Path] = []

    def write_boundary_response(batch_id: str, content: str) -> None:
        path = output_dir / f"{CASE_ID}_{stamp}.{batch_id}.p0_5a1_boundary_quality.response.txt"
        path.write_text(content, encoding="utf-8")
        response_paths.append(path)

    def write_adjudication_response(batch_id: str, content: str) -> None:
        path = output_dir / f"{CASE_ID}_{stamp}.{batch_id}.p0_5a1_final_utterance_adjudication.response.txt"
        path.write_text(content, encoding="utf-8")
        adjudication_response_paths.append(path)

    result = refine_micro_beat_boundaries(
        frozen_inventory=list(reconstructed["beat_inventory"]),
        source_rows=source_rows,
        api_key=str(settings["api_key"]),
        base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
        model=str(settings.get("model") or "deepseek-v4-flash"),
        response_hook=write_boundary_response,
        adjudication_response_hook=write_adjudication_response,
    )
    report = {
        "version": "p0_5a1_micro_beat_boundary_quality_caramel_v1",
        "mode": "frozen_p0_5a_raw_beat_candidates_to_publishable_inventory_only_no_arc_no_composer_no_m3",
        "case_id": CASE_ID,
        "source_srt": source_srt,
        "frozen_p0_5a_baseline": str(baseline_path),
        "frozen_raw_beat_count": reconstructed["raw_beat_count"],
        "frozen_raw_beat_seconds": reconstructed["raw_beat_seconds"],
        "frozen_raw_beat_fingerprint": reconstructed["raw_beat_fingerprint"],
        "raw_ai_inventory_responses": [path.name for path in raw_response_paths],
        "boundary_quality_responses": [path.name for path in response_paths],
        "final_utterance_adjudication_responses": [path.name for path in adjudication_response_paths],
        **result,
    }
    report_path = output_dir / f"p0_5a1_micro_beat_boundary_quality_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    stats = _mapping(report.get("boundary_statistics"))
    print(json.dumps({
        "report": str(report_path),
        "status": report.get("status"),
        "raw_beats": report.get("raw_beats"),
        "raw_seconds": report.get("raw_seconds"),
        "final_beats": stats.get("final_beats"),
        "final_usable_seconds": stats.get("final_usable_seconds"),
        "word_trimmed_count": stats.get("word_trimmed_count"),
        "arc_assembly_performed": _mapping(report.get("contract")).get("arc_assembly_performed"),
        "m3_invoked": _mapping(report.get("contract")).get("m3_invoked"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
