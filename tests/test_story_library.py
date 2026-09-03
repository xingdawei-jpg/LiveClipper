from __future__ import annotations

import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commercial_analyzer import EvidenceItem, Strategy
from story_library import build_story_library
from story_planner import PlanningCandidate


def _strategy(strategy_id: str, subtitle_id: int, *, duration: float = 31.6) -> Strategy:
    core = EvidenceItem("hook", f"{strategy_id} 的购买承诺", (subtitle_id,), "core")
    return Strategy(
        strategy_id=strategy_id,
        type="commercial_story",
        strategy_family="fit",
        sub_angle="身材包容",
        thesis=f"{strategy_id} 主线",
        target_user="需要身材包容的人群",
        evidence=(core,),
        missing_roles=(),
        blocked_evidence_types=(),
        contract_audit_hits=(),
        coherence_reason="完整",
        distinctiveness="high",
        story_strength=0.8,
        material_sufficiency=0.8,
        contract_compatibility=1.0,
        strategy_viability="recommended",
        audience_tension="担心显壮",
        core_commercial_idea="版型解决身材顾虑",
        payoff="穿着更安心",
        core_evidence_pool=(core,),
        recommended_duration_seconds=duration,
        duration_feasibility="insufficient_for_target",
        story_priority="high",
    )


class StoryLibraryTests(unittest.TestCase):
    def test_keeps_all_m1_stories_without_selecting_or_composing(self):
        candidates = (
            PlanningCandidate(11, "SINGLE", 1.0, 4.0, "可追溯证据", origin_subtitle_ids=(7,)),
        )
        library = build_story_library((_strategy("S1", 7), _strategy("S2", 99)), candidates)

        self.assertEqual(library["story_count"], 2)
        self.assertFalse(library["boundary"]["auto_story_selection"])
        self.assertFalse(library["boundary"]["auto_story_composition"])
        self.assertEqual(library["stories"][0]["natural_duration_seconds"], 31.6)
        resolved = library["stories"][0]["assets"][0]
        unresolved = library["stories"][1]["assets"][0]
        self.assertEqual(resolved["candidate_lineage"][0]["candidate_id"], 11)
        self.assertEqual(unresolved["candidate_lineage_status"], "unresolved")
