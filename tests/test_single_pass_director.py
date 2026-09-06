from __future__ import annotations

from dataclasses import replace
import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commercial_analyzer import (
    DirectorBeat,
    DirectorChapterPacket,
    DirectorOpeningAlternative,
    Strategy,
)
from single_pass_director import build_single_pass_director_plan
from story_planner import PlanningCandidate


def _strategy(sequence: tuple[DirectorBeat, ...]) -> Strategy:
    return Strategy(
        strategy_id="S_DIRECTOR", type="", strategy_family="", sub_angle="",
        thesis="夏天大身材也能穿得轻松", target_user="大身材用户", evidence=(),
        missing_roles=(), blocked_evidence_types=(), contract_audit_hits=(),
        coherence_reason="", distinctiveness="high", story_strength=0.8,
        material_sufficiency=0.8, contract_compatibility=1.0, strategy_viability="recommended",
        story_priority="high", director_title="夏天轻松显瘦", core_desire="显瘦又不闷热",
        opening_promise="先给显瘦结果", director_quality_tier="strong", director_sequence=sequence,
    )


def _candidate(candidate_id: int, start: float, end: float) -> PlanningCandidate:
    return PlanningCandidate(
        candidate_id=candidate_id, source_id="SINGLE", start=start, end=end,
        text=f"真实口播 {candidate_id}", origin_subtitle_ids=(candidate_id,),
        hook_eligible=True,
    )


