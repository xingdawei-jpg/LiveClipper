# -*- coding: utf-8 -*-
"""P0.5A: mine a source-wide Micro-Beat Inventory from the caramel SRT only."""

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
from micro_beat_inventory import mine_micro_beat_inventory  # noqa: E402
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source  # noqa: E402
from semantic_word_binder import build_semantic_srt_word_timeline  # noqa: E402


CASE_ID = "jccc_deep_roast_hoodie"
P02_BASELINE = ROOT / "workspace" / "p02_final_utterance_quality_caramel" / "pain_point" / "m3_new_golden_plan_fidelity_20260827_201643.json"


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _source_srt() -> str:
    report = json.loads(P02_BASELINE.read_text(encoding="utf-8"))
    case = _mapping(_mapping(report.get("cases")).get(CASE_ID))
    source_srt = str(case.get("source_srt") or "")
    if not source_srt or not Path(source_srt).is_file():
        raise FileNotFoundError("未找到焦糖完整 SRT，P0.5A 不允许从旧 Candidate 回退。")
    return source_srt


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one P0.5A source-wide caramel Micro-Beat Inventory pass.")
    parser.add_argument("--output-dir", default=str(ROOT / "workspace" / "p05_micro_beat_inventory_caramel"))
    args = parser.parse_args()
    source_srt = _source_srt()
    ledger = _asset_ledger_for_source(CASE_ID, source_srt=source_srt)
    settings = _mapping(load_settings())
    if not str(settings.get("api_key") or ""):
        raise RuntimeError("未找到 AI API Key，无法运行 P0.5A 完整 SRT Beat Inventory。")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    response_paths: list[Path] = []
    quality_response_paths: list[Path] = []

    def write_inventory_response(batch_id: str, content: str) -> None:
        path = output_dir / f"{CASE_ID}_{stamp}.{batch_id}.p0_5_micro_beat_inventory.response.txt"
        path.write_text(content, encoding="utf-8")
        response_paths.append(path)

    def write_quality_response(batch_id: str, content: str) -> None:
        path = output_dir / f"{CASE_ID}_{stamp}.{batch_id}.p0_5_micro_beat_quality.response.txt"
        path.write_text(content, encoding="utf-8")
        quality_response_paths.append(path)
    hard_safe_ids = [int(item.get("candidate_id") or 0) for item in ledger.get("assets") or ()]
    result = mine_micro_beat_inventory(
        source_units=list(ledger.get("source_context_units") or ()),
        hard_safe_subtitle_ids=hard_safe_ids,
        word_timeline=build_semantic_srt_word_timeline(source_srt),
        api_key=str(settings["api_key"]),
        base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
        model=str(settings.get("model") or "deepseek-v4-flash"),
        response_hook=write_inventory_response,
        quality_response_hook=write_quality_response,
    )
    report = {
        "version": "p0_5a_micro_beat_inventory_caramel_v1",
        "mode": "complete_srt_to_one_ai_micro_beat_inventory_only_no_arc_no_composer_no_m3",
        "case_id": CASE_ID,
        "source_srt": source_srt,
        "source_ledger_hard_safe_candidate_count": len(hard_safe_ids),
        "raw_ai_responses": [path.name for path in response_paths],
        "p0_2_quality_responses": [path.name for path in quality_response_paths],
        **result,
    }
    report_path = output_dir / f"p0_5a_micro_beat_inventory_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report": str(report_path),
        "status": report.get("status"),
        "total_srt_duration_seconds": report.get("total_srt_duration_seconds"),
        "total_micro_beats": report.get("total_micro_beats"),
        "total_usable_beat_seconds": report.get("total_usable_beat_seconds"),
        "m3_invoked": _mapping(report.get("contract")).get("m3_invoked"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
