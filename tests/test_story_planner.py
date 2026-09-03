import os
import sys
import unittest
from dataclasses import replace
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commercial_analyzer import Strategy
from story_planner import (
    CommercialStoryBrief,
    DURATION_DEPTH_CHAPTERED_STORY,
    DURATION_DEPTH_CORE_DENSE,
    DURATION_DEPTH_STORY_WITH_SUPPORT,
    DURATION_STATUS_FEASIBLE,
    DURATION_STATUS_INSUFFICIENT_FOR_TARGET,
    DURATION_STATUS_UNREPORTED,
    DEPTH_EXPANSION_EXPANDED,
    DEPTH_EXPANSION_INSUFFICIENT_DISTINCT_VALUE,
    EXPANSION_SCOUT_FOUND,
    NarrativeBeat,
    NarrativePlan,
    OpeningPackage,
    StoryConsumption,
    DepthExpansionContract,
    DepthExpansionValue,
    DurationExpansionAsset,
    DurationExpansionScout,
    PLANNER_SYSTEM_PROMPT,
    PlanningCandidate,
    bind_story_assets,
    build_executable_evidence_view,
    audit_story_consumption,
    build_planner_prompt,
    build_opening_quality_review_prompt,
    build_replan_prompt,
    build_duration_refinement_prompt,
    build_duration_expansion_scout_prompt,
    discover_duration_expansion_assets_llm,
    duration_plan_needs_refinement,
    finalize_duration_budget_after_retry,
    _duration_depth_mode,
    _duration_expansion_budget_options,
    _parse_duration_plan,
    _format_supporting_arc_recall_index,
    _prompt_candidate_groups,
    _supporting_arc_recall_ids,
    plan_narrative_llm,
    refine_duration_narrative_llm,
    replan_narrative_llm,
    review_opening_quality_llm,
    validate_narrative_plan,
)


def _strategy() -> Strategy:
    return Strategy.from_dict(
        {
            "strategy_id": "S1",
            "type": "identity_style",
            "strategy_family": "style",
            "sub_angle": "standalone_shape",
            "story_priority": "high",
            "thesis": "这不是职业衬衫，而是单穿就成立的有型衬衫",
            "story_premise": "普通衬衫容易板正，这件靠结构让单穿也有型。",
            "audience_tension": "想穿衬衫又不想显得职业和死板。",
            "story_trigger": "主播把普通衬衫与这件的版型状态作对比。",
            "transformation": "职业板正到单穿有型、利落松弛。",
            "product_role": "用结构给日常穿搭增加造型感。",
            "core_commercial_idea": "简单衬衫也不用叠搭就能穿出状态。",
            "payoff": "观众立刻理解这件为什么能单穿成立。",
            "supporting_arcs": ["面料筋骨", "早秋场景"],
            "core_evidence_pool": [
                {"role": "hook", "claim": "普通衬衫容易职业", "subtitle_ids": [10]},
                {"role": "proof", "claim": "结构让单穿有型", "subtitle_ids": [11]},
            ],
            "supporting_evidence_pool": [
                {"role": "proof", "claim": "面料有筋骨", "subtitle_ids": [12]},
            ],
            "bridge_candidates": [
                {"role": "scene", "claim": "早秋可单穿", "subtitle_ids": [13]},
            ],
        },
        1,
    )


def _safe_candidates(*, hook_eligible: bool = True) -> tuple[PlanningCandidate, ...]:
    return (
        PlanningCandidate(101, "V1", 0.0, 3.0, "普通衬衫一穿就很职业", (10,), hook_eligible),
        PlanningCandidate(102, "V1", 3.0, 9.0, "这件靠肩线和捏褶单穿就有型", (11,)),
        PlanningCandidate(103, "V1", 9.0, 16.0, "面料有筋骨所以不软塌", (12,)),
        PlanningCandidate(104, "V1", 16.0, 24.0, "搭一条牛仔裤也不会像上班穿的", (13,)),
        # Not mentioned by M1. It must remain visible and selectable as safe reserve.
        PlanningCandidate(105, "V1", 24.0, 31.0, "颜色上身很有精神", (99,)),
    )


def _opening() -> OpeningPackage:
    return OpeningPackage(
        promise="不想穿衬衫太职业的人，看这个单穿状态。",
        payoff_relation="第二段用肩线和捏褶马上解释为什么单穿有型。",
        hook_candidate_ids=(101,),
        payoff_candidate_ids=(102,),
    )


def _beat(
    chapter_id: str,
    role: str,
    candidate_ids: tuple[int, ...],
    *,
    target_seconds: float = 1.0,
    required: bool = True,
    value_dimension: str = "",
) -> NarrativeBeat:
    return NarrativeBeat(
        source_role="proof",
        narrative_role=role,
        goal="推进故事",
        candidate_evidence=candidate_ids,
        required=required,
        target_seconds=target_seconds,
        selection_instruction="使用真实候选",
        chapter_id=chapter_id,
        value_dimension=value_dimension,
    )


def _plan(beats: tuple[NarrativeBeat, ...], *, opening: OpeningPackage | None = None, target: float = 16.0) -> NarrativePlan:
    return NarrativePlan(
        "S1", "单穿有型", target, beats, "ok", 0.0, (), (), True,
        story_brief=CommercialStoryBrief.from_strategy(_strategy()),
        opening_package=_opening() if opening is None else opening,
        selection_contract={"category": "上衣", "price": "forbid"},
    )