class SinglePassDirectorTests(unittest.TestCase):
    def test_duration_uses_final_speed_not_source_time_and_retains_audit(self) -> None:
        from commercial_analyzer import director_delivery_duration_range
        sequence = tuple(DirectorBeat(f"B{i}", "proof", "说明", (i,), "证据", "Q1", "为什么", "proof", "", "result", "required") for i in range(1, 11))
        strategy = replace(_strategy(sequence), whole_video_audit={"duration_control": {"status": "target_range_fulfilled"}})
        plan = build_single_pass_director_plan(
            strategy=strategy, safe_candidates=tuple(_candidate(i, i * 8, i * 8 + 6.9) for i in range(1, 11)),
            target_duration=60, selection_contract={"target_duration_range": director_delivery_duration_range(60, None, 1.15)},
            director_contract={"single_ai_director_packet": True, "two_pass_director_packet": True},
        )
        self.assertTrue(plan.plan_valid)
        self.assertAlmostEqual(plan.duration_assessment["actual_seconds"], 69)
        self.assertAlmostEqual(plan.duration_assessment["projected_final_seconds"], 60)
        self.assertEqual(plan.duration_assessment["status"], "target_range_fulfilled")
        self.assertEqual(plan.duration_assessment["duration_control"]["status"], "target_range_fulfilled")

    def test_two_pass_chapter_readthrough_is_verified_without_mutating_selection(self) -> None:
        first = DirectorBeat(
            "C1B1", "result", "显瘦结果", (11,), "先兑现购买理由",
            "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required",
        )
        proof = DirectorBeat(
            "C1B2", "proof", "解释结果", (22,), "补充证明",
            "Q1", "我为什么想买？", "proof", "Q1", "shape_proof", "required",
        )
        chapter = DirectorChapterPacket(
            "C1", "opening", "先讲显瘦", "结果到证明", "显瘦有证据", "required",
            (first, proof), purchase_question_id="Q1", buyer_advance="显瘦有证据",
            chapter_kind="result", chapter_readthrough="错误的自称连读", continuity_status="pass",
        )
        strategy = replace(
            _strategy((first, proof)),
            director_chapter_packets=(chapter,),
            whole_video_audit={
                "status": "natural_complete_below_target",
                "selected_subtitle_id_count": 2,
                "estimated_source_seconds": 6.0,
            },
        )

        plan = build_single_pass_director_plan(
            strategy=strategy,
            safe_candidates=(_candidate(11, 0, 3), _candidate(22, 3, 6)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True, "two_pass_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22])
        self.assertIn(
            "single_pass_director_chapter_readthrough_mismatch:C1",
            plan.selection_contract["director_quality_warnings"],
        )
        self.assertEqual(
            plan.selection_contract["director_verified_chapter_readthroughs"]["C1"],
            "真实口播 11｜真实口播 22",
        )

    def test_preserves_ai_chapter_packet_grouping_without_semantic_editing(self) -> None:
        first = DirectorBeat("C1B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required")
        mechanism = DirectorBeat("C1B2", "mechanism", "肩部机制", (22,), "马上解释原因", "Q2", "为什么真的有效？", "mechanism", "Q1", "shoulder_narrowing", "required")
        comfort = DirectorBeat("C2B1", "comfort", "夏天体验", (33,), "再解除闷热顾虑", "Q4", "夏天舒服吗？", "comfort", "", "summer_breathability", "recommended")
        chapters = (
            DirectorChapterPacket("C1", "pain_or_result_hook", "显瘦先兑现", "结果后解释", "不只是口号", "required", (first, mechanism)),
            DirectorChapterPacket("C2", "wearing_experience", "夏天也好穿", "解除闷热", "显瘦不等于闷", "recommended", (comfort,)),
        )
        strategy = _strategy((first, mechanism, comfort))
        strategy = replace(
            strategy,
            video_structure_id="pain_point",
            video_structure_name="痛点切入型",
            director_chapter_packets=chapters,
            whole_video_audit={"status": "pass", "selected_beat_count": 3, "estimated_source_seconds": 45.0},
        )
        plan = build_single_pass_director_plan(
            strategy=strategy,
            safe_candidates=(_candidate(11, 0, 15), _candidate(22, 15, 30), _candidate(33, 30, 45)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([beat.chapter_id for beat in plan.beats], ["C1", "C1", "C2"])
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22, 33])
        self.assertEqual(plan.selection_contract["director_video_structure"]["id"], "pain_point")
        self.assertEqual(plan.selection_contract["director_chapter_packets"][0]["beats"][1]["beat_id"], "C1B2")
        self.assertEqual(plan.selection_contract["director_whole_video_audit"]["status"], "pass")

    def test_preserves_ai_source_order_without_program_ranking(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required"),
            DirectorBeat("B2", "mechanism", "版型原因", (22,), "马上解释原因", "Q2", "为什么真的有效？", "mechanism", "Q1", "shoulder_narrowing", "required"),
            DirectorBeat("B3", "comfort", "夏天体验", (33,), "再解除闷热顾虑", "Q4", "夏天舒服吗？", "comfort", "", "summer_breathability", "recommended"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(33, 0, 15), _candidate(22, 15, 30), _candidate(11, 30, 45)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22, 33])
        self.assertEqual([beat.chapter_id for beat in plan.beats], ["D1", "D2", "D3"])
        self.assertEqual(plan.duration_assessment["status"], "natural_shortfall_below_requested_duration")
        self.assertEqual(plan.duration_assessment["requested_seconds"], 60.0)
        self.assertEqual(plan.duration_assessment["preferred_low"], 48.0)
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])
        self.assertEqual(plan.selection_contract["final_story_brief"]["authority"], "director_narrative_contract")
        self.assertEqual(plan.selection_contract["director_journey_question_ids"], ["Q1", "Q2", "Q4"])
        self.assertFalse(plan.selection_contract["strong_clip_ranking_used_before_journey"])

    def test_outside_normal_duration_remains_preview_materializable(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由"),
            DirectorBeat("B2", "comfort", "夏天体验", (22,), "再解除闷热顾虑"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(11, 0, 3), _candidate(22, 3, 8)),
            target_duration=60.0,
            selection_contract={},
            director_contract={},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual(plan.duration_assessment["status"], "short_draft_below_normal_duration")
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])

    def test_single_packet_warns_about_missing_purchase_relationships_without_blocking_m3(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由"),
            DirectorBeat("B2", "comfort", "夏天体验", (22,), "再解除闷热顾虑"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(11, 0, 3), _candidate(22, 3, 8)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22])
        self.assertTrue(any(
            "purchase_question_invalid" in item
            for item in plan.selection_contract["director_quality_warnings"]
        ))
        self.assertEqual(plan.issues, ())
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])

    def test_single_packet_warns_about_repeated_question_role_and_outcome_without_deleting_it(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required"),
            DirectorBeat("B2", "proof", "再次显瘦", (22,), "重复", "Q1", "我为什么想买？", "proof", "", "body_looks_narrower", "recommended"),
            DirectorBeat("B3", "proof", "还是显瘦", (33,), "重复", "Q1", "我为什么想买？", "proof", "", "body_looks_narrower", "recommended"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(11, 0, 3), _candidate(22, 3, 8), _candidate(33, 8, 13)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22, 33])
        self.assertTrue(any(
            "repeated_purchase_value" in item
            for item in plan.selection_contract["director_quality_warnings"]
        ))
        self.assertEqual(plan.issues, ())
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])

    def test_reusing_one_source_beat_is_warned_but_preserved_in_ai_playback_order(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required"),
            DirectorBeat("B2", "proof", "再次展示", (11,), "导演决定重复展示", "Q1", "真的成立吗？", "proof", "Q1", "visual_proof", "recommended"),
            DirectorBeat("B3", "comfort", "夏天体验", (22,), "进入新价值", "Q4", "夏天舒服吗？", "comfort", "", "summer_breathability", "recommended"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(11, 0, 3), _candidate(22, 3, 8)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 11, 22])
        self.assertTrue(any(
            "candidate_reused" in item
            for item in plan.selection_contract["director_quality_warnings"]
        ))
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])

    def test_single_packet_keeps_live_delivery_leadin_as_opening_and_reports_warning(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required"),
            DirectorBeat("B2", "mechanism", "版型原因", (22,), "马上解释原因", "Q2", "为什么真的有效？", "mechanism", "Q1", "shoulder_narrowing", "required"),
            DirectorBeat("B3", "comfort", "夏天体验", (33,), "再解除闷热顾虑", "Q4", "夏天舒服吗？", "comfort", "", "summer_breathability", "recommended"),
        )
        lede = PlanningCandidate(
            candidate_id=11, source_id="SINGLE", start=0, end=3,
            text="的确非常的显瘦啊。你看两边肩膀。", origin_subtitle_ids=(11,), hook_eligible=True,
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(lede, _candidate(22, 3, 8), _candidate(33, 8, 13)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22, 33])
        self.assertTrue(any(
            "opening_quality_warning" in item
            for item in plan.selection_contract["director_quality_warnings"]
        ))
        self.assertEqual(plan.selection_contract["director_opening_fallbacks_used"], [])
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])

    def test_program_does_not_replace_ai_opening_with_legacy_fallback(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required", (44,)),
            DirectorBeat("B2", "mechanism", "版型原因", (22,), "马上解释原因", "Q2", "为什么真的有效？", "mechanism", "Q1", "shoulder_narrowing", "required"),
            DirectorBeat("B3", "comfort", "夏天体验", (33,), "再解除闷热顾虑", "Q4", "夏天舒服吗？", "comfort", "", "summer_breathability", "recommended"),
        )
        lede = PlanningCandidate(
            candidate_id=11, source_id="SINGLE", start=0, end=10,
            text="的确非常的显瘦啊。你看两边肩膀。", origin_subtitle_ids=(11,), hook_eligible=True,
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(lede, _candidate(44, 10, 25), _candidate(22, 25, 40), _candidate(33, 40, 55)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22, 33])
        self.assertEqual(plan.selection_contract["director_opening_fallbacks_used"], [])
        self.assertTrue(plan.selection_contract["director_semantic_mutation_disabled"])
        self.assertTrue(any(
            "opening_quality_warning" in item
            for item in plan.selection_contract["director_quality_warnings"]
        ))

    def test_program_does_not_replace_primary_opening_with_alternative_package(self) -> None:
        primary_result = DirectorBeat(
            "C1B1", "result", "主开场结果", (11,), "先开场",
            "Q1", "为什么想买？", "result", "", "primary_result", "required",
        )
        primary_proof = DirectorBeat(
            "C1B2", "proof", "主开场证明", (12,), "立即证明",
            "Q1", "真的成立吗？", "proof", "Q1", "primary_proof", "required",
        )
        comfort = DirectorBeat(
            "C2B1", "comfort", "穿着体验", (33,), "进入新价值",
            "Q4", "穿着舒服吗？", "comfort", "", "comfort_result", "recommended",
        )
        alternative_result = DirectorBeat(
            "OP2B1", "comfort", "备用体验开场", (44,), "独立开场",
            "Q4", "夏天舒服吗？", "comfort", "", "alternative_result", "required",
        )
        alternative_proof = DirectorBeat(
            "OP2B2", "mechanism", "备用开场兑现", (45,), "立即兑现",
            "Q2", "为什么真的有效？", "mechanism", "Q4", "alternative_proof", "required",
        )
        chapters = (
            DirectorChapterPacket(
                "C1", "opening", "完整开场", "承诺后兑现", "建立第一购买理由",
                "required", (primary_result, primary_proof),
            ),
            DirectorChapterPacket(
                "C2", "experience", "体验推进", "进入新价值", "不再重复开场结果",
                "recommended", (comfort,),
            ),
        )
        strategy = replace(
            _strategy((primary_result, primary_proof, comfort)),
            director_chapter_packets=chapters,
            director_opening_alternatives=(
                DirectorOpeningAlternative(
                    "OP2", "完整备用开场", (alternative_result, alternative_proof),
                ),
            ),
        )
        invalid_primary = PlanningCandidate(
            candidate_id=11, source_id="SINGLE", start=0, end=3,
            text="的确非常显瘦。", origin_subtitle_ids=(11,), hook_eligible=True,
        )
        plan = build_single_pass_director_plan(
            strategy=strategy,
            safe_candidates=(
                invalid_primary, _candidate(12, 3.1, 6),
                _candidate(44, 10, 13), _candidate(45, 13.2, 16),
                _candidate(33, 20, 24),
            ),
            target_duration=45,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 12, 33])
        self.assertEqual(plan.selection_contract["director_opening_fallbacks_used"], [])
        self.assertTrue(plan.selection_contract["director_semantic_mutation_disabled"])
        self.assertNotIn(44, [item.candidate_id for item in plan.selected_candidates])
        self.assertNotIn(45, [item.candidate_id for item in plan.selected_candidates])

    def test_one_beat_can_materialize_an_ordered_contiguous_srt_span(self) -> None:
        opening = DirectorBeat(
            "B1", "result", "完整开场跨度", (11, 12), "连续两句",
            "Q1", "为什么想买？", "result", "", "complete_result", "required",
        )
        comfort = DirectorBeat(
            "B2", "comfort", "体验", (22,), "进入新价值",
            "Q4", "舒服吗？", "comfort", "", "comfort", "recommended",
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy((opening, comfort)),
            safe_candidates=(_candidate(11, 0, 1.2), _candidate(12, 1.3, 3.4), _candidate(22, 5, 8)),
            target_duration=30,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual(plan.beats[0].candidate_ids, (11, 12))

    def test_known_director_role_aliases_do_not_invalidate_paid_one_call_packet(self) -> None:
        opening = DirectorBeat(
            "B1", "hook", "开场结果", (11,), "建立购买理由",
            "Q1", "为什么想买？", "hook", "", "opening_result", "required",
        )
        pain = DirectorBeat(
            "B2", "problem", "用户痛点", (12,), "补充痛点共鸣",
            "Q1", "为什么想买？", "problem", "Q1", "buyer_pain", "required",
        )
        fit = DirectorBeat(
            "B3", "size_interaction", "尺码证明", (22,), "解除适穿顾虑",
            "Q3", "我这种身材能不能穿？", "size_interaction", "Q1", "fit_range", "recommended",
        )
        strategy = replace(
            _strategy((opening, pain, fit)),
            opening_scope={
                "allowed_purchase_question_ids": ["Q1"],
                "allowed_answer_roles": ["hook"],
            },
        )
        plan = build_single_pass_director_plan(
            strategy=strategy,
            safe_candidates=(
                _candidate(11, 0, 4), _candidate(12, 4.2, 8), _candidate(22, 9, 14),
            ),
            target_duration=30,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertFalse(any("answer_role_invalid" in item for item in plan.issues))

    def test_visual_demonstration_opening_is_preserved_with_nonblocking_warning(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required", (44,)),
            DirectorBeat("B2", "mechanism", "版型原因", (22,), "马上解释原因", "Q2", "为什么真的有效？", "mechanism", "Q1", "shoulder_narrowing", "required"),
            DirectorBeat("B3", "proof", "展示效果", (33,), "用画面扩大证明", "Q3", "我穿会是什么效果？", "proof", "Q1", "back_looks_narrower", "recommended"),
        )
        visual_opening = PlanningCandidate(
            candidate_id=11, source_id="SINGLE", start=0, end=10,
            text="就的确是把我的肩头从这儿变成了这儿。", origin_subtitle_ids=(11,), hook_eligible=True,
        )
        visual_body = PlanningCandidate(
            candidate_id=33, source_id="SINGLE", start=30, end=40,
            text="你看连后背都显瘦。", origin_subtitle_ids=(33,), hook_eligible=False,
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(visual_opening, _candidate(44, 10, 25), _candidate(22, 25, 40), visual_body),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22, 33])
        self.assertEqual(plan.selection_contract["director_opening_fallbacks_used"], [])
        self.assertTrue(any(
            "opening_quality_warning" in item
            for item in plan.selection_contract["director_quality_warnings"]
        ))

    def test_first_director_beat_sets_scope_when_optional_scope_object_is_omitted(self) -> None:
        sequence = (
            DirectorBeat("B1", "comfort", "先给夏天体验", (11,), "先停住怕热用户", "Q4", "夏天舒服吗？", "comfort", "", "cool_and_dry", "required"),
            DirectorBeat("B2", "mechanism", "解释透气原因", (22,), "解释为什么凉快", "Q2", "为什么真的有效？", "mechanism", "Q4", "breathable_fabric", "required"),
            DirectorBeat("B3", "risk_remove", "不粘身", (33,), "解除闷热顾虑", "Q5", "穿着有没有风险或顾虑？", "risk_remove", "", "no_sticky_feel", "recommended"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(11, 0, 15), _candidate(22, 15, 30), _candidate(33, 30, 45)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual(
            plan.selection_contract["director_opening_scope"]["allowed_purchase_question_ids"], ["Q4"],
        )

    def test_short_strong_packet_remains_materializable_as_a_labeled_draft(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由", "Q1", "我为什么想买？", "result", "", "body_looks_narrower", "required"),
            DirectorBeat("B2", "mechanism", "版型原因", (22,), "马上解释原因", "Q2", "为什么真的有效？", "mechanism", "Q1", "shoulder_narrowing", "required"),
            DirectorBeat("B3", "comfort", "夏天体验", (33,), "再解除闷热顾虑", "Q4", "夏天舒服吗？", "comfort", "", "summer_breathability", "recommended"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(11, 0, 3), _candidate(22, 3, 7), _candidate(33, 7, 12)),
            target_duration=60.0,
            selection_contract={},
            director_contract={"single_ai_director_packet": True},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual(plan.status, "director_short_draft")
        self.assertEqual(plan.duration_assessment["status"], "natural_shortfall_below_requested_duration")
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])

    def test_unknown_source_is_reported_instead_of_substituted(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由"),
            DirectorBeat("B2", "comfort", "夏天体验", (999,), "再解除闷热顾虑"),
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(_candidate(11, 0, 3),),
            target_duration=60.0,
            selection_contract={},
            director_contract={},
        )

        self.assertFalse(plan.plan_valid)
        self.assertTrue(any("source_unresolved" in item for item in plan.issues))
        self.assertFalse(plan.duration_assessment["m3_render_gate"]["passed"])

    def test_obvious_asr_fragment_is_preserved_with_nonblocking_warning(self) -> None:
        sequence = (
            DirectorBeat("B1", "result", "显瘦结果", (11,), "先兑现购买理由"),
            DirectorBeat("B2", "comfort", "夏天体验", (22,), "再解除闷热顾虑"),
        )
        malformed = PlanningCandidate(
            candidate_id=11, source_id="SINGLE", start=0, end=3,
            text="的，很大。但是一穿上就显瘦。", origin_subtitle_ids=(11,), hook_eligible=True,
        )
        plan = build_single_pass_director_plan(
            strategy=_strategy(sequence),
            safe_candidates=(malformed, _candidate(22, 3, 8)),
            target_duration=60.0,
            selection_contract={},
            director_contract={},
        )

        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [11, 22])
        self.assertTrue(any(
            "final_utterance_warning" in item
            for item in plan.selection_contract["director_quality_warnings"]
        ))
        self.assertEqual(plan.issues, ())
        self.assertTrue(plan.duration_assessment["m3_render_gate"]["passed"])


if __name__ == "__main__":
    unittest.main()
