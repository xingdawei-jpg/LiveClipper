"""Opening/short-beat fidelity: no semantic substitution, safety stays active."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "web_client")]
import server
from commercial_analyzer import (
    Strategy, TWO_PASS_CAST_SYSTEM_PROMPT, TWO_PASS_STORY_SYSTEM_PROMPT,
    _normalize_two_pass_director_payload, build_two_pass_story_prompt,
    build_two_pass_cast_prompt, normalize_director_controls,
)
from content_policy import default_content_policy


class DirectorOpeningFidelityTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(server, "_preview_forbidden_words", return_value=[])
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def preview(texts, *, preserve=True, policy=None, word_timed=True):
        raw = [("hook" if i == 0 else "product", text, 10.0 * i, 10.0 * i + 3.0, 80, 3.0) for i, text in enumerate(texts)]
        words = [{"words": [
            {"text": char, "start": start + j * 3.0 / len(text), "end": start + (j + 1) * 3.0 / len(text)}
            for j, char in enumerate(text)
        ]} for _, text, start, *_ in raw]
        public = server._preview_public_clips(
            raw, word_timings=words if word_timed else [],
            preserve_director_selection=preserve,
            content_policy=policy if policy is not None else default_content_policy(),
        )
        for i, clip in enumerate(public):
            clip.update(director_chapter_id="C1" if i < 3 else "C2", director_candidate_id=i + 1)
        return {"id": "fidelity-test", "raw_clips": raw, "clips": public, "commercial_director_sentence_preview": True}

    def test_question_reason_payoff_survives_as_separate_short_beats(self):
        texts = ["你家里有那么多白衬衫为什么还想选这件", "因为这件衣服给到你是普通白衬衫", "给不到你的干净利落的精细裁剪", "而且这样的量感需要工艺", "然后下摆还要做这样的版型"]
        preview = self.preview(texts)
        self.assertTrue(all(s["selected"] for c in preview["clips"] for s in c["segments"]))
        self.assertIn("continuity_warning", preview["clips"][1]["segments"][0])
        final = server._clips_from_preview_selection(preview, list(range(len(texts))))
        self.assertEqual([c[1] for c in final], texts)
        self.assertTrue(all(c[5] <= 3.01 for c in final))
        self.assertEqual(server._director_preview_fidelity_audit(preview["clips"])["status"], "preserved")

    def test_legacy_prefix_selection_is_unchanged(self):
        preview = self.preview(["而且这样的量感需要工艺"], preserve=False)
        self.assertFalse(preview["clips"][0]["segments"][0]["selected"])

    def test_no_words_fallback_preserves_real_leading_words(self):
        text = "然后这样的版型会让你穿得更利落"
        preview = self.preview([text], word_timed=False)
        self.assertEqual(preview["clips"][0]["segments"][0]["text"], text)
        self.assertEqual(server._clips_from_preview_selection(preview, [0])[0][1], text)

    def test_word_deletion_remains_exact(self):
        preview = self.preview(["而且这个袖子用料足"])
        kept = server._clips_from_preview_selection(preview, [0], selected_words={"0": {"0": list(range(2, 9))}})
        self.assertEqual([c[1] for c in kept], ["这个袖子用料足"])
        self.assertAlmostEqual(kept[0][2], 0.667, places=3)

    def test_dangling_piece_is_not_programmatically_removed_for_director(self):
        preview = self.preview(["整套穿搭会更加的"], word_timed=False)
        legacy = self.preview(["整套穿搭会更加的"], preserve=False, word_timed=False)
        with mock.patch.object(server, "_preview_standalone_fragment_reason", return_value="依赖下一句"):
            self.assertEqual(server._clips_from_preview_selection(preview, [0])[0][1], "整套穿搭会更加的")
            self.assertEqual(server._clips_from_preview_selection(legacy, [0]), [])

    def test_task_price_policy_is_used_in_preview_and_export(self):
        text = "这一件的价格是199元"
        blocked = self.preview([text])
        self.assertTrue(blocked["clips"][0]["segments"][0]["selection_locked"])
        policy = {**default_content_policy(), "price": "allow"}
        allowed = self.preview([text], policy=policy)
        self.assertTrue(allowed["clips"][0]["segments"][0]["selected"])
        final = server._clips_from_preview_selection(allowed, [0])
        with mock.patch.object(server, "emit_log"):
            self.assertEqual(len(server._hard_filter_preview_selection(final, "test", content_policy=policy)), 1)

    def test_forbidden_token_stays_locked_and_opening_warns(self):
        with mock.patch.object(server, "_preview_forbidden_words", return_value=["绝对"]):
            preview = self.preview(["这件绝对显得肩窄", "而且下摆更利落"])
            audit = server._director_preview_fidelity_audit(preview["clips"])
            final = server._clips_from_preview_selection(preview, [0, 1])
        self.assertEqual(audit["status"], "warning")
        self.assertTrue(audit["opening_affected"])
        self.assertFalse(audit["opening_auto_replaced"])
        self.assertNotIn("绝对", "".join(c[1] for c in final))

    def test_export_safety_removal_is_reported_not_replaced(self):
        preview = self.preview(["先看版型效果", "而且这样的量感需要工艺"])
        before = server._clips_from_preview_selection(preview, [0, 1])
        after = before[1:]
        with mock.patch.object(server, "emit_log"), mock.patch.object(server, "_set_task"), mock.patch.object(server, "_store_preview") as store:
            audit = server._record_director_export_fidelity(preview, before, after, "task", "smart-cut")
        self.assertTrue(audit["opening_affected"])
        self.assertFalse(audit["opening_auto_replaced"])
        self.assertEqual(audit["changed_clips"][0]["preview_parent_index"], 0)
        self.assertEqual(after, before[1:])
        self.assertEqual(store.call_args.kwargs["director_review"]["export_fidelity"], audit)

    def test_export_fidelity_does_not_treat_manual_edits_as_program_removal(self):
        preview = self.preview(["而且这个袖子用料足"])
        edited = server._clips_from_preview_selection(preview, [0], selected_words={"0": {"0": list(range(2, 9))}})
        with mock.patch.object(server, "_set_task"), mock.patch.object(server, "_store_preview"):
            audit = server._record_director_export_fidelity(preview, edited, edited, "task", "mix")
        self.assertEqual(audit["status"], "preserved")

    @staticmethod
    def normalized(receipt_ids=(213, 214), link=(213, 214), *, repeated=False, readthrough=""):
        scope = {"main_product": "白衬衫", "sales_scope": "single_product", "supporting_products_rule": "仅讲搭配"}
        story = {"strategies": [{"strategy_id": "S1", "core_desire": "白衬衫有何不同", "product_scope": scope,
                                  "chapter_packets": [{"chapter_id": "C1", "title": "为什么选它"}]}]}
        cast = {"strategies": [{"product_scope": {"main_product": "裙子"},
                                "opening_selection": {"selected_subtitle_ids": list(receipt_ids), "compared_packages": [{"subtitle_ids": [213, 214]}, {"subtitle_ids": [215]}]},
                                "chapter_packets": [{"chapter_id": "C1", "chapter_readthrough": readthrough, "beats": [{"subtitle_ids": [213]}, {"subtitle_ids": [214]}] + ([{"subtitle_ids": [213]}] if repeated else [])}],
                                "whole_video_audit": {"status": "pass", "continuity_links": [{"from_id": link[0], "to_id": link[1]}]}}]}
        rows = [{"id": i, "text": f"原话{i}"} for i in (213, 214, 215)]
        return _normalize_two_pass_director_payload(story, cast, casting_rows=rows)["strategies"][0]

    def test_opening_receipt_and_product_scope_survive_strategy(self):
        primary = self.normalized()
        self.assertEqual(primary["opening_selection"]["verification"]["status"], "consistent")
        strategy = Strategy.from_dict(primary, 1)
        self.assertEqual(strategy.to_dict()["product_scope"]["main_product"], "白衬衫")
        self.assertEqual(strategy.to_dict()["opening_selection"]["selected_subtitle_ids"], [213, 214])

    def test_wrong_receipt_warns_without_replacing_actual_opening(self):
        primary = self.normalized(receipt_ids=(215,))
        self.assertEqual(primary["opening_selection"]["verification"]["status"], "warning")
        self.assertEqual([b["subtitle_ids"] for b in primary["chapter_packets"][0]["beats"]], [[213], [214]])

    def test_reversed_dependency_warns_without_reordering(self):
        primary = self.normalized(link=(214, 213))
        self.assertEqual(primary["whole_video_audit"]["status"], "needs_review")
        self.assertEqual([b["subtitle_ids"] for b in primary["chapter_packets"][0]["beats"]], [[213], [214]])

    def test_repeated_ids_warn_without_programmatic_deletion(self):
        primary = self.normalized(repeated=True)
        self.assertEqual(primary["whole_video_audit"]["status"], "needs_review")
        self.assertEqual([b["subtitle_ids"] for b in primary["chapter_packets"][0]["beats"]], [[213], [214], [213]])

    def test_ai_readthrough_cannot_rewrite_source_audio(self):
        primary = self.normalized(readthrough="AI 自己改写的好听口播")
        self.assertEqual(primary["whole_video_audit"]["status"], "needs_review")
        self.assertEqual(primary["chapter_packets"][0]["chapter_readthrough"], "原话213｜原话214")

    def test_controls_and_both_prompts_include_non_factual_identity_hint(self):
        controls = normalize_director_controls({"source_product_hints": ["品牌优雅白色衬衫"], "main_product": "用户指定衬衫"})
        self.assertEqual(normalize_director_controls(controls), controls)
        kwargs = {"subtitles": [{"id": 1, "start": 0, "end": 3, "text": "袖子很特别"}], "director_controls": controls}
        story = build_two_pass_story_prompt(product="当前直播商品", **kwargs)
        casting = build_two_pass_cast_prompt(story_contract={}, **kwargs)
        for prompt in (story, casting):
            self.assertIn("品牌优雅白色衬衫", prompt)
            self.assertIn("用户指定衬衫", prompt)
            self.assertIn("不能据此编造", prompt)
        self.assertIn("product_scope", story)
        self.assertNotIn('"subtitle_ids"', story)
        self.assertIn("single_product/explicit_set", TWO_PASS_STORY_SYSTEM_PROMPT)
        self.assertIn("2–3 个真实短句开场组合", TWO_PASS_CAST_SYSTEM_PROMPT)
        self.assertIn("opening_selection", casting)

    @unittest.skipUnless(shutil.which("node"), "Node required")
    def test_ui_boundary_warning_does_not_claim_good_opening_or_block_export(self):
        script = (ROOT / "web_client/frontend/assets/app.js").read_text(encoding="utf-8")
        function = script[script.index("function previewDirectorCurrentStatus("):script.index("function focusPreviewDirectorChapter(")]
        code = 'const buildPreviewFilmOverview = () => ({status:"ok",issues:[]});\n' + function
        code += 'console.log(JSON.stringify(previewDirectorCurrentStatus("smart",{director_review:{preview_fidelity:{status:"warning",opening_affected:true,message:"需复核"}}},"target",[{director_beat_function:"result"}],{})));'
        result = subprocess.run([shutil.which("node"), "-e", code], capture_output=True, text=True, encoding="utf-8", check=True)
        status = json.loads(result.stdout)
        self.assertEqual(status["status"], "warn")
        self.assertEqual(status["openingLabel"], "开场受内容边界影响")
        self.assertEqual(status["boundaryMessage"], "需复核")


if __name__ == "__main__":
    unittest.main()
