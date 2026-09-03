# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
for item in (str(ROOT), str(APP)):
    if item not in sys.path:
        sys.path.insert(0, item)

from commercial_analyzer import Strategy  # noqa: E402
from run_m2_opening_replan_v2 import _opening_text, _plan_from_payload  # noqa: E402
from story_planner import PlanningCandidate  # noqa: E402


class OpeningReplanV2RunnerTests(unittest.TestCase):
    def test_frozen_plan_round_trip_preserves_opening_without_replanning_story(self) -> None:
        strategy = Strategy.from_dict({
            "strategy_id": "S1", "thesis": "单穿成立", "story_priority": "high",
            "core_evidence_pool": [{"role": "hook", "claim": "承诺", "subtitle_ids": [101]}],
            "supporting_evidence_pool": [{"role": "proof", "claim": "证明", "subtitle_ids": [102]}],
        }, 1)
        raw = {
            "strategy_id": "S1", "thesis": "单穿成立", "target_duration": 45,
            "status": "insufficient_material", "plan_valid": True,
            "opening_package": {
                "hook_promise": "承诺", "payoff_delivery": "证明", "connection_reason": "承接",
                "hook_candidate_ids": [101], "payoff_candidate_ids": [102],
            },
            "beats": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101], "required": True},
                {"chapter_id": "C2", "narrative_role": "development", "candidate_ids": [102], "required": True},
                {"chapter_id": "C3", "narrative_role": "proof", "candidate_ids": [103], "required": True},
            ],
            "selected_candidates": [
                {"candidate_id": 101, "start": 0, "end": 2, "text": "强承诺。", "origin_subtitle_ids": [101]},
                {"candidate_id": 102, "start": 2, "end": 4, "text": "立即证明。", "origin_subtitle_ids": [102]},
                {"candidate_id": 103, "start": 4, "end": 6, "text": "后续章节。", "origin_subtitle_ids": [103]},
            ],
        }
        plan = _plan_from_payload(raw, strategy)
        opening = _opening_text(plan, tuple(PlanningCandidate.from_mapping(item) for item in raw["selected_candidates"]))
        self.assertEqual([beat.chapter_id for beat in plan.beats], ["C1", "C2", "C3"])
        self.assertEqual(opening["hook"]["text"], "强承诺。")
        self.assertEqual(opening["payoff"]["text"], "立即证明。")
        self.assertEqual(opening["opening_unit_seconds"], 4.0)
