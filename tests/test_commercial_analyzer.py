import json
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commercial_analyzer import (
    ANALYZER_SYSTEM_PROMPT,
    AnalyzerError,
    TWO_PASS_CAST_SYSTEM_PROMPT,
    TWO_PASS_STORY_SYSTEM_PROMPT,
    DirectorBeat,
    Strategy,
    StrategyDiscoveryResult,
    assess_story_commercial_change,
    analyze_commercial_story,
    build_analyzer_user_prompt,
    build_two_pass_cast_prompt,
    build_two_pass_draft_audit,
    build_two_pass_story_audit,
    build_two_pass_story_prompt,
    compute_material_sufficiency,
    compute_story_strength,
    compute_duration_feasibility,
    detect_content_dependencies,
    director_duration_depth_contract,
    director_target_duration_range,
    matches_story_semantic_signature,
    normalize_director_controls,
    parse_strategy_result,
    _extract_json,
    _post_two_pass_director_request,
    resolve_commercial_director_model,
)


SAMPLE_RESPONSE = {
    "strategies": [
        {
            "strategy_id": "S1",
            "type": "problem_solver",
            "thesis": "正肩蝙蝠袖+条纹拼接视觉差解决腰粗背厚肚子有肉",
            "target_user": "担心显胖的姐妹",
            "evidence": [
                {"role": "hook", "subtitle_ids": [9, 22], "claim": "腰粗显腰细"},
                {"role": "mechanism", "subtitle_ids": [29, 30], "claim": "条纹拼接视觉内收"},
                {"role": "result", "subtitle_ids": [33, 34, 58], "claim": "正侧背显瘦遮肚"},
            ],
            "missing_roles": [],
            "coherence_reason": "围绕版型显瘦主线",
            "distinctiveness": "high",
        },
        {
            "strategy_id": "S2",
            "type": "lifestyle",
            "thesis": "早秋薄针织解决温差",
            "target_user": "早秋乱穿衣的姐妹",
            "evidence": [
                {"role": "hook", "subtitle_ids": [80], "claim": "脱了冷穿上热"},
                {"role": "benefit", "subtitle_ids": [81], "claim": "体感舒服"},
            ],
            "missing_roles": ["mechanism", "result"],
            "coherence_reason": "围绕早秋场景",
            "distinctiveness": "high",
        },
    ]
}


SAMPLE_SUBTITLES = [
    {"id": 9, "start": 30.0, "end": 34.7, "text": "腰粗显腰细"},
    {"id": 22, "start": 87.0, "end": 91.3, "text": "蝙蝠袖显胳膊细"},
    {"id": 29, "start": 116.1, "end": 122.1, "text": "条纹拼接视觉内收"},
    {"id": 30, "start": 122.5, "end": 125.3, "text": "显腰细正肩"},
    {"id": 33, "start": 132.9, "end": 137.4, "text": "侧面显薄"},
    {"id": 34, "start": 137.6, "end": 140.3, "text": "背后显瘦"},
    {"id": 58, "start": 227.5, "end": 231.0, "text": "肚子有肉有空间"},
    {"id": 80, "start": 315.0, "end": 319.0, "text": "薄针织体感舒服"},
    {"id": 81, "start": 319.2, "end": 322.7, "text": "体感舒服没有之一"},
]


