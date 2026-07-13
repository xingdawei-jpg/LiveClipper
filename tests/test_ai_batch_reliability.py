from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))


ai_clipper = importlib.import_module("ai_clipper")
cutter_logic = importlib.import_module("cutter_logic")
selection_contracts = importlib.import_module("selection_contracts")
server = importlib.import_module("server")


class AiCandidateReliabilityTests(unittest.TestCase):
    def test_incomplete_product_is_removed_without_rejecting_whole_story(self) -> None:
        clips = [
            ("hook", "这件衣服特别修饰肩型", 0.0, 2.0, 50, 2.0, "版型"),
            ("product", "肩膀两边有黑色编织线所以肩型会", 2.0, 6.0, 50, 4.0, "工艺"),
            ("product", "落肩轮廓穿起来很自然", 6.0, 12.0, 50, 6.0, "版型"),
            ("close", "这一身整体很耐看", 12.0, 14.0, 50, 2.0, "风格"),
        ]
        safe, audit = ai_clipper._director_hard_audit(clips, 10, 8)
        self.assertEqual(len(safe), 3)
        self.assertFalse(any("半句边界" in issue for issue in audit.get("issues", [])))
        self.assertFalse(any("肩型会" in clip[1] for clip in safe))

    def test_incomplete_close_is_removed_without_rejecting_whole_story(self) -> None:
        clips = [
            ("hook", "这件衣服特别修饰肩型", 0.0, 2.0, 50, 2.0, "版型"),
            ("product", "落肩轮廓穿起来很自然", 2.0, 8.0, 50, 6.0, "版型"),
            ("close", "六月七月八月可以和朋友出去旅行，", 8.0, 14.0, 50, 6.0, "场景"),
        ]
        safe, audit = ai_clipper._director_hard_audit(clips, 10, 8)
        self.assertEqual([clip[0] for clip in safe], ["hook", "product"])
        self.assertFalse(any("半句边界" in issue for issue in audit.get("issues", [])))
        self.assertFalse(any("Close必须" in issue for issue in audit.get("issues", [])))

    def test_director_candidate_can_exceed_soft_cap_to_finish_sentence(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:10,000\n六月七月八月可以和朋友出去旅行，\n\n"
            "2\n00:00:10,200 --> 00:00:21,000\n可以去凉爽舒服的地方，也可以去海岛，\n\n"
            "3\n00:00:21,200 --> 00:00:25,000\n洱海的天和海都特别蓝。\n"
        )
        frozen = ai_clipper._freeze_director_candidates(source)
        entries = ai_clipper._parse_srt_entries_for_hook(frozen)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], 0.0)
        self.assertEqual(entries[0][1], 25.0)
        self.assertTrue(entries[0][2].endswith("特别蓝。"))

    def test_output_subtitles_remove_chinese_and_english_punctuation(self) -> None:
        cleaned = cutter_logic._strip_output_subtitle_punctuation("你好，今天穿这件！Really?【显瘦】")
        self.assertEqual(cleaned, "你好今天穿这件Really显瘦")

    def test_director_candidates_merge_sentence_fragments_before_selection(self) -> None:
        source = (
            "1\n00:00:01,000 --> 00:00:04,000\n这个领口拉开以后，\n\n"
            "2\n00:00:04,200 --> 00:00:07,000\n它还是不会变形。\n"
        )
        frozen = ai_clipper._freeze_director_candidates(source)
        entries = ai_clipper._parse_srt_entries_for_hook(frozen)
        indexed_entries = ai_clipper._build_ai_srt_entry_index(frozen)
        self.assertEqual(len(entries), 1)
        self.assertEqual(indexed_entries, entries)
        self.assertEqual(entries[0][0], 1.0)
        self.assertEqual(entries[0][1], 7.0)
        self.assertIn("这个领口拉开以后，它还是不会变形", entries[0][2])

        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "product", "srt_indices": [1], "focus": "面料质感", "reason": "完整卖点"}
            ], ensure_ascii=False),
            None,
            indexed_entries,
            set(),
            require_srt_indices=True,
        )
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0][2], 1.0)
        self.assertEqual(clips[0][3], 7.0)
        self.assertIn("不会变形", clips[0][1])

        legacy_clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "product", "start": 1.0, "end": 3.0, "text": "人工填写的半句话"}
            ], ensure_ascii=False),
            None,
            indexed_entries,
            set(),
            require_srt_indices=True,
        )
        self.assertEqual(legacy_clips, [])

    def test_director_candidates_never_merge_across_mix_sources(self) -> None:
        source = (
            "1\n00:00:01,000 --> 00:00:04,000\n[V1] 这个领口拉开以后，\n\n"
            "2\n00:00:04,100 --> 00:00:07,000\n[V2] 它还是不会变形。\n"
        )
        frozen = ai_clipper._freeze_director_candidates(source)
        self.assertEqual(len(ai_clipper._parse_srt_entries_for_hook(frozen)), 2)

    def test_insufficient_content_message_exposes_duration_evidence(self) -> None:
        metadata = {
            "selection_failure": {
                "code": "insufficient_content",
                "candidate_count": 5,
                "best_duration": 16.5,
                "duration_low": 58,
            }
        }
        message = ai_clipper.selection_failure_message(metadata)
        self.assertIn("有效内容不足", message)
        self.assertIn("16.5秒", message)
        self.assertIn("至少58秒", message)

    def test_overlong_director_result_is_a_hard_failure(self) -> None:
        clips = [
            ("hook", "这件上衣很显精神", 0.0, 5.0, 50, 5.0, "效果"),
            ("product", "肩线自然而且整体轮廓很利落。", 5.0, 75.0, 50, 70.0, "版型"),
            ("close", "日常通勤穿这一身就很好看。", 75.0, 80.0, 50, 5.0, "场景"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(clips, 60, 8)
        self.assertTrue(audit["duration_long"])
        self.assertTrue(any("超过目标上限70s" in issue for issue in audit["issues"]))

    def test_overlong_repair_instruction_contains_exact_duration_budget(self) -> None:
        clips = [
            ("hook", "开场", 0.0, 5.0, 50, 5.0),
            ("product", "核心卖点", 5.0, 75.0, 50, 70.0),
            ("close", "收尾", 75.0, 80.0, 50, 5.0),
        ]
        instruction = ai_clipper._director_repair_instruction(
            clips,
            ["总时长80.0s超过目标上限70s，至少需删除约10s"],
            60,
            8,
            [(clip[2], clip[3], clip[1]) for clip in clips],
        )
        self.assertIn('"duration_sec": 70.0', instruction)
        self.assertIn("至少删除10.0秒", instruction)
        self.assertIn("删除后必须落在50-70秒", instruction)

    def test_short_repair_instruction_requires_new_candidates_instead_of_copying_skeleton(self) -> None:
        clips = [
            ("hook", "开场", 0.0, 4.0, 50, 4.0),
            ("product", "承接", 4.0, 10.0, 50, 6.0),
            ("close", "收尾", 10.0, 14.0, 50, 4.0),
        ]
        instruction = ai_clipper._director_repair_instruction(
            clips,
            ["总时长14.0s低于目标下限50s，至少还需补足约36s"],
            60,
            8,
            [(clip[2], clip[3], clip[1]) for clip in clips],
        )
        self.assertIn("必须新增至少5个当前骨架没有的安全候选编号组", instruction)
        self.assertIn("不得原样照抄骨架", instruction)

    def test_specialized_ai_trim_applies_ai_removal_priority_with_exact_arithmetic(self) -> None:
        clips = [
            ("hook", "开场", 0.0, 5.0, 50, 5.0, "效果"),
            ("product", "兑现", 5.0, 10.0, 50, 5.0, "证据"),
            ("product", "低价值重复", 10.0, 20.0, 50, 10.0, "重复"),
            ("close", "收尾", 20.0, 25.0, 50, 5.0, "场景"),
        ]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": '{"remove_priority":[3]}'}}]
        }).encode("utf-8")
        with mock.patch.object(ai_clipper.urllib.request, "urlopen", return_value=response) as urlopen:
            trimmed = ai_clipper._call_director_trim_selection(
                "key", "https://example.com/v1", "deepseek-test", clips, 15,
                preferred_focus="版型显瘦",
            )
        self.assertEqual([clip[1] for clip in trimmed], ["开场", "兑现", "收尾"])
        request_body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertIn('"duration_sec": 10.0', request_body["messages"][1]["content"])
        self.assertIn("remove_priority", request_body["messages"][1]["content"])
        self.assertIn("版型显瘦", request_body["messages"][1]["content"])

    def test_ai_removal_priority_never_deletes_hook_followup_or_close(self) -> None:
        clips = [
            ("hook", "开场", 0.0, 5.0, 50, 5.0, "版型显瘦"),
            ("product", "承接开场", 5.0, 10.0, 50, 5.0, "版型显瘦"),
            ("product", "补充场景", 10.0, 20.0, 50, 10.0, "场景搭配"),
            ("product", "核心证据", 20.0, 30.0, 50, 10.0, "版型显瘦"),
            ("close", "自然收尾", 30.0, 35.0, 50, 5.0, "场景搭配"),
        ]
        trimmed = ai_clipper._apply_ai_removal_priority(
            clips,
            [1, 2, 5, 3, 4],
            20,
        )
        self.assertEqual([clip[1] for clip in trimmed], ["开场", "承接开场", "核心证据", "自然收尾"])

    def test_analysis_metadata_exposes_mix_source_contract(self) -> None:
        metadata = ai_clipper._begin_analysis_metadata()
        metadata["source_contract"] = {"required_counts": {"[V1]": 2, "[V2]": 2}}

        public_metadata = ai_clipper.get_last_analysis_metadata()

        self.assertEqual(public_metadata["source_contract"]["required_counts"]["[V1]"], 2)

    def test_parsed_trim_priorities_can_close_duration_without_second_ai_call(self) -> None:
        ai_clipper._begin_analysis_metadata()
        entries = [
            (0.0, 5.0, "面料摸起来很清爽"),
            (5.0, 10.0, "竹节麻透气而且不粘汗"),
            (10.0, 15.0, "这一身适合海岛度假"),
            (15.0, 20.0, "草帽和编织包都能搭"),
            (20.0, 25.0, "整体穿起来很松弛"),
        ]
        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "hook", "srt_indices": [1], "focus": "面料质感", "reason": "偏好Hook", "trim_priority": 0},
                {"clip_type": "product", "srt_indices": [2], "focus": "面料质感", "reason": "承接Hook", "trim_priority": 0},
                {"clip_type": "product", "srt_indices": [3], "focus": "场景搭配", "reason": "补充场景", "trim_priority": 1},
                {"clip_type": "product", "srt_indices": [4], "focus": "场景搭配", "reason": "补充搭配", "trim_priority": 2},
                {"clip_type": "close", "srt_indices": [5], "focus": "场景搭配", "reason": "自然收尾", "trim_priority": 0},
            ], ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
        )
        trimmed = ai_clipper._apply_declared_trim_priorities(clips, 10)
        self.assertEqual([clip[1] for clip in trimmed], [entries[0][2], entries[1][2], entries[4][2]])

    def test_preference_hook_and_followup_are_audited_as_a_pair(self) -> None:
        clips = [
            ("hook", "穿上以后肩宽只有三十多", 0.0, 3.0, 50, 3.0, "版型显瘦"),
            ("product", "黑线让肩膀往里收三公分", 3.0, 8.0, 50, 5.0, "版型显瘦"),
            ("close", "这一身整体很耐看", 8.0, 12.0, 50, 4.0, "场景搭配"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            12,
            8,
            preferred_focus="面料质感",
            require_preference_hook=True,
        )
        self.assertTrue(any("Hook未体现指定偏好" in issue for issue in audit["issues"]))
        self.assertTrue(any("Hook第二段未承接" in issue for issue in audit["issues"]))

    def test_manual_preference_drift_is_reported_without_rejecting_ai_story(self) -> None:
        clips = [
            ("hook", "竹节麻摸起来清爽透气", 0.0, 3.0, 50, 3.0, "面料质感"),
            ("product", "再生纤维接近天丝的触感", 3.0, 8.0, 50, 5.0, "面料质感"),
            ("product", "穿去海岛度假很合适", 8.0, 16.0, 50, 8.0, "场景搭配"),
            ("product", "去洱海拍照很有氛围", 16.0, 24.0, 50, 8.0, "场景搭配"),
            ("product", "搭草帽就有度假感觉", 24.0, 32.0, 50, 8.0, "场景搭配"),
            ("close", "这一身整体很松弛", 32.0, 36.0, 50, 4.0, "场景搭配"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            36,
            8,
            preferred_focus="面料质感",
            require_preference_hook=True,
            require_preference_mainline=True,
        )
        self.assertFalse(any("未成为正文主线" in issue for issue in audit["issues"]))
        self.assertTrue(any("偏好覆盖提示" in warning for warning in audit["warnings"]))

    def test_manual_preference_overconcentration_is_preview_warning(self) -> None:
        clips = [
            ("hook", "天丝木浆摸起来很清爽", 0.0, 3.0, 50, 3.0, "面料质感"),
            ("product", "再生纤维触感很舒服", 3.0, 8.0, 50, 5.0, "面料质感"),
        ]
        for index in range(5):
            start = 8.0 + index * 5.0
            clips.append(("product", f"第{index}个面料卖点透气不扎", start, start + 5.0, 50, 5.0, "面料质感"))
        clips.extend([
            ("product", "海岛度假可以搭草帽", 33.0, 41.0, 50, 8.0, "场景搭配"),
            ("product", "肩线往里收会更显瘦", 41.0, 49.0, 50, 8.0, "版型显瘦"),
            ("close", "这一身整体很耐看", 49.0, 54.0, 50, 5.0, "场景搭配"),
        ])
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            54,
            8,
            preferred_focus="面料质感",
            require_preference_hook=True,
            require_preference_mainline=True,
        )
        self.assertFalse(any("超过上限" in issue for issue in audit["issues"]))
        self.assertTrue(any("超过上限" in warning for warning in audit["warnings"]))

    def test_preference_quota_uses_final_duration_not_speed_adjusted_source_duration(self) -> None:
        clips = [
            ("hook", "天丝木浆摸起来很清爽", 0.0, 3.0, 50, 3.0, "面料质感"),
            ("product", "再生纤维触感很舒服", 3.0, 8.0, 50, 5.0, "面料质感"),
            ("product", "竹节麻透气不粘汗", 8.0, 13.0, 50, 5.0, "面料质感"),
            ("product", "纱线轻薄夏天很凉快", 13.0, 18.0, 50, 5.0, "面料质感"),
            ("product", "面料柔软贴肤不扎", 18.0, 23.0, 50, 5.0, "面料质感"),
            ("product", "天然木浆纤维不容易起球", 23.0, 28.0, 50, 5.0, "面料质感"),
            ("product", "海岛度假可以搭草帽", 28.0, 36.0, 50, 8.0, "场景搭配"),
            ("product", "肩线往里收会更显瘦", 36.0, 44.0, 50, 8.0, "版型显瘦"),
            ("close", "这一身整体很耐看", 44.0, 49.0, 50, 5.0, "场景搭配"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            86,
            8,
            preferred_focus="面料质感",
            require_preference_hook=True,
            require_preference_mainline=True,
            preference_target_duration=75,
        )
        self.assertFalse(any("超过上限4段" in issue for issue in audit["issues"]))
        self.assertTrue(any("超过上限4段" in warning for warning in audit["warnings"]))

    def test_ninety_second_manual_preference_allows_six_balanced_mainline_clips(self) -> None:
        self.assertEqual(ai_clipper._preference_target_bounds(90, "面料质感"), (3, 6))

    def test_hook_candidates_keep_original_srt_indices_after_filtering(self) -> None:
        candidates, _total = ai_clipper._collect_hook_candidates_from_entries(
            [
                (7, 0.0, 3.0, "这个面料非常透气"),
                (19, 3.0, 6.0, "穿上以后特别显瘦"),
            ],
            focus_hint="面料质感",
            limit=12,
        )
        self.assertEqual({candidate[0] for candidate in candidates}, {7, 19})

    def test_incomplete_sentence_is_never_advertised_as_hook_candidate(self) -> None:
        candidates, total = ai_clipper._collect_hook_candidates_from_entries(
            [
                (7, 0.0, 3.0, "妈呀这个颜色真的太绝了，"),
                (19, 3.0, 6.0, "这个颜色真的太绝了。"),
                (23, 6.0, 9.0, "但是这个版型特别显瘦。"),
            ],
            focus_hint="情绪感染",
            limit=12,
        )
        self.assertEqual(total, 1)
        self.assertEqual([candidate[0] for candidate in candidates], [19])

    def test_ai_hook_must_use_one_index_from_advertised_contract(self) -> None:
        entries = [
            (0.0, 3.0, "这个颜色真的太绝了。"),
            (3.0, 7.0, "但是这根黑线可以把肩往里收。"),
            (7.0, 11.0, "黑线让肩膀看起来更窄。"),
        ]
        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "hook", "srt_indices": [2], "focus": "情绪感染"},
                {"clip_type": "product", "srt_indices": [3], "focus": "版型显瘦"},
            ], ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
            allowed_hook_indices={1},
        )
        self.assertEqual([clip[0] for clip in clips], ["product"])

        valid = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "hook", "srt_indices": [1], "focus": "情绪感染"},
                {"clip_type": "product", "srt_indices": [3], "focus": "版型显瘦"},
            ], ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
            allowed_hook_indices={1},
        )
        self.assertEqual([clip[0] for clip in valid], ["hook", "product"])

    def test_selection_duration_contract_accepts_only_projected_final_range(self) -> None:
        valid = [("product", "完整卖点", 0.0, 69.0, 50, 69.0)]
        result = cutter_logic._validate_selected_duration_contract(valid, 60, 1.15)
        self.assertAlmostEqual(result["projected_final"], 60.0, places=1)

        for duration in (46.3, 137.1):
            clips = [("product", "完整卖点", 0.0, duration, 50, duration)]
            with self.assertRaisesRegex(RuntimeError, "AI未满足时长"):
                cutter_logic._validate_selected_duration_contract(clips, 60, 1.15)

    def test_actual_duration_contract_rejects_previous_bad_outputs(self) -> None:
        self.assertTrue(cutter_logic._validate_actual_duration_contract(60.0, 60)[0])
        self.assertFalse(cutter_logic._validate_actual_duration_contract(44.6, 60)[0])
        self.assertFalse(cutter_logic._validate_actual_duration_contract(124.1, 60)[0])

    def test_tiny_mapped_subtitle_fragments_are_merged(self) -> None:
        segments = cutter_logic._prepare_mapped_subtitle_segments([
            {"start": 0.0, "end": 1.0, "text": "你好"},
            {"start": 1.0, "end": 1.03, "text": "啊"},
            {"start": 1.03, "end": 2.0, "text": "今天很好看"},
        ])
        self.assertTrue(all(item["end"] - item["start"] >= 0.22 for item in segments))
        self.assertIn("你好啊", "".join(item["text"] for item in segments))


    def test_vertical_category_profiles_support_beauty_and_household_without_clothing_fallback(self) -> None:
        self.assertEqual(ai_clipper._normalize_forced_category("美妆"), "美妆护肤")
        self.assertEqual(ai_clipper._normalize_forced_category("日用百货"), "家居百货")
        self.assertEqual(ai_clipper.infer_category_from_filename("水光玻璃唇釉新品.mp4"), "美妆护肤")
        self.assertEqual(ai_clipper.infer_category_from_filename("厨房收纳置物架.mp4"), "家居百货")
        self.assertEqual(ai_clipper._feedback_category_bucket("美妆"), "beauty")
        self.assertEqual(ai_clipper._feedback_category_bucket("家居"), "household")
        beauty_overlay = ai_clipper._category_system_overlay("美妆护肤")
        self.assertIn("美妆护肤", beauty_overlay)
        self.assertIn("医疗功效", beauty_overlay)

    def test_unknown_future_category_uses_general_semantics_not_a_false_clothing_profile(self) -> None:
        self.assertIsNone(ai_clipper._normalize_forced_category("宠物用品"))
        self.assertEqual(ai_clipper._feedback_category_bucket("宠物用品"), "general")

    def test_neutral_filler_cannot_hide_incomplete_hook_start(self) -> None:
        candidates, total = ai_clipper._collect_hook_candidates_from_entries(
            [
                (1, 0.0, 4.0, "嗯。但是这根黑线真的很显肩窄。"),
                (2, 4.0, 8.0, "妈呀这根黑线真的太绝了。"),
            ],
            focus_hint="情绪感染",
            limit=12,
        )
        self.assertEqual(total, 1)
        self.assertEqual([candidate[0] for candidate in candidates], [2])

    def test_repeated_weak_prefixes_are_removed_before_candidate_indexing(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:06,000\n"
            "嗯。但是这根黑线真的很显肩窄而且很自然。\n"
        )
        frozen = ai_clipper._freeze_director_candidates(source)
        entries = ai_clipper._build_ai_srt_entry_index(frozen)
        self.assertEqual(len(entries), 1)
        self.assertGreater(entries[0][0], 0.0)
        self.assertEqual(entries[0][2], "这根黑线真的很显肩窄而且很自然。")

    def test_candidate_edges_are_trimmed_before_indexing_and_then_stay_fixed(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:04,000\n"
            "嗯。但是这根黑线真的很显肩窄。\n"
        )
        word_timings = [{
            "words": [
                {"text": "嗯", "start": 0.0, "end": 0.2},
                {"text": "但是", "start": 0.25, "end": 0.65},
                {"text": "这根", "start": 0.7, "end": 1.0},
                {"text": "黑线", "start": 1.0, "end": 1.3},
                {"text": "真的", "start": 1.3, "end": 1.6},
                {"text": "很显", "start": 1.6, "end": 1.9},
                {"text": "肩窄", "start": 1.9, "end": 2.3},
                {"text": "而且很自然", "start": 2.3, "end": 3.4},
            ]
        }]
        frozen = ai_clipper._freeze_director_candidates(source, word_timings=word_timings)
        entries = ai_clipper._build_ai_srt_entry_index(frozen)
        self.assertEqual(len(entries), 1)
        self.assertAlmostEqual(entries[0][0], 0.7, places=2)
        self.assertEqual(entries[0][2], "这根黑线真的很显肩窄而且很自然")

        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "hook", "srt_indices": [1], "focus": "版型显瘦"}
            ], ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
            allowed_hook_indices={1},
        )
        self.assertEqual(clips[0][1], entries[0][2])
        self.assertAlmostEqual(clips[0][2], entries[0][0], places=3)
        self.assertAlmostEqual(clips[0][3], entries[0][1], places=3)

    def test_selection_manifest_locks_order_boundaries_and_duration_contract(self) -> None:
        contract = selection_contracts.DurationContract.create(60, 1.15)
        clips = [
            ("hook", "第一句完整开场", 1.0, 5.0, 50, 4.0, "效果"),
            ("product", "第二句完整卖点", 8.0, 14.0, 50, 6.0, "面料"),
        ]
        first = selection_contracts.SelectionManifest.from_clips(
            clips,
            candidate_digest="candidate-contract",
            duration_contract=contract,
        )
        again = selection_contracts.SelectionManifest.from_clips(
            clips,
            candidate_digest="candidate-contract",
            duration_contract=contract,
        )
        reordered = selection_contracts.SelectionManifest.from_clips(
            list(reversed(clips)),
            candidate_digest="candidate-contract",
            duration_contract=contract,
        )
        self.assertEqual(first.digest, again.digest)
        self.assertNotEqual(first.digest, reordered.digest)
        self.assertEqual(first.to_dict()["selected_count"], 2)
        self.assertAlmostEqual(first.to_dict()["projected_final_duration"], 10.0 / 1.15, places=3)
        request = selection_contracts.SelectionRequest.create(
            source_ids=("V1", "V2"),
            category="套装",
            focus="场景搭配",
            merge_mode=True,
            duration_contract=contract,
            controls={"hook_cap": "5秒", "category_filter": True},
        )
        result = selection_contracts.SelectionResult.success(first)
        self.assertEqual(request.to_dict()["source_ids"], ["V1", "V2"])
        self.assertEqual(request.duration_contract, contract)
        self.assertTrue(result.to_dict()["ok"])
        self.assertEqual(result.to_dict()["manifest"]["digest"], first.digest)

    def test_ai_audit_and_cutter_share_the_same_speed_adjusted_duration_contract(self) -> None:
        contract = selection_contracts.DurationContract.create(90, 1.15)
        clips = [
            ("hook", "这件上衣穿上很显精神", 0.0, 3.0, 50, 3.0, "版型显瘦"),
            ("product", "肩线和落肩轮廓会让肩膀看起来更窄。", 3.0, 82.7, 50, 79.7, "版型显瘦"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            contract.ai_target_seconds,
            8,
            duration_contract=contract,
        )
        self.assertTrue(audit["duration_short"])
        self.assertAlmostEqual(audit["duration_low"], 86.25, places=2)
        with self.assertRaisesRegex(RuntimeError, "AI未满足时长"):
            cutter_logic._validate_selected_duration_contract(clips, 90, 1.15)
    def test_declared_expansion_plan_adds_only_ai_approved_complete_clip(self) -> None:
        ai_clipper._begin_analysis_metadata()
        entries = [
            (0.0, 4.0, "这件上衣穿上很显精神。"),
            (4.0, 10.0, "肩线自然，正面轮廓也很利落。"),
            (10.0, 18.0, "面料轻薄透气，夏天穿不会闷。"),
            (18.0, 26.0, "通勤搭西裤，周末搭牛仔裤都可以。"),
            (26.0, 30.0, "这一身日常穿很耐看。"),
        ]
        clips = ai_clipper._parse_ai_response(
            json.dumps({
                "clips": [
                    {"clip_type": "hook", "srt_indices": [1], "focus": "版型", "trim_priority": 0},
                    {"clip_type": "product", "srt_indices": [2], "focus": "版型", "trim_priority": 0},
                    {"clip_type": "close", "srt_indices": [5], "focus": "场景", "trim_priority": 0},
                ],
                "expansion_plan": [
                    {
                        "priority": 1,
                        "after_srt_indices": [2],
                        "after_order": 2,
                        "srt_indices": [3],
                        "focus": "面料质感",
                        "reason": "补充面料证据",
                    }
                ],
            }, ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
        )
        plan = ai_clipper._analysis_metadata_context().get("expansion_plan")
        expanded = ai_clipper._apply_ai_expansion_plan(clips, plan, entries, 20)
        self.assertEqual(
            [clip[1] for clip in expanded],
            [entries[0][2], entries[1][2], entries[2][2], entries[4][2]],
        )
        self.assertFalse(ai_clipper._director_duration_status(expanded, 20)["short"])

    def test_incremental_expand_call_preserves_story_and_prioritizes_missing_mix_source(self) -> None:
        entries = [
            (0.0, 4.0, "[V1]这件上衣穿上很显精神。"),
            (4.0, 10.0, "[V1]肩线自然，正面轮廓也很利落。"),
            (10.0, 18.0, "[V2]面料轻薄透气，夏天穿不会闷。"),
            (18.0, 26.0, "[V2]通勤搭西裤，周末搭牛仔裤都可以。"),
            (26.0, 30.0, "[V1]这一身日常穿很耐看。"),
        ]
        clips = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "版型"),
            ("product", entries[1][2], 4.0, 10.0, 50, 6.0, "版型"),
            ("close", entries[4][2], 26.0, 30.0, 50, 4.0, "场景"),
        ]
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({
                "expansion_plan": [{
                    "priority": 1,
                    "after_srt_indices": [2],
                    "after_order": 2,
                    "srt_indices": [3],
                    "focus": "面料质感",
                    "reason": "补充另一来源证据",
                }]
            }, ensure_ascii=False)}}]
        }, ensure_ascii=False).encode("utf-8")
        with mock.patch.object(ai_clipper.urllib.request, "urlopen", return_value=response) as urlopen:
            expanded = ai_clipper._call_director_expand_selection(
                "key",
                "https://example.com/v1",
                "deepseek-test",
                clips,
                entries,
                20,
                preferred_focus="版型显瘦",
                merge_mode=True,
            )
        self.assertEqual([clip[1] for clip in expanded], [
            entries[0][2], entries[1][2], entries[2][2], entries[4][2],
        ])
        request_body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        prompt = request_body["messages"][1]["content"]
        self.assertIn("尚未使用来源V2", prompt)
        self.assertIn("不能删除、改写、拆分、替换或重排", request_body["messages"][0]["content"])

    def test_source_contract_uses_ai_reserve_even_when_duration_is_already_valid(self) -> None:
        entries = [
            (0.0, 4.0, "[V1]这件上衣穿上很显精神。"),
            (4.0, 10.0, "[V1]肩线自然，正面轮廓也很利落。"),
            (10.0, 18.0, "[V2]面料轻薄透气，夏天穿不会闷。"),
            (18.0, 26.0, "[V2]通勤搭西裤，周末搭牛仔裤都可以。"),
            (26.0, 30.0, "[V1]这一身日常穿很耐看。"),
        ]
        clips = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "版型"),
            ("product", entries[1][2], 4.0, 10.0, 50, 6.0, "版型"),
            ("close", entries[4][2], 26.0, 30.0, 50, 4.0, "场景"),
        ]
        self.assertFalse(ai_clipper._director_duration_status(clips, 18)["short"])
        plan = [{
            "priority": 1,
            "after_srt_indices": [2],
            "after_order": 2,
            "srt_indices": [3],
            "focus": "面料质感",
            "reason": "补足V2来源",
        }]
        expanded = ai_clipper._apply_ai_expansion_plan(
            clips,
            plan,
            entries,
            18,
            required_sources={"[V1]", "[V2]"},
        )
        self.assertIn(entries[2][2], [clip[1] for clip in expanded])
        self.assertEqual(
            ai_clipper._director_missing_sources(expanded, {"[V1]", "[V2]"}),
            [],
        )

    def test_partial_word_sidecars_keep_every_mix_source(self) -> None:
        from volcengine_asr import build_semantic_segments, semantic_segments_to_srt

        v1_srt = [{"start": 0.0, "end": 4.0, "text": "这件上衣肩线很利落。"}]
        v2_words = [{
            "start": 1.0,
            "end": 5.0,
            "text": "这个面料轻薄透气。",
            "words": [
                {"text": "这个面料", "start": 1.0, "end": 2.5},
                {"text": "轻薄透气", "start": 2.6, "end": 5.0},
            ],
            "semantic_unit": True,
        }]
        mixed = (
            cutter_logic._mix_semantic_segments_for_source(v1_srt, [], "V1", "one.mp4")
            + cutter_logic._mix_semantic_segments_for_source([], v2_words, "V2", "two.mp4")
        )
        semantic = build_semantic_segments(mixed)
        rendered = semantic_segments_to_srt(semantic)

        self.assertEqual(len(semantic), 2)
        self.assertIn("[V1]", rendered)
        self.assertIn("[V2]", rendered)
        self.assertEqual(semantic[0]["timing_precision"], "srt")
        self.assertEqual(semantic[1]["timing_precision"], "word")

    def test_source_contract_supports_natural_minimum_quotas(self) -> None:
        clips = [
            ("hook", "[V1]这件衣服很显瘦。", 0.0, 4.0, 50, 4.0, "版型"),
            ("product", "[V2]面料轻薄透气。", 4.0, 9.0, 50, 5.0, "面料"),
            ("product", "[V2]通勤搭配很利落。", 9.0, 14.0, 50, 5.0, "场景"),
        ]
        requirements = {"[V1]": 2, "[V2]": 2}

        self.assertEqual(
            ai_clipper._director_source_deficits(clips, requirements),
            {"[V1]": 1},
        )
        self.assertEqual(
            ai_clipper._director_missing_sources(clips, requirements),
            ["[V1]"],
        )

    def test_mix_prompt_interleaves_sources_without_changing_candidate_ids(self) -> None:
        entries = [
            (0.0, 1.0, "[V1]一"),
            (1.0, 2.0, "[V1]二"),
            (2.0, 3.0, "[V2]三"),
            (3.0, 4.0, "[V2]四"),
            (4.0, 5.0, "[V3]五"),
            (5.0, 6.0, "[V3]六"),
        ]
        lines = [f"[#{index:02d}]" for index in range(1, 7)]

        result = ai_clipper._director_interleave_prompt_lines(lines, entries, chunk_size=1)

        self.assertEqual(result, ["[#01]", "[#03]", "[#05]", "[#02]", "[#04]", "[#06]"])
        self.assertCountEqual(result, lines)

    def test_mix_distribution_detects_dominance_and_long_same_source_run(self) -> None:
        clips = [
            ("product", f"[V1]卖点{index}", float(index), float(index + 1), 50, 1.0, "卖点")
            for index in range(24)
        ]
        clips += [
            ("product", "[V2]面料", 30.0, 31.0, 50, 1.0, "面料"),
            ("product", "[V2]场景", 31.0, 32.0, 50, 1.0, "场景"),
            ("product", "[V3]版型", 32.0, 33.0, 50, 1.0, "版型"),
            ("product", "[V3]工艺", 33.0, 34.0, 50, 1.0, "工艺"),
        ]
        summary = ai_clipper._director_source_distribution_summary(
            clips, {"[V1]": 2, "[V2]": 2, "[V3]": 2}
        )

        self.assertFalse(summary["balanced"])
        self.assertTrue(any("超过55%" in issue for issue in summary["issues"]))
        self.assertTrue(any("连续24段" in issue for issue in summary["issues"]))

    def test_mix_distribution_accepts_natural_interleaving(self) -> None:
        clips = []
        for index in range(4):
            for source in ("V1", "V2", "V3"):
                clips.append(("product", f"[{source}]卖点{index}", 0.0, 1.0, 50, 1.0, "卖点"))
        summary = ai_clipper._director_source_distribution_summary(
            clips, {"[V1]": 2, "[V2]": 2, "[V3]": 2}
        )
        self.assertTrue(summary["balanced"])
        self.assertEqual(summary["issues"], [])

    def test_expansion_plan_rejects_forbidden_candidate_and_uses_next_ai_choice(self) -> None:
        entries = [
            (0.0, 4.0, "这件上衣穿上很显精神。"),
            (4.0, 10.0, "肩线自然，正面轮廓也很利落。"),
            (10.0, 18.0, "现在价格只要九十九元。"),
            (18.0, 26.0, "面料轻薄透气，夏天穿不会闷。"),
            (26.0, 30.0, "这一身日常穿很耐看。"),
        ]
        clips = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "版型"),
            ("product", entries[1][2], 4.0, 10.0, 50, 6.0, "版型"),
            ("close", entries[4][2], 26.0, 30.0, 50, 4.0, "场景"),
        ]
        plan = [
            {"priority": 1, "after_order": 2, "srt_indices": [3], "focus": "价格"},
            {"priority": 2, "after_order": 2, "srt_indices": [4], "focus": "面料质感"},
        ]
        expanded = ai_clipper._apply_ai_expansion_plan(clips, plan, entries, 20)
        self.assertNotIn(entries[2][2], [clip[1] for clip in expanded])
        self.assertIn(entries[3][2], [clip[1] for clip in expanded])

