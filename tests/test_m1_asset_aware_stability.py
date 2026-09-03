# -*- coding: utf-8 -*-
"""Deterministic tests for M1 stability assessment; no model calls."""

from __future__ import annotations

import json
import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commercial_analyzer import parse_strategy_result
from run_m1_asset_aware_stability import assess_stability_run, summarize_direction_coverage, summarize_stability


def _result(*, priority: str = "high", extra_high: bool = False):
    strategies = [{
        "strategy_id": "S1",
        "story_priority": priority,
        "story_premise": "样衣太短会露，大货加长两公分。",
        "audience_tension": "短裙太短担心走光。",
        "core_commercial_idea": "加长后安全放心穿。",
        "payoff": "日常也能放心穿。",
        "core_evidence_pool": [
            {"role": "problem", "subtitle_ids": [1], "claim": "样衣太短会露"},
            {"role": "mechanism", "subtitle_ids": [2], "claim": "大货加长两公分"},
        ],
    }]
    if extra_high:
        strategies.append({
            "strategy_id": "S2",
            "story_priority": "high",
            "thesis": "价格优势",
            "audience_tension": "价格高",
            "transformation": "更划算",
            "core_evidence_pool": [
                {"role": "proof", "subtitle_ids": [3], "claim": "价格划算"},
                {"role": "result", "subtitle_ids": [4], "claim": "值得买"},
            ],
        })
    return parse_strategy_result(
        json.dumps({"strategies": strategies}, ensure_ascii=False),
        subtitles=[
            {"id": 1, "start": 0, "end": 2, "text": "样衣太短会露"},
            {"id": 2, "start": 2, "end": 4, "text": "大货加长两公分"},
            {"id": 3, "start": 4, "end": 6, "text": "价格划算"},
            {"id": 4, "start": 6, "end": 8, "text": "值得买"},
        ],
    )


CONTRACT = {
    "problem": (("短",), ("露", "走光")),
    "solution": (("加长", "两公分", "2cm"),),
    "outcome": (("安全", "放心", "敢穿"),),
}


class M1StabilityAssessmentTests(unittest.TestCase):
    def test_expected_high_hero_is_stable_and_clean(self) -> None:
        assessment = assess_stability_run(result=_result(), commercial_change=CONTRACT)
        self.assertTrue(assessment["has_valid_story"])
        self.assertEqual(assessment["high_priority_hero_ids"], ["S1"])
        self.assertEqual(assessment["high_priority_drift_ids"], [])
        self.assertTrue(assessment["asset_boundary_clean"])

    def test_unrelated_high_story_is_reported_as_drift(self) -> None:
        assessment = assess_stability_run(result=_result(extra_high=True), commercial_change=CONTRACT)
        self.assertEqual(assessment["high_priority_drift_ids"], ["S2"])

    def test_summary_requires_all_valid_clean_and_no_drift(self) -> None:
        clean = assess_stability_run(result=_result(), commercial_change=CONTRACT)
        failed = dict(clean, high_priority_drift_ids=["S2"])
        summary = summarize_stability({"a": [clean] * 12, "b": [failed] * 3})
        self.assertEqual(summary["high_priority_expected_hero_runs"], 15)
        self.assertEqual(summary["high_priority_drift_count"], 3)
        self.assertFalse(summary["m2_source_only_prerequisite_passed"])

    def test_direction_coverage_reports_medium_and_high_separately(self) -> None:
        run = assess_stability_run(result=_result(priority="medium"), commercial_change={"safe": CONTRACT})
        coverage = summarize_direction_coverage({"hanxi": [run]})
        self.assertEqual(coverage["hanxi"]["safe"]["any_match_runs"], 1)
        self.assertEqual(coverage["hanxi"]["safe"]["high_match_runs"], 0)


if __name__ == "__main__":
    unittest.main()
