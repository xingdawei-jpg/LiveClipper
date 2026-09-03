import json
import os
import sys
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commerce_planner_lite import (  # noqa: E402
    build_commerce_lite_ranking_prompt,
    build_purchase_question_local_quality_prompt,
    build_commerce_lite_tags,
    plan_commerce_lite_chapter_compression_llm,
    plan_commerce_lite_final_editor_llm,
    plan_commerce_lite_strong_clip_llm,
    plan_commerce_lite_draft_final_llm,
    plan_commerce_lite_draft_rank_final_llm,
    plan_commerce_lite_llm,
    _purchase_path_audit,
    _parse_purchase_journey_quality,
    _parse_strong_clip_ranking,
    _director_opening_scope,
    _strong_optional_quality_question_ids,
    _quality_opening_reject_marker,
    _quality_local_selection_contract_errors,
    _quality_order_opening_contract_errors,
    _quality_order_literal_redundancy_contract_errors,
    _quality_order_journey_contract_errors,
    _quality_final_utterance_reject_reason,
    _quality_candidate_pool_item,
    _quality_utterance_reject_marker,
    _narrative_depth_blueprint,
    _blueprint_missing_purchase_questions,
    build_narrative_enrichment_prompt,
    _parse_narrative_enrichment,
    build_chapter_packet_source_windows,
    _parse_chapter_packets,
    plan_commerce_lite_narrative_mode_llm,
    StrongClipRank,
)
from commerce_lite_execution_adapter import align_lite_execution_metadata  # noqa: E402
from story_planner import (  # noqa: E402
    NarrativeBeat,
    NarrativePlan,
    OpeningPackage,
    PlanningCandidate,
    commerce_lite_story_budget,
    validate_narrative_plan,
)


def _strategy():
    evidence = SimpleNamespace(subtitle_ids=(2,), role="mechanism")
    return SimpleNamespace(
        strategy_id="S1",
        thesis="同一商业故事",
        story_premise="故事前提",
        audience_tension="用户顾虑",
        story_trigger="触发事实",
        transformation="获得变化",
        product_role="产品角色",
        core_commercial_idea="核心购买理由",
        payoff="购买结果",
        story_priority="high",
        supporting_arcs=("场景延展",),
        content_dependencies=(),
        core_evidence_pool=(evidence,),
        supporting_evidence_pool=(),
        bridge_candidates=(),
        evidence=(evidence,),
    )


_FORMAL_QUESTIONS = {
    "Q1": "为什么想买？它带来什么关键结果？",
    "Q2": "为什么这个结果可信、有效？",
    "Q3": "我这种身材或尺码能不能穿好？",
    "Q4": "夏天或长时间穿舒服吗？",
    "Q5": "穿着有没有实际顾虑需要解除？",
    "Q6": "日常怎么穿、怎么搭或适合什么场景？",
    "Q7": "面料或品质为什么值得信任？",
}
_QUALITY_ROLES = {
    "Q1": "result", "Q2": "mechanism", "Q3": "result", "Q4": "comfort",
    "Q5": "risk_remove", "Q6": "styling", "Q7": "trust",
}


def _director_depth_contract(archetype: str):
    orders = {
        "pain_point": ("Q3", "Q5", "Q4", "Q6", "Q7"),
        "scene_immersion": ("Q6", "Q4", "Q3", "Q5", "Q7"),
    }
    roles = {
        "Q3": ("result", "proof"), "Q4": ("comfort", "proof"),
        "Q5": ("risk_remove", "proof", "comfort"), "Q6": ("styling", "scene"),
        "Q7": ("trust", "proof"),
    }
    return {
        "narrative_archetype": archetype,
        "core_desire": "大身材也能显瘦",
        "blueprint": {
            "version": "narrative-blueprint-p0",
            "duration_policy": "soft_target_no_padding",
            "stop_rule": "stop_when_no_unexplored_high_value_slot_has_clean_source_evidence",
            "chapter_slots": [
                {"slot_id": f"depth_{question_id.lower()}", "priority": index, "phase": "depth",
                 "purchase_question_id": question_id, "answer_roles": roles[question_id]}
                for index, question_id in enumerate(orders[archetype], 3)
            ],
        },
    }

def _quality_response(question_candidate_pairs):
    """A clean, model-declared local top-one response for M2 quality tests."""
    question_ids = [question_id for question_id, _ in question_candidate_pairs]
    local_rows = []
    route = []
    path = []
    chapters = []
    for index, (question_id, candidate_id) in enumerate(question_candidate_pairs, 1):
        role = _QUALITY_ROLES[question_id]
        support = "Q1" if question_id == "Q2" else ""
        local_rows.append({
            "purchase_question_id": question_id,
            "purchase_question": _FORMAL_QUESTIONS[question_id],
            "supports_question_id": support,
            "local_candidates": [{
                "candidate_id": candidate_id, "local_rank": 1,
                "purchase_outcome": f"outcome_{candidate_id}", "answer_role": role,
                "commercial_impact": 5, "independent_completeness": 5, "specificity": 5,
                "asr_quality": 5, "semantic_cleanliness": 5, "previous_connection": 5,
                "spoken_completeness": "complete", "spoken_completeness_reason": "可独立口播",
                "incremental_purchase_value": True, "incremental_purchase_value_reason": "锚点或独立新认知",
                "final_utterance_eligible": True, "quality_reason": "局部最强且口播干净",
            }],
            "selected_candidate_id": candidate_id, "omit_reason": "",
        })
        route.append({"question_id": question_id, "question": _FORMAL_QUESTIONS[question_id], "journey_role": "quality", "why_now": "质量重排"})
        path.append({
            "step_id": f"P{index}", "chapter_id": f"C{index}", "purchase_cognition": f"回答{question_id}",
            "purchase_question_id": question_id, "purchase_question": _FORMAL_QUESTIONS[question_id],
            "supports_question_id": support, "answer_role": role, "answered_question": _FORMAL_QUESTIONS[question_id],
            "advance_type": "necessary_stronger_proof" if question_id == "Q2" else "new_purchase_cognition",
            "candidate_ids": [candidate_id], "why_it_advances": "局部最强候选",
        })
        chapters.append({
            "chapter_id": f"C{index}", "narrative_role": "hook" if index == 1 else "proof",
            "candidate_ids": [candidate_id], "purchase_value_dimension": "new_outcome",
            "purchase_value_domain": "quality", "purchase_value_outcomes": [f"outcome_{candidate_id}"],
            "purchase_value_reason": "局部最强候选",
        })
    return {
        "quality_by_question": local_rows,
        "retained_question_ids": question_ids,
        "dropped_optional_question_ids": [],
        "final_plan": {
            "purchase_question_route": route,
            "opening_package": {
                "hook_promise": "干净结果", "payoff_delivery": "干净机制", "connection_reason": "结果后解释",
                "hook_candidate_ids": [question_candidate_pairs[0][1]],
                "payoff_candidate_ids": [question_candidate_pairs[1][1]],
            },
            "purchase_cognition_path": path,
            "chapters": chapters,
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一商品", "no_rediscovery": True},
            "duration_assessment": {"status": "journey_complete", "reason": "质量自然结束"},
        },
    }