class ParseTests(unittest.TestCase):
    def test_parse_builds_evidence_and_computes_scores(self) -> None:
        result = parse_strategy_result(
            json.dumps(SAMPLE_RESPONSE, ensure_ascii=False),
            product="简致衬衫",
            subtitles=SAMPLE_SUBTITLES,
            target_duration=45.0,
        )
        self.assertIsInstance(result, StrategyDiscoveryResult)
        self.assertEqual(result.product, "简致衬衫")
        self.assertEqual(len(result.strategies), 2)
        self.assertEqual(result.strategies[0].evidence[0].role, "hook")
        self.assertEqual(result.strategies[0].evidence[0].subtitle_ids, (9, 22))

    def test_parse_preserves_story_fields(self) -> None:
        payload = {
            "strategies": [
                {
                    "strategy_id": "S1",
                    "type": "problem_solver",
                    "strategy_family": "body_shaping",
                    "sub_angle": "waist_hip",
                    "story_priority": "high",
                    "thesis": "前短后长解决腰腹遮肉",
                    "story_premise": "一个怕显胖的女生，穿上后自信出门",
                    "audience_tension": "想穿得利落又怕显胖",
                    "story_trigger": "主播上身的前短后长版型",
                    "transformation": "不敢露腰腹 → 遮肉显背薄 → 自信单穿",
                    "product_role": "帮用户把身材焦虑变成穿搭自信的救星",
                    "core_commercial_idea": "简单衬衫也能不靠叠搭穿出有型感",
                    "payoff": "用户敢于单穿并感到利落自信",
                    "supporting_arcs": ["早秋叠搭"],
                    "inference_notes": ["目标人群为基于主播话术的合理推断"],
                    "core_evidence_pool": [
                        {"role": "hook", "subtitle_ids": [9], "claim": "前短后长遮臀"}
                    ],
                    "supporting_evidence_pool": [
                        {"role": "proof", "subtitle_ids": [22], "claim": "袖型修饰上身"}
                    ],
                    "bridge_candidates": [
                        {"role": "scene", "subtitle_ids": [29], "claim": "搭配场景"}
                    ],
                    "content_dependencies": ["price", "social_proof"],
                    "evidence": [
                        {"role": "hook", "subtitle_ids": [9], "claim": "前短后长遮臀"}
                    ],
                }
            ]
        }
        result = parse_strategy_result(json.dumps(payload, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES)
        s = result.strategies[0]
        self.assertEqual(s.story_premise, "一个怕显胖的女生，穿上后自信出门")
        self.assertEqual(s.audience_tension, "想穿得利落又怕显胖")
        self.assertEqual(s.story_trigger, "主播上身的前短后长版型")
        self.assertEqual(s.transformation, "不敢露腰腹 → 遮肉显背薄 → 自信单穿")
        self.assertEqual(s.product_role, "帮用户把身材焦虑变成穿搭自信的救星")
        self.assertEqual(s.core_commercial_idea, "简单衬衫也能不靠叠搭穿出有型感")
        self.assertEqual(s.payoff, "用户敢于单穿并感到利落自信")
        self.assertEqual(s.story_priority, "high")
        self.assertEqual(s.supporting_arcs, ("早秋叠搭",))
        self.assertEqual(s.inference_notes, ("目标人群为基于主播话术的合理推断",))
        self.assertEqual([item.asset_tier for item in s.evidence], ["core", "supporting", "bridge"])
        # LLM 的声明不会无证据地污染合同依赖；只有核心证据实际依赖时才保留。
        self.assertEqual(s.content_dependencies, ())

    def test_parse_preserves_single_call_director_packet(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S1", "thesis": "夏天轻松穿", "target_user": "怕热大身材",
                "director_title": "夏天出门不用费劲穿",
                "core_desire": "显瘦也舒服",
                "opening_promise": "先给显瘦结果",
                "director_quality_tier": "strong",
                "director_sequence": [
                    {"beat_id": "B1", "purchase_question_id": "Q1", "purchase_question": "我为什么想买？", "coverage": "required", "answer_role": "result", "supports_question_id": "", "purchase_outcome": "body_looks_narrower", "role": "result", "goal": "显瘦结果", "subtitle_ids": [9], "opening_fallback_subtitle_ids": [22], "why_this_follows": "先停住人"},
                    {"beat_id": "B2", "purchase_question_id": "Q4", "purchase_question": "夏天穿着舒服吗？", "coverage": "recommended", "answer_role": "comfort", "supports_question_id": "", "purchase_outcome": "summer_breathability", "role": "comfort", "goal": "夏天体验", "subtitle_ids": [80], "why_this_follows": "回应穿着顾虑"},
                ],
            }],
        }
        strategy = parse_strategy_result(
            json.dumps(payload, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES,
        ).strategies[0]

        self.assertEqual(strategy.director_title, "夏天出门不用费劲穿")
        self.assertEqual(strategy.director_quality_tier, "strong")
        self.assertEqual(strategy.director_sequence[1].subtitle_ids, (80,))
        self.assertEqual(strategy.to_dict()["director_sequence"][0]["role"], "result")
        self.assertEqual(strategy.director_sequence[0].purchase_question_id, "Q1")
        self.assertEqual(strategy.director_sequence[1].answer_role, "comfort")
        self.assertEqual(strategy.to_dict()["director_sequence"][0]["purchase_outcome"], "body_looks_narrower")
        self.assertEqual(strategy.director_sequence[0].opening_fallback_subtitle_ids, (22,))

    def test_parse_flattens_structured_chapter_packets_without_reordering_beats(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S1", "thesis": "夏天显瘦也舒服", "target_user": "怕热又怕显壮",
                "director_plan_role": "primary",
                "director_title": "夏天大身材也能轻松穿",
                "core_desire": "显瘦但不闷热、出门不费劲",
                "opening_promise": "先给独立显瘦结果，再马上兑现",
                "director_quality_tier": "strong",
                "video_structure": {
                    "id": "pain_point", "name": "痛点切入型", "selection_reason": "有结果、机制和体验证据",
                },
                "chapter_packets": [
                    {
                        "chapter_id": "C1", "structure_slot": "pain_or_result_hook", "title": "显瘦先兑现",
                        "purpose": "结果后立即解释", "new_buyer_knowledge": "不是空口号", "coverage": "required",
                        "beats": [
                            {"beat_id": "C1B1", "purchase_question_id": "Q1", "purchase_question": "我为什么想买？", "answer_role": "result", "supports_question_id": "", "purchase_outcome": "body_looks_narrower", "role": "result", "goal": "显瘦结果", "subtitle_ids": [9], "opening_fallback_subtitle_ids": [22], "why_this_follows": "先停住人"},
                            {"beat_id": "C1B2", "purchase_question_id": "Q2", "purchase_question": "为什么真的有效？", "answer_role": "mechanism", "supports_question_id": "Q1", "purchase_outcome": "shoulder_narrowing", "role": "mechanism", "goal": "机制解释", "subtitle_ids": [29], "why_this_follows": "兑现结果"},
                        ],
                    },
                    {
                        "chapter_id": "C2", "structure_slot": "wearing_experience", "title": "夏天也好穿",
                        "purpose": "解除闷热顾虑", "new_buyer_knowledge": "显瘦不等于闷", "coverage": "recommended",
                        "beats": [
                            {"beat_id": "C2B1", "purchase_question_id": "Q4", "purchase_question": "夏天舒服吗？", "answer_role": "comfort", "supports_question_id": "", "purchase_outcome": "summer_breathability", "role": "comfort", "goal": "夏天体验", "subtitle_ids": [80], "why_this_follows": "落到体感"},
                        ],
                    },
                ],
                "whole_video_audit": {"status": "pass", "selected_beat_count": 3, "estimated_source_seconds": 10.2, "first_15_seconds_progression": "结果→机制→体验"},
            }],
        }
        strategy = parse_strategy_result(json.dumps(payload, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES).strategies[0]

        self.assertEqual(strategy.video_structure_id, "pain_point")
        self.assertEqual(strategy.video_structure_name, "痛点切入型")
        self.assertEqual([item.beat_id for item in strategy.director_sequence], ["C1B1", "C1B2", "C2B1"])
        self.assertEqual(len(strategy.director_chapter_packets), 2)
        self.assertEqual(strategy.director_chapter_packets[0].beats[1].subtitle_ids, (29,))
        self.assertEqual(strategy.whole_video_audit["status"], "pass")
        serialized = strategy.to_dict()
        self.assertEqual(serialized["chapter_packets"][1]["title"], "夏天也好穿")
        self.assertEqual(serialized["director_sequence"][2]["beat_id"], "C2B1")

    def test_parse_preserves_atomic_opening_alternative_packages(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S1",
                "chapter_packets": [{
                    "chapter_id": "C1", "title": "主开场", "coverage": "required",
                    "beats": [{
                        "beat_id": "C1B1", "purchase_question_id": "Q1",
                        "purchase_question": "为什么想买", "answer_role": "result",
                        "purchase_outcome": "primary_result", "role": "result",
                        "subtitle_ids": [9], "opening_fallback_subtitle_ids": [],
                    }],
                }],
                "opening_alternative_packages": [{
                    "package_id": "OP2", "title": "完整备用开场",
                    "beats": [{
                        "beat_id": "OP2B1", "purchase_question_id": "Q1",
                        "purchase_question": "为什么想买", "answer_role": "result",
                        "purchase_outcome": "alternative_result", "role": "result",
                        "subtitle_ids": [29, 30], "opening_fallback_subtitle_ids": [],
                    }],
                }],
            }],
        }
        strategy = parse_strategy_result(
            json.dumps(payload, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES,
        ).strategies[0]

        self.assertEqual(strategy.director_opening_alternatives[0].package_id, "OP2")
        self.assertEqual(strategy.director_opening_alternatives[0].beats[0].subtitle_ids, (29, 30))
        self.assertEqual(
            strategy.to_dict()["opening_alternative_packages"][0]["title"], "完整备用开场",
        )

    def test_parse_compact_single_pass_director_response(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S1",
                "director_plan_role": "primary",
                "director_title": "夏天省心显瘦",
                "core_desire": "大身材夏天也能轻松显瘦",
                "opening_promise": "先给显瘦结果",
                "chapter_packets": [{
                    "chapter_id": "C1",
                    "chapter_kind": "result",
                    "title": "先看显瘦结果",
                    "buyer_advance": "穿上正面会显窄",
                    "window_id": "W001",
                    "source_span": {"start_id": 9, "end_id": 10},
                    "verbatim": "穿上以后，正面整个人看起来很窄。",
                }],
                "final_readthrough": "穿上以后，正面整个人看起来很窄。",
            }],
        }
        strategy = parse_strategy_result(json.dumps(payload, ensure_ascii=False)).strategies[0]

        self.assertEqual(strategy.director_sequence[0].subtitle_ids, (9, 10))
        self.assertEqual(strategy.director_sequence[0].purchase_question_id, "Q1")
        self.assertEqual(strategy.director_sequence[0].answer_role, "result")
        self.assertEqual(strategy.director_sequence[0].purchase_outcome, "穿上正面会显窄")
        self.assertEqual(strategy.director_sequence[0].verbatim, "穿上以后，正面整个人看起来很窄。")
        self.assertEqual(strategy.director_readthrough, "穿上以后，正面整个人看起来很窄。")
        self.assertEqual(strategy.core_evidence_pool[0].subtitle_ids, (9, 10))

    def test_prompt_inlines_surface_anomaly_on_the_real_spoken_window(self) -> None:
        prompt = build_analyzer_user_prompt(
            product="x",
            subtitles=[{"id": 7, "start": 0, "end": 2, "text": "的，很大。"}],
        )
        self.assertIn("[W001][IDs 7]", prompt)
        self.assertIn("[surface=dangling_function_word_opening]", prompt)
        self.assertIn("[opening=clean] 的，很大。", prompt)
        self.assertEqual(prompt.count("的，很大。"), 1)

    def test_prompt_inlines_asr_anomaly_on_the_real_spoken_window(self) -> None:
        prompt = build_analyzer_user_prompt(
            product="x",
            subtitles=[{"id": 8, "start": 0, "end": 2, "text": "木浆纤维可降解的A类母婴店。"}],
        )
        self.assertIn("[W001][IDs 8]", prompt)
        self.assertIn("[surface=asr_material_claim_anomaly]", prompt)
        self.assertIn("[opening=clean] 木浆纤维可降解的A类母婴店。", prompt)
        self.assertEqual(prompt.count("木浆纤维可降解的A类母婴店。"), 1)

    def test_prompt_marks_dependent_opening_without_removing_the_window(self) -> None:
        prompt = build_analyzer_user_prompt(
            product="x",
            subtitles=[{"id": 9, "start": 0, "end": 2, "text": "你可能会说你瘦啊，你相信我。"}],
        )
        self.assertIn("[opening=live_or_dependent_leadin]", prompt)
        self.assertIn("你可能会说你瘦啊，你相信我。", prompt)

    def test_asset_aware_parse_keeps_supporting_scenes_but_rejects_unavailable_evidence(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S1",
                "thesis": "单穿成立也能搭配延展",
                "core_evidence_pool": [
                    {"role": "mechanism", "subtitle_ids": [9], "claim": "版型解释"},
                    {"role": "proof", "subtitle_ids": [22], "claim": "搭配资产可在真实语义足够时建立主证明"},
                    {"role": "trust", "subtitle_ids": [29], "claim": "不可用交易素材"},
                ],
                "supporting_evidence_pool": [
                    {"role": "scene", "subtitle_ids": [22], "claim": "搭配场景"},
                ],
                "bridge_candidates": [],
            }],
        }
        assets = [
            {"candidate_id": 9, "story_permission": "main_story"},
            {"candidate_id": 22, "story_permission": "supporting_story"},
            {"candidate_id": 29, "story_permission": "unavailable"},
        ]

        strategy = parse_strategy_result(
            json.dumps(payload, ensure_ascii=False),
            subtitles=SAMPLE_SUBTITLES,
            commercial_assets=assets,
        ).strategies[0]

        self.assertEqual([item.subtitle_ids for item in strategy.core_evidence_pool], [(9,), (22,)])
        self.assertEqual([item.subtitle_ids for item in strategy.supporting_evidence_pool], [(22,)])
        self.assertEqual(strategy.bridge_candidates, ())
        self.assertEqual(len(strategy.excluded_assets_reason), 1)
        self.assertTrue(any("unavailable=29" in item for item in strategy.excluded_assets_reason))


