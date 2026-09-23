import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from commercial_analyzer import (
    AnalyzerError, analyze_commercial_story, build_director_duration_audit,
    build_two_pass_story_prompt, build_two_pass_cast_prompt, director_delivery_duration_range,
    _extract_json, sanitize_two_pass_story_for_content_policy,
    _casting_chapter_duration_budgets,
    _casting_execution_contract,
    _semantic_unit_issues, _story_evidence_issues,
    _story_evidence_location_conflicts, _apply_casting_chapter_revisions,
)


class DirectorDurationControlTests(unittest.TestCase):
    def test_grouped_cast_is_expanded_without_a_third_call(self):
        payload = self.cast(21)
        chapter = payload["strategies"][0]["chapter_packets"][0]
        before = chapter["beats"]
        group = dict(before[0])
        group["subtitle_ids"] = [sid for beat in before for sid in beat["subtitle_ids"]]
        chapter["beats"] = [group]
        result, calls, captured = self.run_ai(payload)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(result.director_sequence), 21)
        self.assertIn("casting_format_normalization", captured)
        audit = result.whole_video_audit["duration_control"]["final"]
        self.assertEqual(audit["grouped_beat_issues"][0]["subtitle_ids"], list(range(1, 8)))
        self.assertFalse(audit["target_range_fulfilled"])

    def test_policy_removes_only_forbidden_requirements_from_mixed_chapter(self):
        story = {"strategies": [{"strategy_id": "S2", "chapter_packets": [{
            "chapter_id": "C2", "title": "毛衣的前世", "source_budget_seconds": 20,
            "chapter_job": "从昂贵帽子的价格讲毛衣设计",
            "completion_requirements": ["说明帽子如何启发毛衣设计", "强调帽子价格", "展示定制配色"],
        }]}]}
        fixed, audit = sanitize_two_pass_story_for_content_policy(story, {"price": "block"})
        chapter = fixed["strategies"][0]["chapter_packets"][0]
        self.assertEqual(chapter["chapter_id"], "C2")
        self.assertEqual(chapter["completion_requirements"], ["说明帽子如何启发毛衣设计", "展示定制配色"])
        self.assertNotIn("价格", chapter["chapter_job"])
        self.assertEqual(chapter["source_budget_seconds"], 20)
        self.assertEqual(audit["removed_chapters"], [])
        self.assertEqual(len(audit["trimmed_chapters"]), 1)
        self.assertEqual(len(story["strategies"][0]["chapter_packets"][0]["completion_requirements"]), 3)

    def test_body_only_sales_chapter_is_not_promoted_to_opening(self):
        story = {"strategies": [{"chapter_packets": [
            {"chapter_id": "C1", "title": "百单火热进行", "chapter_job": "说明销量临近100单", "completion_requirements": ["营造商品受欢迎的氛围"]},
            {"chapter_id": "C2", "title": "设计来源", "chapter_job": "解释设计由来", "completion_requirements": ["展示配色灵感"]},
        ]}]}
        fixed, audit = sanitize_two_pass_story_for_content_policy(story, {"social_proof": "body_only"})
        self.assertEqual([c["chapter_id"] for c in fixed["strategies"][0]["chapter_packets"]], ["C2"])
        self.assertEqual(len(audit["removed_chapters"]), 1)

    def test_setup_budget_is_reserved_without_trimming_or_reordering_content(self):
        story = {"strategies": [{"chapter_packets": [
            {"chapter_id": "C1", "chapter_kind": "pain", "source_budget_seconds": 30},
            {"chapter_id": "C2", "chapter_kind": "mechanism", "source_budget_seconds": 20},
            {"chapter_id": "C3", "chapter_kind": "comfort", "source_budget_seconds": 19},
        ]}]}
        before = json.dumps(story)
        chapters = _casting_chapter_duration_budgets(story, 69)[0]["chapters"]
        self.assertEqual(chapters[0]["source_budget_seconds"], 10.35)
        self.assertEqual(chapters[-1]["cumulative_source_seconds"], 69)
        self.assertEqual(json.dumps(story), before)
        self.assertEqual([c["chapter_id"] for c in chapters], ["C1", "C2", "C3"])

    def test_overlong_cast_remains_editable_without_third_call(self):
        strategy, calls, _ = self.run_ai(self.cast(45))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(strategy.director_sequence), 45)

    def test_invalid_location_is_omitted_without_blocking_casting(self):
        self.story["strategies"][0]["chapter_packets"][0]["evidence_locations"] = [1, 9999]
        _, calls, _ = self.run_ai(self.cast(21))
        self.assertEqual(len(calls), 2)
        self.assertIn('"evidence_locations":[1]', calls[1].kwargs["user_prompt"])
        self.assertNotIn('9999', calls[1].kwargs["user_prompt"])

    def test_many_story_locations_do_not_block_second_call(self):
        self.story["strategies"][0]["chapter_packets"][0]["evidence_locations"] = list(range(1, 35))
        _, calls, captured = self.run_ai(self.cast(21))
        self.assertEqual(len(calls), 2)
        self.assertIn("many_locations", captured["story_evidence_notes"])
        self.assertIn('"evidence_locations":[1,2,3,4,5', calls[1].kwargs["user_prompt"])

    def test_story_evidence_errors_distinguish_format_from_pool(self):
        def check(value):
            return _story_evidence_issues({"strategies": [{"chapter_packets": [
                {"evidence_locations": value}]}]}, [1, 2])
        self.assertEqual(check([]), [])
        self.assertIn("evidence_format_invalid", check("1")[0])
        self.assertIn("evidence_id_format_invalid", check(["1"])[0])
        self.assertIn("evidence_outside_safe_pool", check([3])[0])

    def test_story_locations_reach_casting_without_becoming_selected_beats(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [{
            "chapter_id": "C1", "source_budget_seconds": 20,
            "evidence_locations": [41, 69], "completion_requirements": ["解释穿法"],
        }]}]}
        self.assertEqual(_story_evidence_issues(story, [41, 69]), [])
        self.assertTrue(_story_evidence_issues(story, [41]))
        contract = _casting_execution_contract(story, duration_range=director_delivery_duration_range(60))
        chapter = contract["strategies"][0]["chapters"][0]
        self.assertEqual(chapter["evidence_locations"], [41, 69])
        self.assertNotIn("beats", chapter)

    def test_cast_contract_carries_new_buyer_advance_and_shared_evidence_hint(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [
            {"chapter_id": "C1", "buyer_advance": "知道裙子能遮住胯部", "source_budget_seconds": 20,
             "evidence_locations": [49, 50]},
            {"chapter_id": "C2", "buyer_advance": "明白高腰怎么拉长比例", "source_budget_seconds": 20,
             "evidence_locations": [28, 50]},
        ]}]}
        contract = _casting_execution_contract(story, duration_range=director_delivery_duration_range(60))
        strategy = contract["strategies"][0]
        self.assertEqual(strategy["chapters"][0]["advance"], "知道裙子能遮住胯部")
        self.assertEqual(strategy["evidence_conflicts"], [{
            "strategy_id": "S1", "subtitle_ids": [50], "chapter_ids": ["C1", "C2"],
        }])
        self.assertEqual(_story_evidence_location_conflicts(story["strategies"][0]), [{
            "strategy_id": "S1", "subtitle_ids": [50], "chapter_ids": ["C1", "C2"],
        }])

    def test_casting_can_merge_and_drop_chapters_within_the_same_second_call(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [
            {"chapter_id": "C1", "title": "胯宽困扰", "buyer_advance": "认识腿型问题", "chapter_job": "提出问题",
             "completion_requirements": ["说清胯宽困扰"]},
            {"chapter_id": "C2", "title": "高腰显高", "buyer_advance": "理解高腰作用", "chapter_job": "解释原因",
             "completion_requirements": ["说清高腰穿法"]},
            {"chapter_id": "C3", "title": "空场景", "buyer_advance": "适合所有场合", "chapter_job": "讲场景",
             "completion_requirements": ["说明场景"]},
        ]}]}
        cast = {"strategies": [{"strategy_id": "S1", "chapter_packets": [
            {"chapter_id": "C1", "beats": [{"subtitle_ids": [1]}], "chapter_revision": {
                "action": "merge", "source_chapter_ids": ["C1", "C2"],
                "title": "身材适配与穿法", "buyer_advance": "知道如何通过穿法兼顾遮胯和比例",
                "chapter_job": "把身材问题落到高低腰穿法", "completion_requirements": ["说明身材适配和穿法"],
            }},
            {"chapter_id": "C3", "beats": [], "chapter_revision": {
                "action": "drop", "source_chapter_ids": ["C3"], "reason": "没有真实场景口播",
            }},
        ]}]}
        effective_story, effective_cast, audit = _apply_casting_chapter_revisions(story, cast)
        chapters = effective_story["strategies"][0]["chapter_packets"]
        self.assertEqual([chapter["chapter_id"] for chapter in chapters], ["C1"])
        self.assertEqual(chapters[0]["title"], "身材适配与穿法")
        self.assertEqual(chapters[0]["cast_chapter_revision"]["source_chapter_ids"], ["C1", "C2"])
        self.assertEqual(audit["strategies"][0]["status"], "applied")
        self.assertEqual(effective_cast["strategies"][0]["chapter_packets"][1]["chapter_revision"]["action"], "drop")

    def test_invalid_chapter_revision_does_not_change_the_first_story_contract(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [
            {"chapter_id": "C1", "title": "第一章"}, {"chapter_id": "C2", "title": "第二章"},
        ]}]}
        cast = {"strategies": [{"strategy_id": "S1", "chapter_packets": [{
            "chapter_id": "C2", "beats": [{"subtitle_ids": [2]}], "chapter_revision": {
                "action": "merge", "source_chapter_ids": ["C2", "C1"],
                "title": "错误合并", "buyer_advance": "错误", "chapter_job": "错误", "completion_requirements": ["错误"],
            },
        }]}]}
        effective_story, effective_cast, audit = _apply_casting_chapter_revisions(story, cast)
        self.assertEqual([chapter["chapter_id"] for chapter in effective_story["strategies"][0]["chapter_packets"]], ["C1", "C2"])
        self.assertEqual(audit["strategies"][0]["status"], "ignored_invalid")
        self.assertNotIn("chapter_revision", effective_cast["strategies"][0]["chapter_packets"][0])

    def test_duration_audit_reports_long_continuous_source_group_without_splitting_it(self):
        rows = [
            {"id": 1, "start": 0.0, "end": 3.0, "text": "第一句完整解释"},
            {"id": 2, "start": 3.2, "end": 6.2, "text": "第二句继续解释"},
            {"id": 3, "start": 6.4, "end": 9.4, "text": "第三句完成结论"},
        ]
        story = {"strategies": [{"chapter_packets": [{"chapter_id": "C1"}]}]}
        cast = {"strategies": [{"chapter_packets": [{"chapter_id": "C1", "beats": [
            {"subtitle_ids": [1]}, {"subtitle_ids": [2]}, {"subtitle_ids": [3]},
        ]}]}]}
        audit = build_director_duration_audit(
            casting_payload=cast, story_contract=story, subtitles=rows, target_duration=10,
        )
        self.assertEqual(audit["long_continuous_utterance_group_count"], 1)
        self.assertEqual(audit["chapters"][0]["long_continuous_utterance_groups"][0]["subtitle_ids"], [1, 2, 3])

    def test_execution_frontloads_each_chapter_duration_window(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [
            {"chapter_id": "C1", "source_budget_seconds": 20, "completion_requirements": ["结果"]},
            {"chapter_id": "C2", "source_budget_seconds": 40, "completion_requirements": ["证明"]},
        ]}]}
        duration = director_delivery_duration_range(60, 10, 1.15)
        contract = _casting_execution_contract(story, duration_range=duration)
        chapters = contract["strategies"][0]["chapters"]

        self.assertAlmostEqual(sum(chapter["budget_floor"] for chapter in chapters), duration["source_min"])
        self.assertAlmostEqual(sum(chapter["budget"] for chapter in chapters), duration["source_target"])
        self.assertAlmostEqual(sum(chapter["budget_ceiling"] for chapter in chapters), duration["source_max"])
        self.assertEqual(chapters[-1]["budget_end_floor"], duration["source_min"])
        self.assertEqual(chapters[-1]["budget_end"], duration["source_target"])
        self.assertEqual(chapters[-1]["budget_end_ceiling"], duration["source_max"])

    def test_cross_line_units_require_selected_adjacent_ordered_ids(self):
        chapter = {"semantic_units": [[41, 42], [68, 69]]}
        self.assertEqual(_semantic_unit_issues(chapter, [41, 42, 43, 68, 69]), [])
        for sequence in ([42, 68, 69], [42, 41, 68, 69], [41, 43, 42, 68, 69]):
            self.assertTrue(_semantic_unit_issues(chapter, sequence))

    def test_unexecuted_semantic_unit_marks_chapter_for_review(self):
        payload = self.cast(6)
        chapter = payload["strategies"][0]["chapter_packets"][0]
        chapter["completion_receipts"] = [{"requirement_index": 1, "subtitle_ids": [1, 2]}]
        chapter["semantic_units"] = [[1, 3]]
        audit = self.measure(payload)
        self.assertIn("C1", audit["unverified_completion_chapter_ids"])
        self.assertEqual(audit["selected_subtitle_ids"], [1, 2, 3, 4, 5, 6])

    def test_short_utterances_scale_pacing_without_fixed_beat_cap(self):
        rows = [{"id": i, "start": i * 3.0, "end": i * 3.0 + 2.5, "text": "真实短句"}
                for i in range(1, 41)]
        prompt = build_two_pass_cast_prompt(
            story_contract={"strategies": [{"strategy_id": "S1", "chapter_packets": [
                {"chapter_id": "C1", "source_budget_seconds": 15, "completion_requirements": ["结果"]},
                {"chapter_id": "C2", "source_budget_seconds": 45, "completion_requirements": ["证据"]},
            ]}]}, subtitles=rows, target_duration=60, output_speed_factor=1.15,
        )
        self.assertIn('"approximate_beats_for_target":28', prompt)
        self.assertIn('"approximate_beats":21', prompt)
        self.assertNotIn("每章最多 4", prompt)
        self.assertNotIn("15-22", prompt)

    def test_casting_prompt_requires_one_call_duration_self_check(self):
        prompt = build_two_pass_cast_prompt(
            story_contract=self.story, subtitles=self.rows,
            target_duration=60, output_speed_factor=1.15,
        )

        self.assertIn('"budget_floor"', prompt)
        self.assertIn('"budget_end_floor"', prompt)
        self.assertIn('"duration_receipt"', prompt)
        self.assertIn("若不在 source_min/source_max 内，不得写 pass", prompt)
        self.assertIn('"safe_pool_can_reach_source_min":true', prompt)
        self.assertIn("source_min 是本次交付下限", prompt)
        self.assertIn("每个最终 beat 的 ids 必须恰好写一个 ID", prompt)
        self.assertIn("总原声不超过 8 秒", prompt)

    def test_execution_preserves_fourth_completion_requirement(self):
        requirements = ["回应顾虑", "斜肩", "小白鞋", "帽子"]
        contract = _casting_execution_contract(
            {"strategies": [{"chapter_packets": [{"chapter_id": "C5", "source_budget_seconds": 15,
                                                 "completion_requirements": requirements}]}]},
            duration_range=director_delivery_duration_range(60, 10, 1.15),
        )
        self.assertEqual(contract["strategies"][0]["chapters"][0]["needs"], requirements)

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

    def test_blocked_story_promise_is_removed_before_paid_beat_casting(self):
        story = json.loads(json.dumps(self.story))
        story["strategies"][0]["chapter_packets"] = [
            {
                "chapter_id": "C-price", "coverage": "required", "title": "99 元现在下单",
                "chapter_job": "用价格促成购买", "completion_requirements": ["说清价格和抢购"],
            },
            {
                "chapter_id": "C-proof", "coverage": "required", "title": "解释设计细节",
                "chapter_job": "用口袋和袖边说明差异", "completion_requirements": ["展示设计细节"],
            },
            {
                "chapter_id": "C-styling", "coverage": "recommended", "title": "搭配美元裤",
                "chapter_job": "说明绿色上衣能搭美元裤", "completion_requirements": ["展示搭配效果"],
            },
        ]
        sanitized, audit = sanitize_two_pass_story_for_content_policy(
            story, {"price": "block", "cta": "block"},
        )
        kept = sanitized["strategies"][0]["chapter_packets"]
        self.assertEqual([item["chapter_id"] for item in kept], ["C-proof", "C-styling"])
        self.assertEqual(audit["status"], "policy_trimmed")
        self.assertEqual(audit["removed_chapters"][0]["blocked_kinds"], ["cta", "price"])

    def test_cast_execution_budgets_reconcile_speed_for_each_plan_without_changing_story(self):
        story = json.loads(json.dumps(self.story))
        primary = story["strategies"][0]
        primary["chapter_packets"] = [
            {**primary["chapter_packets"][0], "chapter_id": f"C{i}", "source_budget_seconds": seconds}
            for i, seconds in enumerate([10, 8, 12, 12, 7, 6, 6], 1)
        ]
        other = json.loads(json.dumps(self.story["strategies"][0]))
        other["strategy_id"] = "S2"
        other["director_plan_role"] = "alternative"
        story["strategies"].append(other)
        original = json.loads(json.dumps(story))
        prompt = build_two_pass_cast_prompt(
            story_contract=story, subtitles=self.rows, target_duration=60,
            output_speed_factor=1.15,
        )
        contract = next(json.loads(line) for line in prompt.splitlines()
                        if line.startswith('{"version":"director-cast-exec-v1"'))
        self.assertAlmostEqual(contract["duration"]["source_target"], 69)
        for plan in contract["strategies"]:
            self.assertAlmostEqual(sum(c["budget"] for c in plan["chapters"]), 69)
        self.assertAlmostEqual(contract["strategies"][0]["chapters"][0]["budget"], 69 * 10 / 61, places=3)
        self.assertEqual(story, original)

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
        self.assertIn("[ID 001][3.00s]", prompt)
        self.assertIn("[ID 003][3.00s]", prompt)
        self.assertNotIn("[ID 002][3.00s]", prompt)
        schema = json.loads(prompt.split("实际回复必须使用 director-wire-v1：", 1)[1].splitlines()[1])
        chapter = schema["packet"]["strategies"][0]["chapter_packets"][0]
        self.assertNotIn("duration_fill_beats", chapter)
        self.assertNotIn("budget_execution", chapter)
        self.assertNotIn("sec", chapter["beats"][0])

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

    def test_complete_chapter_without_receipts_is_reviewable_but_does_not_buy_a_third_call(self):
        audit = self.measure(self.cast(20))
        self.assertEqual(audit["unverified_completion_chapter_ids"], ["C1", "C2", "C3"])
        self.assertFalse(audit["needs_calibration"])
        self.assertEqual(audit["chapters"][0]["completion_status"], "needs_review")

    def test_completion_receipts_must_point_to_the_same_chapter_selection(self):
        payload = self.cast(20)
        for chapter in payload["strategies"][0]["chapter_packets"]:
            chapter["completion_receipts"] = [{
                "requirement_index": 1,
                "subtitle_ids": [chapter["beats"][0]["subtitle_ids"][0]],
            }]
        audit = self.measure(payload)
        self.assertEqual(audit["unverified_completion_chapter_ids"], [])
        self.assertTrue(audit["chapters"][0]["completion_receipt_verified"])

    def test_source_limited_chapter_remains_visible_without_an_unproductive_retry(self):
        payload = self.cast(20)
        payload["strategies"][0]["chapter_packets"][1].update({
            "completion_status": "source_limited",
            "missing_content": "没有主商品可执行的外套实穿证明",
        })
        audit = self.measure(payload)
        self.assertEqual(audit["incomplete_chapter_ids"], ["C2"])
        self.assertEqual(audit["retryable_incomplete_chapter_ids"], [])
        self.assertFalse(audit["needs_calibration"])

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
                if fault == "unknown":
                    # 2026-09-23（David 定调）：只有一个不可用 ID 的修订不再整版作废，
                    # 程序逐句剔除该 ID 后重新实测，清理版仍是改进就采纳。
                    control = primary.whole_video_audit["duration_control"]
                    self.assertEqual(control["calibration"]["sanitized_revision"]["dropped_ids"], [9999])
                    self.assertTrue(control["calibration"]["accepted_revision"])
                    self.assertEqual(len(primary.director_sequence), 19)
                    self.assertNotIn(9999, [beat.subtitle_ids[0] for beat in primary.director_sequence])
                else:
                    # 章节顺序/开场被改动仍必须整版拒绝：程序只清句子，不重排内容。
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

    def test_duplicate_beat_is_deleted_by_program_before_delivery(self):
        revised = self.cast(20)
        revised["strategies"][0]["chapter_packets"][-1]["beats"][-1]["subtitle_ids"] = [2]
        primary, _, _ = self.run_ai(self.cast(10), revised)
        self.assertTrue(primary.whole_video_audit["duration_control"]["calibration"]["accepted_revision"])
        # 2026-09-23（David 定调）：重复句必须由程序删除，只保留首次出现。
        # 重复口播不得进入预览/成片，也不得计入可用时长。
        delivered = [beat.subtitle_ids[0] for beat in primary.director_sequence]
        self.assertEqual(len(delivered), len(set(delivered)))
        control = primary.whole_video_audit["duration_control"]
        self.assertEqual(control["dedup_removed_ids"], [2])
        final_audit = control["final"]
        self.assertEqual(final_audit["duplicate_subtitle_ids"], [])
        self.assertEqual(final_audit["repeated_source_seconds"], 0.0)
        self.assertEqual(
            final_audit["source_seconds"], final_audit["unique_source_seconds"])

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
