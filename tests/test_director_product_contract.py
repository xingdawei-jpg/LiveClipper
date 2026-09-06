import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from director_product_contract import build_product_target, audit_product_selection, scope_errors
from commercial_analyzer import analyze_commercial_story, AnalyzerError, build_two_pass_story_prompt, build_two_pass_cast_prompt


class ProductContractTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"id": 1, "start": 0, "end": 2, "text": "这件白衬衫的肩线很利落"},
            {"id": 2, "start": 2, "end": 4, "text": "面料轻薄透气"},
            {"id": 3, "start": 50, "end": 52, "text": "这条裤子能遮O型腿"},
            {"id": 4, "start": 52, "end": 54, "text": "这件衬衫配牛仔裤就很利落"},
        ]
        self.controls = {"primary_category": "服饰内衣", "leaf_category": "衬衫", "main_product": "白衬衫"}
        self.story = {"main_product": "白衬衫", "product_type": "shirt", "sales_scope": "single_product", "identity_evidence_ids": [1], "target_confirmation": "match",
                      "source_product_sections": [
                          {"start_id": 1, "end_id": 2, "subject_product": "白衬衫", "product_type": "shirt", "identity_evidence_ids": [1]},
                          {"start_id": 3, "end_id": 3, "subject_product": "裤子", "product_type": "pants", "identity_evidence_ids": [3]},
                          {"start_id": 4, "end_id": 4, "subject_product": "白衬衫", "product_type": "shirt", "identity_evidence_ids": [4]},
                      ]}

    def story_payload(self, kind="shirt"):
        scope = dict(self.story)
        if kind == "pants":
            scope.update(main_product="裤子", product_type="pants", identity_evidence_ids=[3])
        return {"strategies": [{"strategy_id": "S1", "director_plan_role": "primary", "core_desire": "清爽利落" if kind == "shirt" else "遮腿", "product_scope": scope,
                                "chapter_packets": [{"chapter_id": "C1", "title": "穿着结果", "chapter_kind": "result", "coverage": "required", "chapter_job": "说明穿着结果"}]}]}

    def beat(self, i=1, **kwargs):
        return {"subtitle_ids": [i], "beat_function": "result", "product_relation": "main_product", "subject_product": "白衬衫", "subject_product_type": "shirt", "product_evidence_ids": [1], **kwargs}

    def cast(self, beats=None):
        return {"strategies": [{"strategy_id": "S1", "chapter_packets": [{"chapter_id": "C1", "completion_status": "complete", "beats": beats or [self.beat(), self.beat(2)]}], "whole_video_audit": {"status": "pass"}}]}

    def audit(self, beat):
        return audit_product_selection({"product_scope": self.story}, self.cast([beat])["strategies"][0], target=build_product_target(self.controls), subtitles=self.rows)

    def run_ai(self, story, cast, revision=None, controls=None, **kwargs):
        responses = [json.dumps(story), json.dumps(cast)] + ([json.dumps(revision)] if revision is not None else [])
        with patch("commercial_analyzer._post_two_pass_director_request", side_effect=responses) as post:
            result = analyze_commercial_story(api_key="test", base_url="https://example.invalid", model="test", product="白衬衫", subtitles=self.rows, two_pass_director=True, target_duration=4, duration_tolerance=1, director_controls=self.controls if controls is None else controls, **kwargs)
        return result, post.call_args_list

    def test_explicit_name_wins_over_stale_trousers_leaf(self):
        target = build_product_target({"main_product": "白色抽褶衬衫", "leaf_category": "裤子"})
        self.assertEqual(target["product_type"], "shirt")
        self.assertFalse(scope_errors(self.story, target))

    def test_unambiguous_source_title_binds_auto_product_not_clip_keywords(self):
        target = build_product_target({"source_product_hints": ["AELE中袖T恤T8532832AD_1", "AELE中袖T恤T8532832AD_2"]})
        self.assertEqual((target["mode"], target["identity_source"], target["product_type"]), ("locked", "source_title", "tshirt"))
        self.assertTrue(scope_errors({**self.story, "main_product": "天丝神裤", "product_type": "pants"}, target))

    def test_user_choice_wins_over_title_and_ambiguous_title_does_not_bind(self):
        self.assertEqual(build_product_target({"main_product": "裤子", "source_product_hints": ["中袖T恤"]})["product_type"], "pants")
        for hints in [["衬衫配裤子"], ["T恤", "裤子"], ["录屏1"]]:
            self.assertEqual(build_product_target({"source_product_hints": hints})["mode"], "auto")
    def test_leaf_alone_locks_single_product(self):
        target = build_product_target({"leaf_category": "针织衫"})
        self.assertEqual((target["mode"], target["product_type"]), ("locked", "knitwear"))
        self.assertTrue(scope_errors(self.story, target))

    def test_top_allows_specific_shirt_but_not_pants(self):
        target = build_product_target({"leaf_category": "上衣"})
        self.assertFalse(scope_errors(self.story, target))
        self.assertTrue(scope_errors({**self.story, "main_product": "裤子", "product_type": "pants"}, target))

    def test_broad_clothes_requests_specific_category_before_spending(self):
        with patch("commercial_analyzer._post_two_pass_director_request") as post, self.assertRaisesRegex(AnalyzerError, "范围太宽"):
            analyze_commercial_story(api_key="test", base_url="https://example.invalid", model="test", product="衣服", subtitles=self.rows, two_pass_director=True, director_controls={"main_product": "衣服"})
        post.assert_not_called()

    def test_other_product_cannot_be_attributed_to_shirt(self):
        audit = self.audit(self.beat(3, subject_product="裤子", subject_product_type="pants"))
        self.assertEqual(audit["conflicting_subtitle_ids"], [3])
        self.assertFalse(audit["program_deleted_beats"])

    def test_other_product_section_cannot_be_disguised_as_styling_support(self):
        audit = self.audit(self.beat(3, product_relation="styling_support", subject_product="裤子",
                                    subject_product_type="pants", product_evidence_ids=[3],
                                    supports_main_product="声称能搭配衬衫"))
        self.assertEqual(audit["status"], "conflict")
        self.assertIn("选句位于其他商品的讲解范围", audit["beats"][0]["issues"])

    def test_sustained_explicit_pants_talk_overrules_a_false_tshirt_section(self):
        rows = self.rows + [
            {"id": 10, "start": 10, "end": 12, "text": "我特别喜欢我这条裤子"},
            {"id": 12, "start": 12, "end": 14, "text": "这个牛仔裤今年主推"},
            {"id": 14, "start": 14, "end": 16, "text": "小个子把裤脚卷起来"},
        ]
        story = {**self.story, "source_product_sections": [
            {"start_id": 1, "end_id": 20, "subject_product": "T恤", "product_type": "tshirt", "identity_evidence_ids": [1]}
        ], "main_product": "T恤", "product_type": "tshirt"}
        beat = self.beat(12, subject_product="T恤", subject_product_type="tshirt", product_evidence_ids=[1])
        audit = audit_product_selection({"product_scope": story}, self.cast([beat])["strategies"][0],
                                        target=build_product_target({"leaf_category": "T恤"}), subtitles=rows)
        self.assertEqual(audit["status"], "conflict")
        self.assertIn("选句邻近字幕持续明确讲其他商品", audit["beats"][0]["issues"])
        self.assertIn("pants", [item["product_type"] for item in audit["foreign_product_ranges"]])

    def test_one_styling_mention_does_not_create_a_product_switch(self):
        rows = self.rows[:2] + [{"id": 10, "start": 10, "end": 12, "text": "这个衬衫配裤子就很好看"}]
        from director_product_contract import foreign_product_ranges
        self.assertEqual(foreign_product_ranges("shirt", rows), [])

    def test_renaming_type_only_is_not_identity_repair(self):
        self.assertEqual(self.audit(self.beat(3, subject_product="裤子"))["status"], "conflict")

    def test_styling_sentence_mentioning_pants_is_allowed(self):
        self.assertEqual(self.audit(self.beat(4, product_relation="styling_support", subject_product="牛仔裤", subject_product_type="pants", product_evidence_ids=[4], supports_main_product="展示衬衫的日常搭配"))["status"], "consistent")

    def test_visible_exclude_styling_choice_is_enforced_without_auto_deletion(self):
        self.controls["supporting_products"] = "block"
        audit = self.audit(self.beat(4, product_relation="styling_support", subject_product="牛仔裤", subject_product_type="pants", product_evidence_ids=[4], supports_main_product="展示衬衫的日常搭配"))
        self.assertEqual(audit["status"], "conflict")
        self.assertIn("本次已排除其他商品搭配", audit["beats"][0]["issues"])
        self.assertFalse(audit["program_deleted_beats"])
        self.assertEqual(self.audit(self.beat())["status"], "consistent")

    def test_uncertain_reference_and_fabricated_evidence_are_not_pass(self):
        self.assertEqual(self.audit(self.beat(product_relation="uncertain"))["status"], "conflict")
        self.assertEqual(self.audit(self.beat(product_evidence_ids=[999]))["status"], "conflict")

    def test_explicit_set_may_describe_component(self):
        self.story.update(main_product="衬衫裤子套装", product_type="set", sales_scope="explicit_set")
        self.controls.update(main_product="衬衫裤子套装", leaf_category="套装")
        self.assertEqual(self.audit(self.beat(3, subject_product="裤子", subject_product_type="pants", product_evidence_ids=[3], set_component=True))["status"], "consistent")

    def test_auto_can_identify_actual_set_but_explicit_shirt_cannot(self):
        scope = {**self.story, "main_product": "套装", "product_type": "set", "sales_scope": "explicit_set"}
        self.assertFalse(scope_errors(scope, build_product_target({})))
        self.assertTrue(scope_errors(scope, build_product_target(self.controls)))

    def test_complete_source_context_is_read_only_not_castable(self):
        story_prompt = build_two_pass_story_prompt(product="白衬衫", subtitles=self.rows, executable_subtitle_ids=[1, 2])
        cast_prompt = build_two_pass_cast_prompt(story_contract=self.story_payload(), subtitles=self.rows, executable_subtitle_ids=[1, 2])
        for prompt in [story_prompt, cast_prompt]:
            self.assertIn("[context ID 003][不可选", prompt)
            self.assertNotIn("[ID 003]", prompt)
        self.assertEqual(cast_prompt.count("[ID 001]"), 1)
        self.assertEqual(cast_prompt.count("[context ID 003]"), 1)

    def test_unlocked_category_is_a_soft_hint_when_source_disagrees(self):
        prompt = build_two_pass_story_prompt(
            product="", subtitles=self.rows,
            director_controls={"primary_category": "生鲜", "secondary_category": "食品/生鲜"},
        )
        self.assertIn("一级/二级类目只是项目提示", prompt)
        self.assertIn("以素材事实识别当前商品", prompt)

    def test_consistent_plan_keeps_two_calls_and_exact_ai_order(self):
        result, calls = self.run_ai(self.story_payload(), self.cast())
        self.assertEqual(len(calls), 2)
        self.assertEqual([b.subtitle_ids for b in result.strategies[0].director_sequence], [(1,), (2,)])
        self.assertEqual(result.strategies[0].whole_video_audit["product_control"]["final"]["status"], "consistent")

    def test_filtered_identity_context_is_not_false_rejected_or_selected(self):
        context = self.rows + [{"id": 90, "start": 200, "end": 213, "text": "这件白衬衫的价格介绍"}]
        story = self.story_payload()
        story["strategies"][0]["product_scope"]["identity_evidence_ids"] = [90]
        cast = self.cast([self.beat(1, product_evidence_ids=[90]), self.beat(2, product_evidence_ids=[90])])
        result, calls = self.run_ai(story, cast, source_context_subtitles=context)
        self.assertEqual(len(calls), 2)
        self.assertEqual([b.subtitle_ids for b in result.strategies[0].director_sequence], [(1,), (2,)])
        self.assertIn("[context ID 090][不可选", calls[0].kwargs["user_prompt"])

    def test_missing_identity_reference_can_be_repaired_in_second_call(self):
        story = self.story_payload()
        story["strategies"][0]["product_scope"]["identity_evidence_ids"] = [999]
        cast = self.cast()
        cast["corrected_story"] = self.story_payload()["strategies"][0]
        result, calls = self.run_ai(story, cast)
        self.assertEqual(len(calls), 2)
        self.assertIn("corrected_story", calls[1].kwargs["user_prompt"])
        self.assertEqual(result.strategies[0].product_scope["identity_evidence_ids"], [1])

    def test_wrong_story_is_repaired_by_existing_second_call_not_relabelled(self):
        cast = self.cast()
        cast["corrected_story"] = self.story_payload()["strategies"][0]
        result, calls = self.run_ai(self.story_payload("pants"), cast)
        self.assertEqual(len(calls), 2)
        self.assertIn("corrected_story", calls[1].kwargs["user_prompt"])
        self.assertEqual(result.strategies[0].core_desire, "清爽利落")

    def test_product_and_duration_share_one_correction_not_four_calls(self):
        first = self.cast([self.beat(3, subject_product="裤子", subject_product_type="pants"), self.beat(2)])
        first["strategies"][0]["opening_selection"] = {"selected_subtitle_ids": [3]}
        result, calls = self.run_ai(
            self.story_payload(),
            first,
            self.cast(),
            enable_duration_calibration=True,
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(result.strategies[0].director_sequence[0].subtitle_ids, (1,))

    def test_auto_product_ambiguity_does_not_trigger_a_third_paid_call(self):
        first = self.cast([self.beat(3, subject_product="裤子", subject_product_type="pants"), self.beat(2)])
        result, calls = self.run_ai(self.story_payload(), first, controls={})
        control = result.strategies[0].whole_video_audit["product_control"]
        self.assertEqual(len(calls), 2)
        self.assertFalse(control["blocking"])
        self.assertEqual(control["preview_status"], "preview_ready_with_warnings")

    def test_unrepaired_conflict_returns_editable_warning_instead_of_failing(self):
        first = self.cast([self.beat(3, subject_product="裤子", subject_product_type="pants"), self.beat(2)])
        result, _ = self.run_ai(self.story_payload(), first, copy.deepcopy(first))
        audit = result.strategies[0].whole_video_audit
        control = audit["product_control"]
        self.assertEqual(audit["status"], "needs_review")
        self.assertEqual(control["final"]["status"], "conflict")
        self.assertFalse(control["blocking"])
        self.assertEqual(control["preview_status"], "preview_ready_with_warnings")
        self.assertTrue(control["warnings"])


if __name__ == "__main__":
    unittest.main()