class StrengthComputationTests(unittest.TestCase):
    def test_story_strength_differentiates_strong_vs_weak(self) -> None:
        result = parse_strategy_result(
            json.dumps(SAMPLE_RESPONSE, ensure_ascii=False),
            subtitles=SAMPLE_SUBTITLES,
            target_duration=45.0,
        )
        s1, s2 = result.strategies
        # S1 有 hook+mechanism+result，S2 只有 hook+benefit 且缺 mechanism/result
        self.assertGreater(s1.story_strength, s2.story_strength)
        self.assertNotEqual(s1.story_strength, s2.story_strength)

    def test_story_strength_penalizes_missing_mechanism_and_result(self) -> None:
        full = compute_story_strength(
            [__import__("commercial_analyzer").EvidenceItem("hook", "x", (1,))],
            missing_roles=["mechanism", "proof", "benefit", "result"],
        )
        self.assertLess(full, 0.3)

    def test_material_sufficiency_depends_on_duration_and_target(self) -> None:
        result = parse_strategy_result(
            json.dumps(SAMPLE_RESPONSE, ensure_ascii=False),
            subtitles=SAMPLE_SUBTITLES,
            target_duration=45.0,
        )
        s1 = result.strategies[0]
        # S1 引用了 7 条字幕，总时长约 7+4.3+6+2.8+4.5+2.7+3.5 ≈ 30s
        self.assertGreater(s1.material_sufficiency, 0.0)
        self.assertLessEqual(s1.material_sufficiency, 1.0)

    def test_material_sufficiency_rises_with_longer_target(self) -> None:
        short = compute_material_sufficiency(
            [__import__("commercial_analyzer").EvidenceItem("hook", "x", (9, 22))],
            {9: 4.7, 22: 4.3},
            target_duration=45.0,
        )
        long = compute_material_sufficiency(
            [__import__("commercial_analyzer").EvidenceItem("hook", "x", (9, 22))],
            {9: 4.7, 22: 4.3},
            target_duration=15.0,
        )
        # 同样的素材，目标时长越短，覆盖度越高
        self.assertGreater(long, short)