class StoryBriefAndPoolTests(unittest.TestCase):
    def test_duration_depth_modes_follow_story_depth_not_a_filler_quota(self) -> None:
        self.assertEqual(_duration_depth_mode(30.0), DURATION_DEPTH_CORE_DENSE)
        self.assertEqual(_duration_depth_mode(45.0), DURATION_DEPTH_CORE_DENSE)
        self.assertEqual(_duration_depth_mode(60.0), DURATION_DEPTH_STORY_WITH_SUPPORT)
        self.assertEqual(_duration_depth_mode(90.0), DURATION_DEPTH_CHAPTERED_STORY)
        self.assertEqual(_duration_depth_mode(120.0), DURATION_DEPTH_CHAPTERED_STORY)
        prompt = build_planner_prompt(_strategy(), 90.0, _safe_candidates(), {"category": "上衣"})
        self.assertIn("本次叙事深度: chaptered_story", prompt)
        self.assertIn("二到四个 Supporting 或 Bridge Arc", prompt)
        self.assertIn("Duration-Aware Planning", prompt)
        self.assertIn("Depth Expansion Contract", prompt)
        self.assertIn("长版不是证明更多，而是购买理由更完整", prompt)

    def test_invalid_director_duration_plan_is_auditable_not_silently_accepted(self) -> None:
        budget = _parse_duration_plan(
            {
                "target_duration": 120,
                "feasible_duration_range": {"min_seconds": 90, "max_seconds": 70},
                "recommended_duration": 80,
                "duration_status": "feasible",
                "depth_mode": "chaptered_story",
            },
            target_duration=60.0,
        )
        self.assertFalse(budget.reported)
        self.assertEqual(budget.duration_status, DURATION_STATUS_UNREPORTED)
        self.assertIn("planner_report", budget.to_dict())

    def test_declared_feasible_budget_must_match_real_selected_candidate_duration(self) -> None:
        plan = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,))), target=30.0),
            _safe_candidates(),
        )
        feasible = _parse_duration_plan(
            {
                "target_duration": 30,
                "feasible_duration_range": {"min_seconds": 25, "max_seconds": 36},
                "recommended_duration": 30,
                "duration_status": "feasible",
                "depth_mode": "core_dense",
                "reason": "误报可满时长。",
            },
            target_duration=30.0,
        )
        audited = validate_narrative_plan(replace(plan, duration_plan=feasible), _safe_candidates())
        self.assertTrue(duration_plan_needs_refinement(audited))
        self.assertTrue(audited.duration_assessment["duration_refinement_recommended"])
        self.assertTrue(any("duration_budget_unfulfilled" in issue for issue in audited.issues))
        finalized = finalize_duration_budget_after_retry(audited)
        self.assertEqual(finalized.duration_plan.duration_status, "insufficient_for_target")
        self.assertEqual(finalized.duration_plan.recommended_duration, 9.0)
        self.assertFalse(finalized.duration_plan.reported)
        self.assertEqual(finalized.beats, audited.beats)

    def test_prompt_exposes_full_brief_and_full_safe_pool(self) -> None:
        strategy = _strategy()
        prompt = build_planner_prompt(strategy, 40.0, _safe_candidates(), {"category": "上衣"})
        self.assertIn("想穿衬衫又不想显得职业和死板", prompt)
        self.assertIn("职业板正到单穿有型", prompt)
        self.assertIn("简单衬衫也不用叠搭", prompt)
        self.assertIn("id=105 tier=safe_reserve", prompt)
        self.assertIn("tier 是优先级，不是准入限制", prompt)
        self.assertIn("优先实际时长区间: 31.2-48.0 秒（软合同）", prompt)
        self.assertIn("提交前的确定性自检", prompt)
        self.assertLess(prompt.index("id=101 tier=core"), prompt.index("id=105 tier=safe_reserve"))
        self.assertIn("开场优先索引", prompt)
        self.assertIn("没有合格收尾可自然结束", PLANNER_SYSTEM_PROMPT)
        self.assertIn("明显残句、无意义重复", PLANNER_SYSTEM_PROMPT)
        self.assertIn("章节衔接检查", prompt)
        self.assertIn("共同支点", prompt)
        self.assertIn("足够兑现", prompt)
        self.assertIn("信息增量检查", prompt)
        self.assertIn("辅助弧线召回", prompt)
        self.assertIn("不要放弃这些候选后又报素材不足", prompt)
        self.assertIn("Hook 自足检查", prompt)
        self.assertIn("Hook 时长合同", prompt)
        self.assertIn("Bridge 不改变商业推进顺序", prompt)
        self.assertIn("Supporting Arc Recall Index", prompt)
        self.assertIn("transition_from_previous", PLANNER_SYSTEM_PROMPT)
        self.assertIn("story_priority=high", prompt)
        self.assertIn("Story Consumption Contract", prompt)

    def test_supporting_arc_recall_index_is_visible_but_not_a_candidate_filter(self) -> None:
        brief = CommercialStoryBrief.from_strategy(_strategy())
        index = _format_supporting_arc_recall_index(brief, _safe_candidates())

        self.assertIn('arc="面料筋骨"', index)
        self.assertIn("id=103", index)
        extra = PlanningCandidate(106, "V1", 31.0, 34.0, "颜色上身很有精神", (100,))
        self.assertNotIn("颜色上身很有精神", _format_supporting_arc_recall_index(brief, _safe_candidates() + (extra,)))
        prompt = build_planner_prompt(_strategy(), 40.0, _safe_candidates() + (extra,))
        self.assertIn("id=106 tier=safe_reserve", prompt)

    def test_supporting_arc_recall_is_prominent_without_hiding_remaining_reserve(self) -> None:
        extra = PlanningCandidate(106, "V1", 31.0, 34.0, "颜色上身很有精神", (100,))
        strategy = replace(_strategy(), supporting_arcs=("颜色上身",))
        brief = CommercialStoryBrief.from_strategy(strategy)
        candidates = _safe_candidates() + (extra,)
        recalled = _supporting_arc_recall_ids(brief, candidates)
        groups = _prompt_candidate_groups(candidates, supporting_recall_ids=recalled)

        self.assertIn(106, recalled)
        self.assertEqual(sum(candidate.candidate_id == 106 for _title, members in groups for candidate in members), 1)
        self.assertTrue(any(title.startswith("M1 Supporting Arc recall") for title, _members in groups))
        self.assertTrue(any(candidate.candidate_id == 105 for _title, members in groups for candidate in members))

    def test_binding_marks_assets_without_removing_safe_reserve(self) -> None:
        bound = bind_story_assets(CommercialStoryBrief.from_strategy(_strategy()), _safe_candidates())
        by_id = {item.candidate_id: item for item in bound}
        self.assertEqual(len(bound), 5)
        self.assertEqual(by_id[101].asset_tiers, ("core",))
        self.assertEqual(by_id[102].asset_tiers, ("core",))
        self.assertEqual(by_id[103].asset_tiers, ("supporting",))
        self.assertEqual(by_id[104].asset_tiers, ("bridge",))
        self.assertEqual(by_id[105].asset_tiers, ())

    def test_executable_evidence_view_keeps_m1_asset_visible_but_marks_it_unusable(self) -> None:
        brief = CommercialStoryBrief.from_strategy(_strategy())
        view = build_executable_evidence_view(
            brief,
            tuple(item for item in _safe_candidates() if item.candidate_id != 104),
            {104: {
                "materializable": False,
                "materialization_issue": "word_boundary_residual",
                "origin_subtitle_ids": [13],
            }},
        )
        bridge = next(item for item in view if item.candidate_id == 104)
        self.assertEqual(bridge.commercial_role, "scene")
        self.assertEqual(bridge.story_tier, "bridge")
        self.assertFalse(bridge.materializable)
        self.assertEqual(bridge.materialization_issue, "word_boundary_residual")

    def test_long_complete_candidate_stays_in_pool_but_loses_hook_role(self) -> None:
        long_candidate = PlanningCandidate(106, "V1", 31.0, 39.1, "完整但较长的版型解释", (10,))
        bound = bind_story_assets(CommercialStoryBrief.from_strategy(_strategy()), (long_candidate,))
        self.assertEqual(len(bound), 1)
        self.assertEqual(bound[0].asset_tiers, ("core",))
        self.assertFalse(bound[0].hook_eligible)


