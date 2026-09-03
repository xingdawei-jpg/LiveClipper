from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commercial_analyzer import DirectorBeat, EvidenceItem, Strategy
from director_strategy import (
    build_director_strategy_library,
    build_narrative_blueprint_contract,
    compile_director_strategy,
)


def _story(strategy_id: str, family: str, claim: str, subtitle_id: int, *, priority: str = "medium") -> Strategy:
    core = EvidenceItem("hook", claim, (subtitle_id,), "core")
    return Strategy(
        strategy_id=strategy_id, type="problem_transformation", strategy_family=family,
        sub_angle=family, thesis=claim, target_user="目标人群", evidence=(core,),
        missing_roles=(), blocked_evidence_types=(), contract_audit_hits=(),
        coherence_reason="完整", distinctiveness="high", story_strength=0.8,
        material_sufficiency=0.8, contract_compatibility=1.0, strategy_viability="recommended",
        core_commercial_idea=claim, core_evidence_pool=(core,), story_priority=priority,
    )


class DirectorStrategyTests(unittest.TestCase):
    def test_exposes_three_sell_paths_from_m1_map(self):
        body = _story("S1", "body_confidence", "宽肩也显瘦", 1, priority="high")
        comfort = _story("S2", "comfort_lifestyle", "夏天穿着舒服", 2)
        quality = _story("S3", "quality_assurance", "面料品质可靠", 3)
        library = build_director_strategy_library((body, comfort, quality))

        self.assertEqual([item["director_mode"] for item in library["proposals"]], ["traffic", "conversion", "premium"])
        conversion = library["proposals"][1]
        premium = library["proposals"][2]
        self.assertEqual(conversion["story_mix"][0]["story_id"], "S2")
        self.assertEqual(premium["primary_story_id"], "S3")

    def test_compiled_plan_has_explicit_source_story_lineage(self):
        body = _story("S1", "body_confidence", "宽肩也显瘦", 1, priority="high")
        comfort = _story("S2", "comfort_lifestyle", "夏天穿着舒服", 2)
        proposal = build_director_strategy_library((body, comfort))["proposals"][1]
        composite, contract = compile_director_strategy(proposal, (body, comfort))

        self.assertEqual(composite.strategy_id, "D_CONVERSION_S2")
        self.assertEqual(composite.thesis, "夏天穿着舒服")
        self.assertEqual(composite.core_commercial_idea, "夏天穿着舒服")
        self.assertEqual(contract["source_story_ids"], ["S2", "S1"])
        self.assertTrue(contract["no_new_claims"])
        self.assertIn(1, composite.supporting_evidence_pool[0].subtitle_ids)

    def test_baby_grade_material_story_can_form_premium_path(self):
        body = _story("S1", "body_confidence", "宽肩也显瘦", 1, priority="high")
        comfort = _story("S2", "summer_cooling", "夏天穿着凉快", 2)
        safety = _story("S3", "baby_grade_material", "A类母婴级面料更安心", 3)
        library = build_director_strategy_library((body, comfort, safety))

        premium = next(item for item in library["proposals"] if item["director_mode"] == "premium")
        self.assertEqual(premium["primary_story_id"], "S3")
        self.assertEqual(premium["supporting_story_ids"], ["S1"])

    def test_comfort_copy_mentioning_material_does_not_steal_premium_slot(self):
        body = _story("S1", "body_confidence", "宽肩也显瘦", 1, priority="high")
        comfort = replace(
            _story("S2", "comfort_health", "面料透气、夏天不粘身", 2),
            sub_angle="breathable_cooling",
        )
        safety = replace(
            _story("S3", "safety_health", "A类母婴级面料更安心", 3),
            sub_angle="baby_grade_material",
        )
        library = build_director_strategy_library((body, comfort, safety))

        modes = {item["director_mode"]: item["primary_story_id"] for item in library["proposals"]}
        self.assertEqual(modes["conversion"], "S2")
        self.assertEqual(modes["premium"], "S3")

    def test_p01_blueprints_give_each_archetype_an_independent_desire_opening_and_slot_policy(self):
        story = _story("S1", "body_confidence", "大身材也能显瘦", 1, priority="high")
        pain = build_narrative_blueprint_contract(story, "pain_point")
        scene = build_narrative_blueprint_contract(story, "scene_immersion")

        self.assertEqual(pain["narrative_archetype"], "pain_point")
        self.assertNotEqual(pain["core_desire"], story.core_commercial_idea)
        self.assertNotEqual(pain["core_desire"], scene["core_desire"])
        self.assertEqual(pain["early_journey_scope"]["opening_question_ids"], ["Q1", "Q2"])
        self.assertEqual(scene["early_journey_scope"]["opening_question_ids"], ["Q6", "Q4"])
        self.assertEqual(scene["opening_scope"]["allowed_purchase_question_ids"], ["Q6", "Q4"])
        self.assertFalse(scene["opening_scope"]["fallback_to_global_opening"])
        self.assertEqual(
            [item["purchase_question_id"] for item in pain["blueprint"]["chapter_slots"]],
            ["Q1", "Q2", "Q3", "Q5", "Q4", "Q6", "Q7"],
        )
        self.assertEqual(
            [item["purchase_question_id"] for item in scene["blueprint"]["chapter_slots"]],
            ["Q6", "Q4", "Q1", "Q3", "Q5", "Q2", "Q7"],
        )
        self.assertEqual(
            [item["coverage"] for item in scene["blueprint"]["chapter_slots"]],
            ["required", "required", "recommended", "recommended", "recommended", "optional", "optional"],
        )
        self.assertTrue(all(item["duration_seconds"] is None for item in pain["blueprint"]["chapter_slots"]))
        self.assertEqual(pain["blueprint"]["duration_policy"], "soft_target_no_padding")

    def test_single_ai_director_packet_keeps_dynamic_titles_and_exact_sequence(self):
        story = replace(
            _story("S1", "body_confidence", "大身材穿得利落", 1, priority="high"),
            director_title="夏天大身材也能轻松穿",
            core_desire="显瘦但不闷热、出门不用费劲搭配",
            opening_promise="先给大身材显瘦结果",
            director_quality_tier="strong",
            director_sequence=(
                DirectorBeat("B1", "result", "先兑现显瘦", (1,), "先告诉观众为什么值得看"),
                DirectorBeat("B2", "comfort", "再回答夏天舒服吗", (2,), "把结果落到日常体验"),
            ),
        )

        library = build_director_strategy_library((story,))
        proposal = library["proposals"][0]
        composite, contract = compile_director_strategy(proposal, (story,))

        self.assertEqual(library["version"], "director-strategy-single-pass-v1")
        self.assertFalse(library["boundary"]["fixed_mode_templates_used"])
        self.assertEqual(proposal["name"], "夏天大身材也能轻松穿")
        self.assertEqual(proposal["director_mode"], "single_pass_director")
        self.assertEqual(proposal["director_sequence"][1]["subtitle_ids"], [2])
        self.assertEqual(composite.director_sequence[0].subtitle_ids, (1,))
        self.assertTrue(contract["single_ai_director_packet"])

    def test_one_call_library_materializes_only_primary_and_keeps_alternative_direction_only(self):
        primary = replace(
            _story("S1", "body_confidence", "显瘦又好穿", 1, priority="high"),
            director_plan_role="primary",
            director_sequence=(DirectorBeat("B1", "result", "结果", (1,)), DirectorBeat("B2", "proof", "证明", (2,))),
        )
        alternative = replace(
            _story("S2", "comfort_lifestyle", "夏天轻松出门", 3),
            director_plan_role="alternative",
            director_title="夏天出门不费劲",
            core_desire="轻松好搭又舒服",
            opening_promise="先进入夏天出门场景",
        )

        library = build_director_strategy_library((primary, alternative))
        main, option = library["proposals"]

        self.assertEqual(main["director_plan_role"], "primary")
        self.assertFalse(main["requires_additional_ai_call"])
        self.assertEqual(option["director_plan_role"], "alternative")
        self.assertTrue(option["requires_additional_ai_call"])
        self.assertEqual(option["director_sequence"], [])
