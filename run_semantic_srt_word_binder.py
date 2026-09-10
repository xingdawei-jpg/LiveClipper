# -*- coding: utf-8 -*-
"""Audit the selected M3 golden sources without invoking M1, M2 or M3."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from run_m3_golden_source_identity import M3_GOLDEN_SOURCES  # noqa: E402
from semantic_word_binder import build_semantic_srt_word_timeline  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(M3_GOLDEN_SOURCES) + ("all",), default="all")
    parser.add_argument("--out-dir", default="workspace/semantic_srt_word_binder")
    args = parser.parse_args()
    case_ids = tuple(M3_GOLDEN_SOURCES) if args.case == "all" else (args.case,)
    report: dict[str, Any] = {
        "version": "semantic-srt-word-binder-v1",
        "mode": "source_only_no_m1_m2_m3_plan_or_render",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": {},
    }
    for case_id in case_ids:
        item = M3_GOLDEN_SOURCES[case_id]
        timeline = build_semantic_srt_word_timeline(item["srt"])
        audit = timeline.report()
        passed = bool(
            audit["coverage"] == 1.0
            and audit["ambiguous"] == 0
            and audit["unmatched"] == 0
            and not audit["validation_issues"]
        )
        report["cases"][case_id] = {"label": item["label"], "binder": audit, "passed": passed}
        print(
            f"[Semantic Binder] {case_id}: coverage={audit['coverage']:.3f} "
            f"exact={audit['exact_aligned']} normalized={audit['normalized_aligned']} "
            f"ambiguous={audit['ambiguous']} unmatched={audit['unmatched']} passed={passed}"
        )
    passed_count = sum(bool(item["passed"]) for item in report["cases"].values())
    report["summary"] = {
        "passed": passed_count,
        "expected": len(case_ids),
        "all_passed": passed_count == len(case_ids),
        "contract": "coverage=100%, ambiguous=0, unmatched=0, validation_issues=0",
    }
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"semantic_srt_word_binder_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Semantic Binder] {passed_count}/{len(case_ids)} report={output_path}")
    raise SystemExit(0 if report["summary"]["all_passed"] else 2)


if __name__ == "__main__":
    main()