class ValidatorTests(unittest.TestCase):
    def test_consumption_audit_requires_hero_and_declared_supporting_bridge(self) -> None:
        beats = (
            replace(_beat("C1", "hook", (101,)), story_support="提出不想职业的主矛盾"),
            replace(_beat("C2", "development", (102,)), story_support="用版型立即兑现单穿有型"),
            replace(_beat("C3", "proof", (103,)), story_support="面料筋骨让单穿状态持续成立"),
            replace(_beat("C4", "scene", (104,)), story_support="把单穿状态连接到早秋日常搭配"),
        )
        plan = validate_narrative_plan(
            replace(
                _plan(beats),
                story_consumption=StoryConsumption(
                    hero_strategy_id="S1",
                    hero_priority="high",
                    hero_consistency_reason="从职业感顾虑到版型兑现，再以面料与场景延展单穿成立。",
                    supporting_chapter_ids=("C3",),
                    bridge_chapter_ids=("C4",),
                    no_rediscovery=True,
                    supporting_candidate_ids=(103,),
                    bridge_candidate_ids=(104,),
                ),
            ),
            _safe_candidates(),
        )
        audit = audit_story_consumption(plan, _strategy(), _safe_candidates())
        self.assertTrue(audit["passed"], audit)
        self.assertEqual(audit["selected_asset_tiers"]["supporting"], [103])
        self.assertEqual(audit["selected_asset_tiers"]["bridge"], [104])

    def test_consumption_audit_flags_unapproved_high_theme_without_repairing_plan(self) -> None:
        plan = replace(
            _plan((
                replace(_beat("C1", "hook", (101,)), story_support="提出主故事"),
                replace(_beat("C2", "development", (102,)), story_support="兑现主故事"),
                replace(_beat("C3", "proof", (103,)), story_support="支持主故事"),
                replace(_beat("C4", "scene", (104,)), story_support="连接主故事"),
            )),
            story_consumption=StoryConsumption(
                hero_strategy_id="S1", hero_priority="high", hero_consistency_reason="价格更划算",
                supporting_chapter_ids=("C3",), bridge_chapter_ids=("C4",), no_rediscovery=True,
                supporting_candidate_ids=(103,), bridge_candidate_ids=(104,),
            ),
        )
        audit = audit_story_consumption(plan, _strategy(), _safe_candidates())
        self.assertFalse(audit["passed"])
        self.assertIn("unapproved_theme_markers=价格", audit["issues"])

    def test_director_final_story_audit_ignores_internal_cta_word_when_source_utterances_are_clean(self) -> None:
        beats = (
            replace(_beat("C1", "hook", (101,)), story_support="建立承诺"),
            replace(_beat("C2", "development", (102,)), story_support="兑现承诺"),
        )
        plan = replace(
            _plan(beats),
            selection_contract={
                "final_story_brief": {"authority": "director_narrative_contract"},
                "director_narrative_contract": {"authority": "director_strategy_contract"},
            },
            story_consumption=StoryConsumption(
                hero_strategy_id="S1", hero_priority="high", hero_consistency_reason="通过风险解除促进下单",
                supporting_chapter_ids=(), bridge_chapter_ids=(), no_rediscovery=True,
            ),
        )
        audit = audit_story_consumption(plan, _strategy(), _safe_candidates())
        self.assertTrue(audit["passed"], audit)
        self.assertEqual(audit["intent_authority"], "director_narrative_contract")
        self.assertEqual(audit["unapproved_theme_markers"], [])

    def test_safe_reserve_is_legal_not_an_evidence_boundary_failure(self) -> None:
        plan = _plan((
            _beat("C1", "hook", (101,)),
            _beat("C2", "development", (102,)),
            _beat("C3", "close", (105,)),
        ))
        result = validate_narrative_plan(plan, _safe_candidates())
        self.assertTrue(result.plan_valid)
        self.assertFalse(any("out_of_strategy" in issue for issue in result.issues))
        self.assertEqual([item.candidate_id for item in result.selected_candidates], [101, 102, 105])

    def test_actual_candidate_duration_overrides_llm_target_seconds(self) -> None:
        plan = _plan((
            _beat("C1", "hook", (101,), target_seconds=99.0),
            _beat("C2", "development", (102,), target_seconds=99.0),
            _beat("C3", "proof", (103,), target_seconds=99.0),
        ), target=16.0)
        result = validate_narrative_plan(plan, _safe_candidates())
        self.assertEqual(result.total_seconds, 16.0)
        self.assertEqual(result.recommended_duration, 16.0)

    def test_unknown_candidate_returns_replan_without_story_rewrite(self) -> None:
        beats = (
            _beat("C1", "hook", (101,)),
            _beat("C2", "development", (102,)),
            _beat("C3", "proof", (999,)),
        )
        result = validate_narrative_plan(_plan(beats), _safe_candidates())
        self.assertFalse(result.plan_valid)
        self.assertEqual(result.beats, beats)
        self.assertEqual(result.removed_beats, ())
        self.assertIn("unknown_candidate_id", result.replan_request.reason_codes)

    def test_optional_reuse_returns_replan_instead_of_silent_delete(self) -> None:
        beats = (
            _beat("C1", "hook", (101,)),
            _beat("C2", "development", (102,)),
            _beat("C3", "close", (101,), required=False),
        )
        result = validate_narrative_plan(_plan(beats), _safe_candidates())
        self.assertFalse(result.plan_valid)
        self.assertEqual(result.beats, beats)
        self.assertEqual(result.removed_beats, ())
        self.assertIn("candidate_reuse", result.replan_request.reason_codes)

    def test_ineligible_hook_returns_replan(self) -> None:
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "development", (102,)))),
            _safe_candidates(hook_eligible=False),
        )
        self.assertFalse(result.plan_valid)
        self.assertIn("hook_candidate_ineligible", result.replan_request.reason_codes)

    def test_opening_is_a_promise_and_payoff_unit_not_generic_role_sequence(self) -> None:
        opening = _opening()
        plan = _plan((
            _beat("C1", "hook", (101,)),
            _beat("C2", "turn", (102,)),
            _beat("C3", "scene", (104,)),
        ), opening=opening)
        result = validate_narrative_plan(plan, _safe_candidates())
        self.assertTrue(result.plan_valid)
        self.assertEqual(result.beats[1].narrative_role, "turn")

    def test_transition_note_is_preserved_as_director_audit_without_local_reordering(self) -> None:
        beats = (
            _beat("C1", "hook", (101,)),
            NarrativeBeat(
                source_role="proof",
                narrative_role="development",
                goal="解释结构",
                candidate_evidence=(102,),
                required=True,
                target_seconds=3.0,
                selection_instruction="用结构兑现承诺",
                chapter_id="C2",
                transition_from_previous="用肩线和捏褶兑现开场的单穿有型承诺",
            ),
        )
        result = validate_narrative_plan(_plan(beats), _safe_candidates())

        self.assertTrue(result.plan_valid)
        self.assertEqual(result.beats[1].transition_from_previous, "用肩线和捏褶兑现开场的单穿有型承诺")

    def test_opening_payoff_mismatch_is_a_replan(self) -> None:
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (103,)))),
            _safe_candidates(),
        )
        self.assertFalse(result.plan_valid)
        self.assertIn("opening_payoff_mismatch", result.replan_request.reason_codes)

    def test_complete_6_18_second_hook_is_accepted_with_integrity_reason(self) -> None:
        safe = list(_safe_candidates())
        safe[0] = replace(safe[0], end=3.09)
        safe[1] = replace(safe[1], start=3.09, end=6.18)
        safe[2] = replace(safe[2], start=6.18, end=9.0)
        opening = OpeningPackage(
            promise="完整承诺", payoff_relation="紧接着兑现", hook_candidate_ids=(101, 102),
            payoff_candidate_ids=(103,), hook_promise="完整承诺", payoff_delivery="用下一句立即证明",
            connection_reason="前两句完整提出承诺，第三句立即说明其成立原因。",
            hook_integrity_reason="两句共同构成完整承诺，删任一句都会丢失对象或结果。",
        )
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101, 102)), _beat("C2", "turn", (103,))), opening=opening),
            tuple(safe),
        )
        self.assertTrue(result.plan_valid, result.issues)
        self.assertIn("hook_duration_soft_tolerance:actual=6.180s,preferred_max=5.0s", result.issues)
        self.assertFalse(any("opening_unit_duration" in issue for issue in result.issues))

    def test_6_18_second_hook_requires_integrity_reason(self) -> None:
        safe = list(_safe_candidates())
        safe[0] = replace(safe[0], end=3.09)
        safe[1] = replace(safe[1], start=3.09, end=6.18)
        safe[2] = replace(safe[2], start=6.18, end=9.0)
        opening = OpeningPackage(
            promise="完整承诺", payoff_relation="紧接着兑现", hook_candidate_ids=(101, 102),
            payoff_candidate_ids=(103,), hook_promise="完整承诺", payoff_delivery="用下一句立即证明",
            connection_reason="前两句完整提出承诺，第三句立即说明其成立原因。",
        )
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101, 102)), _beat("C2", "turn", (103,))), opening=opening),
            tuple(safe),
        )
        self.assertFalse(result.plan_valid)
        self.assertIn("hook_soft_tolerance_rationale_missing", result.replan_request.reason_codes)

    def test_complete_7_38_second_hook_is_accepted_with_integrity_reason(self) -> None:
        safe = list(_safe_candidates())
        safe[0] = replace(safe[0], end=3.69)
        safe[1] = replace(safe[1], start=3.69, end=7.38)
        safe[2] = replace(safe[2], start=7.38, end=10.0)
        opening = OpeningPackage(
            promise="完整承诺", payoff_relation="紧接着兑现", hook_candidate_ids=(101, 102),
            payoff_candidate_ids=(103,), hook_promise="完整承诺", payoff_delivery="用下一句立即证明",
            connection_reason="前两句共同构成完整承诺，第三句立即说明其成立原因。",
            hook_integrity_reason="两句共同构成完整承诺，删任一句都会丢失对象或结果。",
        )
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101, 102)), _beat("C2", "turn", (103,))), opening=opening),
            tuple(safe),
        )
        self.assertTrue(result.plan_valid, result.issues)
        self.assertIn("hook_duration_soft_tolerance:actual=7.380s,preferred_max=5.0s", result.issues)

    def test_hook_over_8_seconds_is_still_rejected(self) -> None:
        safe = list(_safe_candidates())
        safe[0] = replace(safe[0], end=4.05)
        safe[1] = replace(safe[1], start=4.05, end=8.1)
        safe[2] = replace(safe[2], start=8.1, end=10.5)
        opening = OpeningPackage(
            promise="过长承诺", payoff_relation="紧接着兑现", hook_candidate_ids=(101, 102),
            payoff_candidate_ids=(103,), hook_integrity_reason="即使完整也太长。",
        )
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101, 102)), _beat("C2", "turn", (103,))), opening=opening),
            tuple(safe),
        )
        self.assertFalse(result.plan_valid)
        self.assertIn("hook_actual_duration_exceeds_8s", result.replan_request.reason_codes)

    def test_opening_contract_requires_exact_first_two_candidate_groups(self) -> None:
        result = validate_narrative_plan(
            _plan((
                _beat("C1", "hook", (101, 105)),
                _beat("C2", "turn", (102,)),
            )),
            _safe_candidates(),
        )
        self.assertFalse(result.plan_valid)
        self.assertIn("opening_hook_mismatch", result.replan_request.reason_codes)

    def test_executable_evidence_blocks_unmaterializable_plan_candidate(self) -> None:
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)))),
            _safe_candidates(),
            executable_evidence={102: {"materializable": False, "materialization_issue": "word_boundary_residual"}},
        )
        self.assertFalse(result.plan_valid)
        self.assertIn("candidate_not_materializable", result.replan_request.reason_codes)
        self.assertIn("opening_candidate_not_materializable", result.replan_request.reason_codes)

    def test_bridge_declaration_must_equal_actual_selected_bridge_assets(self) -> None:
        candidates = bind_story_assets(CommercialStoryBrief.from_strategy(_strategy()), _safe_candidates())
        plan = replace(
            _plan((
                _beat("C1", "hook", (101,)),
                _beat("C2", "turn", (102,)),
                replace(_beat("C3", "scene", (104,)), story_support="将版型结论接到早秋单穿场景"),
            )),
            selection_contract={"m1_consumption_validation_require_supporting_bridge": True},
            story_consumption=StoryConsumption(
                hero_strategy_id="S1", hero_priority="high", hero_consistency_reason="执行主故事。",
                supporting_chapter_ids=(), bridge_chapter_ids=("C3",), no_rediscovery=True,
                bridge_candidate_ids=(103,),
            ),
        )
        result = validate_narrative_plan(plan, candidates)
        self.assertFalse(result.plan_valid)
        self.assertIn("bridge_not_consumed", result.replan_request.reason_codes)

    def test_selected_candidates_preserve_real_source_boundaries_and_text(self) -> None:
        safe = _safe_candidates()
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            safe,
        )
        expected = {item.candidate_id: item for item in safe}
        for selected in result.selected_candidates:
            original = expected[selected.candidate_id]
            self.assertEqual((selected.source_id, selected.start, selected.end, selected.text),
                             (original.source_id, original.start, original.end, original.text))

    def test_missing_full_safe_pool_is_structured_replan(self) -> None:
        result = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)))),
            (),
        )
        self.assertFalse(result.plan_valid)
        self.assertIn("safe_candidate_pool_missing", result.replan_request.reason_codes)

    def test_short_plan_is_measured_and_kept_as_soft_duration_fallback(self) -> None:
        plan = _plan(
            (_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,))),
            target=20.0,
        )
        plan = NarrativePlan(
            **{**plan.__dict__, "duration_assessment": {"status": "full", "reason": "素材足够"}}
        )
        result = validate_narrative_plan(plan, _safe_candidates())
        self.assertTrue(result.plan_valid)
        self.assertEqual(result.status, "insufficient_material")
        self.assertIn("duration_assessment_corrected_by_measurement", result.issues[1])
        self.assertEqual(result.duration_assessment["status"], "insufficient_material")
        self.assertEqual(result.duration_assessment["planner_report"]["status"], "full")

    def test_long_plan_is_measured_without_local_trimming_or_failure(self) -> None:
        beats = (
            _beat("C1", "hook", (101,)),
            _beat("C2", "turn", (102,)),
            _beat("C3", "proof", (103,)),
            _beat("C4", "scene", (104,)),
            _beat("C5", "close", (105,)),
        )
        result = validate_narrative_plan(_plan(beats, target=20.0), _safe_candidates())
        self.assertTrue(result.plan_valid)
        self.assertEqual(result.beats, beats)
        self.assertEqual(result.status, "acceptable_long")
        self.assertIn("duration_above_preferred_range", result.issues[0])
        self.assertEqual(result.duration_assessment["status"], "acceptable_long")

    def test_long_plan_depth_contract_binds_distinct_new_values_to_real_chapters(self) -> None:
        beats = (
            _beat("C1", "hook", (101,), value_dimension="core_promise"),
            _beat("C2", "turn", (102,), value_dimension="mechanism"),
            _beat("C3", "proof", (103,), value_dimension="same_claim_additional_proof"),
            _beat("C4", "scene", (104,), value_dimension="new_scene"),
            _beat("C5", "close", (105,), value_dimension="new_styling_result"),
        )
        depth = DepthExpansionContract(
            base_covered_values=("core_promise", "mechanism", "proof"),
            new_value_chapters=(
                DepthExpansionValue("new_scene", ("C4",), "增加早秋和日常搭配场景。"),
                DepthExpansionValue("new_styling_result", ("C5",), "增加上身精神和整体造型结果。"),
            ),
            same_claim_additional_proof_chapter_ids=("C3",),
            status=DEPTH_EXPANSION_EXPANDED,
            reason="长版在核心证明后加入两个不同购买价值。",
        )
        plan = NarrativePlan(
            **{**_plan(beats, target=90.0).__dict__, "depth_expansion": depth}
        )

        result = validate_narrative_plan(plan, _safe_candidates())

        self.assertTrue(result.plan_valid)
        self.assertEqual(result.depth_expansion.status, DEPTH_EXPANSION_EXPANDED)
        self.assertEqual(
            [item.dimension for item in result.depth_expansion.new_value_chapters],
            ["new_scene", "new_styling_result"],
        )

    def test_long_plan_cannot_disguise_same_claim_proof_as_a_new_value(self) -> None:
        beats = (
            _beat("C1", "hook", (101,), value_dimension="core_promise"),
            _beat("C2", "turn", (102,), value_dimension="mechanism"),
            _beat("C3", "proof", (103,), value_dimension="same_claim_additional_proof"),
        )
        depth = DepthExpansionContract(
            base_covered_values=("core_promise", "mechanism", "proof"),
            new_value_chapters=(
                DepthExpansionValue("new_scene", ("C3",), "把同一段面料证明错误包装成场景。"),
            ),
            same_claim_additional_proof_chapter_ids=(),
            status=DEPTH_EXPANSION_EXPANDED,
            reason="错误示例。",
        )
        plan = NarrativePlan(
            **{**_plan(beats, target=90.0).__dict__, "depth_expansion": depth}
        )

        result = validate_narrative_plan(plan, _safe_candidates())

        self.assertFalse(result.plan_valid)
        self.assertIn("depth_expansion_value_dimension_mismatch", result.replan_request.reason_codes)

    def test_long_plan_may_explicitly_mark_a_normal_proof_as_the_one_extra_same_claim_proof(self) -> None:
        beats = (
            _beat("C1", "hook", (101,), value_dimension="core_promise"),
            _beat("C2", "turn", (102,), value_dimension="mechanism"),
            _beat("C3", "proof", (103,), value_dimension="proof"),
            _beat("C4", "scene", (104,), value_dimension="new_scene"),
        )
        depth = DepthExpansionContract(
            base_covered_values=("core_promise", "mechanism", "proof"),
            new_value_chapters=(
                DepthExpansionValue("new_scene", ("C4",), "增加真实使用场景。"),
            ),
            same_claim_additional_proof_chapter_ids=("C3",),
            status=DEPTH_EXPANSION_EXPANDED,
            reason="一个额外证明后转入新的使用场景。",
        )
        plan = NarrativePlan(
            **{**_plan(beats, target=90.0).__dict__, "depth_expansion": depth}
        )

        result = validate_narrative_plan(plan, _safe_candidates())

        self.assertTrue(result.plan_valid)

    def test_long_plan_that_lacks_distinct_value_must_not_claim_feasible_depth(self) -> None:
        beats = (
            _beat("C1", "hook", (101,), value_dimension="core_promise"),
            _beat("C2", "turn", (102,), value_dimension="mechanism"),
            _beat("C3", "proof", (103,), value_dimension="same_claim_additional_proof"),
        )
        depth = DepthExpansionContract(
            base_covered_values=("core_promise", "mechanism", "proof"),
            new_value_chapters=(),
            same_claim_additional_proof_chapter_ids=("C3",),
            status=DEPTH_EXPANSION_INSUFFICIENT_DISTINCT_VALUE,
            reason="只有同一显瘦机制的重复证明，没有新的购买价值。",
        )
        plan = NarrativePlan(
            **{
                **_plan(beats, target=90.0).__dict__,
                "depth_expansion": depth,
                "duration_plan": _parse_duration_plan({
                    "target_duration": 90.0,
                    "feasible_duration_range": {"min_seconds": 70.2, "max_seconds": 108.0},
                    "recommended_duration": 90.0,
                    "duration_status": DURATION_STATUS_FEASIBLE,
                    "depth_mode": DURATION_DEPTH_CHAPTERED_STORY,
                    "reason": "错误地只按候选时长判为可行。",
                }, target_duration=90.0),
            }
        )

        result = validate_narrative_plan(plan, _safe_candidates())

        self.assertFalse(result.plan_valid)
        self.assertIn("depth_expansion_duration_status_conflict", result.replan_request.reason_codes)

    def test_replan_prompt_requires_honest_short_or_in_range_duration(self) -> None:
        invalid = validate_narrative_plan(
            _plan((
                _beat("C1", "hook", (101,)),
                _beat("C2", "turn", (102,)),
            ), target=20.0),
            _safe_candidates(),
        )
        prompt = build_replan_prompt(_strategy(), 20.0, _safe_candidates(), {"category": "上衣"}, invalid)
        self.assertIn("对时长问题只能二选一", prompt)
        self.assertIn("短于优先下限时严禁写 full", prompt)
        self.assertIn("不能因为每段都不错就全部保留", prompt)
        self.assertIn("chapter_actual_seconds", prompt)
        self.assertIn("minimum_seconds_to_add", prompt)
        self.assertIn("唯一可信的时长事实", prompt)


