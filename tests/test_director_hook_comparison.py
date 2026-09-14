"""The production two-pass Hook receipt is evidence, not a local selector."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from commercial_analyzer import (
    Strategy, _director_casting_rows, _normalize_two_pass_director_payload,
    build_two_pass_cast_prompt, build_two_pass_story_prompt,
)
from director_opening_audit import OPENING_RECEIPT_VERSION, audit_opening_candidates
from director_wire_schema import compact_director_wire_payload, expand_director_wire_payload


class DirectorHookComparisonTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {"id": 1, "start": 0, "end": 2, "text": "我跟你说这件穿上肩很窄"},
            {"id": 2, "start": 2.1, "end": 4.6, "text": "因为肩线向里面收了一点"},
            {"id": 3, "start": 7, "end": 9, "text": "这一件是圆领的"},
        ]
        self.opening = {
            "receipt_version": OPENING_RECEIPT_VERSION,
            "selected_subtitle_ids": [1, 2], "quality": "usable",
            "compared_packages": [
                {"subtitle_ids": [1], "payoff_subtitle_ids": [2], "product_evidence_ids": [1],
                 "decision": "selected", "reason": "肩窄结果由肩线解释承接"},
                {"subtitle_ids": [3], "payoff_subtitle_ids": [], "product_evidence_ids": [],
                 "decision": "rejected", "reason": "普通介绍，缺少立即兑现素材"},
            ],
        }

    def audit(self, opening=None, executed_ids=(1, 2, 3)):
        return audit_opening_candidates(opening or self.opening, source_rows=self.rows, executed_ids=executed_ids)

    def test_reconstructs_original_words_and_times_without_keyword_ranking(self):
        before = copy.deepcopy(self.opening)
        result = self.audit()
        self.assertEqual(result["status"], "references_consistent")
        self.assertEqual(result["candidates"][0]["hook_source"], [self.rows[0]])
        self.assertEqual(result["candidates"][0]["source_seconds"], 4.5)
        self.assertEqual(self.opening, before)
        self.assertFalse(result["program_selected_or_reordered"])

    def test_source_membership_does_not_certify_appeal_or_same_sku(self):
        result = self.audit()
        self.assertEqual(result["quality_status"], "human_review_required")
        self.assertEqual(result["product_identity_status"], "human_review_required")
        self.assertEqual(result["appeal_and_continuation_status"], "human_review_required")

    def test_ai_supplied_rewritten_quote_is_not_used_as_source(self):
        self.opening["compared_packages"][0]["verbatim"] = "AI 编造的显瘦效果"
        result = self.audit()
        self.assertEqual(result["candidates"][0]["hook_source"][0]["text"], self.rows[0]["text"])

    def test_outside_pool_payoff_and_identity_evidence_are_detected(self):
        for field in ("subtitle_ids", "payoff_subtitle_ids", "product_evidence_ids"):
            with self.subTest(field=field):
                opening = copy.deepcopy(self.opening)
                opening["compared_packages"][0][field] = [999]
                result = self.audit(opening)
                self.assertEqual(result["status"], "warning")
                self.assertTrue(any(field in issue for issue in result["issues"]))

    def test_unsafe_removed_id_cannot_be_reintroduced_by_receipt(self):
        self.rows = self.rows[:2]
        self.assertIn("candidate_2:subtitle_ids:missing_or_outside_safe_pool", self.audit()["issues"])

    def test_product_counterevidence_survives_policy_filter_in_source_order(self):
        rows = [
            {"id": 1, "start": 0, "end": 2, "text": "上衣的设计是原创"},
            {"id": 2, "start": 2, "end": 4, "text": "搭的裤子有水墨画的感觉"},
            {"id": 3, "start": 4, "end": 6, "text": "这个裤子不是原创"},
        ]
        for prompt in (
            build_two_pass_story_prompt(product="裤子", subtitles=rows, executable_subtitle_ids=[2]),
            build_two_pass_cast_prompt(story_contract={}, subtitles=rows, executable_subtitle_ids=[2]),
        ):
            self.assertLess(prompt.index("[context ID 001]"), prompt.index("[ID 002]"))
            self.assertLess(prompt.index("[ID 002]"), prompt.index("[context ID 003]"))
            self.assertIn(rows[2]["text"], prompt)
            self.assertNotIn("[ID 001]", prompt)
            self.assertNotIn("[ID 003]", prompt)
        self.assertEqual(_director_casting_rows(rows, [2]), [rows[1]])

    def test_empty_safe_pool_cannot_reenable_context_rows(self):
        self.assertEqual(_director_casting_rows(self.rows, []), [])
        self.assertEqual(_director_casting_rows(self.rows, None), self.rows)

    def test_context_can_prove_identity_but_cannot_become_hook_or_payoff(self):
        context = {"id": 90, "start": 90, "end": 93, "text": "这件外套，不是刚才的衬衫"}
        self.opening["compared_packages"][0]["product_evidence_ids"] = [90]
        result = audit_opening_candidates(self.opening, source_rows=self.rows,
                                         product_context_rows=[context], executed_ids=[1, 2])
        self.assertEqual(result["status"], "references_consistent")
        self.assertEqual(result["candidates"][0]["product_evidence_source"], [context])
        for key in ("subtitle_ids", "payoff_subtitle_ids"):
            opening = copy.deepcopy(self.opening)
            opening["compared_packages"][0][key] = [90]
            result = audit_opening_candidates(opening, source_rows=self.rows,
                                             product_context_rows=[context], executed_ids=[90, 2])
            self.assertIn(f"candidate_1:{key}:missing_or_outside_safe_pool", result["issues"])

    def test_repeated_hook_is_not_an_immediate_new_payoff(self):
        self.opening["compared_packages"][0]["payoff_subtitle_ids"] = [1]
        self.assertIn("candidate_1:repeated_opening_id", self.audit()["issues"])

    def test_receipt_does_not_insert_missing_or_reorder_delayed_payoff(self):
        for executed in ([1, 3, 2], [1, 3], [2, 1]):
            result = self.audit(executed_ids=executed)
            self.assertIn("candidate_1:payoff_not_immediately_executed", result["issues"])
            self.assertFalse(result["program_selected_or_reordered"])

    def test_rejection_may_truthfully_lack_payoff_or_identity(self):
        self.assertEqual(self.audit()["candidates"][1]["issues"], [])

    def test_empty_unknown_and_non_integer_receipts_do_not_pass(self):
        for value in ([], None, [True], ["1"], [1.0]):
            with self.subTest(value=value):
                opening = copy.deepcopy(self.opening)
                opening["compared_packages"][0]["subtitle_ids"] = value
                self.assertEqual(self.audit(opening)["status"], "warning")
        opening = {**self.opening, "compared_packages": []}
        self.assertEqual(self.audit(opening)["status"], "warning")
        self.assertEqual(self.audit({"selected_subtitle_ids": [1, 2]})["status"], "not_recorded")

    def test_wrong_rank_or_multiple_selected_packages_warn_without_sorting(self):
        self.opening["compared_packages"].reverse()
        self.assertIn("candidate_2:selected_package_not_first", self.audit()["issues"])
        self.opening["compared_packages"][0]["decision"] = "selected"
        self.assertIn("requires_one_selected_package", self.audit()["issues"])

    def test_real_normalization_wire_and_strategy_preserve_comparisons(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [{"chapter_id": "C1"}]}]}
        cast = {"strategies": [{"strategy_id": "S1", "opening_selection": self.opening,
            "chapter_packets": [{"chapter_id": "C1", "beats": [{"subtitle_ids": [1]}, {"subtitle_ids": [2]}]}]}]}
        wire = compact_director_wire_payload(cast)
        result = _normalize_two_pass_director_payload(story, expand_director_wire_payload(wire), casting_rows=self.rows)
        strategy = Strategy.from_dict(result["strategies"][0], 1).to_dict()
        self.assertEqual(strategy["opening_selection"]["compared_packages"], self.opening["compared_packages"])
        self.assertEqual(strategy["opening_selection"]["candidate_audit"]["status"], "references_consistent")
        self.assertEqual(strategy["final_readthrough"], "我跟你说这件穿上肩很窄｜因为肩线向里面收了一点")

    def test_normalizer_surfaces_false_selected_receipt_without_substitution(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [{"chapter_id": "C1"}]}]}
        cast = {"strategies": [{"strategy_id": "S1", "opening_selection": self.opening,
            "chapter_packets": [{"chapter_id": "C1", "beats": [{"subtitle_ids": [3]}, {"subtitle_ids": [2]}]}]}]}
        primary = _normalize_two_pass_director_payload(story, cast, casting_rows=self.rows)["strategies"][0]
        self.assertEqual(primary["whole_video_audit"]["status"], "needs_review")
        self.assertEqual(primary["opening_selection"]["candidate_audit"]["status"], "warning")
        self.assertEqual([beat["subtitle_ids"] for beat in primary["chapter_packets"][0]["beats"]], [[3], [2]])

    def test_full_pool_prompt_requests_comparison_without_another_call_or_rewrites(self):
        prompt = build_two_pass_cast_prompt(story_contract={}, subtitles=self.rows)
        for row in self.rows:
            self.assertIn(row["text"], prompt)
        self.assertIn(OPENING_RECEIPT_VERSION, prompt)
        self.assertIn("同品类不等于同一件商品", prompt)
        self.assertIn("quality=limited", prompt)
        self.assertIn("结果型Hook之后须增加解释或证据", prompt)
        self.assertIn("selected 的 reason 必须点明 payoff 新增的具体事实", prompt)
        self.assertIn("任一答案缺失就换候选或 quality=limited", prompt)
        self.assertIn("不得加入未选原话的视觉细节、设计名或结论", prompt)
        self.assertIn("默改疑似 ASR 错词", prompt)
        self.assertNotIn("不返回 alternative_beats、候选开场", prompt)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_ui_does_not_hide_receipt_warning_behind_result_role(self):
        root = Path(__file__).resolve().parents[1]
        script = (root / "web_client/frontend/assets/app.js").read_text()
        function = script[script.index("function previewDirectorCurrentStatus("):script.index("function focusPreviewDirectorChapter(")]
        code = 'const buildPreviewFilmOverview = () => ({status:"ok",issues:[]});\n' + function
        code += 'console.log(JSON.stringify(previewDirectorCurrentStatus("smart",{director_review:{opening_selection:{verification:{status:"warning"}}}},"target",[{director_beat_function:"result"}],{})));'
        result = subprocess.run([shutil.which("node"), "-e", code], capture_output=True, text=True, check=True)
        status = json.loads(result.stdout)
        self.assertEqual(status["status"], "warn")
        self.assertEqual(status["openingLabel"], "开场回执待复核")
        self.assertIn("紧接的兑现句", status["openingMessage"])


if __name__ == "__main__":
    unittest.main()