class PromptTests(unittest.TestCase):
    def test_prompt_uses_extended_role_taxonomy(self) -> None:
        prompt = build_analyzer_user_prompt(product="x", subtitles=[])
        combined = ANALYZER_SYSTEM_PROMPT + prompt
        self.assertIn("chapter_kind", combined)
        self.assertIn("pain", combined)
        self.assertIn("mechanism", combined)
        self.assertIn("scene", combined)
        self.assertIn("trust", combined)

    def test_prompt_forbids_model_outputting_total_score(self) -> None:
        self.assertIn("不要输出卖点清单、证据库存、评分或自我审计", ANALYZER_SYSTEM_PROMPT)

    def test_prompt_requires_only_compact_director_fields(self) -> None:
        prompt = build_analyzer_user_prompt(product="x", subtitles=[])
        for field in ("core_desire", "chapter_packets", "buyer_advance", "verbatim", "final_readthrough"):
            self.assertIn(field, prompt)
        for legacy_field in ("story_premise", "core_evidence_pool", "supporting_evidence_pool", "bridge_candidates"):
            self.assertNotIn(legacy_field, prompt)

    def test_prompt_selects_one_buyer_belief_before_source_casting(self) -> None:
        self.assertIn("先确定一个核心购买认知", ANALYZER_SYSTEM_PROMPT)
        self.assertIn("不要按高频词选主题", ANALYZER_SYSTEM_PROMPT)
        self.assertIn("设计 4–8 个有因果顺序的购买章节", ANALYZER_SYSTEM_PROMPT)

    def test_prompt_asks_for_source_span_and_verbatim_not_legacy_inventory(self) -> None:
        prompt = build_analyzer_user_prompt(product="x", subtitles=[])
        self.assertIn("source_span", prompt)
        self.assertIn("verbatim", prompt)
        self.assertNotIn("missing_roles", prompt)

    def test_prompt_does_not_duplicate_transcript_as_commercial_asset_ledger(self) -> None:
        prompt = build_analyzer_user_prompt(
            product="x",
            subtitles=SAMPLE_SUBTITLES[:1],
            commercial_assets=[{
                "candidate_id": 9,
                "subject_context": {"product_focus": "related_product", "confidence": "medium"},
                "asset_role": "styling_scene",
                "story_permission": "supporting_story",
                "reason": "搭配结果。",
            }],
        )
        self.assertIn("Hard-safe 原始字幕事实", prompt)
        self.assertIn("不提供商品名称", prompt)
        self.assertIn("[W001][IDs 9]", prompt)
        self.assertIn("scope=related_product", prompt)
        self.assertNotIn("Commercial Asset Ledger", prompt)
        self.assertNotIn("permission=supporting_story", prompt)
        self.assertNotIn("搭配结果。", prompt)

    def test_prompt_requires_one_call_purchase_journey_contract(self) -> None:
        prompt = build_analyzer_user_prompt(product="x", subtitles=[])
        self.assertIn("chapter_kind", prompt)
        self.assertIn("buyer_advance", prompt)
        self.assertIn("source_span", prompt)
        self.assertIn("verbatim", prompt)
        self.assertIn("final_readthrough", prompt)
        self.assertIn('"beats":[', prompt)
        self.assertIn('"beat_function":"result"', prompt)
        self.assertIn("不要输出 purchase_question_id、answer_role", prompt)
        self.assertNotIn("不要输出 purchase_question_id、answer_role、beats 数组", prompt)
        self.assertNotIn("opening_fallback_subtitle_ids", prompt)
        self.assertNotIn("opening_alternative_packages", prompt)
        self.assertNotIn("whole_video_audit", prompt)

    def test_prompt_casts_a_clean_opening_package_inside_the_same_director_call(self) -> None:
        prompt = build_analyzer_user_prompt(product="x", subtitles=[])

        self.assertIn("Opening 是 C1 的完整开场小故事", prompt)
        self.assertIn("opening=clean + surface=clean", prompt)
        self.assertIn("C1 总计 1–3 个 Beat", prompt)
        self.assertIn("仍须交付可编辑方案", prompt)
        self.assertIn("返回前必须在内部从头连读一次", prompt)
        self.assertIn("不得改写主播原话", prompt)

    def test_duration_depth_contract_changes_internal_beat_budget_without_adding_calls(self) -> None:
        standard = director_duration_depth_contract(60)
        deep = director_duration_depth_contract(90)

        self.assertEqual(standard["mode"], "standard")
        self.assertEqual(standard["expected_total_beats"], {"low": 15, "high": 22})
        self.assertEqual(standard["preferred_source_seconds"], {"low": 48.0, "high": 66.0})
        self.assertEqual(deep["mode"], "deep")
        self.assertEqual(deep["expected_total_beats"], {"low": 24, "high": 32})
        self.assertEqual(deep["preferred_source_seconds"], {"low": 72.0, "high": 99.0})

    def test_compact_chapter_packet_preserves_multiple_ai_selected_windows(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S1",
                "chapter_packets": [{
                    "chapter_id": "C1", "chapter_kind": "comfort",
                    "title": "夏天也干爽", "buyer_advance": "透气且不粘肉",
                    "beats": [
                        {"beat_id": "C1B1", "beat_function": "experience", "beat_advance": "先给体感", "source_span": {"start_id": 80, "end_id": 80}, "verbatim": "薄针织体感舒服"},
                        {"beat_id": "C1B2", "beat_function": "proof", "source_span": {"start_id": 81, "end_id": 81}, "verbatim": "体感舒服没有之一"},
                    ],
                }],
            }],
        }
        strategy = parse_strategy_result(
            json.dumps(payload, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES,
        ).strategies[0]

        self.assertEqual([item.subtitle_ids for item in strategy.director_sequence], [(80,), (81,)])
        self.assertEqual([item.role for item in strategy.director_sequence], ["experience", "proof"])
        self.assertEqual(strategy.director_sequence[0].goal, "先给体感")
        self.assertEqual([item.purchase_question_id for item in strategy.director_sequence], ["Q4", "Q4"])

    def test_director_beat_expands_ai_authored_inclusive_source_span(self) -> None:
        beat = DirectorBeat.from_dict({
            "beat_id": "B1",
            "role": "result",
            "source_span": {"start_id": 101, "end_id": 103},
        }, 1)

        self.assertEqual(beat.subtitle_ids, (101, 102, 103))
        self.assertEqual(beat.to_dict()["source_span"], {"start_id": 101, "end_id": 103})

    def test_prompt_exposes_policy_gated_structure_catalog_and_packet_contract(self) -> None:
        prompt = build_analyzer_user_prompt(
            product="x", subtitles=[],
            content_contract={"price": "block", "cta": "block", "inventory_pressure": "block"},
        )
        self.assertIn("可选视频结构", prompt)
        self.assertIn('"id": "pain_point"', prompt)
        self.assertIn('"id": "scene_immersion"', prompt)
        self.assertNotIn('"id": "price_anchor"', prompt)
        self.assertNotIn('"id": "urgency_conversion"', prompt)
        self.assertIn("chapter_packets", prompt)
        self.assertNotIn("whole_video_audit", prompt)

    def test_prompt_keeps_supporting_purchase_chapters_inside_one_directed_film(self) -> None:
        self.assertIn("购买章节", ANALYZER_SYSTEM_PROMPT)
        self.assertIn("正面、侧面、后背、肩部", ANALYZER_SYSTEM_PROMPT)
        self.assertIn("完整字幕存在这些证据时优先采用", ANALYZER_SYSTEM_PROMPT)

    def test_prompt_uses_context_windows_without_story_specific_schema_anchoring(self) -> None:
        prompt = build_analyzer_user_prompt(
            product="x",
            subtitles=[
                {"id": 1, "start": 0.0, "end": 1.2, "text": "第一句，"},
                {"id": 2, "start": 1.3, "end": 3.6, "text": "第二句讲完整。"},
            ],
        )
        self.assertIn("spoken window", prompt)
        self.assertIn("IDs 1-2", prompt)
        self.assertIn("[W001][IDs 1-2][00:00:00.000-00:00:03.600]", prompt)
        self.assertIn("第一句， 第二句讲完整。", prompt)
        self.assertNotIn("大身材也能穿出利落感", prompt)
        self.assertNotIn("让怕显胖的人相信这件能修饰身形且日常好穿", prompt)
        self.assertIn("不能截取窗口内单行", prompt)
        self.assertIn("pain+result+mechanism 合计最多3章", ANALYZER_SYSTEM_PROMPT)
        self.assertIn("final_readthrough", prompt)

    def test_prompt_uses_complete_executable_pool_not_a_ranked_slice(self) -> None:
        prompt = build_analyzer_user_prompt(
            product="x",
            subtitles=SAMPLE_SUBTITLES,
            commercial_assets=[{"candidate_id": 9, "story_permission": "main_story"}],
            executable_subtitle_ids=[9, 29, 81],
        )
        self.assertIn("完整安全且可执行的字幕池", prompt)
        self.assertIn("IDs 9", prompt)
        self.assertIn("IDs 29", prompt)
        self.assertIn("IDs 81", prompt)
        self.assertNotIn("IDs 22", prompt)
        self.assertIn("不是 TopK", prompt)


class TwoPassDirectorTests(unittest.TestCase):
    def test_director_controls_are_idempotent_and_do_not_weaken_quality(self) -> None:
        controls = normalize_director_controls({
            "primary_category": "服饰内衣", "secondary_category": "自动识别",
            "main_product": " 焦糖朗姆套装 ", "goal": "场景种草",
            "selling_points": ["场景搭配", "场景搭配", "穿着体验"],
            "priority_terms": ["三伏天", "不粘身"], "focus_hint": "自动",
            "hook_style": "上身效果开头", "ending_style": "自然结束",
            "avoid": ["闲聊", "价格", "闲聊"], "strictness": "宽松",
            "preference_weights": {"场景搭配": 3, "面料质感": -2, "异常": float("nan")},
        })
        self.assertEqual(controls["director_direction"], "场景种草")
        self.assertEqual(controls["preferred_topics"], ["场景搭配", "穿着体验"])
        self.assertEqual(controls["preferred_terms"], ["三伏天", "不粘身"])
        self.assertEqual(controls["preference_weights"], {"场景搭配": 3.0, "面料质感": 0.0})
        self.assertEqual(controls["secondary_category"], "")
        self.assertNotIn("strictness", controls)
        self.assertEqual(normalize_director_controls(controls), controls)

    def test_both_prompts_receive_ui_controls_with_explicit_priority(self) -> None:
        controls = {
            "primary_category": "服饰内衣", "secondary_category": "女装",
            "leaf_category": "针织套装", "main_product": "焦糖朗姆",
            "goal": "场景种草", "selling_points": ["穿着体验"],
            "priority_terms": ["三伏天"], "avoid": ["闲聊", "搭配其他品"],
            "hook_style": "上身效果开头", "ending_style": "自然结束",
            "preference_weights": {"场景搭配": 3},
        }
        shared = {"subtitles": SAMPLE_SUBTITLES, "director_controls": controls,
                  "content_contract": {"price": "block"}}
        story_prompt = build_two_pass_story_prompt(
            product="焦糖朗姆", director_focus={"core_desire": "已选夏日场景"}, **shared,
        )
        cast_prompt = build_two_pass_cast_prompt(story_contract={"core_desire": "夏日场景"}, **shared)
        for prompt in (story_prompt, cast_prompt):
            for expected in ("焦糖朗姆", "针织套装", "场景种草", "穿着体验", "三伏天",
                             "搭配其他品", "上身效果开头", "自然结束", '"场景搭配":3.0'):
                self.assertIn(expected, prompt)
            self.assertIn("优先讲/重点词不是必须覆盖清单", prompt)
            self.assertIn("本次导演方向/优先讲 > 长期选片倾向", prompt)
            self.assertIn("价格/报价：不得进入成片", prompt)
        self.assertIn("不要重新换方向", story_prompt)
        self.assertIn("不得改写或重构第一遍故事", cast_prompt)

    def test_extract_json_repairs_padded_numeric_id_without_touching_text(self) -> None:
        parsed = _extract_json(
            '{"subtitle_ids":[099,7],"spoken":"原句里的 099 保持不变"}'
        )

        self.assertEqual(parsed["subtitle_ids"], [99, 7])
        self.assertEqual(parsed["spoken"], "原句里的 099 保持不变")

    def test_story_contract_uses_complete_pool_but_selects_no_subtitle_ids(self) -> None:
        prompt = build_two_pass_story_prompt(
            product="x",
            subtitles=[{"id": 77, "start": 0.0, "end": 2.0, "text": "大身材穿着也很利落"}],
            content_contract={"price": "block", "inventory_pressure": "block"},
            executable_subtitle_ids=[77],
            target_duration=60,
            duration_tolerance=5,
        )

        self.assertIn("大身材穿着也很利落", prompt)
        self.assertIn("[ID 077]", prompt)
        self.assertIn("55.0-65.0 秒", prompt)
        self.assertIn("没有 Strong Ranking、没有 TopK", prompt)
        self.assertIn("chapter_packets", prompt)
        self.assertIn('"chapter_job"', prompt)
        self.assertNotIn('"subtitle_ids"', prompt)
        self.assertNotIn('"beats"', prompt)
        self.assertIn("不得出现 beats、subtitle_ids", prompt)
        for repeated_field in (
            '"verbatim"', '"source_span"', '"beat_advance"',
            '"why_this_follows"', '"final_readthrough"',
        ):
            self.assertNotIn(repeated_field, prompt)
        self.assertNotIn("4–8", TWO_PASS_STORY_SYSTEM_PROMPT + prompt)
        self.assertNotIn("4至7", TWO_PASS_STORY_SYSTEM_PROMPT + prompt)
        self.assertNotIn("expected_total_beats", TWO_PASS_STORY_SYSTEM_PROMPT + prompt)
        self.assertIn("价格/报价：不得进入成片", prompt)
        self.assertIn("库存/稀缺催促：不得进入成片", prompt)
        self.assertNotIn("00:00:", prompt)
        self.assertLess(prompt.index("[ID 077]"), prompt.index("商品：x"))

    def test_beat_casting_uses_complete_pool_and_keeps_long_complete_exception(self) -> None:
        audit = {
            "preferred_low": 48.0,
            "preferred_high": 66.0,
            "chapter_count": 5,
            "story_contract_valid": True,
            "warnings": [],
        }
        prompt = build_two_pass_cast_prompt(
            story_contract={"core_desire": "夏天穿得轻松"},
            story_audit=audit,
            subtitles=[
                {"id": 1, "start": 0.0, "end": 0.8, "text": "太短"},
                {"id": 2, "start": 1.0, "end": 2.0, "text": "刚好一秒"},
                {"id": 3, "start": 3.0, "end": 8.0, "text": "刚好五秒"},
                {"id": 4, "start": 9.0, "end": 14.1, "text": "超过五秒"},
                {"id": 5, "start": 15.0, "end": 17.0, "text": "不在执行池"},
            ],
            executable_subtitle_ids=[1, 2, 3, 4],
            target_duration=60,
        )

        self.assertIn("[ID 002]", prompt)
        self.assertIn("[ID 003]", prompt)
        self.assertNotIn("[ID 001]", prompt)
        self.assertIn("[ID 004]", prompt)
        self.assertIn("long_complete_exception", prompt)
        self.assertNotIn("[ID 005]", prompt)
        self.assertIn("没有 Strong Ranking、没有 TopK", prompt)
        self.assertIn("subtitle_ids 只能有一个 ID", prompt)
        self.assertIn('"subtitle_ids"', prompt)
        self.assertIn('"chapter_readthrough"', prompt)
        self.assertNotIn('"verbatim"', prompt)
        self.assertNotIn('"source_span"', prompt)
        self.assertIn('"chapter_count":5', prompt)
        self.assertIn("不得为下限填充", prompt)
        self.assertIn("48.0-66.0 秒", prompt)
        self.assertNotIn("expected_total_beats", TWO_PASS_CAST_SYSTEM_PROMPT + prompt)
        self.assertNotIn("00:00:", prompt)
        self.assertLess(prompt.index("[ID 002]"), prompt.index("下面是第一遍 AI"))

    def test_chapter_alternatives_survive_without_entering_final_sequence(self) -> None:
        strategy = Strategy.from_dict({
            "strategy_id": "S1",
            "chapter_packets": [{
                "chapter_id": "C1",
                "title": "先讲显瘦结果",
                "beats": [{"beat_id": "C1B1", "beat_function": "result", "subtitle_ids": [8]}],
                "alternative_beats": [{
                    "beat_id": "C1A1",
                    "beat_function": "result",
                    "subtitle_ids": [9],
                    "replaces_beat_id": "C1B1",
                }],
            }],
        }, index=1)

        self.assertEqual([beat.subtitle_ids for beat in strategy.director_sequence], [(8,)])
        self.assertEqual(
            [beat.subtitle_ids for beat in strategy.director_chapter_packets[0].alternative_beats],
            [(9,)],
        )
        serialized = strategy.to_dict()["chapter_packets"][0]
        self.assertEqual(serialized["alternative_beats"][0]["replaces_beat_id"], "C1B1")

    def test_story_audit_flags_any_first_call_subtitle_selection(self) -> None:
        audit = build_two_pass_story_audit({"strategies": [{
            "director_plan_role": "primary",
            "core_desire": "穿得利落",
            "central_promise": "版型让日常穿着更利落",
            "chapter_packets": [{
                "chapter_id": "C1", "coverage": "required",
                "beats": [{"subtitle_ids": [7]}],
            }],
        }]})

        self.assertFalse(audit["story_contract_valid"])
        self.assertEqual(audit["unexpected_selected_subtitle_ids"], [7])
        self.assertIn("story_stage_must_not_select_subtitle_ids", audit["warnings"])

    def test_draft_audit_measures_exact_ids_without_semantic_mutation(self) -> None:
        draft = {"strategies": [{
            "director_plan_role": "primary",
            "chapter_packets": [{"beats": [
                {"source_span": {"start_id": 1, "end_id": 1}},
                {"source_span": {"start_id": 2, "end_id": 2}},
            ]}],
        }]}
        audit = build_two_pass_draft_audit(
            initial_draft=draft,
            subtitles=[
                {"id": 1, "start": 0.0, "end": 2.0, "text": "显瘦结果"},
                {"id": 2, "start": 2.1, "end": 5.1, "text": "因为肩线往里收"},
                {"id": 3, "start": 5.2, "end": 9.2, "text": "夏天穿着也透气"},
            ],
            executable_subtitle_ids=[1, 2, 3],
            target_duration=60,
        )

        self.assertEqual(audit["selected_subtitle_ids"], [1, 2])
        self.assertEqual(audit["actual_seconds"], 5.0)
        self.assertEqual(audit["unused_pool_count"], 1)
        self.assertTrue(any("possible_dangling_context" in item for item in audit["warnings"]))

    def test_duration_range_respects_explicit_ui_tolerance(self) -> None:
        self.assertEqual(director_target_duration_range(60, 10)["preferred_low"], 50.0)
        self.assertEqual(director_target_duration_range(60, 10)["preferred_high"], 70.0)
        self.assertEqual(director_target_duration_range(120, None)["preferred_low"], 96.0)
        self.assertEqual(director_target_duration_range(120, None)["preferred_high"], 120.0)

    def test_two_pass_analyzer_calls_exactly_twice_and_freezes_story_fields(self) -> None:
        subtitles = [
            {"id": 1, "start": 0.0, "end": 2.0, "text": "穿上正面很显窄"},
            {"id": 2, "start": 2.1, "end": 4.1, "text": "肩线会往里收"},
        ]
        story = {
            "strategies": [{
                "strategy_id": "S1", "director_plan_role": "primary",
                "director_title": "冻结标题",
                "core_desire": "大身材也能穿得利落",
                "central_promise": "用真实版型说明大身材也能利落",
                "opening_promise": "先看到显窄结果",
                "narrative_archetype": "pain_point",
                "video_structure": {"id": "pain_point", "name": "痛点切入", "selection_reason": "有结果和机制"},
                "chapter_packets": [{
                    "chapter_id": "C1", "chapter_kind": "result", "title": "先给结果",
                    "buyer_advance": "先看见显窄", "coverage": "required",
                    "chapter_job": "先给出结果",
                }],
            }, {
                "strategy_id": "S2", "director_title": "夏日场景",
                "director_plan_role": "alternative",
                "core_desire": "夏天轻松出门", "opening_promise": "先给夏日体验",
                "narrative_archetype": "scene_immersion", "chapter_packets": [],
            }],
        }
        cast = {"strategies": [{
            "strategy_id": "wrong", "director_plan_role": "primary",
            "director_title": "不得覆盖冻结标题", "core_desire": "不得覆盖冻结欲望",
            "chapter_packets": [{
                "chapter_id": "C1",
                "beats": [
                    {"beat_function": "result", "subtitle_ids": [1]},
                    {"beat_function": "mechanism", "subtitle_ids": [2]},
                ],
                "continuity_status": "pass",
            }],
            "whole_video_audit": {"progression": "结果到机制"},
        }]}
        captured_stages = []
        progress_stages = []

        with mock.patch(
            "commercial_analyzer._post_two_pass_director_request",
            side_effect=[json.dumps(story, ensure_ascii=False), json.dumps(cast, ensure_ascii=False)],
        ) as post:
            result = analyze_commercial_story(
                api_key="test-key", base_url="https://example.invalid/v1", model="test-model",
                product="x", subtitles=subtitles, executable_subtitle_ids=[1, 2],
                two_pass_director=True,
                director_controls={"goal": "显瘦转化", "priority_terms": ["利落"]},
                stage_response_hook=lambda stage, _value: captured_stages.append(stage),
                stage_progress_hook=progress_stages.append,
            )

        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            self.assertIn('"director_direction":"显瘦转化"', call.kwargs["user_prompt"])
            self.assertIn('"preferred_terms":["利落"]', call.kwargs["user_prompt"])
        self.assertEqual(captured_stages, ["story_contract", "beat_casting"])
        self.assertEqual(post.call_args_list[0].kwargs["stage"], "Director_story_contract")
        self.assertEqual(post.call_args_list[1].kwargs["stage"], "Director_beat_casting")
        self.assertIn("chapter_readthrough", post.call_args_list[1].kwargs["user_prompt"])
        self.assertEqual(post.call_args_list[1].kwargs["max_tokens"], 8000)
        self.assertEqual(progress_stages, [
            "story_contract_started", "story_contract_completed",
            "beat_casting_started", "beat_casting_completed",
        ])
        primary, alternative = result.strategies
        self.assertEqual(primary.director_title, "冻结标题")
        self.assertEqual(primary.core_desire, "大身材也能穿得利落")
        self.assertEqual([beat.subtitle_ids for beat in primary.director_sequence], [(1,), (2,)])
        self.assertEqual(primary.director_chapter_packets[0].title, "先给结果")
        self.assertEqual(
            primary.director_chapter_packets[0].chapter_readthrough,
            "穿上正面很显窄｜肩线会往里收",
        )
        self.assertEqual(alternative.director_plan_role, "alternative")
        self.assertEqual(alternative.director_sequence, ())

    def test_compact_two_pass_beats_keep_ai_order_and_chapter_semantics(self) -> None:
        payload = {"strategies": [{
            "strategy_id": "S1", "director_plan_role": "primary",
            "director_title": "显瘦也舒服", "core_desire": "夏天轻松穿得显瘦",
            "chapter_packets": [{
                "chapter_id": "C1", "chapter_kind": "result", "title": "先给结果",
                "purchase_question_id": "Q1", "buyer_advance": "正面显窄", "coverage": "required",
                "beats": [
                    {"beat_function": "result", "subtitle_ids": [1]},
                    {"beat_function": "proof", "subtitle_ids": [2]},
                ],
            }],
        }]}

        strategy = parse_strategy_result(
            json.dumps(payload, ensure_ascii=False),
            subtitles=[
                {"id": 1, "start": 0.0, "end": 2.0, "text": "穿上正面很显窄"},
                {"id": 2, "start": 2.1, "end": 4.1, "text": "肩线会往里收"},
            ],
        ).strategies[0]

        self.assertEqual([beat.subtitle_ids for beat in strategy.director_sequence], [(1,), (2,)])
        self.assertEqual([beat.role for beat in strategy.director_sequence], ["result", "proof"])
        self.assertEqual([beat.purchase_question_id for beat in strategy.director_sequence], ["Q1", "Q1"])

    def test_two_pass_token_limit_reports_truncation_instead_of_generic_json_error(self) -> None:
        provider_response = {
            "choices": [{
                "message": {"content": '{"strategies":[{"strategy_id":"S1"}'},
                "finish_reason": "length",
            }],
            "usage": {"completion_tokens": 16000},
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(provider_response).encode("utf-8")

        with (
            mock.patch("commercial_analyzer.urllib.request.urlopen", return_value=FakeResponse()),
            mock.patch("commercial_analyzer.record_ai_call") as ledger,
        ):
            with self.assertRaisesRegex(AnalyzerError, "16000 token 上限，JSON 被截断"):
                _post_two_pass_director_request(
                    api_key="test", base_url="https://example.invalid/v1", model="deepseek-v4-pro",
                    system_prompt="system", user_prompt="user", stage="Director_beat_casting",
                    max_tokens=16000, timeout=30,
                )

        self.assertFalse(ledger.call_args.kwargs["success"])
        self.assertEqual(ledger.call_args.kwargs["error_type"], "output_truncated")

    def test_two_pass_casting_disables_provider_thinking(self) -> None:
        captured = {}
        provider_response = {
            "choices": [{"message": {"content": '{"strategies":[]}'}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 8},
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(provider_response).encode("utf-8")

        def fake_urlopen(request, **_kwargs):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with (
            mock.patch("commercial_analyzer.urllib.request.urlopen", side_effect=fake_urlopen),
            mock.patch("commercial_analyzer.record_ai_call"),
        ):
            _post_two_pass_director_request(
                api_key="test", base_url="https://api.deepseek.com",
                model="deepseek-v4-pro", system_prompt="system", user_prompt="user",
                stage="Director_beat_casting", max_tokens=8000, timeout=30,
            )

        self.assertEqual(captured["body"]["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", captured["body"])


class CallTests(unittest.TestCase):
    def test_deepseek_default_flash_is_upgraded_only_for_commercial_director(self) -> None:
        self.assertEqual(
            resolve_commercial_director_model("https://api.deepseek.com", "deepseek-v4-flash"),
            "deepseek-v4-pro",
        )
        self.assertEqual(
            resolve_commercial_director_model("https://example.invalid/v1", "deepseek-v4-flash"),
            "deepseek-v4-flash",
        )

    def test_analyze_posts_and_computes_scores(self) -> None:
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                content = json.dumps(SAMPLE_RESPONSE, ensure_ascii=False)
                return json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, **_kwargs):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with mock.patch("commercial_analyzer.urllib.request.urlopen", side_effect=fake_urlopen):
            result = analyze_commercial_story(
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="deepseek-v4-flash",
                product="简致衬衫",
                subtitles=SAMPLE_SUBTITLES,
                target_duration=45.0,
            )

        self.assertEqual(len(result.strategies), 2)
        self.assertGreater(result.strategies[0].story_strength, result.strategies[1].story_strength)
        self.assertNotIn("test-key", json.dumps(captured["body"], ensure_ascii=False))
        self.assertEqual(captured["body"]["max_tokens"], 5000)


class ContractCompatibilityTests(unittest.TestCase):
    def test_compute_contract_compatibility_levels(self) -> None:
        from commercial_analyzer import compute_contract_compatibility
        self.assertEqual(compute_contract_compatibility([]), (1.0, "recommended"))
        self.assertEqual(compute_contract_compatibility(["after_sales"]), (0.75, "conditional"))
        self.assertEqual(compute_contract_compatibility(["price", "inventory", "cta"]), (0.25, "not_recommended"))

    def test_contract_enforcement_uses_dependencies_not_an_absent_contract(self) -> None:
        payload = {
            "strategies": [
                {
                    "strategy_id": "S4",
                    "type": "trust_builder",
                    "thesis": "老粉口碑售后",
                    "evidence": [
                        {"role": "trust", "subtitle_ids": [10], "claim": "老粉回购"},
                        {"role": "trust", "subtitle_ids": [11], "claim": "售后包退换"},
                    ],
                    "content_dependencies": ["social_proof", "after_sales"],
                }
            ]
        }
        result = parse_strategy_result(
            json.dumps(payload, ensure_ascii=False),
            subtitles=SAMPLE_SUBTITLES,
            content_contract={"social_proof": "block", "after_sales": "block"},
        )
        s = result.strategies[0]
        self.assertEqual(s.contract_compatibility, 0.5)
        self.assertEqual(s.strategy_viability, "not_recommended")
        self.assertEqual(s.blocked_evidence_types, ("after_sale", "social_proof"))

    def test_no_contract_keeps_sensitive_dependency_without_blocking(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S-price",
                "type": "value_trust",
                "thesis": "实体店价值对比",
                "content_dependencies": ["price"],
                "evidence": [{"role": "trust", "subtitle_ids": [4], "claim": "实体店三四百元"}],
            }]
        }
        subtitles = [*SAMPLE_SUBTITLES, {"id": 4, "start": 12.0, "end": 16.0, "text": "实体店三四百元"}]
        result = parse_strategy_result(json.dumps(payload, ensure_ascii=False), subtitles=subtitles)
        s = result.strategies[0]
        self.assertIn("price", s.content_dependencies)
        self.assertEqual(s.blocked_evidence_types, ())
        self.assertEqual(s.contract_compatibility, 1.0)

    def test_no_blocked_types_means_recommended(self) -> None:
        result = parse_strategy_result(
            json.dumps(SAMPLE_RESPONSE, ensure_ascii=False),
            subtitles=SAMPLE_SUBTITLES,
        )
        for s in result.strategies:
            self.assertEqual(s.contract_compatibility, 1.0)
            self.assertEqual(s.strategy_viability, "recommended")

    def test_hard_audit_catches_after_sale_even_if_llm_reports_none(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        subtitle_text_map = {11: "如有异味免费包退换"}
        evidence = [EvidenceItem("trust", "售后承诺", (11,))]
        blocked, hits = hard_audit_blocked_types(evidence, subtitle_text_map, {"after_sale": "block"})
        self.assertIn("after_sale", blocked)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].type, "after_sale")
        self.assertEqual(hits[0].subtitle_id, 11)
        self.assertEqual(hits[0].matched_keyword, "退换")
        self.assertEqual(hits[0].evidence_role, "trust")

    def test_hard_audit_returns_empty_when_no_contract(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        subtitle_text_map = {11: "如有异味免费包退换"}
        evidence = [EvidenceItem("trust", "售后承诺", (11,))]
        self.assertEqual(hard_audit_blocked_types(evidence, subtitle_text_map, None), ((), ()))


class StoryValidityAndDurationTests(unittest.TestCase):
    def test_strong_28_second_story_remains_valid_for_a_60_second_task(self) -> None:
        payload = {
            "strategies": [{
                "strategy_id": "S-safe-skirt",
                "type": "problem_transformation",
                "thesis": "保留短裙风格，同时解决日常走光顾虑",
                "story_premise": "喜欢短裙的女生，担心一动就走光，终于有了安全长度。",
                "audience_tension": "喜欢短裙但担心走光。",
                "story_trigger": "样衣太短，抬腿会露。",
                "transformation": "不安全的短裙 → 大货加长两公分 → 日常也敢穿。",
                "core_commercial_idea": "短裙可以保留风格，同时拥有日常安全感。",
                "payoff": "穿着时不再反复担心走光。",
                "core_evidence_pool": [
                    {"role": "problem", "subtitle_ids": [9, 22], "claim": "样衣偏短会走光"},
                    {"role": "mechanism", "subtitle_ids": [29, 30], "claim": "大货加长两公分"},
                    {"role": "result", "subtitle_ids": [33, 34, 58], "claim": "刚好到安全位置"},
                ],
            }]
        }
        result = parse_strategy_result(
            json.dumps(payload, ensure_ascii=False),
            subtitles=SAMPLE_SUBTITLES,
            target_duration=60.0,
        )
        story = result.strategies[0]
        self.assertEqual(story.story_validity, "recommended")
        self.assertEqual(story.duration_feasibility, "insufficient")
        self.assertGreaterEqual(story.recommended_duration_seconds, 28.0)
        self.assertEqual(story.target_duration_seconds, 60.0)

    def test_duration_feasibility_is_independent_from_story_validity(self) -> None:
        self.assertEqual(compute_duration_feasibility(28.0, 60.0)[0], "insufficient")
        self.assertEqual(compute_duration_feasibility(52.0, 60.0)[0], "sufficient")

    def test_dependency_detection_is_independent_of_contract(self) -> None:
        evidence = [
            __import__("commercial_analyzer").EvidenceItem("trust", "自留背书", (1,)),
            __import__("commercial_analyzer").EvidenceItem("close", "价格", (2,)),
        ]
        dependencies = detect_content_dependencies(
            evidence,
            {1: "我自己都留了一件", 2: "这个价格真是太划算了"},
        )
        self.assertEqual(dependencies, ("price", "social_proof"))


class SemanticGoldenTests(unittest.TestCase):
    def test_commercial_change_accepts_equivalent_story_across_fields(self) -> None:
        fixture_path = os.path.join(ROOT, "tests", "fixtures", "commercial_story_semantic_goldens.json")
        with open(fixture_path, "r", encoding="utf-8") as handle:
            contract = json.load(handle)["hanxi_short_skirt_safety"]["commercial_change"]
        payload = {
            "strategies": [{
                "strategy_id": "safe-short-skirt",
                "story_premise": "样衣太短会露，大货加长两公分。",
                "audience_tension": "喜欢短裙但担心走光。",
                "transformation": "从担心尴尬到穿出俏皮感。",
                "core_commercial_idea": "加长后的短裙更安全，可以放心穿。",
                "core_evidence_pool": [
                    {"role": "problem", "subtitle_ids": [9], "claim": "样衣短会露"},
                    {"role": "mechanism", "subtitle_ids": [22], "claim": "大货加长两公分"},
                ],
            }]
        }
        strategy = parse_strategy_result(json.dumps(payload, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES).strategies[0]
        assessment = assess_story_commercial_change(strategy, contract)
        self.assertTrue(assessment["passed"])
        self.assertTrue(assessment["stages"]["problem"]["passed"])
        self.assertTrue(assessment["stages"]["solution"]["passed"])
        self.assertTrue(assessment["stages"]["outcome"]["passed"])

    def test_commercial_change_rejects_selling_point_list_without_solution(self) -> None:
        contract = {
            "problem": (("闷热",),),
            "solution": (("透气",),),
            "outcome": (("舒适",),),
        }
        payload = {
            "strategies": [{
                "strategy_id": "selling-points-only",
                "thesis": "面料舒适口袋好看",
                "audience_tension": "夏天穿衣闷热。",
                "core_commercial_idea": "面料好。",
                "evidence": [{"role": "proof", "subtitle_ids": [9], "claim": "面料舒适"}],
            }]
        }
        strategy = parse_strategy_result(json.dumps(payload, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES).strategies[0]
        self.assertFalse(assess_story_commercial_change(strategy, contract)["passed"])

    def test_optional_supporting_signal_is_reported_but_not_required(self) -> None:
        contract = {
            "problem": (("职业",),),
            "solution": (("单穿",),),
            "outcome": (("有型",),),
            "optional_body_inclusivity": (("微胖", "显瘦"),),
        }
        strategy = parse_strategy_result(json.dumps({"strategies": [{
            "audience_tension": "衬衫太职业。",
            "transformation": "单穿也有型。",
            "core_evidence_pool": [{"role": "result", "subtitle_ids": [9], "claim": "有型"}],
        }]}, ensure_ascii=False), subtitles=SAMPLE_SUBTITLES).strategies[0]
        assessment = assess_story_commercial_change(strategy, contract)
        self.assertTrue(assessment["passed"])
        self.assertFalse(assessment["optional_supporting_signals"]["optional_body_inclusivity"]["passed"])


class KeywordPrecisionTests(unittest.TestCase):
    def test_effect_description_not_size_interaction(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        evidence = [EvidenceItem("benefit", "藏肉效果", (1,))]
        subtitle_text_map = {1: "整个人至少能被藏掉十几二十斤的肉"}
        types, _ = hard_audit_blocked_types(evidence, subtitle_text_map, {"size_interaction": "block"})
        self.assertNotIn("size_interaction", types)

    def test_size_guidance_is_size_interaction(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        evidence = [EvidenceItem("scene", "尺码", (2,))]
        subtitle_text_map = {2: "106斤穿M码"}
        types, _ = hard_audit_blocked_types(evidence, subtitle_text_map, {"size_interaction": "block"})
        self.assertIn("size_interaction", types)

    def test_quality_expression_not_price(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        evidence = [EvidenceItem("proof", "质感", (3,))]
        subtitle_text_map = {3: "穿上会闷，主要是不显贵"}
        types, _ = hard_audit_blocked_types(evidence, subtitle_text_map, {"price": "block"})
        self.assertNotIn("price", types)

    def test_price_amount_is_price(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        evidence = [EvidenceItem("close", "价格", (4,))]
        subtitle_text_map = {4: "三四百元的价格"}
        types, _ = hard_audit_blocked_types(evidence, subtitle_text_map, {"price": "block"})
        self.assertIn("price", types)

    def test_garment_measurement_not_size_interaction(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        evidence = [EvidenceItem("proof", "版型", (5,))]
        subtitle_text_map = {5: "前衣长58后衣长65，遮肚子"}
        types, _ = hard_audit_blocked_types(evidence, subtitle_text_map, {"size_interaction": "block"})
        self.assertNotIn("size_interaction", types)

    def test_live_size_guidance_is_size_interaction(self) -> None:
        from commercial_analyzer import hard_audit_blocked_types, EvidenceItem
        evidence = [EvidenceItem("scene", "尺码", (6,))]
        subtitle_text_map = {6: "M码穿到125斤"}
        types, _ = hard_audit_blocked_types(evidence, subtitle_text_map, {"size_interaction": "block"})
        self.assertIn("size_interaction", types)


if __name__ == "__main__":
    unittest.main()