class LlmBoundaryTests(unittest.TestCase):
    def test_opening_quality_review_replans_only_c1_c2_and_freezes_tail(self) -> None:
        original = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            _safe_candidates(),
        )
        original = replace(
            original,
            opening_package=replace(original.opening_package, hook_integrity_reason="旧 Opening 的两句理由"),
        )
        raw = """{
          "quality_status": "replan",
          "reason": "原 Hook 有直播过程感，改为更快进入承诺。",
          "issues": ["直播过程感"],
          "hook_independence": "新 Hook 独立成立。",
          "live_process_talk": "去除过程话。",
          "promise_speed": "第一句立即出现承诺。",
          "payoff_relation": "第二句解释承诺。",
          "compactness": "Opening 更紧凑。",
          "opening_package": {
            "hook_promise": "别把衬衫穿得太职业", "payoff_delivery": "用场景兑现单穿状态",
            "connection_reason": "先提出职业顾虑，再给出单穿结果", "hook_candidate_ids": [101],
            "payoff_candidate_ids": [104], "selection_instruction": "局部重导"
          },
          "opening_chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101], "goal": "提出顾虑", "required": true, "selection_instruction": "Hook", "story_support": "主故事开场"},
            {"chapter_id": "C2", "narrative_role": "development", "candidate_ids": [104], "goal": "兑现承诺", "required": true, "selection_instruction": "Payoff", "transition_from_previous": "立即兑现", "story_support": "主故事兑现"}
          ]
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw):
            reviewed = review_opening_quality_llm(
                strategy=_strategy(), plan=original, safe_candidates=_safe_candidates(),
                api_key="x", base_url="https://example.com", model="deepseek-v4-flash",
            )
        self.assertTrue(reviewed.plan_valid)
        self.assertEqual([beat.candidate_ids for beat in reviewed.beats], [(101,), (104,), (103,)])
        self.assertEqual(reviewed.beats[2], original.beats[2])
        self.assertTrue(reviewed.opening_quality_review.replanned)
        self.assertEqual(reviewed.opening_quality_review.status, "replan")
        self.assertEqual(reviewed.opening_package.hook_integrity_reason, "")

    def test_opening_quality_review_pass_preserves_whole_plan(self) -> None:
        original = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            _safe_candidates(),
        )
        raw = """{
          "quality_status": "pass", "reason": "开场独立、紧凑并立即兑现。",
          "issues": [], "hook_independence": "独立成立", "live_process_talk": "无明显过程话",
          "promise_speed": "快速", "payoff_relation": "直接兑现", "compactness": "紧凑"
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw):
            reviewed = review_opening_quality_llm(
                strategy=_strategy(), plan=original, safe_candidates=_safe_candidates(),
                api_key="x", base_url="https://example.com", model="deepseek-v4-flash",
            )
        self.assertEqual(reviewed.beats, original.beats)
        self.assertEqual(reviewed.opening_quality_review.status, "pass")
        self.assertFalse(reviewed.opening_quality_review.replanned)

    def test_opening_quality_review_can_honestly_report_no_better_opening(self) -> None:
        original = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            _safe_candidates(),
        )
        raw = """{
          "quality_status": "opening_replan_failed_no_better_opening",
          "reason": "当前 Hero 的可物化证据中没有更独立的完整承诺。",
          "issues": ["没有更强独立 Opening"]
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw):
            reviewed = review_opening_quality_llm(
                strategy=_strategy(), plan=original, safe_candidates=_safe_candidates(),
                api_key="x", base_url="https://example.com", model="deepseek-v4-flash",
            )
        self.assertEqual(reviewed.beats, original.beats)
        self.assertEqual(reviewed.opening_quality_review.status, "opening_replan_failed_no_better_opening")

    def test_opening_quality_replan_cannot_return_same_candidate_groups(self) -> None:
        original = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            _safe_candidates(),
        )
        raw = """{
          "quality_status": "replan", "reason": "需要更紧凑", "issues": ["过程话"],
          "opening_package": {"hook_promise": "承诺", "payoff_delivery": "兑现", "connection_reason": "连接", "hook_candidate_ids": [101], "payoff_candidate_ids": [102]},
          "opening_chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101], "required": true},
            {"chapter_id": "C2", "narrative_role": "development", "candidate_ids": [102], "required": true}
          ]
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw):
            reviewed = review_opening_quality_llm(
                strategy=_strategy(), plan=original, safe_candidates=_safe_candidates(),
                api_key="x", base_url="https://example.com", model="deepseek-v4-flash",
            )
        self.assertEqual(reviewed.beats, original.beats)
        self.assertEqual(reviewed.opening_quality_review.status, "invalid_replan")
        self.assertIn("opening_replan_no_candidate_change", reviewed.opening_quality_review.issues)

    def test_opening_quality_replan_cannot_reuse_frozen_tail_candidate(self) -> None:
        original = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            _safe_candidates(),
        )
        raw = """{
          "quality_status": "replan", "reason": "尝试更强兑现", "issues": [],
          "opening_package": {"hook_promise": "承诺", "payoff_delivery": "兑现", "connection_reason": "连接", "hook_candidate_ids": [101], "payoff_candidate_ids": [103]},
          "opening_chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101], "required": true},
            {"chapter_id": "C2", "narrative_role": "development", "candidate_ids": [103], "required": true}
          ]
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw):
            reviewed = review_opening_quality_llm(
                strategy=_strategy(), plan=original, safe_candidates=_safe_candidates(),
                api_key="x", base_url="https://example.com", model="deepseek-v4-flash",
            )
        self.assertEqual(reviewed.beats, original.beats)
        self.assertEqual(reviewed.opening_quality_review.status, "invalid_replan")
        self.assertIn("opening_replan_reuses_frozen_tail_candidate:103", reviewed.opening_quality_review.issues)

    def test_opening_quality_prompt_is_semantic_review_not_keyword_blacklist(self) -> None:
        plan = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            _safe_candidates(),
        )
        prompt = build_opening_quality_review_prompt(_strategy(), plan, _safe_candidates())
        self.assertIn("不要建立关键词黑名单", prompt)
        self.assertIn("Opening Replan v2", prompt)
        self.assertIn("Opening Evidence Cards", prompt)
        self.assertIn("具体结果、消费者利益、矛盾或变化", prompt)
        self.assertIn("C3 及之后所有章节、顺序和候选均已冻结", prompt)
        self.assertIn("超过12秒只触发本次质量复核", prompt)

    def test_opening_replan_v2_is_a_small_auditable_call(self) -> None:
        original = validate_narrative_plan(
            _plan((_beat("C1", "hook", (101,)), _beat("C2", "turn", (102,)), _beat("C3", "proof", (103,)))),
            _safe_candidates(),
        )
        raw = """{
          "contract_version": "m2-opening-replan-v2",
          "quality_status": "replan", "reason": "换成直接结果句。", "issues": [],
          "opening_decision_audit": {
            "original_opening_failure_reason": "原句只是介绍。", "candidate_card_ids_considered": [101, 104],
            "new_hook_standalone_text": "候选 101", "new_payoff_standalone_text": "候选 104",
            "why_hook_worth_staying": "直接给消费者结果", "how_payoff_delivers": "立即解释结果",
            "commercial_gate_recommendation": "human_review_required"
          },
          "opening_package": {"hook_promise": "承诺", "payoff_delivery": "兑现", "connection_reason": "连接", "hook_candidate_ids": [101], "payoff_candidate_ids": [104]},
          "opening_chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101], "required": true},
            {"chapter_id": "C2", "narrative_role": "development", "candidate_ids": [104], "required": true}
          ]
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw) as request:
            reviewed = review_opening_quality_llm(
                strategy=_strategy(), plan=original, safe_candidates=_safe_candidates(),
                api_key="x", base_url="https://example.com", model="deepseek-v4-flash",
            )
        self.assertEqual(request.call_args.kwargs["stage"], "M2_opening_replan_v2")
        self.assertEqual(request.call_args.kwargs["max_tokens"], 1400)
        report = reviewed.opening_quality_review.review_report
        self.assertEqual(report["contract_version"], "m2-opening-replan-v2")
        self.assertEqual(report["opening_decision_audit"]["commercial_gate_recommendation"], "human_review_required")

    def test_llm_path_requires_full_safe_candidates(self) -> None:
        with self.assertRaisesRegex(ValueError, "full hard-safe candidates"):
            plan_narrative_llm(
                strategy=_strategy(), api_key="x", base_url="https://example.com", model="deepseek-v4-flash"
            )

    def test_llm_path_preserves_raw_response_for_audit(self) -> None:
        raw = """{
          "duration_plan": {
            "target_duration": 15, "feasible_duration_range": {"min_seconds": 12, "max_seconds": 18},
            "recommended_duration": 15, "duration_status": "feasible", "depth_mode": "core_dense",
            "reason": "核心承诺和结构兑现足够形成短版。"
          },
          "duration_assessment": {"status": "full", "reason": "安全素材已覆盖目标"},
          "opening_package": {
            "promise": "别把衬衫穿得太职业", "payoff_relation": "肩线证明单穿也成立",
            "hook_candidate_ids": [101], "payoff_candidate_ids": [102]
          },
          "chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101]},
            {"chapter_id": "C2", "narrative_role": "turn", "candidate_ids": [102]},
            {"chapter_id": "C3", "narrative_role": "scene", "candidate_ids": [104]}
          ]
        }"""
        captured = []
        with mock.patch("story_planner._post_planner_request", return_value=raw):
            result = plan_narrative_llm(
                strategy=_strategy(), target_duration=15.0, safe_candidates=_safe_candidates(),
                selection_contract={"category": "上衣"}, api_key="x", base_url="https://example.com",
                model="deepseek-v4-flash", raw_response_hook=captured.append,
            )
        self.assertEqual(captured, [raw])
        self.assertTrue(result.plan_valid)
        self.assertEqual(result.total_seconds, 17.0)
        self.assertEqual(result.duration_assessment["status"], "full")
        self.assertTrue(result.duration_plan.reported)
        self.assertEqual(result.duration_plan.duration_status, DURATION_STATUS_FEASIBLE)
        self.assertEqual(result.duration_plan.recommended_duration, 15.0)

    def test_duration_refinement_is_ai_authored_without_local_plan_trimming(self) -> None:
        initial = validate_narrative_plan(
            _plan((
                _beat("C1", "hook", (101,)),
                _beat("C2", "turn", (102,)),
                _beat("C3", "proof", (103,)),
                _beat("C4", "scene", (104,)),
                _beat("C5", "close", (105,)),
            ), target=20.0),
            _safe_candidates(),
        )
        self.assertEqual(initial.status, "acceptable_long")
        prompt = build_duration_refinement_prompt(_strategy(), initial, _safe_candidates(), {"category": "上衣"})
        self.assertIn("程序替你删片", prompt)
        self.assertIn("导演级时长重规划", prompt)
        raw = """{
          "duration_plan": {
            "target_duration": 20, "feasible_duration_range": {"min_seconds": 16, "max_seconds": 24},
            "recommended_duration": 17, "duration_status": "feasible", "depth_mode": "core_dense",
            "reason": "去除重复章节后，保留承诺、兑现和新场景。"
          },
          "duration_assessment": {"status": "full", "reason": "压缩后仍有完整主故事。"},
          "opening_package": {
            "promise": "别把衬衫穿得太职业", "payoff_relation": "肩线证明单穿也成立",
            "hook_candidate_ids": [101], "payoff_candidate_ids": [102]
          },
          "chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101]},
            {"chapter_id": "C2", "narrative_role": "turn", "candidate_ids": [102]},
            {"chapter_id": "C3", "narrative_role": "scene", "candidate_ids": [104]}
          ]
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw) as request:
            refined = refine_duration_narrative_llm(
                strategy=_strategy(), current_plan=initial, safe_candidates=_safe_candidates(),
                selection_contract={"category": "上衣"}, api_key="x", base_url="https://example.com",
                model="deepseek-v4-flash",
            )
        self.assertTrue(request.called)
        self.assertEqual([beat.candidate_ids for beat in initial.beats], [(101,), (102,), (103,), (104,), (105,)])
        self.assertEqual([beat.candidate_ids for beat in refined.beats], [(101,), (102,), (104,)])
        self.assertEqual(refined.total_seconds, 17.0)
        self.assertTrue(refined.plan_valid)

    def test_long_shortfall_refinement_exposes_unused_purchase_values(self) -> None:
        beats = (
            _beat("C1", "hook", (101,), value_dimension="core_promise"),
            _beat("C2", "mechanism", (102,), value_dimension="mechanism"),
            _beat("C3", "proof", (103,), value_dimension="proof"),
            _beat("C4", "scene", (104,), value_dimension="new_scene"),
        )
        depth = DepthExpansionContract(
            base_covered_values=("core_promise", "mechanism", "proof"),
            new_value_chapters=(
                DepthExpansionValue("new_scene", ("C4",), "给主故事增加真实穿着场景。"),
            ),
            same_claim_additional_proof_chapter_ids=(),
            status=DEPTH_EXPANSION_EXPANDED,
            reason="已有一个场景扩展，还需要在完整安全池中寻找其他购买价值。",
        )
        current = validate_narrative_plan(
            NarrativePlan(
                **{**_plan(beats, target=90.0).__dict__, "depth_expansion": depth}
            ),
            _safe_candidates(),
        )

        scout = DurationExpansionScout(
            assets=(
                DurationExpansionAsset(105, "new_emotional_value", 7.0, "上身更有精神", "提供独立的情绪结果。"),
            ),
            status=EXPANSION_SCOUT_FOUND,
            reason="发现一个未消费的情绪价值资产。",
        )
        prompt = build_duration_refinement_prompt(
            _strategy(), current, _safe_candidates(), {"category": "上衣"}, scout
        )

        self.assertIn("duration_expansion_audit", prompt)
        self.assertIn("uncovered_value_dimensions_to_search_first", prompt)
        self.assertIn("new_comfort", prompt)
        self.assertIn("长版深度扩展", prompt)
        self.assertIn("M2 Duration Expansion Scout", prompt)
        self.assertIn("new_emotional_value", prompt)
        self.assertIn("candidate_duration", prompt)
        self.assertIn("未使用完整安全候选速查表", prompt)
        self.assertIn("id=105 duration=7.000s", prompt)
        self.assertIn("禁止原样返回同一组 candidate_id 后仍声明 feasible", prompt)

    def test_duration_expansion_scout_keeps_only_unused_real_safe_assets(self) -> None:
        beats = (
            _beat("C1", "hook", (101,), value_dimension="core_promise"),
            _beat("C2", "mechanism", (102,), value_dimension="mechanism"),
            _beat("C3", "proof", (103,), value_dimension="proof"),
            _beat("C4", "scene", (104,), value_dimension="new_scene"),
        )
        depth = DepthExpansionContract(
            base_covered_values=("core_promise", "mechanism", "proof"),
            new_value_chapters=(
                DepthExpansionValue("new_scene", ("C4",), "已使用的穿着场景。"),
            ),
            same_claim_additional_proof_chapter_ids=(),
            status=DEPTH_EXPANSION_EXPANDED,
            reason="还可继续寻找新的购买价值。",
        )
        current = validate_narrative_plan(
            NarrativePlan(
                **{**_plan(beats, target=90.0).__dict__, "depth_expansion": depth}
            ),
            _safe_candidates(),
        )
        scout_prompt = build_duration_expansion_scout_prompt(
            _strategy(), current, _safe_candidates(), {"category": "上衣"}
        )
        self.assertIn("不是剪辑师", scout_prompt)
        self.assertIn("id=105 duration=7.000s", scout_prompt)
        raw = """{
          "status": "found",
          "reason": "发现一个情绪结果表达。",
          "expansion_assets": [
            {"candidate_id": 105, "value_dimension": "new_emotional_value", "purchase_value": "上身精神", "reason": "增加独立情绪价值"},
            {"candidate_id": 101, "value_dimension": "new_trust", "purchase_value": "无效", "reason": "已被当前计划使用"},
            {"candidate_id": 999, "value_dimension": "new_scene", "purchase_value": "无效", "reason": "候选不存在"}
          ]
        }"""
        captured = []
        with mock.patch("story_planner._post_planner_request", return_value=raw) as request:
            scout = discover_duration_expansion_assets_llm(
                strategy=_strategy(), current_plan=current, safe_candidates=_safe_candidates(),
                selection_contract={"category": "上衣"}, api_key="x", base_url="https://example.com",
                model="deepseek-v4-flash", raw_response_hook=captured.append,
            )
        self.assertTrue(request.called)
        self.assertEqual(captured, [raw])
        self.assertEqual(scout.status, EXPANSION_SCOUT_FOUND)
        self.assertEqual([(asset.candidate_id, asset.value_dimension, asset.candidate_duration) for asset in scout.assets], [
            (105, "new_emotional_value", 7.0),
        ])

    def test_duration_budget_witnesses_are_arithmetic_options_not_a_playlist(self) -> None:
        scout = DurationExpansionScout(
            assets=(
                DurationExpansionAsset(201, "new_scene", 8.0, "度假场景", "新场景。"),
                DurationExpansionAsset(202, "new_trust", 6.0, "面料信任", "新信任。"),
                DurationExpansionAsset(203, "new_usage_cycle", 5.0, "穿着周期", "新使用价值。"),
            ),
            status=EXPANSION_SCOUT_FOUND,
            reason="候选足以构成多个预算组合。",
        )

        options = _duration_expansion_budget_options(scout, additional_seconds_needed=12.0)

        self.assertTrue(options)
        self.assertTrue(all(option["additional_seconds"] >= 12.0 for option in options))
        self.assertTrue(all(len(option["value_dimensions"]) >= 2 for option in options))
        self.assertTrue(all("candidate_ids" in option for option in options))

    def test_replan_is_ai_authored_and_never_repairs_old_beats_locally(self) -> None:
        invalid = validate_narrative_plan(
            _plan((_beat("C1", "hook", (999,)), _beat("C2", "turn", (102,))), target=9.0),
            _safe_candidates(),
        )
        raw = """{
          "duration_assessment": {"status": "full", "reason": "使用两个完整安全候选"},
          "opening_package": {
            "promise": "单穿就显瘦", "payoff_relation": "结构解释显瘦原因",
            "hook_candidate_ids": [101], "payoff_candidate_ids": [102]
          },
          "chapters": [
            {"chapter_id": "C1", "narrative_role": "hook", "candidate_ids": [101]},
            {"chapter_id": "C2", "narrative_role": "turn", "candidate_ids": [102]}
          ]
        }"""
        with mock.patch("story_planner._post_planner_request", return_value=raw) as request:
            result = replan_narrative_llm(
                strategy=_strategy(), invalid_plan=invalid, safe_candidates=_safe_candidates(),
                selection_contract={"category": "上衣"}, api_key="x", base_url="https://example.com",
                model="deepseek-v4-flash",
            )
        self.assertTrue(request.called)
        self.assertTrue(result.plan_valid)
        self.assertEqual([beat.candidate_ids for beat in invalid.beats], [(999,), (102,)])
        self.assertEqual([beat.candidate_ids for beat in result.beats], [(101,), (102,)])


if __name__ == "__main__":
    unittest.main()