class CommercePlannerLiteTests(unittest.TestCase):
    def test_p04_packet_restores_a_local_micro_narrative_without_program_selection(self):
        """P0.4 accepts only an M2-declared local Packet with new sub-values."""
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "穿上正面看起来很窄。", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "肩线收进去所以看起来更窄。", (2,)),
            PlanningCandidate(3, "SRT", 30.0, 33.0, "三伏天穿也透薄不粘肉。", (3,)),
            PlanningCandidate(4, "SRT", 33.0, 36.0, "腋下也不会闷汗，出汗不贴身。", (4,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        def rank(candidate_id, question_id, role, outcome, *, support="", opening=0):
            return StrongClipRank(
                candidate_id=candidate_id, rank=candidate_id, standalone_strength=5, hook_power=5,
                purchase_value="已通过 P0.2", purchase_outcome=outcome,
                purchase_question_id=question_id, purchase_question=_FORMAL_QUESTIONS[question_id],
                supports_question_id=support, answer_role=role, purchase_question_role=role,
                answered_question=_FORMAL_QUESTIONS[question_id], evidence_function=role,
                proof_strength=5, redundancy_group=f"r{candidate_id}", fragment=False,
                visual_dependency=False, opening_rank=opening, opening_reason="", selection_reason="已通过 P0.2",
            )
        baseline_ranks = (
            rank(1, "Q1", "result", "body_slimming", opening=1),
            rank(2, "Q2", "mechanism", "shoulder_narrowing", support="Q1"),
            rank(3, "Q4", "comfort", "summer_comfort"),
        )
        baseline = {
            "purchase_question_route": [
                {"question_id": "Q1", "question": _FORMAL_QUESTIONS["Q1"], "journey_role": "core_result", "why_now": "结果"},
                {"question_id": "Q2", "question": _FORMAL_QUESTIONS["Q2"], "journey_role": "mechanism", "why_now": "机制"},
                {"question_id": "Q4", "question": _FORMAL_QUESTIONS["Q4"], "journey_role": "comfort", "why_now": "体感"},
            ],
            "opening_package": {"hook_promise": "显瘦", "payoff_delivery": "肩线", "connection_reason": "结果后解释", "hook_candidate_ids": [1], "payoff_candidate_ids": [2]},
            "purchase_cognition_path": [
                {"step_id": "P1", "chapter_id": "C1", "purchase_cognition": "显瘦", "purchase_question_id": "Q1", "purchase_question": _FORMAL_QUESTIONS["Q1"], "supports_question_id": "", "answer_role": "result", "answered_question": _FORMAL_QUESTIONS["Q1"], "advance_type": "new_purchase_cognition", "candidate_ids": [1], "why_it_advances": "结果"},
                {"step_id": "P2", "chapter_id": "C2", "purchase_cognition": "机制", "purchase_question_id": "Q2", "purchase_question": _FORMAL_QUESTIONS["Q2"], "supports_question_id": "Q1", "answer_role": "mechanism", "answered_question": _FORMAL_QUESTIONS["Q2"], "advance_type": "necessary_stronger_proof", "candidate_ids": [2], "why_it_advances": "机制"},
                {"step_id": "P3", "chapter_id": "C3", "purchase_cognition": "夏天舒服", "purchase_question_id": "Q4", "purchase_question": _FORMAL_QUESTIONS["Q4"], "supports_question_id": "", "answer_role": "comfort", "answered_question": _FORMAL_QUESTIONS["Q4"], "advance_type": "new_purchase_cognition", "candidate_ids": [3], "why_it_advances": "舒适"},
            ],
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body", "purchase_value_outcomes": ["body_slimming"], "purchase_value_reason": "显瘦"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "肩线"},
                {"chapter_id": "C3", "narrative_role": "comfort", "candidate_ids": [3], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "comfort", "purchase_value_outcomes": ["summer_comfort"], "purchase_value_reason": "透薄"},
            ],
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一商品", "no_rediscovery": True},
        }
        source_units = [
            {"id": item.candidate_id, "start": item.start, "end": item.end, "text": item.text}
            for item in candidates
        ]
        windows, _ = build_chapter_packet_source_windows(
            completed_plan_data=baseline, tags=tags, safe_candidates=candidates,
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
            source_context_units=source_units,
        )
        window_id = "EW_C3_3"
        self.assertIn(4, next(item for item in windows if item["source_window_id"] == window_id)["eligible_packet_candidate_ids"])
        clean_annotation = {
            "commercial_impact": 5, "independent_completeness": 5, "specificity": 5,
            "asr_quality": 5, "semantic_cleanliness": 5, "previous_connection": 5,
            "spoken_completeness": "complete", "final_utterance_eligible": True,
            "incremental_purchase_value": True, "incremental_purchase_value_reason": "从高温可穿推进到出汗不贴身",
            "clear_novel_proof": False, "novel_proof_reason": "", "quality_reason": "完整的具体体感证明",
            "answer_role": "proof", "purchase_outcome": "underarm_dry_non_cling",
        }
        quality = {
            "whole_transcript_passed": True, "opening_unchanged": True, "natural_flow": True,
            "no_repeat": True, "no_drag": True, "ending_natural": True,
            "packet_internal_flow": True, "packet_information_progression": True, "packet_redundancy": True,
            "packet_to_previous_chapter_transition": True, "packet_to_next_chapter_transition": True,
            "whole_video_pacing": True, "reason": "从高温适用推进到具体不贴身体验。",
            "dropped_new_candidate_ids": [], "dropped_new_packet_ids": [],
        }
        response = {
            "existing_chapter_packets": [{
                "packet_id": "P_C3_01", "packet_type": "existing_chapter_expansion", "chapter_id": "C3",
                "chapter_intent": "夏季穿着体验", "purchase_value": "高温可穿之外，出汗后也不贴身",
                "anchor_candidate_id": 3, "source_window_id": window_id, "ordered_candidate_ids": [3, 4], "support_candidate_ids": [4],
                "progression": [
                    {"candidate_id": 3, "function": "result", "new_sub_value": "三伏天可穿"},
                    {"candidate_id": 4, "function": "experience", "new_sub_value": "腋下干爽、出汗不贴身", "candidate_annotation": clean_annotation},
                ],
                "why_this_is_a_packet": "高温适用后补充真实出汗体感，不是重复凉爽。",
                "core_desire_compatibility": "让大身材在夏天穿得显瘦也轻松。", "net_improvement": "舒适章节从结论变成有体感证明的小故事。", "cross_window_reason": "",
            }],
            "new_structure_packets": [], "rejected_source_windows": [],
            "explored_source_window_ids": [item["source_window_id"] for item in windows],
            "final_order": [
                {"chapter_ref": "C1", "candidate_ids": [1], "why_now": "先给结果"},
                {"chapter_ref": "C2", "candidate_ids": [2], "why_now": "解释结果"},
                {"chapter_ref": "C3", "candidate_ids": [3, 4], "why_now": "在高温适用后补足体感"},
            ],
            "final_packet_quality": quality, "single_candidate_enrichment_recommended": False, "packet_status": "packet_enriched",
        }
        audit, ranks, packet_plan, errors = _parse_chapter_packets(
            response, baseline_plan_data=baseline, baseline_path_audit={}, baseline_ranks=baseline_ranks,
            remaining_tags=(tags[3],), source_windows=windows,
        )
        self.assertEqual(errors, [])
        self.assertEqual(audit["packet_status"], "packet_enriched")
        self.assertEqual([item.candidate_id for item in ranks], [1, 2, 3, 4])
        self.assertEqual(packet_plan["chapters"][-1]["candidate_ids"], [3, 4])

    def test_p03_enrichment_keeps_p02_baseline_and_accepts_one_dynamic_new_value_chapter(self):
        """P0.3 only materializes M2's declared new chapter; it never fills one."""
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "160斤穿上也显瘦。", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "肩线往里收所以看起来更窄。", (2,)),
            PlanningCandidate(3, "SRT", 6.0, 9.0, "三伏天穿着也透薄不粘肉。", (3,)),
            PlanningCandidate(4, "SRT", 9.0, 12.0, "这条裙子配凉鞋运动鞋都不挑。", (4,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        def rank(candidate_id, question_id, role, outcome, *, support="", opening=0):
            return StrongClipRank(
                candidate_id=candidate_id, rank=candidate_id, standalone_strength=5, hook_power=5,
                purchase_value="已通过 P0.2", purchase_outcome=outcome,
                purchase_question_id=question_id, purchase_question=_FORMAL_QUESTIONS[question_id],
                supports_question_id=support, answer_role=role, purchase_question_role=role,
                answered_question=_FORMAL_QUESTIONS[question_id], evidence_function=role,
                proof_strength=5, redundancy_group=f"r{candidate_id}", fragment=False,
                visual_dependency=False, opening_rank=opening, opening_reason="", selection_reason="已通过 P0.2",
            )
        baseline_ranks = (
            rank(1, "Q1", "result", "body_slimming", opening=1),
            rank(2, "Q2", "mechanism", "shoulder_narrowing", support="Q1"),
            rank(3, "Q4", "comfort", "summer_comfort"),
        )
        baseline = {
            "purchase_question_route": [
                {"question_id": "Q1", "question": _FORMAL_QUESTIONS["Q1"], "journey_role": "core_result", "why_now": "先给结果"},
                {"question_id": "Q2", "question": _FORMAL_QUESTIONS["Q2"], "journey_role": "mechanism", "why_now": "解释结果"},
                {"question_id": "Q4", "question": _FORMAL_QUESTIONS["Q4"], "journey_role": "comfort", "why_now": "解除夏天顾虑"},
            ],
            "opening_package": {"hook_promise": "显瘦结果", "payoff_delivery": "肩线机制", "connection_reason": "结果后解释", "hook_candidate_ids": [1], "payoff_candidate_ids": [2]},
            "purchase_cognition_path": [
                {"step_id": "P1", "chapter_id": "C1", "purchase_cognition": "显瘦", "purchase_question_id": "Q1", "purchase_question": _FORMAL_QUESTIONS["Q1"], "supports_question_id": "", "answer_role": "result", "answered_question": _FORMAL_QUESTIONS["Q1"], "advance_type": "new_purchase_cognition", "candidate_ids": [1], "why_it_advances": "结果"},
                {"step_id": "P2", "chapter_id": "C2", "purchase_cognition": "为什么显瘦", "purchase_question_id": "Q2", "purchase_question": _FORMAL_QUESTIONS["Q2"], "supports_question_id": "Q1", "answer_role": "mechanism", "answered_question": _FORMAL_QUESTIONS["Q2"], "advance_type": "necessary_stronger_proof", "candidate_ids": [2], "why_it_advances": "机制"},
                {"step_id": "P3", "chapter_id": "C3", "purchase_cognition": "夏天舒服", "purchase_question_id": "Q4", "purchase_question": _FORMAL_QUESTIONS["Q4"], "supports_question_id": "", "answer_role": "comfort", "answered_question": _FORMAL_QUESTIONS["Q4"], "advance_type": "new_purchase_cognition", "candidate_ids": [3], "why_it_advances": "体感"},
            ],
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body", "purchase_value_outcomes": ["body_slimming"], "purchase_value_reason": "显瘦"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "肩线"},
                {"chapter_id": "C3", "narrative_role": "comfort", "candidate_ids": [3], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "comfort", "purchase_value_outcomes": ["summer_comfort"], "purchase_value_reason": "透薄"},
            ],
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一商品", "no_rediscovery": True},
        }
        response = {
            "existing_chapter_enrichment": [],
            "new_structure_chapters": [{
                "enrichment_chapter_id": "E1", "chapter_intent": "日常出门怎么搭更省心？", "new_purchase_value": "一条裙子不挑鞋，出门搭配成本低", "candidate_ids": [4],
                "candidate_annotations": [{
                    "candidate_id": 4, "answer_role": "styling", "purchase_outcome": "shoe_versatility",
                    "commercial_impact": 5, "independent_completeness": 5, "specificity": 5, "asr_quality": 5,
                    "semantic_cleanliness": 5, "previous_connection": 5, "spoken_completeness": "complete",
                    "final_utterance_eligible": True, "incremental_purchase_value": True,
                    "incremental_purchase_value_reason": "把舒适延展到真实出门搭配", "clear_novel_proof": False,
                    "novel_proof_reason": "", "quality_reason": "独立、具体的搭配便利",
                }],
                "purchase_value_domain": "styling", "purchase_value_outcomes": ["shoe_versatility"], "narrative_role": "scene",
                "recommended_insert_after": "C3", "compatibility_with_core_desire": "让夏天出门轻松的体验自然落到搭配", "why_add": "新增日常使用认知", "net_improvement": "从体感落到出门决策", "support_count_explanation": "",
            }],
            "rejected_opportunities": [{"candidate_ids": [], "reason": "无"}],
            "final_order": [
                {"chapter_ref": "C1", "candidate_ids": [1], "why_now": "先给核心结果"},
                {"chapter_ref": "C2", "candidate_ids": [2], "why_now": "解释结果"},
                {"chapter_ref": "C3", "candidate_ids": [3], "why_now": "解决夏天体感"},
                {"chapter_ref": "E1", "candidate_ids": [4], "why_now": "体感之后落到出门搭配"},
            ],
            "enrichment_quality": {
                "whole_transcript_passed": True, "opening_unchanged": True, "natural_flow": True,
                "no_repeat": True, "no_drag": True, "ending_natural": True, "reason": "新增搭配便利，未重复显瘦或舒适。",
                "dropped_new_candidate_ids": [], "dropped_new_chapter_ids": [],
            },
            "enrichment_status": "enriched",
        }
        audit, enriched_ranks, enriched_plan, errors = _parse_narrative_enrichment(
            response, baseline_plan_data=baseline, baseline_path_audit={}, baseline_ranks=baseline_ranks,
            remaining_tags=(tags[3],),
        )
        self.assertEqual(errors, [])
        self.assertEqual(audit["enrichment_status"], "enriched")
        self.assertEqual([row["chapter_id"] for row in enriched_plan["chapters"]], ["C1", "C2", "C3", "E1"])
        self.assertEqual([item.candidate_id for item in enriched_ranks], [1, 2, 3, 4])
        prompt = build_narrative_enrichment_prompt(
            strategy=_strategy(), director_strategy_contract={"narrative_archetype": "scene_immersion"},
            narrative_depth={"blueprint_version": "narrative-blueprint-p0.1"}, completed_plan_data=baseline,
            completed_path_audit={}, completed_ranks=baseline_ranks, tags=tags, remaining_tags=(tags[3],), target_duration=60.0,
        )
        self.assertIn('"candidate_id":4', prompt)
        self.assertIn("不能删除、替换、重排", prompt)

    def test_p03_runs_once_after_p02_and_preserves_m3_materialization_boundary(self):
        """The full M2 route reaches one enrichment decision only after P0.2."""
        strategy = _strategy()
        candidates = tuple(
            PlanningCandidate(index, "SRT", float((index - 1) * 3), float(index * 3), text, (index,))
            for index, text in enumerate((
                "160斤穿上也显瘦。", "肩线往里收所以看起来更窄。", "S到L码都能穿到160斤。",
                "三伏天穿也透气不粘肉。", "里面有里衬不用穿安全裤。", "配运动鞋凉鞋都不挑。",
                "通勤出游都能这样穿，省得再想怎么搭。",
            ), 1)
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        question_ids = ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6")
        outcomes = ("body_slimming", "shoulder_narrowing", "size_inclusion", "summer_comfort", "wearing_security", "easy_styling")
        ranking = {"ranking_summary": "每一步回答不同购买问题。", "ranked_candidates": []}
        for index, (question_id, outcome) in enumerate(zip(question_ids, outcomes), 1):
            role = _QUALITY_ROLES[question_id]
            ranking["ranked_candidates"].append({
                "candidate_id": index, "rank": index, "standalone_strength": 9, "hook_power": 9 if index == 1 else 6,
                "purchase_value": outcome, "purchase_outcome": outcome, "purchase_question_id": question_id,
                "purchase_question": _FORMAL_QUESTIONS[question_id], "supports_question_id": "Q1" if question_id == "Q2" else "",
                "answer_role": role, "purchase_question_role": role, "answered_question": _FORMAL_QUESTIONS[question_id],
                "evidence_function": role, "proof_strength": 8, "redundancy_group": outcome,
                "fragment": False, "visual_dependency": False, "opening_rank": 1 if index == 1 else 0,
                "opening_reason": "独立结果" if index == 1 else "", "selection_reason": "回答新的购买问题",
            })
        quality = _quality_response(tuple(zip(question_ids, range(1, 7))))
        composition = quality["final_plan"]
        ordered_questions = ("Q1", "Q2", "Q3", "Q5", "Q4", "Q6")
        ordered_candidate_ids = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4, "Q5": 5, "Q6": 6}
        quality_order = {
            "retained_question_ids": list(ordered_questions), "dropped_optional_question_ids": [],
            "final_order": [{
                "question_id": question_id, "candidate_ids": [ordered_candidate_ids[question_id]],
                "narrative_role": "hook" if index == 1 else "payoff" if index == 2 else "new_value",
                "purchase_value_domain": "quality", "purchase_value_reason": "局部强候选",
                "purchase_cognition": f"回答{question_id}", "why_now": "按购买路径推进", "why_it_advances": "新增购买认知",
                "continuity_assessment": {"previous_to_current": "natural", "current_to_next": "natural_end" if index == len(ordered_questions) else "natural", "reason": "前后承接自然"},
            } for index, question_id in enumerate(ordered_questions, 1)],
            "opening_package": {"hook_promise": "显瘦结果", "payoff_delivery": "肩线机制", "connection_reason": "结果后解释", "hook_candidate_ids": [1], "payoff_candidate_ids": [2]},
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一商品", "no_rediscovery": True},
        }
        director_contract = {
            "narrative_archetype": "pain_point", "core_desire": "大身材想穿得显瘦又省心",
            "opening_promise": "先给显瘦结果", "opening_scope": {"allowed_purchase_question_ids": ["Q1"], "allowed_answer_roles": ["result"], "fallback_to_global_opening": False},
            "early_journey_scope": {"opening_question_ids": ["Q1", "Q2"], "required_question_ids": ["Q1", "Q2"], "recommended_question_ids": ["Q3", "Q5", "Q4", "Q6"], "optional_question_ids": ["Q7"], "preferred_question_order": ["Q1", "Q2", "Q3", "Q5", "Q4", "Q6", "Q7"]},
            "blueprint": {"version": "narrative-blueprint-p0.1", "duration_policy": "soft_target_no_padding", "chapter_slots": [
                {"slot_id": f"slot_{question_id}", "priority": index, "phase": "core" if index < 3 else "depth", "coverage": "required" if index < 3 else "recommended", "purchase_question_id": question_id, "answer_roles": [_QUALITY_ROLES[question_id]]}
                for index, question_id in enumerate(question_ids, 1)
            ]},
        }
        enrichment = {
            "existing_chapter_enrichment": [],
            "new_structure_chapters": [{
                "enrichment_chapter_id": "E1", "chapter_intent": "夏天出门还要不要额外想搭配？", "new_purchase_value": "通勤出游都能直接穿，减少搭配成本", "candidate_ids": [7],
                "candidate_annotations": [{
                    "candidate_id": 7, "answer_role": "scene", "purchase_outcome": "occasion_versatility",
                    "commercial_impact": 5, "independent_completeness": 5, "specificity": 5, "asr_quality": 5,
                    "semantic_cleanliness": 5, "previous_connection": 5, "spoken_completeness": "complete",
                    "final_utterance_eligible": True, "incremental_purchase_value": True,
                    "incremental_purchase_value_reason": "新增通勤出游的使用认知", "clear_novel_proof": False,
                    "novel_proof_reason": "", "quality_reason": "干净完整的使用场景",
                }],
                "purchase_value_domain": "scene", "purchase_value_outcomes": ["occasion_versatility"], "narrative_role": "scene",
                "recommended_insert_after": "C6", "compatibility_with_core_desire": "显瘦、适穿、舒适之后自然落到出门使用", "why_add": "新场景而非重复证明", "net_improvement": "让购买理由从商品属性走到真实使用", "support_count_explanation": "",
            }],
            "rejected_opportunities": [],
            "final_order": [
                *[
                    {"chapter_ref": f"C{index}", "candidate_ids": [candidate_id], "why_now": f"保留 P0.2 第{index}章"}
                    for index, candidate_id in enumerate((1, 2, 3, 5, 4, 6), 1)
                ],
                {"chapter_ref": "E1", "candidate_ids": [7], "why_now": "在搭配便利之后补足可直接出门的使用场景"},
            ],
            "enrichment_quality": {"whole_transcript_passed": True, "opening_unchanged": True, "natural_flow": True, "no_repeat": True, "no_drag": True, "ending_natural": True, "reason": "新场景没有重讲显瘦、机制、尺码、舒适或里衬。", "dropped_new_candidate_ids": [], "dropped_new_chapter_ids": []},
            "enrichment_status": "enriched",
        }
        packet_no_worthwhile = {
            "existing_chapter_packets": [], "new_structure_packets": [],
            "rejected_source_windows": [{"source_window_id": "EW_C1_1", "candidate_ids": [], "reason": "本回归专门验证 P0.3 fallback。"}],
            "explored_source_window_ids": [f"EW_C{index}_{candidate_id}" for index, candidate_id in enumerate((1, 2, 3, 5, 4, 6), 1)],
            "final_order": [
                {"chapter_ref": f"C{index}", "candidate_ids": [candidate_id], "why_now": f"保留 P0.2 第{index}章"}
                for index, candidate_id in enumerate((1, 2, 3, 5, 4, 6), 1)
            ],
            "final_packet_quality": {
                "whole_transcript_passed": True, "opening_unchanged": True, "natural_flow": True,
                "no_repeat": True, "no_drag": True, "ending_natural": True,
                "packet_internal_flow": True, "packet_information_progression": True, "packet_redundancy": True,
                "packet_to_previous_chapter_transition": True, "packet_to_next_chapter_transition": True,
                "whole_video_pacing": True, "reason": "没有 Packet，转交原有单句 fallback。",
                "dropped_new_candidate_ids": [], "dropped_new_packet_ids": [],
            },
            "single_candidate_enrichment_recommended": True,
            "packet_status": "no_worthwhile_packet",
        }
        response = lambda payload: {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", side_effect=[
            response(ranking), response(composition),
            *[response(row) for row in quality["quality_by_question"]], response(quality_order),
            response(packet_no_worthwhile), response(enrichment),
        ]) as request:
            _, plan = plan_commerce_lite_strong_clip_llm(
                strategy=strategy, tags=tags, target_duration=60.0, safe_candidates=candidates,
                selection_contract={}, executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
                director_strategy_contract=director_contract,
            )
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertEqual(plan.status, "journey_complete")
        self.assertEqual(
            plan.duration_assessment["status"], "enriched_natural_complete",
            plan.duration_assessment.get("commerce_chapter_packet_builder"),
        )
        self.assertEqual([beat.chapter_id for beat in plan.beats], ["C1", "C2", "C3", "C4", "C5", "C6", "E1"])
        self.assertEqual(plan.beats[-1].candidate_ids, (7,))
        enrichment_audit = plan.duration_assessment["commerce_narrative_enrichment"]
        self.assertTrue(enrichment_audit["triggered"])
        self.assertEqual(enrichment_audit["status"], "enriched_natural_complete")
        self.assertTrue(plan.duration_assessment["commerce_purchase_journey_quality"]["m3_render_gate"]["passed"])
        self.assertEqual(request.call_args_list[-1].kwargs["stage"], "M2_narrative_enrichment")
        aligned, _ = align_lite_execution_metadata(
            plan=plan, strategy=strategy, safe_candidates=candidates,
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        self.assertEqual(aligned.duration_assessment["status"], "enriched_natural_complete")
        self.assertIn("commerce_narrative_enrichment", aligned.duration_assessment)

    def test_known_caramel_asr_or_live_residue_can_only_be_rejected(self):
        self.assertEqual(_quality_utterance_reject_marker("好像往里挖了是那个35厘米"), "35厘米")
        self.assertEqual(_quality_utterance_reject_marker("这根黑色的花边线会把人的肩往里压。"), "")
        self.assertEqual(_quality_final_utterance_reject_reason("你看这根花边线把肩往里压。"), "live_interaction:你看")
        self.assertEqual(_quality_opening_reject_marker("你是大斜方肌，然后你的肩很壮很宽的。"), "大斜方肌")
        self.assertEqual(
            _quality_final_utterance_reject_reason("直到3到5厘米的一个挖尖挖进来会显得我们的肩干嘛？"),
            "肩干嘛",
        )

    def test_quality_candidate_payload_restates_existing_q1_hook_contract(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 2.0, "穿上以后正面整个人就看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 2.0, 5.0, "它其实大身材也能穿到160斤", (2,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        first = _quality_candidate_pool_item(tags[0])
        second = _quality_candidate_pool_item(tags[1])

        self.assertTrue(first["q1_opening_anchor_eligible"])
        self.assertFalse(second["q1_opening_anchor_eligible"])
        self.assertTrue(second["q1_opening_anchor_block_reason"])

    def test_quality_order_must_follow_purchase_progression(self):
        self.assertEqual(
            _quality_order_journey_contract_errors({
                "final_order": [{"question_id": item} for item in ("Q1", "Q2", "Q3", "Q4", "Q5", "Q6")],
            }),
            (),
        )
        errors = _quality_order_journey_contract_errors({
            "final_order": [{"question_id": item} for item in ("Q1", "Q2", "Q7", "Q3", "Q6")],
        })
        self.assertIn("purchase_quality_journey_order_not_purchase_progression", errors)
        self.assertIn("purchase_quality_journey_trust_must_only_close", errors)

    def test_narrative_depth_blueprint_only_activates_for_45_to_75_seconds(self):
        contract = _director_depth_contract("scene_immersion")
        self.assertIsNone(_narrative_depth_blueprint(contract, target_duration=44.9))
        depth = _narrative_depth_blueprint(contract, target_duration=60.0)
        self.assertEqual(depth["narrative_archetype"], "scene_immersion")
        self.assertEqual(
            [item["purchase_question_id"] for item in depth["chapter_slots"]],
            ["Q6", "Q4", "Q3", "Q5", "Q7"],
        )
        self.assertEqual(depth["duration_policy"], "soft_target_no_padding")
        self.assertIsNone(_narrative_depth_blueprint(contract, target_duration=75.1))

    def test_blueprint_missing_slots_use_declared_question_and_role_not_text_similarity(self):
        depth = _narrative_depth_blueprint(_director_depth_contract("pain_point"), target_duration=60.0)
        missing = _blueprint_missing_purchase_questions({
            "candidate_relations": [
                {"purchase_question_id": "Q3", "answer_role": "result", "purchase_outcome": "body_fit"},
                {"purchase_question_id": "Q5", "answer_role": "risk_remove", "purchase_outcome": "lining"},
                {"purchase_question_id": "Q2", "answer_role": "mechanism", "purchase_outcome": "shoulder_narrowing"},
            ],
        }, depth)
        self.assertEqual([item["purchase_question_id"] for item in missing], ["Q4", "Q6", "Q7"])

    def test_scene_immersion_order_can_move_scene_after_mechanism_but_pain_point_cannot(self):
        scene = _narrative_depth_blueprint(_director_depth_contract("scene_immersion"), target_duration=60.0)
        pain = _narrative_depth_blueprint(_director_depth_contract("pain_point"), target_duration=60.0)
        scene_order = {"final_order": [{"question_id": item} for item in ("Q1", "Q2", "Q6", "Q4", "Q3", "Q5", "Q7")]}
        self.assertEqual(_quality_order_journey_contract_errors(scene_order, narrative_depth=scene), ())
        self.assertIn(
            "purchase_quality_journey_order_not_purchase_progression",
            _quality_order_journey_contract_errors(scene_order, narrative_depth=pain),
        )

    def test_narrative_depth_q1_reserves_fit_and_mechanism_for_their_own_questions(self):
        candidates = (PlanningCandidate(1, "SRT", 0.0, 3.0, "大身材穿上也显瘦", (1,)),)
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=({"candidate_id": 1},),
            executable_evidence={1: {"materializable": True}},
        )
        q1 = {
            "purchase_question_id": "Q1", "purchase_question": _FORMAL_QUESTIONS["Q1"],
            "supports_question_id": "", "allowed_answer_roles": ["result"],
        }
        prompt = build_purchase_question_local_quality_prompt(
            question=q1, tags=tags,
            narrative_depth=_narrative_depth_blueprint(_director_depth_contract("pain_point"), target_duration=60.0),
        )
        self.assertIn("selected_candidate_ids 必须恰好只有这一条", prompt)
        scene_prompt = build_purchase_question_local_quality_prompt(
            question=q1, tags=tags,
            narrative_depth=_narrative_depth_blueprint(_director_depth_contract("scene_immersion"), target_duration=60.0),
        )
        self.assertIn("selected_candidate_ids 必须恰好只有这一条", scene_prompt)

    def test_quality_opening_receipt_must_include_complete_q1_and_q2_micro_sequences(self):
        rows = (
            {"purchase_question_id": "Q1", "selected_candidate_ids": [1, 2]},
            {"purchase_question_id": "Q2", "selected_candidate_ids": [3, 4]},
        )
        complete = {"opening_package": {"hook_candidate_ids": [1, 2], "payoff_candidate_ids": [3, 4]}}
        incomplete = {"opening_package": {"hook_candidate_ids": [1], "payoff_candidate_ids": [3]}}
        self.assertEqual(_quality_order_opening_contract_errors(complete, rows), ())
        self.assertIn(
            "purchase_quality_opening_payoff_must_be_q2",
            _quality_order_opening_contract_errors(incomplete, rows),
        )

    def test_quality_opening_micro_sequence_over_five_seconds_requires_director_reason(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "穿上以后正面整个人看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.1, "肩部线条把视觉往里收", (2,)),
            PlanningCandidate(3, "SRT", 6.1, 8.2, "S码到L码都有明确范围", (3,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        rows = (
            {"purchase_question_id": "Q1", "selected_candidate_ids": [1, 2]},
            {"purchase_question_id": "Q2", "selected_candidate_ids": [3]},
        )
        order = {"opening_package": {"hook_candidate_ids": [1, 2], "payoff_candidate_ids": [3]}}
        self.assertIn(
            "purchase_quality_opening_hook_integrity_reason_missing",
            _quality_order_opening_contract_errors(order, rows, tags=tags),
        )
        order["opening_package"]["hook_integrity_reason"] = "两句分别给出结果和收窄依据，删任一句都会损失完整承诺。"
        self.assertEqual(_quality_order_opening_contract_errors(order, rows, tags=tags), ())

    def test_quality_order_rejects_literal_size_table_repetition_but_never_picks_a_replacement(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 2.0, "显瘦效果很明显", (1,)),
            PlanningCandidate(2, "SRT", 2.0, 5.0, "S码可以穿到120斤，M码120到140斤，L码140到160斤。", (2,)),
            PlanningCandidate(3, "SRT", 5.0, 8.0, "S码可以穿到120斤，M码120到140斤，L码140到160斤都是没有问题的。", (3,)),
            PlanningCandidate(4, "SRT", 8.0, 10.0, "120到140斤，然后140到160斤都是没有问题的。", (4,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        order = {"final_order": [
            {"question_id": "Q1", "candidate_ids": [1, 2]},
            {"question_id": "Q3", "candidate_ids": [3, 4]},
        ]}
        errors = _quality_order_literal_redundancy_contract_errors(order, tags)
        self.assertIn("purchase_quality_support_repeats_later_question:Q1:2:Q3:3", errors)
        self.assertIn("purchase_quality_support_no_new_literal_fact:Q3:4", errors)

    def test_depth_local_quality_violation_is_a_retry_receipt_not_a_programmatic_replacement(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "黑色花边线会把肩往里压", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "三伏天穿着也很透薄", (2,)),
            PlanningCandidate(3, "SRT", 6.0, 9.0, "上身不粘肉很凉爽", (3,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        row = {
            "purchase_question_id": "Q4", "purchase_question": _FORMAL_QUESTIONS["Q4"],
            "supports_question_id": "", "local_candidates": [
                {"candidate_id": 2, "local_rank": 1, "purchase_outcome": "透薄", "answer_role": "comfort", "independent_completeness": 5, "asr_quality": 5, "semantic_cleanliness": 5, "final_utterance_eligible": True},
                {"candidate_id": 3, "local_rank": 2, "purchase_outcome": "凉爽", "answer_role": "comfort", "independent_completeness": 5, "asr_quality": 5, "semantic_cleanliness": 5, "final_utterance_eligible": True},
            ], "selected_candidate_ids": [2, 3], "selected_candidate_id": 2,
        }
        self.assertNotIn(
            "purchase_quality_support_role_outcome_repeated:Q4:3:comfort:凉爽",
            _quality_local_selection_contract_errors(row, tags=tags),
        )

    def test_quality_order_may_not_drop_a_locally_strong_new_purchase_question(self):
        def row(question_id, candidate_id, *, impact=5, eligible=True):
            return {
                "purchase_question_id": question_id,
                "selected_candidate_id": candidate_id,
                "local_candidates": [{
                    "candidate_id": candidate_id,
                    "commercial_impact": impact,
                    "independent_completeness": 4,
                    "asr_quality": 4,
                    "semantic_cleanliness": 4,
                    "final_utterance_eligible": eligible,
                }],
            }

        self.assertEqual(
            _strong_optional_quality_question_ids((
                row("Q3", 3), row("Q4", 4), row("Q5", 5), row("Q6", 6),
                row("Q7", 7), row("Q3", 30, impact=3), row("Q4", 40, eligible=False),
            )),
            ("Q3", "Q4", "Q5", "Q6"),
        )

    def test_sentence_gate_ignores_rejected_local_alternative_but_not_final_selection(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "穿上以后正面整个人看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "黑色花边线会把人的肩往里压", (2,)),
            PlanningCandidate(3, "SRT", 6.0, 9.0, "好像往里挖了是那个35厘米", (3,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        quality = _quality_response((("Q1", 1), ("Q2", 2)))
        quality["quality_by_question"][1]["local_candidates"].append({
            "candidate_id": 3, "local_rank": 2, "purchase_outcome": "肩部收窄",
            "answer_role": "mechanism", "commercial_impact": 5,
            "independent_completeness": 5, "specificity": 5, "asr_quality": 5,
            "semantic_cleanliness": 5, "previous_connection": 5,
            "final_utterance_eligible": True, "quality_reason": "不应进入最终片",
        })
        _, _, _, errors = _parse_purchase_journey_quality(
            quality, current_question_ids=("Q1", "Q2"), tags=tags,
        )
        self.assertFalse(any("known_unplayable_utterance" in item for item in errors), errors)

    def test_sentence_gate_rejects_selected_live_interaction_fragment(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "穿上以后正面整个人看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "你看这根花边线把肩往里压", (2,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        quality = _quality_response((("Q1", 1), ("Q2", 2)))

        _, _, _, errors = _parse_purchase_journey_quality(
            quality, current_question_ids=("Q1", "Q2"), tags=tags,
        )

        self.assertIn("purchase_quality_known_unplayable_utterance:Q2:2:live_interaction:你看", errors)

    def test_sentence_gate_rejects_selected_unclosed_spoken_utterance_even_when_scores_are_high(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "穿上以后正面整个人看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "完整肩线机制说明", (2,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        quality = _quality_response((("Q1", 1), ("Q2", 2)))
        selected = quality["quality_by_question"][1]["local_candidates"][0]
        selected["spoken_completeness"] = "dependent"
        selected["spoken_completeness_reason"] = "结尾未回答反问"
        _, _, _, errors = _parse_purchase_journey_quality(
            quality, current_question_ids=("Q1", "Q2"), tags=tags,
        )
        self.assertIn("purchase_quality_spoken_completeness_not_complete:Q2:2", errors)

    def test_quality_keeps_ai_declared_anchor_plus_distinct_role_support_in_one_question(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 2.4, "穿上以后正面整个人看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 2.4, 5.4, "黑色花边线把肩部视觉往里收", (2,)),
            PlanningCandidate(3, "SRT", 5.4, 8.4, "肩部的线条能解释为什么看起来更窄", (3,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        self.assertTrue(next(item for item in tags if item.candidate_id == 1).hook_eligible)
        quality = _quality_response((("Q1", 1), ("Q2", 3)))
        quality.pop("final_plan")
        q1 = quality["quality_by_question"][0]
        q1["local_candidates"].append({
            "candidate_id": 2, "local_rank": 2, "purchase_outcome": "肩部视觉内收",
            "answer_role": "proof", "commercial_impact": 4,
            "independent_completeness": 5, "specificity": 5, "asr_quality": 5,
            "semantic_cleanliness": 5, "previous_connection": 5,
            "spoken_completeness": "complete", "spoken_completeness_reason": "独立说明肩部视觉依据",
            "incremental_purchase_value": True, "incremental_purchase_value_reason": "为显窄补充具体肩线依据",
            "final_utterance_eligible": True, "quality_reason": "用花边线补强显窄结果",
        })
        q1["selected_candidate_ids"] = [1, 2]
        q1["selected_candidate_id"] = 1
        quality["opening_package"] = {
            "hook_promise": "正面显窄", "payoff_delivery": "肩部线条证明",
            "connection_reason": "结果后给证明", "hook_candidate_ids": [1, 2], "payoff_candidate_ids": [3],
        }
        quality["final_order"] = [
            {
                "question_id": "Q1", "candidate_ids": [1, 2], "narrative_role": "hook",
                "purchase_value_domain": "body_appearance", "purchase_value_reason": "先给结果再给视觉证明",
                "purchase_cognition": "正面显窄，并给出肩部视觉证明", "why_now": "先建立显瘦期待",
                "why_it_advances": "同一购买问题内完成结果与证明",
            },
            {
                "question_id": "Q2", "candidate_ids": [3], "narrative_role": "payoff",
                "purchase_value_domain": "body_appearance", "purchase_value_reason": "解释显窄机制",
                "purchase_cognition": "肩线机制让结果可信", "why_now": "兑现开场承诺",
                "why_it_advances": "回答为什么有效",
            },
        ]

        audit, ranks, plan, errors = _parse_purchase_journey_quality(
            quality, current_question_ids=("Q1", "Q2"), tags=tags,
        )

        self.assertEqual(errors, [], errors)
        self.assertEqual(audit["quality_by_question"][0]["selected_candidate_ids"], [1, 2])
        self.assertEqual([item.candidate_id for item in ranks], [1, 2, 3])
        self.assertEqual([item.opening_rank for item in ranks], [1, 0, 0])
        self.assertEqual(plan["chapters"][0]["candidate_ids"], [1, 2])
        self.assertEqual(plan["purchase_cognition_path"][0]["answer_role"], "result")

    def test_quality_keeps_rejected_out_of_question_comparison_out_of_final_contract(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 2.4, "穿上以后正面整个人看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 2.4, 5.4, "黑色花边线把肩部视觉往里收", (2,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        quality = _quality_response((("Q1", 1), ("Q2", 2)))
        # The director explicitly rejects this Q2 mechanism as a Q1 answer;
        # it remains visible as a comparison but never enters the edit.
        quality["quality_by_question"][0]["local_candidates"].append({
            "candidate_id": 2, "local_rank": 2, "purchase_outcome": "肩部收窄机制",
            "answer_role": "mechanism", "commercial_impact": 3,
            "independent_completeness": 5, "specificity": 4, "asr_quality": 5,
            "semantic_cleanliness": 5, "previous_connection": 4,
            "spoken_completeness": "complete", "spoken_completeness_reason": "完整机制句，但不回答 Q1",
            "incremental_purchase_value": False, "incremental_purchase_value_reason": "未选",
            "final_utterance_eligible": True, "quality_reason": "机制角色不符，明确不选",
        })

        audit, ranks, _, errors = _parse_purchase_journey_quality(
            quality, current_question_ids=("Q1", "Q2"), tags=tags,
        )

        self.assertEqual(errors, [], errors)
        self.assertEqual([item.candidate_id for item in ranks], [1, 2])
        self.assertEqual([item["candidate_id"] for item in audit["quality_by_question"][0]["local_candidates"]], [1])

    def test_quality_rejects_same_role_and_same_outcome_support_inside_one_question(self):
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 2.4, "穿上以后正面整个人看起来很窄", (1,)),
            PlanningCandidate(2, "SRT", 2.4, 5.4, "后背看起来也很薄", (2,)),
            PlanningCandidate(3, "SRT", 5.4, 8.4, "肩部线条把视觉往里收", (3,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        quality = _quality_response((("Q1", 1), ("Q2", 3)))
        q1 = quality["quality_by_question"][0]
        q1["local_candidates"].append({
            "candidate_id": 2, "local_rank": 2, "purchase_outcome": "outcome_1",
            "answer_role": "result", "commercial_impact": 4,
            "independent_completeness": 5, "specificity": 5, "asr_quality": 5,
            "semantic_cleanliness": 5, "previous_connection": 5,
            "spoken_completeness": "complete", "spoken_completeness_reason": "完整但重复",
            "incremental_purchase_value": True, "incremental_purchase_value_reason": "模型误报为新价值",
            "final_utterance_eligible": True, "quality_reason": "同角色重复，不应进入小段落",
        })
        q1["selected_candidate_ids"] = [1, 2]
        q1["selected_candidate_id"] = 1

        _, _, _, errors = _parse_purchase_journey_quality(
            quality, current_question_ids=("Q1", "Q2"), tags=tags,
        )

        self.assertIn("purchase_quality_support_role_outcome_repeated:Q1:2:result:outcome_1", errors)

    def test_strong_ranking_cannot_promote_hook_ineligible_candidate_to_opening(self):
        response = {
            "ranked_candidates": [{
                "candidate_id": 7, "rank": 1, "standalone_strength": 10, "hook_power": 10,
                "purchase_value": "强结果", "purchase_outcome": "result", "purchase_question_id": "Q1",
                "purchase_question": _FORMAL_QUESTIONS["Q1"], "supports_question_id": "", "answer_role": "result",
                "purchase_question_role": "result", "answered_question": _FORMAL_QUESTIONS["Q1"], "evidence_function": "result",
                "proof_strength": 10, "redundancy_group": "result", "fragment": False,
                "visual_dependency": False, "opening_rank": 1, "opening_reason": "强结果", "selection_reason": "强结果",
            }],
        }
        with self.assertRaisesRegex(ValueError, "无开场权限"):
            _parse_strong_clip_ranking(response, candidate_ids={7}, hook_eligible_ids=set())

    def test_scene_opening_scope_rejects_global_q1_hook_without_fallback(self):
        response = {
            "ranked_candidates": [{
                "candidate_id": 7, "rank": 1, "standalone_strength": 10, "hook_power": 10,
                "purchase_value": "显瘦结果", "purchase_outcome": "slimming", "purchase_question_id": "Q1",
                "purchase_question": _FORMAL_QUESTIONS["Q1"], "supports_question_id": "", "answer_role": "result",
                "purchase_question_role": "result", "answered_question": _FORMAL_QUESTIONS["Q1"], "evidence_function": "result",
                "proof_strength": 10, "redundancy_group": "result", "fragment": False,
                "visual_dependency": False, "opening_rank": 1, "opening_reason": "显瘦", "selection_reason": "显瘦",
            }],
        }
        scope = _director_opening_scope({
            "narrative_archetype": "scene_immersion",
            "opening_scope": {
                "allowed_purchase_question_ids": ["Q6", "Q4"],
                "allowed_answer_roles": ["scene", "styling", "comfort", "proof"],
                "fallback_to_global_opening": False,
                "requires_clean_independent_utterance": True,
            },
        })
        with self.assertRaisesRegex(ValueError, "范围外购买问题"):
            _parse_strong_clip_ranking(
                response, candidate_ids={7}, hook_eligible_ids={7}, opening_scope=scope,
            )

    def test_scene_blueprint_completion_skips_optional_mechanism_and_trust_recall(self):
        contract = {
            "narrative_archetype": "scene_immersion",
            "core_desire": "夏天出门舒服好搭",
            "opening_scope": {"allowed_purchase_question_ids": ["Q6", "Q4"], "allowed_answer_roles": ["scene", "styling", "comfort", "proof"]},
            "early_journey_scope": {
                "opening_question_ids": ["Q6", "Q4"], "required_question_ids": ["Q6", "Q4"],
                "recommended_question_ids": ["Q1", "Q3", "Q5"], "optional_question_ids": ["Q2", "Q7"],
                "preferred_question_order": ["Q6", "Q4", "Q1", "Q3", "Q5", "Q2", "Q7"],
            },
            "blueprint": {
                "version": "narrative-blueprint-p0.1", "duration_policy": "soft_target_no_padding",
                "chapter_slots": [
                    {"slot_id": "scene", "priority": 1, "phase": "core", "coverage": "required", "purchase_question_id": "Q6", "answer_roles": ["scene", "styling"]},
                    {"slot_id": "comfort", "priority": 2, "phase": "core", "coverage": "required", "purchase_question_id": "Q4", "answer_roles": ["comfort", "proof"]},
                    {"slot_id": "result", "priority": 3, "phase": "depth", "coverage": "recommended", "purchase_question_id": "Q1", "answer_roles": ["result", "proof"]},
                    {"slot_id": "mechanism", "priority": 6, "phase": "depth", "coverage": "optional", "purchase_question_id": "Q2", "answer_roles": ["mechanism", "proof"]},
                    {"slot_id": "trust", "priority": 7, "phase": "depth", "coverage": "optional", "purchase_question_id": "Q7", "answer_roles": ["trust", "proof"]},
                ],
            },
        }
        depth = _narrative_depth_blueprint(contract, target_duration=60.0)
        missing = _blueprint_missing_purchase_questions({
            "candidate_relations": [
                {"purchase_question_id": "Q6", "answer_role": "scene"},
                {"purchase_question_id": "Q4", "answer_role": "comfort"},
                {"purchase_question_id": "Q1", "answer_role": "result"},
            ],
        }, depth)
        self.assertEqual(missing, [])

    def test_purchase_path_audit_rejects_repeated_question_and_duplicate_proof_function(self):
        ranked = (
            SimpleNamespace(candidate_id=1, purchase_outcome="shoulder_narrowing", purchase_question_id="Q1", answered_question="我能不能穿", evidence_function="result"),
            SimpleNamespace(candidate_id=2, purchase_outcome="shoulder_narrowing", purchase_question_id="Q2", answered_question="为什么有效", evidence_function="mechanism"),
            SimpleNamespace(candidate_id=3, purchase_outcome="shoulder_narrowing", purchase_question_id="Q2", answered_question="为什么有效", evidence_function="mechanism"),
        )
        beats = tuple(
            SimpleNamespace(
                chapter_id=f"C{index}", candidate_ids=(index,),
                purchase_value_outcomes=("shoulder_narrowing",),
            )
            for index in range(1, 4)
        )
        audit = _purchase_path_audit({
            "purchase_question_route": [
                {"question_id": "Q1", "question": "我能不能穿", "journey_role": "fit"},
                {"question_id": "Q2", "question": "为什么有效", "journey_role": "why_believe"},
                {"question_id": "Q2", "question": "为什么有效", "journey_role": "why_believe"},
            ],
            "purchase_cognition_path": [
                {"chapter_id": "C1", "candidate_ids": [1], "purchase_cognition": "能不能穿", "purchase_question_id": "Q1", "answered_question": "我能不能穿", "advance_type": "new_purchase_cognition"},
                {"chapter_id": "C2", "candidate_ids": [2], "purchase_cognition": "为什么有效", "purchase_question_id": "Q2", "answered_question": "为什么有效", "advance_type": "necessary_stronger_proof"},
                {"chapter_id": "C3", "candidate_ids": [3], "purchase_cognition": "再次解释", "purchase_question_id": "Q2", "answered_question": "为什么有效", "advance_type": "necessary_stronger_proof"},
            ],
        }, beats=beats, ranked=ranked)
        self.assertFalse(audit["passed"])
        self.assertIn("purchase_question_route_duplicate_id:Q2", audit["errors"])
        self.assertIn("purchase_outcome_function_repeated:shoulder_narrowing:mechanism", audit["errors"])

    def test_strong_clip_ranking_then_cognition_composition_uses_only_ranked_ids(self):
        evidence = SimpleNamespace(subtitle_ids=(1, 2, 3), role="mechanism")
        strategy = _strategy()
        strategy = SimpleNamespace(**{**strategy.__dict__, "core_evidence_pool": (evidence,), "evidence": (evidence,)})
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "160斤也能穿得显瘦", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "黑线把肩往里压", (2,)),
            PlanningCandidate(3, "SRT", 6.0, 9.0, "腋下很清爽不会热", (3,)),
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        ranking = {
            "ranking_summary": "先给大身材的结果，再用肩线机制兑现。",
            "ranked_candidates": [
                {"candidate_id": 1, "rank": 1, "standalone_strength": 9, "hook_power": 9, "purchase_value": "大身材显瘦", "purchase_outcome": "body_slimming", "purchase_question_id": "Q1", "purchase_question": _FORMAL_QUESTIONS["Q1"], "supports_question_id": "", "answer_role": "result", "purchase_question_role": "result", "answered_question": _FORMAL_QUESTIONS["Q1"], "evidence_function": "result", "proof_strength": 8, "redundancy_group": "body_result", "fragment": False, "visual_dependency": False, "opening_rank": 1, "opening_reason": "结果独立成立", "selection_reason": "结果清晰"},
                {"candidate_id": 2, "rank": 2, "standalone_strength": 8, "hook_power": 7, "purchase_value": "肩线内收机制", "purchase_outcome": "body_slimming", "purchase_question_id": "Q2", "purchase_question": _FORMAL_QUESTIONS["Q2"], "supports_question_id": "Q1", "answer_role": "mechanism", "purchase_question_role": "mechanism", "answered_question": _FORMAL_QUESTIONS["Q2"], "evidence_function": "mechanism", "proof_strength": 9, "redundancy_group": "shoulder_mechanism", "fragment": False, "visual_dependency": False, "opening_rank": 0, "opening_reason": "", "selection_reason": "机制具体"},
                {"candidate_id": 3, "rank": 3, "standalone_strength": 7, "hook_power": 5, "purchase_value": "夏天清爽", "purchase_outcome": "summer_comfort", "purchase_question_id": "Q3", "purchase_question": _FORMAL_QUESTIONS["Q3"], "supports_question_id": "", "answer_role": "proof", "purchase_question_role": "proof", "answered_question": _FORMAL_QUESTIONS["Q3"], "evidence_function": "proof", "proof_strength": 8, "redundancy_group": "comfort", "fragment": False, "visual_dependency": False, "opening_rank": 0, "opening_reason": "", "selection_reason": "新增舒适理由"},
            ],
        }
        composition = {
            "purchase_question_route": [
                {"question_id": "Q1", "question": _FORMAL_QUESTIONS["Q1"], "journey_role": "fit", "why_now": "先给用户适穿答案"},
                {"question_id": "Q2", "question": _FORMAL_QUESTIONS["Q2"], "journey_role": "why_believe", "why_now": "解释结果"},
                {"question_id": "Q3", "question": _FORMAL_QUESTIONS["Q3"], "journey_role": "comfort", "why_now": "解除体验顾虑"},
            ],
            "opening_package": {"hook_promise": "大身材也能显瘦", "payoff_delivery": "黑线把肩往里压", "connection_reason": "先给结果再说明机制", "hook_candidate_ids": [1], "payoff_candidate_ids": [2]},
            "purchase_cognition_path": [
                {"step_id": "P1", "chapter_id": "C1", "purchase_cognition": "大身材也可穿出显瘦比例", "purchase_question_id": "Q1", "purchase_question": _FORMAL_QUESTIONS["Q1"], "supports_question_id": "", "answer_role": "result", "answered_question": _FORMAL_QUESTIONS["Q1"], "advance_type": "new_purchase_cognition", "candidate_ids": [1], "why_it_advances": "结果先出现"},
                {"step_id": "P2", "chapter_id": "C2", "purchase_cognition": "肩线内收解释显瘦", "purchase_question_id": "Q2", "purchase_question": _FORMAL_QUESTIONS["Q2"], "supports_question_id": "Q1", "answer_role": "mechanism", "answered_question": _FORMAL_QUESTIONS["Q2"], "advance_type": "new_purchase_cognition", "candidate_ids": [2], "why_it_advances": "兑现开场"},
                {"step_id": "P3", "chapter_id": "C3", "purchase_cognition": "夏天穿也清爽", "purchase_question_id": "Q3", "purchase_question": _FORMAL_QUESTIONS["Q3"], "supports_question_id": "", "answer_role": "proof", "answered_question": _FORMAL_QUESTIONS["Q3"], "advance_type": "new_purchase_cognition", "candidate_ids": [3], "why_it_advances": "增加舒适购买理由"},
            ],
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["body_slimming"], "purchase_value_reason": "大身材显瘦"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["body_slimming"], "purchase_value_reason": "解释显瘦机制"},
                {"chapter_id": "C3", "narrative_role": "new_value", "candidate_ids": [3], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "comfort", "purchase_value_outcomes": ["cooling"], "purchase_value_reason": "夏天不闷热"},
            ],
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "只围绕同一购买故事", "no_rediscovery": True},
            "duration_assessment": {"status": "insufficient_material", "reason": "无新认知时自然结束"},
        }
        quality = _quality_response((("Q1", 1), ("Q2", 2), ("Q3", 3)))
        response = lambda payload: {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        quality_locals = quality["quality_by_question"]
        quality_order = {key: value for key, value in quality.items() if key != "quality_by_question"}
        with patch("commerce_planner_lite._post_lite_request", side_effect=[response(ranking), response(composition), *[response(row) for row in quality_locals], response(quality_order)]) as request:
            ranked, plan = plan_commerce_lite_strong_clip_llm(
                strategy=strategy, tags=tags, target_duration=60.0, safe_candidates=candidates,
                selection_contract={}, executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertEqual([item.candidate_id for item in ranked], [1, 2, 3])
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertEqual([beat.candidate_ids for beat in plan.beats], [(1,), (2,), (3,)])
        self.assertEqual(plan.status, "journey_complete")
        self.assertEqual(plan.duration_assessment["status"], "natural_complete_below_target")
        self.assertEqual(plan.duration_assessment["duration_note"], "below_preferred_target")
        self.assertFalse(any("insufficient_material" in item for item in plan.issues))
        self.assertTrue(plan.duration_assessment["commerce_purchase_cognition_path"]["passed"])
        self.assertTrue(plan.duration_assessment["commerce_purchase_journey_quality"]["m3_render_gate"]["passed"])
        self.assertEqual(request.call_args_list[0].kwargs["stage"], "M2_strong_clip_ranking")
        self.assertEqual(request.call_args_list[1].kwargs["stage"], "M2_purchase_cognition_composition")
        self.assertEqual(request.call_args_list[2].kwargs["stage"], "M2_purchase_question_local_quality")
        self.assertEqual(request.call_args_list[5].kwargs["stage"], "M2_purchase_journey_quality")

    def test_short_core_path_recalls_full_safe_pool_by_missing_purchase_question(self):
        strategy = _strategy()
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "160斤穿上也显瘦", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "肩线往里收所以显瘦", (2,)),
            PlanningCandidate(3, "SRT", 6.0, 9.0, "S到L码都能穿到160斤", (3,)),
            PlanningCandidate(4, "SRT", 9.0, 12.0, "三伏天穿也透气不粘肉", (4,)),
            PlanningCandidate(5, "SRT", 12.0, 15.0, "里面有里衬不用穿安全裤", (5,)),
            PlanningCandidate(6, "SRT", 15.0, 18.0, "配运动鞋凉鞋都不挑还能出游穿", (6,)),
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        q1 = "为什么想买？它带来什么关键结果？"
        q2 = "为什么这个结果可信、有效？"
        q3 = "我这种身材或尺码能不能穿好？"
        q4 = "夏天或长时间穿舒服吗？"
        q5 = "穿着有没有实际顾虑需要解除？"
        q6 = "日常怎么穿、怎么搭或适合什么场景？"
        q7 = "面料或品质为什么值得信任？"
        ranking = {
            "ranking_summary": "核心结果和肩线机制最强。",
            "ranked_candidates": [
                {"candidate_id": 1, "rank": 1, "standalone_strength": 9, "hook_power": 9, "purchase_value": "160斤显瘦", "purchase_outcome": "body_slimming", "purchase_question_id": "Q1", "purchase_question": q1, "supports_question_id": "", "answer_role": "result", "purchase_question_role": "result", "answered_question": q1, "evidence_function": "result", "proof_strength": 9, "redundancy_group": "body_result", "fragment": False, "visual_dependency": False, "opening_rank": 1, "opening_reason": "独立结果", "selection_reason": "强开场"},
                {"candidate_id": 2, "rank": 2, "standalone_strength": 8, "hook_power": 5, "purchase_value": "肩线机制", "purchase_outcome": "shoulder_narrowing", "purchase_question_id": "Q2", "purchase_question": q2, "supports_question_id": "Q1", "answer_role": "mechanism", "purchase_question_role": "mechanism", "answered_question": q2, "evidence_function": "mechanism", "proof_strength": 8, "redundancy_group": "shoulder_mechanism", "fragment": False, "visual_dependency": False, "opening_rank": 0, "opening_reason": "", "selection_reason": "立即兑现"},
            ],
        }
        composition = {
            "purchase_question_route": [
                {"question_id": "Q1", "question": q1, "journey_role": "core_result", "why_now": "先给结果"},
                {"question_id": "Q2", "question": q2, "journey_role": "mechanism", "why_now": "解释结果"},
            ],
            "opening_package": {"hook_promise": "160斤也显瘦", "payoff_delivery": "肩线往里收", "connection_reason": "结果后给机制", "hook_candidate_ids": [1], "payoff_candidate_ids": [2]},
            "purchase_cognition_path": [
                {"step_id": "P1", "chapter_id": "C1", "purchase_cognition": "先看到显瘦结果", "purchase_question_id": "Q1", "purchase_question": q1, "supports_question_id": "", "answer_role": "result", "answered_question": q1, "advance_type": "new_purchase_cognition", "candidate_ids": [1], "why_it_advances": "建立购买欲"},
                {"step_id": "P2", "chapter_id": "C2", "purchase_cognition": "肩线解释结果", "purchase_question_id": "Q2", "purchase_question": q2, "supports_question_id": "Q1", "answer_role": "mechanism", "answered_question": q2, "advance_type": "necessary_stronger_proof", "candidate_ids": [2], "why_it_advances": "证明 Q1"},
            ],
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body", "purchase_value_outcomes": ["body_slimming"], "purchase_value_reason": "显瘦结果"},
                {"chapter_id": "C2", "narrative_role": "proof", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "肩线机制"},
            ],
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一商品", "no_rediscovery": True},
            "duration_assessment": {"status": "journey_incomplete", "reason": "初稿只完成 Q1/Q2"},
        }
        recall = {
            "missing_purchase_questions": [],
            "recall_by_question": [
                {"purchase_question_id": "Q3", "purchase_question": q3, "supports_question_id": "", "recall_candidates": [{"candidate_id": 3, "recall_rank": 1, "answer_role": "result", "purchase_outcome": "size_inclusion", "relevance_reason": "具体尺码"}], "selected_candidate_id": 3, "selection_reason": "先解决适穿"},
                {"purchase_question_id": "Q4", "purchase_question": q4, "supports_question_id": "", "recall_candidates": [{"candidate_id": 4, "recall_rank": 1, "answer_role": "comfort", "purchase_outcome": "summer_breathability", "relevance_reason": "夏天体验"}], "selected_candidate_id": 4, "selection_reason": "增加舒适理由"},
                {"purchase_question_id": "Q5", "purchase_question": q5, "supports_question_id": "", "recall_candidates": [{"candidate_id": 5, "recall_rank": 1, "answer_role": "risk_remove", "purchase_outcome": "wearing_security", "relevance_reason": "不用安全裤"}], "selected_candidate_id": 5, "selection_reason": "解除穿着顾虑"},
                {"purchase_question_id": "Q6", "purchase_question": q6, "supports_question_id": "", "recall_candidates": [{"candidate_id": 6, "recall_rank": 1, "answer_role": "styling", "purchase_outcome": "easy_styling", "relevance_reason": "不挑鞋"}], "selected_candidate_id": 6, "selection_reason": "补充日常场景"},
                {"purchase_question_id": "Q7", "purchase_question": q7, "supports_question_id": "", "recall_candidates": [], "selected_candidate_id": 0, "selection_reason": "完整池没有可信面料内容"},
            ],
            "append_purchase_question_route": [
                {"question_id": "Q3", "question": q3, "journey_role": "fit_or_body_coverage", "why_now": "确认可穿"},
                {"question_id": "Q4", "question": q4, "journey_role": "comfort", "why_now": "解除夏季顾虑"},
                {"question_id": "Q5", "question": q5, "journey_role": "wearing_security", "why_now": "解除安全顾虑"},
                {"question_id": "Q6", "question": q6, "journey_role": "styling_or_scene", "why_now": "落到日常使用"},
            ],
            "append_purchase_cognition_path": [
                {"step_id": "P3", "chapter_id": "C3", "purchase_cognition": "尺码适配", "purchase_question_id": "Q3", "purchase_question": q3, "supports_question_id": "", "answer_role": "result", "answered_question": q3, "advance_type": "new_purchase_cognition", "candidate_ids": [3], "why_it_advances": "新问题"},
                {"step_id": "P4", "chapter_id": "C4", "purchase_cognition": "夏天舒服", "purchase_question_id": "Q4", "purchase_question": q4, "supports_question_id": "", "answer_role": "comfort", "answered_question": q4, "advance_type": "new_purchase_cognition", "candidate_ids": [4], "why_it_advances": "新问题"},
                {"step_id": "P5", "chapter_id": "C5", "purchase_cognition": "穿着放心", "purchase_question_id": "Q5", "purchase_question": q5, "supports_question_id": "", "answer_role": "risk_remove", "answered_question": q5, "advance_type": "new_purchase_cognition", "candidate_ids": [5], "why_it_advances": "新问题"},
                {"step_id": "P6", "chapter_id": "C6", "purchase_cognition": "日常好搭", "purchase_question_id": "Q6", "purchase_question": q6, "supports_question_id": "", "answer_role": "styling", "answered_question": q6, "advance_type": "new_purchase_cognition", "candidate_ids": [6], "why_it_advances": "新问题"},
            ],
            "append_chapters": [
                {"chapter_id": "C3", "narrative_role": "new_value", "candidate_ids": [3], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "fit", "purchase_value_outcomes": ["size_inclusion"], "purchase_value_reason": "尺码"},
                {"chapter_id": "C4", "narrative_role": "new_value", "candidate_ids": [4], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "comfort", "purchase_value_outcomes": ["summer_breathability"], "purchase_value_reason": "透气"},
                {"chapter_id": "C5", "narrative_role": "new_value", "candidate_ids": [5], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "security", "purchase_value_outcomes": ["wearing_security"], "purchase_value_reason": "里衬"},
                {"chapter_id": "C6", "narrative_role": "new_value", "candidate_ids": [6], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "styling", "purchase_value_outcomes": ["easy_styling"], "purchase_value_reason": "搭配"},
            ],
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一商品的新购买问题", "no_rediscovery": True},
            "journey_status": "source_material_insufficient", "stop_reason": "Q7 无真实新候选",
        }
        quality = _quality_response((("Q1", 1), ("Q2", 2), ("Q3", 3), ("Q4", 4), ("Q5", 5), ("Q6", 6)))
        response = lambda payload: {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        quality_locals = quality["quality_by_question"]
        quality_order = {key: value for key, value in quality.items() if key != "quality_by_question"}
        with patch("commerce_planner_lite._post_lite_request", side_effect=[response(ranking), response(composition), response(recall), *[response(row) for row in quality_locals], response(quality_order)]) as request:
            _, plan = plan_commerce_lite_strong_clip_llm(
                strategy=strategy, tags=tags, target_duration=60.0, safe_candidates=candidates,
                selection_contract={}, executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertEqual(plan.status, "journey_complete")
        self.assertEqual([beat.candidate_ids for beat in plan.beats], [(1,), (2,), (3,), (4,), (5,), (6,)])
        journey = plan.duration_assessment["commerce_purchase_journey"]
        self.assertEqual(journey["initial_selected_candidate_ids"], [1, 2])
        self.assertEqual(journey["final_selected_candidate_ids"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(journey["journey_status"], "journey_complete")
        self.assertTrue(journey["m3_render_gate"]["passed"])
        self.assertEqual(request.call_args_list[2].kwargs["stage"], "M2_purchase_journey_targeted_recall")
        self.assertEqual(request.call_args_list[3].kwargs["stage"], "M2_purchase_question_local_quality")
        self.assertEqual(request.call_args_list[9].kwargs["stage"], "M2_purchase_journey_quality")
        aligned, _ = align_lite_execution_metadata(
            plan=plan, strategy=strategy, safe_candidates=candidates,
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        self.assertTrue(aligned.plan_valid, aligned.issues)
        self.assertEqual(aligned.status, "journey_complete")
        self.assertEqual(aligned.duration_assessment["status"], "natural_complete_below_target")
        self.assertEqual(aligned.duration_assessment["duration_note"], "below_preferred_target")
        self.assertIn("commerce_purchase_journey", aligned.duration_assessment)

    def test_tags_keep_every_materializable_candidate_in_input_order(self):
        candidates = (
            PlanningCandidate(2, "SRT", 2.0, 4.0, "版型设计", origin_subtitle_ids=(2,)),
            PlanningCandidate(1, "SRT", 0.0, 1.0, "完整开场", origin_subtitle_ids=(1,)),
        )
        tags = build_commerce_lite_tags(
            strategy=_strategy(),
            safe_candidates=candidates,
            ledger_assets=(
                {"candidate_id": 1, "asset_role": "wearing_effect", "story_permission": "main_story"},
                {"candidate_id": 2, "asset_role": "design_explanation", "story_permission": "main_story"},
            ),
            executable_evidence={1: {"materializable": True}, 2: {"materializable": True}},
        )
        self.assertEqual([tag.candidate_id for tag in tags], [2, 1])
        self.assertEqual(tags[0].m1_tiers, ("core",))
        self.assertEqual(tags[1].m1_tiers, ())
        self.assertEqual(tags[0].m1_claims, ())
        self.assertIn("design_mechanism", tags[0].purchase_value_hints)

    def test_candidate_visual_role_is_a_hint_not_a_claimed_frame_fact(self):
        candidate = PlanningCandidate(1, "SRT", 0.0, 2.0, "你看上身后肩线更窄", origin_subtitle_ids=(1,))
        tag = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=(candidate,),
            ledger_assets=({"candidate_id": 1, "asset_role": "wearing_effect"},),
            executable_evidence={1: {"materializable": True}},
        )[0]
        self.assertIn("result_show", tag.visual_role_hints)
        self.assertEqual(tag.visual_role_provenance, "ledger_m1_text_hint_not_frame_vision")

    def test_prompt_exposes_tags_without_local_candidate_shortlist(self):
        candidates = (PlanningCandidate(7, "SRT", 0.0, 2.0, "真实证据", origin_subtitle_ids=(7,)),)
        tags = build_commerce_lite_tags(
            strategy=_strategy(), safe_candidates=candidates,
            ledger_assets=({"candidate_id": 7, "asset_role": "unknown", "story_permission": "supporting_story"},),
            executable_evidence={7: {"materializable": True}},
        )
        prompt = build_commerce_lite_ranking_prompt(
            strategy=_strategy(), tags=tags, target_duration=45.0,
            selection_contract={"m1_consumption_only": True},
        )
        self.assertIn('"candidate_id":7', prompt)
        self.assertIn('"text":"真实证据"', prompt)
        self.assertIn('"purchase_value_dimension"', prompt)
        self.assertIn('"purchase_value_domain"', prompt)
        self.assertIn('"purchase_value_outcomes"', prompt)
        self.assertIn("商业片节奏预算", prompt)
        self.assertIn("你有删除权", prompt)
        self.assertIn("购买价值增量", prompt)
        self.assertIn("不是候选白名单", prompt)
        self.assertNotIn("最高分", prompt)

    def test_lite_validator_receives_the_same_story_tier_annotations_as_m2(self):
        strategy = _strategy()
        bridge = SimpleNamespace(subtitle_ids=(1,), role="scene")
        strategy.bridge_candidates = (bridge,)
        candidates = (
            PlanningCandidate(2, "SRT", 0.0, 2.0, "明确承诺", origin_subtitle_ids=(2,)),
            PlanningCandidate(3, "SRT", 2.0, 4.0, "立即兑现", origin_subtitle_ids=(3,)),
            PlanningCandidate(1, "SRT", 4.0, 6.0, "场景延展", origin_subtitle_ids=(1,)),
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        payload = {
            "opening_package": {
                "hook_promise": "明确承诺", "payoff_delivery": "立即兑现", "connection_reason": "兑现承诺",
                "hook_candidate_ids": [2], "payoff_candidate_ids": [3],
            },
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [2], "story_support": "主线开始", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "提出肩宽显窄的核心购买理由"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [3], "story_support": "兑现主线", "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "用设计机制立即兑现肩宽显窄"},
                {"chapter_id": "C3", "narrative_role": "close", "candidate_ids": [1], "story_support": "桥接主线", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "scene", "purchase_value_outcomes": ["occasion_fit"], "purchase_value_reason": "增加可使用的场景理由"},
            ],
            "story_consumption": {
                "hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一主线",
                "supporting_chapter_ids": [], "bridge_chapter_ids": ["C3"], "no_rediscovery": True,
                "supporting_candidate_ids": [], "bridge_candidate_ids": [1],
            },
            "duration_assessment": {"status": "insufficient_material", "reason": "真实素材不足"},
        }
        response = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", return_value=response):
            plan = plan_commerce_lite_llm(
                strategy=strategy, tags=tags, target_duration=45.0, safe_candidates=candidates,
                selection_contract={"m1_consumption_validation_require_supporting_bridge": True},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertEqual(plan.selected_candidates[-1].asset_tiers, ("bridge",))
        self.assertIn("commerce_lite_story_budget", plan.duration_assessment)

        misdeclared = replace(
            plan,
            opening_package=replace(plan.opening_package, payoff_candidate_ids=(1,)),
            story_consumption=replace(plan.story_consumption, bridge_candidate_ids=()),
        )
        aligned, audit = align_lite_execution_metadata(
            plan=misdeclared, strategy=strategy, safe_candidates=candidates,
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        self.assertTrue(audit.selection_unchanged)
        self.assertEqual(audit.directing_fingerprint_before, audit.directing_fingerprint_after)
        self.assertEqual(aligned.opening_package.payoff_candidate_ids, (3,))
        self.assertEqual(aligned.story_consumption.bridge_candidate_ids, (1,))
        self.assertTrue(aligned.plan_valid)

    def test_lite_allows_distinct_outcomes_in_the_same_purchase_domain(self):
        strategy = _strategy()
        candidates = tuple(
            PlanningCandidate(index, "SRT", float(index * 2), float(index * 2 + 1.5), f"候选{index}", (index,))
            for index in range(1, 6)
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        response_payload = {
            "opening_package": {
                "hook_promise": "核心承诺", "payoff_delivery": "设计兑现", "connection_reason": "立即说明原因",
                "hook_candidate_ids": [1], "payoff_candidate_ids": [2],
            },
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "story_support": "提出承诺", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "肩宽显窄"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "story_support": "兑现承诺", "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "设计如何实现肩宽显窄"},
                {"chapter_id": "C3", "narrative_role": "benefit", "candidate_ids": [3], "story_support": "腰线效果", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["waist_definition"], "purchase_value_reason": "腰线更清楚是新的身材顾虑解决"},
                {"chapter_id": "C4", "narrative_role": "proof", "candidate_ids": [4], "story_support": "胯部包容", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["hip_coverage", "size_inclusion"], "purchase_value_reason": "腰胯有肉和不同体型可穿是新的购买理由"},
                {"chapter_id": "C5", "narrative_role": "trust", "candidate_ids": [5], "story_support": "舒适", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "comfort", "purchase_value_outcomes": ["breathability"], "purchase_value_reason": "增加透气舒适理由"},
            ],
            "duration_assessment": {"status": "insufficient_material", "reason": "真实素材不足"},
        }
        response = {"choices": [{"message": {"content": json.dumps(response_payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", return_value=response):
            plan = plan_commerce_lite_llm(
                strategy=strategy, tags=tags, target_duration=45.0, safe_candidates=candidates,
                selection_contract={},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertTrue(plan.plan_valid)

    def test_lite_rejects_a_repeated_specific_outcome_without_proof_relation(self):
        strategy = _strategy()
        candidates = tuple(
            PlanningCandidate(index, "SRT", float(index * 2), float(index * 2 + 1.5), f"候选{index}", (index,))
            for index in range(1, 5)
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        payload = {
            "opening_package": {
                "hook_promise": "核心承诺", "payoff_delivery": "设计兑现", "connection_reason": "立即说明原因",
                "hook_candidate_ids": [1], "payoff_candidate_ids": [2],
            },
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "肩宽显窄"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "立即证明肩宽显窄"},
                {"chapter_id": "C3", "narrative_role": "benefit", "candidate_ids": [3], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["hip_coverage"], "purchase_value_reason": "遮胯是新理由"},
                {"chapter_id": "C4", "narrative_role": "proof", "candidate_ids": [4], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["hip_coverage"], "purchase_value_reason": "把遮胯重复说一遍"},
            ],
            "duration_assessment": {"status": "insufficient_material", "reason": "真实素材不足"},
        }
        response = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", return_value=response):
            plan = plan_commerce_lite_llm(
                strategy=strategy, tags=tags, target_duration=45.0, safe_candidates=candidates,
                selection_contract={},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertFalse(plan.plan_valid)
        self.assertIn("commerce_lite_purchase_outcome_repeated", plan.replan_request.reason_codes)

    def test_lite_opening_duration_is_a_quality_signal_not_a_plan_veto(self):
        strategy = _strategy()
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 7.0, "完整承诺", (1,), asset_tiers=("core",)),
            PlanningCandidate(2, "SRT", 7.0, 14.5, "立即兑现", (2,)),
            PlanningCandidate(3, "SRT", 14.5, 17.0, "新的舒适理由", (3,)),
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        payload = {
            "opening_package": {
                "hook_promise": "完整承诺", "payoff_delivery": "立即兑现", "connection_reason": "承诺得到解释",
                "hook_candidate_ids": [1], "payoff_candidate_ids": [2], "hook_integrity_reason": "完整表达",
            },
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "肩宽显窄"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "立即证明肩宽显窄"},
                {"chapter_id": "C3", "narrative_role": "benefit", "candidate_ids": [3], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "comfort", "purchase_value_outcomes": ["breathability"], "purchase_value_reason": "增加透气舒适理由"},
            ],
            "duration_assessment": {"status": "insufficient_material", "reason": "真实素材不足"},
        }
        response = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", return_value=response):
            plan = plan_commerce_lite_llm(
                strategy=strategy, tags=tags, target_duration=45.0, safe_candidates=candidates,
                selection_contract={},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertIn("opening_quality_warning:actual=14.500s,warning_threshold=12.0s", plan.issues)

    def test_completion_pass_appends_only_missing_purchase_value(self):
        """Completion may add a bounded tail; it must preserve the initial story."""
        strategy = _strategy()
        candidates = tuple(
            PlanningCandidate(index, "SRT", float((index - 1) * 3), float(index * 3), f"候选{index}", (index,))
            for index in range(1, 7)
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        initial = {
            "opening_package": {
                "hook_promise": "肩宽显窄", "payoff_delivery": "设计让肩线内收", "connection_reason": "用设计解释结果",
                "hook_candidate_ids": [1], "payoff_candidate_ids": [2],
            },
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "story_support": "提出肩宽顾虑", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "肩宽显窄"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "story_support": "兑现肩线机制", "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "设计机制兑现肩宽显窄"},
                {"chapter_id": "C3", "narrative_role": "body_extension", "candidate_ids": [3], "story_support": "增加腰胯包容", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["hip_coverage"], "purchase_value_reason": "腰胯遮肉是新的身材顾虑解决"},
            ],
        }
        completion = {
            "status": "completed",
            "reason": "补足耐穿、信任和场景三个未覆盖购买环节",
            "append_chapters": [
                {"chapter_id": "C4", "narrative_role": "risk_reduction", "candidate_ids": [4], "story_support": "补足耐穿风险", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "durability", "purchase_value_outcomes": ["easy_care"], "purchase_value_reason": "好打理降低长期使用顾虑"},
                {"chapter_id": "C5", "narrative_role": "trust", "candidate_ids": [5], "story_support": "补足信任", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "trust", "purchase_value_outcomes": ["skin_friendly"], "purchase_value_reason": "贴身安全降低购买风险"},
                {"chapter_id": "C6", "narrative_role": "scene", "candidate_ids": [6], "story_support": "补足场景", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "scene", "purchase_value_outcomes": ["occasion_fit"], "purchase_value_reason": "增加明确使用场景"},
            ],
        }
        response = lambda payload: {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", side_effect=[response(initial), response(completion)]) as request:
            plan = plan_commerce_lite_llm(
                strategy=strategy, tags=tags, target_duration=60.0, safe_candidates=candidates,
                selection_contract={},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertEqual([beat.chapter_id for beat in plan.beats], ["C1", "C2", "C3", "C4", "C5", "C6"])
        self.assertEqual([beat.candidate_ids for beat in plan.beats[:3]], [(1,), (2,), (3,)])
        self.assertEqual(plan.duration_assessment["commerce_lite_completion"]["status"], "completed")
        self.assertEqual(request.call_count, 2)

    def test_draft_final_keeps_draft_non_executable_and_final_strict(self):
        strategy = _strategy()
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "肩宽显壮", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "肩线往里收", (2,)),
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        draft_payload = {
            "draft_rationale": "先完整考虑购买路径，再压缩为最终片。",
            "suggested_duration": 48,
            "buying_path": [
                {"draft_id": "D1", "purchase_journey_role": "problem", "buyer_question": "肩宽为什么显壮", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "decision_reason": "先建立停留理由", "suggested_seconds": 5, "priority": "hero", "evidence_source_ids": [1]},
                {"draft_id": "D2", "purchase_journey_role": "mechanism", "buyer_question": "为什么能显窄", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "decision_reason": "解释产品机制", "suggested_seconds": 5, "priority": "hero", "evidence_source_ids": [2]},
            ],
        }
        final_payload = {
            "opening_package": {"hook_promise": "肩宽显壮", "payoff_delivery": "肩线往里收", "connection_reason": "设计解释显窄", "hook_candidate_ids": [1], "payoff_candidate_ids": [2]},
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "提出肩宽显壮的问题"},
                {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "用版型机制兑现肩宽显窄"},
            ],
        }
        response = lambda payload: {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", side_effect=[response(draft_payload), response(final_payload)]) as request:
            draft, plan = plan_commerce_lite_draft_final_llm(
                strategy=strategy, tags=tags, target_duration=60.0, safe_candidates=candidates,
                selection_contract={},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertFalse(draft.to_dict()["executable"])
        self.assertNotIn("candidate_id", json.dumps(draft.to_dict(), ensure_ascii=False))
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertTrue(plan.selection_contract["commerce_lite_draft_final_experiment"])
        self.assertTrue(plan.duration_assessment["commerce_lite_draft_final"]["draft_grounding"]["grounded"])
        aligned, _ = align_lite_execution_metadata(
            plan=plan, strategy=strategy, safe_candidates=candidates,
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        self.assertIn("commerce_lite_draft_final", aligned.duration_assessment)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["stage"], "M2_5_commerce_lite_draft")
        self.assertEqual(request.call_args_list[1].kwargs["stage"], "M2_5_commerce_lite_draft_final")

    def test_commercial_ranking_caps_values_before_final_selection(self):
        strategy = _strategy()
        candidates = (
            PlanningCandidate(1, "SRT", 0.0, 3.0, "肩宽显壮", (1,)),
            PlanningCandidate(2, "SRT", 3.0, 6.0, "肩线往里收", (2,)),
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        draft = {"buying_path": [
            {"draft_id": "D1", "purchase_journey_role": "problem", "buyer_question": "肩宽显壮", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "decision_reason": "核心痛点", "suggested_seconds": 4, "priority": "hero", "evidence_source_ids": [1]},
            {"draft_id": "D2", "purchase_journey_role": "mechanism", "buyer_question": "为什么显窄", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "decision_reason": "机制兑现", "suggested_seconds": 4, "priority": "hero", "evidence_source_ids": [2]},
        ]}
        ranking = {"ranking_reason": "主痛点与机制必须保留", "retained_values": [{"rank": 1, "draft_ids": ["D1", "D2"], "claim": "肩宽显窄", "commercial_role": "pain_solution", "retain_reason": "决定是否购买", "evidence_source_ids": [1, 2], "proof_budget": 2}], "dropped_draft_ids": []}
        final = {"opening_package": {"hook_promise": "肩宽显壮", "payoff_delivery": "肩线往里收", "connection_reason": "机制兑现", "hook_candidate_ids": [1], "payoff_candidate_ids": [2]}, "chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [1], "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "提出核心痛点"},
            {"chapter_id": "C2", "narrative_role": "payoff", "candidate_ids": [2], "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "用机制兑现"},
        ], "proof_allocations": [{"value_rank": 1, "selected_proofs": [{"candidate_id": 1, "evidence_role": "pain"}, {"candidate_id": 2, "evidence_role": "mechanism"}], "discarded_proofs": []}]}
        response = lambda payload: {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", side_effect=[response(draft), response(ranking), response(final)]) as request:
            draft_result, ranking_result, plan = plan_commerce_lite_draft_rank_final_llm(
                strategy=strategy, tags=tags, target_duration=60.0, safe_candidates=candidates,
                selection_contract={}, executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertEqual(len(draft_result.buying_path), 2)
        self.assertEqual(len(ranking_result.retained_values), 1)
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertTrue(plan.duration_assessment["commerce_lite_draft_rank_final"]["ranking_grounding"]["grounded"])
        aligned, _ = align_lite_execution_metadata(
            plan=plan, strategy=strategy, safe_candidates=candidates,
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        self.assertIn("commerce_lite_draft_rank_final", aligned.duration_assessment)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[1].kwargs["stage"], "M2_5_commerce_lite_commercial_ranking")

    def test_chapter_compression_keeps_chapter_structure_and_enforces_saturation(self):
        strategy = _strategy()
        candidates = tuple(
            PlanningCandidate(index, "SRT", float((index - 1) * 3), float(index * 3), f"候选{index}", (index,))
            for index in range(1, 6)
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        current = NarrativePlan(
            strategy_id="S1", thesis="同一商业故事", target_duration=60.0,
            beats=(
                NarrativeBeat("", "hook", "提出痛点", (1,), True, 0.0, "", chapter_id="C1", purchase_value_dimension="new_outcome", purchase_value_domain="body_appearance", purchase_value_outcomes=("shoulder_narrowing",), purchase_value_reason="宽肩焦虑"),
                NarrativeBeat("", "mechanism", "解释机制", (2, 3, 4), True, 0.0, "", chapter_id="C2", purchase_value_dimension="same_claim_additional_proof", purchase_value_domain="body_appearance", purchase_value_outcomes=("shoulder_narrowing",), purchase_value_reason="机制证明"),
                NarrativeBeat("", "visible_result", "展示结果", (5,), True, 0.0, "", chapter_id="C3", purchase_value_dimension="new_outcome", purchase_value_domain="body_appearance", purchase_value_outcomes=("body_slimming",), purchase_value_reason="可见结果"),
            ),
            status="insufficient_material", recommended_duration=0.0, issues=(), removed_beats=(), plan_valid=True,
            story_brief=__import__("story_planner").CommercialStoryBrief.from_strategy(strategy),
        )
        response_payload = {
            "opening_package": {"hook_candidate_ids": [1], "payoff_candidate_ids": [2, 3], "hook_promise": "宽肩显壮", "payoff_delivery": "肩线内收", "connection_reason": "机制兑现"},
            "chapters": [
                {"chapter_id": "C1", "narrative_role": "hook", "goal": "提出痛点", "candidate_ids": [1], "asset_tier": "core", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "宽肩焦虑"},
                {"chapter_id": "C2", "narrative_role": "mechanism", "goal": "解释机制", "candidate_ids": [2, 3], "asset_tier": "core", "purchase_value_dimension": "same_claim_additional_proof", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["shoulder_narrowing"], "purchase_value_reason": "两句完成机制"},
                {"chapter_id": "C3", "narrative_role": "visible_result", "goal": "展示结果", "candidate_ids": [5], "asset_tier": "supporting", "purchase_value_dimension": "new_outcome", "purchase_value_domain": "body_appearance", "purchase_value_outcomes": ["body_slimming"], "purchase_value_reason": "可见结果"},
            ],
            "story_consumption": {"hero_strategy_id": "S1", "hero_priority": "high", "hero_consistency_reason": "同一故事", "no_rediscovery": True},
            "duration_assessment": {"status": "insufficient_material", "reason": "真实素材不足"},
        }
        response = {"choices": [{"message": {"content": json.dumps(response_payload, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", return_value=response) as request:
            plan = plan_commerce_lite_chapter_compression_llm(
                strategy=strategy, current_plan=current, tags=tags, target_duration=60.0,
                safe_candidates=candidates, selection_contract={},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertTrue(plan.plan_valid, plan.issues)
        self.assertEqual([beat.chapter_id for beat in plan.beats], ["C1", "C2", "C3"])
        self.assertEqual([beat.candidate_ids for beat in plan.beats], [(1,), (2, 3), (5,)])
        self.assertIn("commerce_lite_chapter_compression", plan.duration_assessment)
        self.assertIn("commerce_lite_chapter_saturation", plan.duration_assessment)
        self.assertEqual(request.call_args.kwargs["stage"], "M2_4_chapter_compression")

    def test_final_editor_applies_only_explicit_operations_and_measures_real_duration(self):
        strategy = _strategy()
        candidates = tuple(
            PlanningCandidate(index, "SRT", float((index - 1) * 3), float(index * 3), f"候选{index}", (index,))
            for index in range(1, 6)
        )
        tags = build_commerce_lite_tags(
            strategy=strategy, safe_candidates=candidates,
            ledger_assets=tuple({"candidate_id": item.candidate_id} for item in candidates),
            executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
        )
        current = NarrativePlan(
            strategy_id="S1", thesis="同一商业故事", target_duration=60.0,
            beats=(
                NarrativeBeat("", "hook", "提出痛点", (1,), True, 0.0, "", chapter_id="C1", purchase_value_dimension="new_outcome", purchase_value_domain="body_appearance", purchase_value_outcomes=("shoulder_narrowing",), purchase_value_reason="肩宽焦虑"),
                NarrativeBeat("", "mechanism", "解释机制", (2, 3), True, 0.0, "", chapter_id="C2", purchase_value_dimension="same_claim_additional_proof", purchase_value_domain="body_appearance", purchase_value_outcomes=("shoulder_narrowing",), purchase_value_reason="机制证明"),
                NarrativeBeat("", "visible_result", "展示结果", (4,), True, 0.0, "", chapter_id="C3", purchase_value_dimension="new_outcome", purchase_value_domain="body_appearance", purchase_value_outcomes=("body_slimming",), purchase_value_reason="可见结果"),
            ),
            status="insufficient_material", recommended_duration=0.0, issues=(), removed_beats=(), plan_valid=True,
            story_brief=__import__("story_planner").CommercialStoryBrief.from_strategy(strategy),
            opening_package=OpeningPackage(
                promise="肩宽显窄", payoff_relation="机制兑现", hook_candidate_ids=(1,), payoff_candidate_ids=(2, 3),
                hook_promise="肩宽显窄", payoff_delivery="肩线内收", connection_reason="机制兑现",
            ),
        )
        editor = {
            "status": "edited",
            "reason": "删弱证明，再补穿着安全",
            "operations": [
                {"op": "remove_candidate", "chapter_id": "C2", "candidate_id": 3, "reason": "weaker_proof"},
                {
                    "op": "append_chapter", "narrative_role": "risk_reduction", "goal": "补足穿着安全",
                    "candidate_ids": [5], "asset_tier": "supporting", "story_support": "解决日常穿着安全顾虑",
                    "commerce_beat_id": "risk_removal", "value_dimension": "wearing_security",
                    "purchase_value_dimension": "new_outcome", "purchase_value_domain": "wearing_security",
                    "purchase_value_outcomes": ["no_underlayer_needed"], "purchase_value_reason": "不必另穿打底降低穿着风险",
                    "reason": "missing_purchase_journey",
                },
            ],
        }
        response = {"choices": [{"message": {"content": json.dumps(editor, ensure_ascii=False)}}]}
        with patch("commerce_planner_lite._post_lite_request", return_value=response) as request:
            plan = plan_commerce_lite_final_editor_llm(
                strategy=strategy, current_plan=current, tags=tags, target_duration=60.0,
                safe_candidates=candidates, selection_contract={},
                executable_evidence={item.candidate_id: {"materializable": True} for item in candidates},
                api_key="test", base_url="https://example.invalid", model="test-model",
            )
        self.assertEqual([beat.candidate_ids for beat in plan.beats], [(1,), (2,), (4,), (5,)])
        audit = plan.duration_assessment["commerce_lite_final_editor"]
        self.assertTrue(audit["program_measures_duration"])
        self.assertEqual(audit["source_seconds"], 12.0)
        self.assertEqual(audit["final_seconds"], 12.0)
        self.assertEqual(request.call_args.kwargs["stage"], "M2_final_editor_operations")

    def test_story_budget_rejects_auxiliary_padding_without_mutating_the_plan(self):
        candidates = tuple(
            PlanningCandidate(
                index,
                "SRT",
                float((index - 1) * 3),
                float(index * 3),
                f"候选{index}",
                (index,),
                asset_tiers=("core",) if index <= 3 else (),
            )
            for index in range(1, 9)
        )
        domains = (
            ("body_appearance", "shoulder_narrowing"),
            ("body_appearance", "right_angle_shoulder"),
            ("body_appearance", "body_slimming"),
            ("comfort", "breathability"),
            ("trust", "skin_friendly"),
            ("size_inclusion", "size_inclusion"),
            ("styling", "easy_matching"),
            ("durability", "easy_care"),
        )
        beats = tuple(
            NarrativeBeat(
                source_role="",
                narrative_role="hook" if index == 1 else "payoff" if index == 2 else "supporting",
                goal="预算测试",
                candidate_evidence=(index,),
                required=True,
                target_seconds=0.0,
                selection_instruction="",
                chapter_id=f"C{index}",
                purchase_value_dimension="new_outcome",
                purchase_value_domain=domains[index - 1][0],
                purchase_value_outcomes=(domains[index - 1][1],),
                purchase_value_reason="新的购买理由",
            )
            for index in range(1, 9)
        )
        plan = NarrativePlan(
            strategy_id="S1",
            thesis="预算测试",
            target_duration=60.0,
            beats=beats,
            status="ok",
            recommended_duration=0.0,
            issues=(),
            removed_beats=(),
            plan_valid=True,
            opening_package=OpeningPackage(
                promise="承诺", payoff_relation="兑现", hook_candidate_ids=(1,), payoff_candidate_ids=(2,),
                hook_promise="承诺", payoff_delivery="兑现", connection_reason="承诺得到兑现",
            ),
            selection_contract={
                "commerce_lite_purchase_value_progression": True,
                "commerce_lite_story_budget": True,
            },
        )
        validated = validate_narrative_plan(plan, candidates)
        self.assertFalse(validated.plan_valid)
        self.assertIn("commerce_lite_story_budget_chapter_count_exceeded", validated.replan_request.reason_codes)
        self.assertEqual(
            [beat.candidate_ids for beat in validated.beats],
            [beat.candidate_ids for beat in beats],
        )
        budget = validated.duration_assessment["commerce_lite_story_budget"]
        self.assertEqual(budget["contract"], commerce_lite_story_budget(60.0))

    def test_narrative_mode_directs_before_casting_from_full_p05_beat_pool(self):
        candidates = (
            PlanningCandidate(950000001, "P05_MICRO_BEAT:B001", 0.0, 3.0, "穿上正面整个人看起来很窄", (11,), True),
            PlanningCandidate(950000002, "P05_MICRO_BEAT:B002", 3.0, 6.0, "这根花边会把肩往里收", (12,), False, ("product",)),
        )
        beat_candidates = [
            {
                "candidate_id": candidate.candidate_id,
                "beat_id": f"B{index:03d}",
                "duration_seconds": candidate.duration,
                "text": candidate.text,
                "publishability_status": "publishable_clean",
                "visual_dependency": "none",
                "audio_only_eligible": bool(candidate.hook_eligible),
                "hook_eligible": bool(candidate.hook_eligible),
                "narrative_priority": "high",
                "purchase_value": "显瘦" if index == 1 else "肩部收窄机制",
                "sub_outcome": "整体显窄" if index == 1 else "肩线内收",
                "evidence_function": "result" if index == 1 else "mechanism",
                "planning_candidate": candidate,
            }
            for index, candidate in enumerate(candidates, 1)
        ]
        responses = {
            "M2_narrative_mode_journey": {
                "core_desire": "大身材也想穿得显瘦轻松",
                "opening_promise": "先给出显瘦结果",
                "purchase_journey": [
                    {"step_id": "J1", "purchase_question_id": "Q1", "purchase_question": "为什么想买？它带来什么关键结果？", "coverage": "required", "answer_role_intent": "result", "goal": "先证明显瘦结果", "why_now": "先建立购买欲望"},
                    {"step_id": "J2", "purchase_question_id": "Q2", "purchase_question": "为什么这个结果可信、有效？", "coverage": "required", "answer_role_intent": "mechanism", "goal": "解释肩线机制", "why_now": "立即兑现开头"},
                ],
                "stop_intent": "核心链完成自然结束", "reason": "只保留强购买推进",
            },
            "M2_narrative_mode_beat_casting": {
                "casts": [
                    {"step_id": "J1", "purchase_question_id": "Q1", "decision": "cast", "candidate_ids": [950000001], "answer_role": "result", "supports_question_id": "", "purchase_outcome": "整体显窄", "why_it_advances": "先给可见结果", "transition_from_previous": ""},
                    {"step_id": "J2", "purchase_question_id": "Q2", "decision": "cast", "candidate_ids": [950000002], "answer_role": "mechanism", "supports_question_id": "Q1", "purchase_outcome": "肩线内收", "why_it_advances": "解释结果来源", "transition_from_previous": "结果立即兑现"},
                ],
                "opening_package": {"hook_candidate_id": 950000001, "payoff_candidate_ids": [950000002], "promise": "显瘦结果", "payoff_relation": "肩线机制解释结果", "connection_reason": "结果后立刻说明原因", "hook_integrity_reason": ""},
                "reason": "每步只选一条真实 Beat",
            },
            "M2_narrative_mode_whole_video_audit": {
                "status": "pass", "opening_quality": "pass", "first_10_second_progression_count": 2,
                "every_step_new_purchase_value": True, "story_focus": "pass", "logic_flow": "pass",
                "natural_ending": "pass", "m3_ready": True, "reason": "结果到机制连读自然", "issues": [],
            },
        }

        def fake_request(**kwargs):
            return {"choices": [{"message": {"content": json.dumps(responses[kwargs["stage"]], ensure_ascii=False)}}]}

        def opening_package_provider(_journey):
            # The provider represents the already-AI-approved P0.5A.4
            # catalog. It exposes alternatives but does not choose one; the
            # Beat Casting response remains the semantic selector.
            return {
                "beat_candidates": beat_candidates,
                "safe_candidates": candidates,
                "approved_opening_packages": [{
                    "opening_id": "O001", "hook_candidate_id": 950000001,
                    "payoff_candidate_ids": [950000002], "quality": "medium",
                }],
                "audit": {"approved_package_count": 1},
            }

        with patch("commerce_planner_lite._post_lite_request", side_effect=fake_request) as request:
            audit, plan = plan_commerce_lite_narrative_mode_llm(
                strategy=_strategy(), beat_candidates=beat_candidates, target_duration=30.0,
                safe_candidates=candidates, selection_contract={}, api_key="k", base_url="u", model="m",
                opening_package_provider=opening_package_provider,
            )
        self.assertTrue(plan.plan_valid)
        self.assertEqual([item.candidate_id for item in plan.selected_candidates], [950000001, 950000002])
        self.assertFalse(audit["strong_clip_ranking_used_before_journey"])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(audit["whole_video_audit"]["status"], "pass")
        self.assertEqual(audit["p0_5a4_opening_package"]["approved_package_count"], 1)

    def test_narrative_mode_rejects_a_self_invented_opening_pair_outside_p05a4_catalog(self):
        candidates = (
            PlanningCandidate(950000001, "P05_MICRO_BEAT:H001", 0.0, 3.0, "穿上以后整个人看起来很窄", (11,), True),
            PlanningCandidate(950000002, "P05_MICRO_BEAT:B001", 3.0, 6.0, "黑色花边会把肩往里压", (12,), False, ("product",)),
            PlanningCandidate(950000003, "P05_MICRO_BEAT:B002", 6.0, 9.0, "腰胯也会显得更收", (13,), False, ("product",)),
        )
        beat_candidates = [{
            "candidate_id": candidate.candidate_id, "beat_id": candidate.source_id.rsplit(":", 1)[-1],
            "duration_seconds": candidate.duration, "text": candidate.text,
            "publishability_status": "publishable_clean", "visual_dependency": "none",
            "audio_only_eligible": bool(candidate.hook_eligible), "hook_eligible": bool(candidate.hook_eligible),
            "narrative_priority": "high", "purchase_value": "显瘦", "sub_outcome": "显窄",
            "evidence_function": "result" if candidate.hook_eligible else "mechanism",
            "planning_candidate": candidate,
        } for candidate in candidates]
        responses = {
            "M2_narrative_mode_journey": {
                "core_desire": "显瘦", "opening_promise": "先给显瘦结果",
                "purchase_journey": [
                    {"step_id": "J1", "purchase_question_id": "Q1", "coverage": "required", "answer_role_intent": "result", "goal": "结果", "why_now": "开头"},
                    {"step_id": "J2", "purchase_question_id": "Q2", "coverage": "required", "answer_role_intent": "mechanism", "goal": "机制", "why_now": "兑现"},
                ], "stop_intent": "完成", "reason": "只走核心链",
            },
            "M2_narrative_mode_beat_casting": {
                "casts": [
                    {"step_id": "J1", "purchase_question_id": "Q1", "decision": "cast", "candidate_ids": [950000001], "answer_role": "result", "supports_question_id": "", "purchase_outcome": "显窄", "why_it_advances": "结果", "transition_from_previous": ""},
                    {"step_id": "J2", "purchase_question_id": "Q2", "decision": "cast", "candidate_ids": [950000003], "answer_role": "mechanism", "supports_question_id": "Q1", "purchase_outcome": "腰胯", "why_it_advances": "机制", "transition_from_previous": "兑现"},
                ],
                "opening_package": {"hook_candidate_id": 950000001, "payoff_candidate_ids": [950000003], "promise": "显瘦", "payoff_relation": "机制", "connection_reason": "承接", "hook_integrity_reason": ""},
                "reason": "故意选择目录外配对",
            },
        }

        def fake_request(**kwargs):
            return {"choices": [{"message": {"content": json.dumps(responses[kwargs["stage"]], ensure_ascii=False)}}]}

        with patch("commerce_planner_lite._post_lite_request", side_effect=fake_request):
            audit, plan = plan_commerce_lite_narrative_mode_llm(
                strategy=_strategy(), beat_candidates=beat_candidates, target_duration=30.0,
                safe_candidates=candidates, selection_contract={}, api_key="k", base_url="u", model="m",
                opening_package_provider=lambda _journey: {
                    "beat_candidates": beat_candidates, "safe_candidates": candidates,
                    "approved_opening_packages": [{
                        "opening_id": "O001", "hook_candidate_id": 950000001,
                        "payoff_candidate_ids": [950000002], "quality": "medium",
                    }],
                },
            )
        self.assertFalse(plan.plan_valid)
        self.assertIn("p0_5a4_opening_package_not_selected", plan.issues)
        self.assertEqual(audit["whole_video_audit"]["status"], "not_run_beat_casting_contract_invalid")


if __name__ == "__main__":
    unittest.main()