class BatchSummaryReliabilityTests(unittest.TestCase):
    def test_final_preview_keeps_ai_preference_separate_from_concrete_mainline(self) -> None:
        result = server._preview_final_preference_summary(
            {
                "requested": "情绪感染",
                "label": "情绪感染",
                "used_label": "情绪感染",
            },
            {
                "topic_counts": {"面料质感": 3, "场景搭配": 2},
                "topic_durations": {"面料质感": 30.0, "场景搭配": 18.0},
            },
        )
        self.assertEqual(result["used_label"], "情绪感染")
        self.assertEqual(result["actual_mainline_label"], "面料质感")
        self.assertIn("正文主题主线为面料质感", result["detail"])

    def test_smart_cut_worker_continues_after_one_video_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            videos = []
            for index in range(1, 4):
                video = root / f"v{index}.mp4"
                video.write_bytes(b"video")
                videos.append(video)
            payload = server.SmartCutPayload(video_paths=[str(item) for item in videos], output_dir=str(root))
            task_id = server._new_task("smart-cut", "智能成片")

            def fake_process(video_path, **_kwargs):
                if Path(video_path).stem == "v2":
                    raise RuntimeError("有效内容不足：可用候选5条，最佳片单16.5秒，目标至少58秒")
                return {"ok": True}

            def fake_outputs(_out_dir, video, _started_at, _out_path, _result):
                return [str(root / f"{video.stem}_done.mp4")]

            with (
                mock.patch("cutter_logic.process_video", side_effect=fake_process),
                mock.patch.object(server, "_collect_smart_cut_outputs", side_effect=fake_outputs),
                mock.patch.object(server, "_ensure_feature_access"),
                mock.patch.object(server, "_pick_pip_asset", return_value=("", None)),
                mock.patch.object(server, "_archive_used_pip"),
                mock.patch.object(server, "_consume_trial"),
                mock.patch.object(server, "_record_output_history"),
            ):
                server._run_smart_cut(task_id, payload)

            task = server._TASKS[task_id]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["batch_done"], 3)
            self.assertEqual(task["batch_succeeded"], 2)
            self.assertEqual(task["batch_failed"], 1)
            self.assertEqual(task["batch_insufficient"], 1)

    def test_mix_worker_keeps_two_successes_after_third_group_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            groups = []
            for index in range(1, 4):
                video = root / f"g{index}.mp4"
                video.write_bytes(b"video")
                groups.append(server.MixBatchGroup(name=f"第{index}组", video_paths=[str(video)]))
            payload = server.MixBatchPayload(groups=groups, output_dir=str(root))
            task_id = server._new_task("mix", "批量混剪")
            calls = {"count": 0}

            def fake_mix(*_args, **_kwargs):
                calls["count"] += 1
                if calls["count"] == 3:
                    raise RuntimeError("有效内容不足：可用候选5条，最佳片单16.5秒，目标至少58秒")
                return True

            with (
                mock.patch("cutter_logic.process_video_mix", side_effect=fake_mix),
                mock.patch.object(server, "_ensure_feature_access"),
                mock.patch.object(server, "_pick_pip_asset", return_value=("", None)),
                mock.patch.object(server, "_archive_used_pip"),
                mock.patch.object(server, "_consume_trial"),
                mock.patch.object(server, "_record_output_history"),
            ):
                server._run_mix_batch(task_id, payload)

            task = server._TASKS[task_id]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["batch_done"], 3)
            self.assertEqual(task["batch_succeeded"], 2)
            self.assertEqual(task["batch_failed"], 1)
            self.assertEqual(task["batch_insufficient"], 1)
            self.assertIn("成功 2/3", task["message"])
            self.assertIn("内容不足 1", task["message"])

    def test_mix_summary_fields_survive_output_history(self) -> None:
        task = {
            "id": "mix-test",
            "scope": "mix",
            "title": "批量混剪",
            "status": "completed",
            "outputs": ["C:/out/g1.mp4", "C:/out/g2.mp4"],
            "batch_total": 3,
            "batch_done": 3,
            "batch_succeeded": 2,
            "batch_failed": 1,
            "batch_insufficient": 1,
        }
        records = server._output_history_records_from_task(task)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item["batch_done"] == 3 for item in records))
        self.assertTrue(all(item["batch_succeeded"] == 2 for item in records))
        self.assertTrue(all(item["batch_insufficient"] == 1 for item in records))

    def test_prefixed_batch_summary_is_parsed_as_all_items_processed(self) -> None:
        updates = server._task_batch_updates_from_message("批量混剪完成：成功 2/3 组 · 内容不足 1")
        self.assertEqual(updates["batch_total"], 3)
        self.assertEqual(updates["batch_done"], 3)
        self.assertEqual(updates["batch_succeeded"], 2)
        self.assertEqual(updates["batch_failed"], 1)

    def test_failure_classifier_separates_content_shortage(self) -> None:
        detail = server._batch_failure_detail(
            "短素材.mp4",
            "有效内容不足：可用候选5条，最佳片单16.5秒，目标至少58秒",
        )
        self.assertEqual(detail["code"], "insufficient_content")
        self.assertEqual(detail["candidate_count"], 5)
        self.assertEqual(detail["best_duration"], 16.5)

    def test_failure_classifier_and_summary_separate_duration_mismatch(self) -> None:
        detail = server._batch_failure_detail(
            "超长素材.mp4",
            "AI未满足时长：最佳片单137.1秒，要求50-70秒",
        )
        self.assertEqual(detail["code"], "duration_mismatch")
        self.assertEqual(detail["duration_high"], 70.0)
        summary = server._batch_summary_message("智能成片完成", 2, 3, [detail], "个")
        self.assertIn("成功 2/3", summary)
        self.assertIn("时长未达标 1", summary)

    def test_source_srt_mapping_is_not_reported_as_asr(self) -> None:
        message = server._progress_message(
            "字幕：使用源 SRT 映射 35 条，跳过成片语音识别。",
            "smart-cut",
        )
        self.assertEqual(message, "正在烧录源字幕。")

    def test_task_stage_message_can_advance_while_numeric_progress_stays_monotonic(self) -> None:
        task_id = server._new_task("smart-cut", "阶段测试")
        try:
            server._set_task_progress(task_id, 90, "字幕处理")
            server._set_task_progress(task_id, 50, "裁剪片段", force_message=True)
            task = server._TASKS[task_id]
            self.assertEqual(task["progress"], 90)
            self.assertEqual(task["message"], "裁剪片段")
        finally:
            server._TASKS.pop(task_id, None)


if __name__ == "__main__":
    unittest.main()
