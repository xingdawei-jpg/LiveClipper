import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from commercial_analyzer import (
    AnalyzerError, analyze_commercial_story, build_director_duration_audit,
    build_two_pass_story_prompt, build_two_pass_cast_prompt, director_delivery_duration_range,
    _extract_json,
    _casting_chapter_duration_budgets,
)


class DirectorDurationControlTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": i, "start": i * 5.0, "end": i * 5.0 + 3.0,
                      "text": f"真实口播{i}讲明一个证据"} for i in range(1, 61)]
        self.story = {"strategies": [{
            "strategy_id": "S1", "director_plan_role": "primary",
            "director_title": "同一故事", "core_desire": "夏天轻松穿得好看",
            "central_promise": "一套解决夏日穿搭",
            "chapter_packets": [{"chapter_id": f"C{i}", "chapter_kind": kind,
                                 "title": kind, "chapter_job": kind, "coverage": "required",
                                 "source_budget_seconds": 20,
                                 "completion_requirements": ["先说问题，再用证据说清结果"]}
                                for i, kind in enumerate(["result", "comfort", "styling"], 1)]
        }]}

    def cast(self, count):
        chapters = []
        per = count // 3
        for index in range(3):
            ids = range(index * per + 1, (index + 1) * per + 1 if index < 2 else count + 1)
            chapters.append({"chapter_id": f"C{index + 1}", "completion_status": "complete",
                             "continuity_status": "pass",
                             "beats": [{"subtitle_ids": [i], "beat_function": "proof"} for i in ids]})
        return {"strategies": [{"strategy_id": "S1", "chapter_packets": chapters,
                                "opening_selection": {"selected_subtitle_ids": [1]},
                                 "whole_video_audit": {"status": "pass"},
                                 "stop_reason": "模型估算已达90秒"}]}

    def cast_with_duration_fill(self, count, fill_ids):
        payload = self.cast(count)
        chapters = payload["strategies"][0]["chapter_packets"]
        for index, subtitle_id in enumerate(fill_ids):
            chapter = chapters[index % len(chapters)]
            chapter.setdefault("duration_fill_beats", []).append({
                "fill_priority": index + 1,
                "subtitle_ids": [subtitle_id],
                "beat_function": "proof",
                "fill_reason": "补上新的真实购买证据",
            })
        return payload

    def measure(self, cast, **kwargs):
        return build_director_duration_audit(casting_payload=cast, story_contract=self.story,
                                            subtitles=self.rows, target_duration=kwargs.pop("target_duration", 60), **kwargs)

    def run_ai(self, first, revision=None, **kwargs):
        story = kwargs.pop("story", self.story)
        responses = [json.dumps(story), json.dumps(first)]
        if revision is not None:
            responses.append(revision if isinstance(revision, Exception) else json.dumps(revision))
            kwargs.setdefault("enable_duration_calibration", True)
        captured = {}
        with patch("commercial_analyzer._post_two_pass_director_request", side_effect=responses) as post:
            result = analyze_commercial_story(
                api_key="test", base_url="https://example.invalid", model="test", product="x",
                subtitles=self.rows, two_pass_director=True, target_duration=kwargs.pop("target_duration", 60),
                stage_response_hook=lambda name, value: captured.update({name: value}), **kwargs)
        return result.strategies[0], post.call_args_list, captured

    def test_export_speed_contract_including_120_seconds(self):
        contract = director_delivery_duration_range(120, None, 1.15)
        self.assertAlmostEqual(contract["source_target"], 138)
        self.assertAlmostEqual(contract["source_min"], 115)
        self.assertAlmostEqual(contract["preferred_low"], 100)
        explicit = director_delivery_duration_range(90, 5, 1.2)
        self.assertAlmostEqual(explicit["source_min"], 102)

    def test_truthful_sum_ignores_model_claim_and_counts_chapters(self):
        audit = self.measure(self.cast(13), target_duration=90)
        self.assertEqual(audit["source_seconds"], 39)
        self.assertEqual(sum(c["source_seconds"] for c in audit["chapters"]), 39)
        self.assertTrue(audit["needs_calibration"])
        self.assertFalse(audit["target_range_fulfilled"])

    def test_story_budget_is_instructions_not_concrete_selection(self):
        prompt = build_two_pass_story_prompt(product="x", subtitles=self.rows, target_duration=90, output_speed_factor=1.15)
        self.assertIn("source_budget_seconds", prompt)
        self.assertIn("completion_requirements", prompt)
        self.assertIn('"source_target": 103.', prompt)
        self.assertIn("不得出现 beats、subtitle_ids", prompt)

    def test_physical_pool_limit_is_separate_from_model_selection_shortfall(self):
        self.rows = self.rows[:10]
        limited = self.measure(self.cast(6))
        self.assertEqual(limited["complete_pool_seconds"], 30)
        self.assertTrue(limited["pool_cannot_reach_minimum"])
        self.assertEqual(limited["pool_upper_bound_final_seconds"], 30)

    def test_good_plan_uses_only_two_calls(self):
        primary, calls, _ = self.run_ai(self.cast(20))
        self.assertEqual(len(calls), 2)
        self.assertEqual(primary.whole_video_audit["duration_control"]["status"], "target_range_fulfilled")

    def test_underlength_keeps_final_ai_sequence_without_fill_or_third_call(self):
        first = self.cast_with_duration_fill(16, [17, 18, 19])
        primary, calls, captured = self.run_ai(first, self.cast(20), enable_duration_calibration=False)
        control = primary.whole_video_audit["duration_control"]
        selected_ids = [beat.subtitle_ids[0] for beat in primary.director_sequence]
        self.assertEqual(len(calls), 2)
        self.assertNotIn("duration_calibration", captured)
        self.assertEqual(control["semantic_call_count"], 2)
        self.assertFalse(control["duration_fill"]["applied"])
        self.assertEqual(control["duration_fill"]["added_subtitle_ids"], [])
        self.assertEqual(selected_ids, list(range(1, 17)))
        self.assertEqual(control["status"], "target_not_met_editable")

    def test_each_multi_plan_strategy_is_measured_without_appending_its_fill_queue(self):
        story_two = {"strategies": []}
        for index, role in [(1, "primary"), (2, "alternative")]:
            strategy = json.loads(json.dumps(self.story["strategies"][0], ensure_ascii=False))
            strategy["strategy_id"] = f"S{index}"
            strategy["director_plan_role"] = role
            story_two["strategies"].append(strategy)
        primary_cast = self.cast(20)["strategies"][0]
        primary_cast["strategy_id"] = "S1"
        primary_cast["director_plan_role"] = "primary"
        alternative_cast = self.cast_with_duration_fill(16, [17])["strategies"][0]
        alternative_cast["strategy_id"] = "S2"
        alternative_cast["director_plan_role"] = "alternative"
        payload = {"strategies": [primary_cast, alternative_cast]}

        responses = [json.dumps(story_two), json.dumps(payload)]
        with patch("commercial_analyzer._post_two_pass_director_request", side_effect=responses) as post:
            result = analyze_commercial_story(
                api_key="test", base_url="https://example.invalid", model="test", product="x",
                subtitles=self.rows, two_pass_director=True, target_duration=60,
                director_plan_count=2,
            )

        self.assertEqual(len(post.call_args_list), 2)
        self.assertEqual(len(result.strategies), 2)
        alternative = result.strategies[1]
        control = alternative.whole_video_audit["duration_control"]
        self.assertEqual(alternative.strategy_id, "S2")
        self.assertEqual(control["semantic_call_count"], 2)
        self.assertFalse(control["duration_fill"]["applied"])
        self.assertEqual(control["duration_fill"]["added_subtitle_ids"], [])
        self.assertEqual(control["final"]["source_seconds"], 48)
        self.assertEqual(control["status"], "target_not_met_editable")
        self.assertEqual(result.strategies[0].whole_video_audit["duration_control"]["status"], "target_range_fulfilled")

    def test_cast_receipt_lists_only_executable_ids_and_their_real_seconds(self):
        prompt = build_two_pass_cast_prompt(
            story_contract=self.story, subtitles=self.rows,
            executable_subtitle_ids=[1, 3], source_context_subtitles=self.rows,
            target_duration=60, output_speed_factor=1.15,
        )
        table_line = prompt.split("可选时长表（ID→真实原声秒数）", 1)[1].splitlines()[1]
        self.assertEqual(json.loads(table_line), {"1": 3.0, "3": 3.0})
        schema = json.loads(prompt.split("实际回复必须使用 director-wire-v1：", 1)[1].splitlines()[1])
        chapter = schema["packet"]["strategies"][0]["chapter_packets"][0]
        self.assertNotIn("duration_fill_beats", chapter)
        self.assertLess(list(chapter).index("beats"), list(chapter).index("budget_execution"))
        self.assertIn("sec", chapter["beats"][0])

    def test_ai_receipt_cannot_inflate_measured_duration(self):
        payload = self.cast(10)
        for chapter in payload["strategies"][0]["chapter_packets"]:
            chapter["budget_execution"] = {"selected_source_seconds": 60}
            for beat in chapter["beats"]:
                beat["source_seconds"] = 20
        primary, calls, _ = self.run_ai(payload, output_speed_factor=1.15)
        self.assertEqual(len(calls), 2)
        audit = primary.whole_video_audit["duration_control"]["final"]
        self.assertEqual(audit["source_seconds"], 30)
        self.assertFalse(audit["target_range_fulfilled"])

    def test_chapter_budgets_scale_to_final_speed_target_without_changing_story(self):
        story = json.loads(json.dumps(self.story))
        story["strategies"][0]["chapter_packets"][0]["source_budget_seconds"] = 10
        before = json.dumps(story)
        budget = _casting_chapter_duration_budgets(story, 69)[0]
        self.assertEqual(json.dumps(story), before)
        self.assertEqual([c["chapter_id"] for c in budget["chapters"]], ["C1", "C2", "C3"])
        self.assertEqual([c["source_budget_seconds"] for c in budget["chapters"]], [13.8, 27.6, 27.6])
        self.assertEqual(budget["chapters"][-1]["cumulative_source_seconds"], 69)

    def test_underlength_calls_casting_once_more_with_full_pool_and_math(self):
        primary, calls, captured = self.run_ai(self.cast(10), self.cast(20))
        control = primary.whole_video_audit["duration_control"]
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[2].kwargs["stage"], "Director_duration_calibration")
        self.assertIn("[ID 060]", calls[2].kwargs["user_prompt"])
        self.assertIn('"source_seconds":30.0', calls[2].kwargs["user_prompt"])
        self.assertTrue(control["calibration"]["accepted_revision"])
        self.assertEqual(control["final"]["source_seconds"], 60)
        self.assertEqual(primary.core_desire, "夏天轻松穿得好看")
        self.assertIn("duration_control", captured)

    def test_overlong_plan_also_calibrates_without_program_trimming(self):
        primary, calls, _ = self.run_ai(self.cast(30), self.cast(20))
        self.assertEqual(len(calls), 3)
        self.assertEqual([b.subtitle_ids[0] for b in primary.director_sequence], list(range(1, 21)))

    def test_speed_is_applied_before_acceptance(self):
        primary, calls, _ = self.run_ai(self.cast(17), self.cast(23), output_speed_factor=1.15)
        self.assertEqual(len(calls), 3)
        self.assertEqual(primary.whole_video_audit["duration_control"]["final"]["projected_final_seconds"], 60)

    def test_incomplete_chapter_triggers_once_even_in_duration_range(self):
        first = self.cast(20)
        first["strategies"][0]["chapter_packets"][1]["completion_status"] = "needs_context"
        primary, calls, _ = self.run_ai(first, self.cast(20))
        self.assertEqual(len(calls), 3)
        self.assertEqual(primary.whole_video_audit["duration_control"]["initial"]["incomplete_chapter_ids"], ["C2"])

    def test_still_short_is_truthful_editable_no_fourth_call(self):
        primary, calls, _ = self.run_ai(self.cast(10), self.cast(13))
        self.assertEqual(len(calls), 3)
        self.assertEqual(primary.whole_video_audit["duration_control"]["status"], "target_not_met_editable")
        self.assertEqual(len(primary.director_sequence), 13)

    def test_worse_duration_revision_cannot_replace_better_first_draft(self):
        primary, calls, _ = self.run_ai(self.cast(15), self.cast(13))
        control = primary.whole_video_audit["duration_control"]
        self.assertEqual(len(calls), 3)
        self.assertFalse(control["calibration"]["accepted_revision"])
        self.assertEqual(control["calibration"]["fallback_reason"], "duration_revision_not_improved")
        self.assertEqual(len(primary.director_sequence), 15)

    def test_calibration_prompt_uses_compact_receipts_not_prior_readthrough(self):
        first = self.cast(10)
        first["strategies"][0]["chapter_packets"][0]["chapter_readthrough"] = "不应再次发送的长篇连读"
        _, calls, _ = self.run_ai(first, self.cast(20))
        prompt = calls[2].kwargs["user_prompt"]
        self.assertNotIn("不应再次发送的长篇连读", prompt)
        self.assertIn('"source_seconds":30.0', prompt)
        self.assertIn('"opening_subtitle_ids":[1]', prompt)

    def test_failed_optional_correction_keeps_first_draft(self):
        for failure in [AnalyzerError("timeout"), {}]:
            with self.subTest(failure=type(failure).__name__):
                primary, calls, _ = self.run_ai(self.cast(10), failure)
                self.assertEqual(len(calls), 3)
                self.assertEqual(len(primary.director_sequence), 10)
                self.assertFalse(primary.whole_video_audit["duration_control"]["calibration"]["accepted_revision"])

    def test_empty_first_cast_can_be_corrected_without_a_new_story_call(self):
        primary, calls, _ = self.run_ai(self.cast(0), self.cast(20))
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(primary.director_sequence), 20)

    def test_invalid_references_or_order_or_changed_opening_cannot_replace_draft(self):
        for fault in ["unknown", "order", "opening"]:
            with self.subTest(fault=fault):
                revised = self.cast(20)
                chapters = revised["strategies"][0]["chapter_packets"]
                if fault == "unknown":
                    chapters[-1]["beats"][-1]["subtitle_ids"] = [9999]
                elif fault == "order":
                    chapters.reverse()
                else:
                    chapters[0]["beats"].reverse()
                primary, _, _ = self.run_ai(self.cast(10), revised)
                self.assertEqual(len(primary.director_sequence), 10)

    def test_duplicates_cannot_inflate_available_seconds(self):
        cast = self.cast(10)
        cast["strategies"][0]["chapter_packets"][0]["beats"] *= 2
        audit = self.measure(cast)
        self.assertEqual(audit["source_seconds"], 39)
        self.assertEqual(audit["unique_source_seconds"], 30)
        self.assertTrue(audit["technical_valid"])
        self.assertTrue(audit["needs_calibration"])
        self.assertEqual(audit["duplicate_subtitle_ids"], [1, 2, 3])

    def test_duplicate_is_a_warning_not_program_semantic_deletion(self):
        revised = self.cast(20)
        revised["strategies"][0]["chapter_packets"][-1]["beats"][-1]["subtitle_ids"] = [2]
        primary, _, _ = self.run_ai(self.cast(10), revised)
        self.assertTrue(primary.whole_video_audit["duration_control"]["calibration"]["accepted_revision"])
        self.assertEqual(primary.director_sequence[-1].subtitle_ids, (2,))
        self.assertEqual(primary.whole_video_audit["status"], "needs_review")
        self.assertFalse(primary.whole_video_audit["duration_control"]["final"]["target_range_fulfilled"])

    def test_repeated_playback_in_range_is_not_duration_success(self):
        cast = self.cast(10)
        for chapter in cast["strategies"][0]["chapter_packets"]:
            chapter["beats"] *= 2
        audit = self.measure(cast)
        self.assertEqual(audit["source_seconds"], 60)
        self.assertEqual(audit["unique_source_seconds"], 30)
        self.assertEqual(audit["repeated_source_seconds"], 30)
        self.assertEqual(audit["selected_mean_beat_seconds"], 3)
        self.assertEqual(audit["estimated_beat_count_at_current_pace"], 20)
        self.assertFalse(audit["target_range_fulfilled"])
        self.assertGreater(audit["shortfall_source_seconds"], 0)
        self.assertEqual(sum(c["new_source_seconds"] for c in audit["chapters"]), 30)

    def test_stale_opening_receipt_does_not_discard_unchanged_real_opening(self):
        first = self.cast(10)
        first["strategies"][0]["opening_selection"]["selected_subtitle_ids"] = [51, 52]
        primary, _, _ = self.run_ai(first, self.cast(20))
        self.assertTrue(primary.whole_video_audit["duration_control"]["calibration"]["accepted_revision"])
        self.assertEqual(primary.director_sequence[0].subtitle_ids, (1,))

    def test_json_trailing_comma_repair_never_changes_quotes_or_ids(self):
        result = _extract_json('{"beats":[{"subtitle_ids":[1,2,],},],"text":"原话,}不改\\\"",}')
        self.assertEqual(result["beats"][0]["subtitle_ids"], [1, 2])
        self.assertEqual(result["text"], '原话,}不改"')
        with self.assertRaises(AnalyzerError):
            _extract_json('{"beats":[{"subtitle_ids":[1,2,')


if __name__ == "__main__":
    unittest.main()
