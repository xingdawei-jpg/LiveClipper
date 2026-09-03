from __future__ import annotations

import ast
import importlib
import inspect
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
content_review = importlib.import_module("content_review")
cutter_logic = importlib.import_module("cutter_logic")
selection_contracts = importlib.import_module("selection_contracts")
server = importlib.import_module("server")
volcengine_asr = importlib.import_module("volcengine_asr")


class AiCandidateReliabilityTests(unittest.TestCase):
    def test_focus_detection_does_not_treat_apparel_quality_as_food_freshness(self) -> None:
        self.assertEqual(
            ai_clipper._detect_focus_point(
                "这件衣服的拉链做工和品质都很好",
                main_category="服饰内衣",
            ),
            "品质",
        )
        self.assertNotEqual(
            ai_clipper._detect_focus_point(
                "这件新款刚刚新鲜出炉",
                main_category="服饰内衣",
            ),
            "新鲜品质",
        )
        self.assertEqual(
            ai_clipper._detect_focus_point(
                "当天现摘，果形饱满而且新鲜",
                main_category="食品/生鲜",
            ),
            "新鲜品质",
        )
        self.assertNotEqual(
            ai_clipper._detect_focus_point(
                "这个产品品质很好",
                main_category="食品/生鲜",
            ),
            "新鲜品质",
        )
        self.assertEqual(
            ai_clipper._clip_focus_block(
                ("product", "这件衣服的拉链做工和品质都很好", 0, 5, 8, 5, "新鲜品质")
            ),
            "品质细节",
        )
        self.assertEqual(
            ai_clipper._clip_focus_block(
                ("product", "果园当天现摘，果形饱满", 0, 5, 8, 5, "新鲜品质")
            ),
            "新鲜品质",
        )
        self.assertEqual(
            ai_clipper._clip_focus_block(
                ("product", "这件新款刚刚上新", 0, 5, 8, 5, "新鲜品质")
            ),
            "流行趋势",
        )

    def test_long_preview_candidates_cover_each_source_and_timeline(self) -> None:
        candidates = []
        candidate_id = 1
        for source_id, count in (("[V1]", 180), ("[V2]", 60), ("[V3]", 6)):
            for index in range(count):
                candidates.append(selection_contracts.SelectionCandidate(
                    candidate_id=candidate_id,
                    source_id=source_id,
                    start=float(index * 5),
                    end=float(index * 5 + 4),
                    text=f"{source_id} candidate {index}",
                    hook_eligible=False,
                ))
                candidate_id += 1

        sampled = ai_clipper._stratified_preview_candidates(candidates, limit=30)

        self.assertEqual(len(sampled), 30)
        self.assertEqual([item.candidate_id for item in sampled], sorted(item.candidate_id for item in sampled))
        by_source = {}
        for item in sampled:
            by_source.setdefault(item.source_id, []).append(item.start)
        self.assertEqual(set(by_source), {"[V1]", "[V2]", "[V3]"})
        self.assertEqual(len(by_source["[V3]"]), 6)
        self.assertEqual(by_source["[V1]"][0], 0.0)
        self.assertEqual(by_source["[V1]"][-1], 895.0)
        self.assertEqual(by_source["[V2]"][0], 0.0)
        self.assertEqual(by_source["[V2]"][-1], 295.0)

    def test_partial_shortage_is_preview_only_and_never_success(self) -> None:
        ai_clipper._begin_analysis_metadata()
        contract = selection_contracts.DurationContract.create(60, 1.0, tolerance=10)
        clips = [
            ("hook", "这件上衣肩线很利落。", 0.0, 4.0, 50, 4.0, "版型"),
            ("product", "面料轻薄透气，夏天穿不会闷。", 4.0, 29.0, 50, 25.0, "面料"),
        ]

        details = ai_clipper._record_partial_insufficient(
            clips,
            candidate_count=2,
            duration_contract=contract,
        )
        metadata = ai_clipper.get_last_analysis_metadata()

        self.assertTrue(details["preview_only"])
        self.assertTrue(details["requires_user_confirmation"])
        self.assertFalse(details["export_allowed"])
        self.assertEqual(metadata["selection_result"]["status"], "partial_insufficient")
        self.assertFalse(metadata["selection_result"]["ok"])
        self.assertEqual(metadata["selection_manifest"]["selected_count"], 2)

    def test_partial_shortage_can_be_explicitly_exportable_without_becoming_success(self) -> None:
        ai_clipper._begin_analysis_metadata()
        contract = selection_contracts.DurationContract.create(90, 1.15, tolerance=15)
        clips = [
            ("hook", "这件上衣肩线很利落。", 0.0, 4.0, 50, 4.0, "版型"),
            ("product", "面料轻薄透气，夏天穿不会闷。", 4.0, 52.0, 50, 48.0, "面料"),
        ]

        details = ai_clipper._record_partial_insufficient(
            clips,
            candidate_count=2,
            duration_contract=contract,
            export_allowed=True,
            preview_only=False,
        )
        metadata = ai_clipper.get_last_analysis_metadata()

        self.assertFalse(details["preview_only"])
        self.assertFalse(details["requires_user_confirmation"])
        self.assertTrue(details["export_allowed"])
        self.assertTrue(details["duration_soft_constraint"])
        self.assertEqual(metadata["selection_result"]["status"], "partial_insufficient")
        self.assertFalse(metadata["selection_result"]["ok"])

    def test_director_returns_short_safe_plan_for_direct_output(self) -> None:
        srt = (
            "1\n00:00:00,000 --> 00:00:12,000\n"
            "这件上衣肩线向内收，穿上整个人看起来更利落。\n"
        )
        planned = [
            ("product", "这件上衣肩线向内收，穿上整个人看起来更利落。", 0.0, 12.0, 1, 12.0, "版型显瘦"),
        ]
        settings = {
            "api_key": "key",
            "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash",
            "content_review_mode": "off",
        }
        audit = {
            "issues": [],
            "warnings": ["总时长12.0s低于目标下限50s"],
            "needs_repair": False,
            "hard_removed": 0,
            "total_duration": 12.0,
            "duration_short": True,
            "duration_long": False,
            "duration_low": 50.0,
            "duration_high": 70.0,
            "duration_gap": 38.0,
        }
        with mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=[
                 {"srt_index": 1, "source": "V1", "duration_sec": 12.0, "text": planned[0][1]}
             ]), \
             mock.patch.object(ai_clipper, "_call_ai", return_value=planned), \
             mock.patch.object(ai_clipper, "_director_hard_audit", return_value=(planned, audit)):
            clips = ai_clipper.ai_analyze_clips(
                srt,
                target_duration=60,
                allow_short_duration_output=True,
            )

        metadata = ai_clipper.get_last_analysis_metadata()
        self.assertEqual(clips, planned)
        self.assertEqual(metadata["selection_result"]["status"], "partial_insufficient")
        self.assertTrue(metadata["selection_result"]["details"]["export_allowed"])
        self.assertFalse(metadata["selection_result"]["details"]["preview_only"])

    def test_only_preview_paths_enable_partial_ai_selection(self) -> None:
        source = inspect.getsource(cutter_logic)
        self.assertGreaterEqual(source.count("allow_partial=_clips_only"), 2)
        self.assertGreaterEqual(source.count("allow_short_duration_output=not _clips_only"), 2)
        self.assertGreaterEqual(source.count("record_history=not _clips_only"), 2)

    def test_partial_selection_bypasses_duration_only_for_preview(self) -> None:
        metadata = {
            "selection_result": {
                "status": "partial_insufficient",
                "details": {
                    "preview_only": True,
                    "export_allowed": False,
                },
            }
        }
        self.assertTrue(
            cutter_logic._is_preview_only_partial_selection(metadata, True)
        )
        self.assertFalse(
            cutter_logic._is_preview_only_partial_selection(metadata, False)
        )
        metadata["selection_result"]["details"]["export_allowed"] = True
        self.assertFalse(
            cutter_logic._is_preview_only_partial_selection(metadata, True)
        )
        source = inspect.getsource(cutter_logic)
        self.assertGreaterEqual(
            source.count("_is_preview_only_partial_selection(analysis_metadata, _clips_only)"),
            2,
        )

    def test_custom_duration_tolerance_is_preserved_across_contract_validation(self) -> None:
        contract = selection_contracts.DurationContract.create(60, 1.0, tolerance=15)
        self.assertEqual((contract.final_min, contract.final_max), (45.0, 75.0))
        self.assertEqual(contract.to_dict()["tolerance"], 15.0)
        self.assertEqual(
            selection_contracts.DurationContract.coerce(contract.to_dict()),
            contract,
        )
        self.assertTrue(contract.status(75.0)["accepted"])
        self.assertFalse(contract.status(75.8)["accepted"])
        self.assertEqual(ai_clipper._multi_version_target_bounds(30, 30), (1, 60.0))

        clips = [("product", "完整卖点片段", 0.0, 44.5, 50, 44.5)]
        accepted = cutter_logic._validate_selected_duration_contract(
            clips,
            60,
            duration_tolerance=15,
        )
        self.assertEqual((accepted["low"], accepted["high"]), (45.0, 75.0))

    def test_standard_dedup_reuses_the_duration_contract_speed(self) -> None:
        old_preset = cutter_logic.DEDUP_PRESET
        try:
            cutter_logic.DEDUP_PRESET = "medium"
            with mock.patch.object(
                cutter_logic,
                "_generate_random_dedup_params",
                return_value={
                    "crop_w": 1.0,
                    "crop_h": 1.0,
                    "crop_x": 0.0,
                    "crop_y": 0.0,
                    "speed": 1.29,
                    "gamma": 0.0,
                    "corner_mask": False,
                    "audio_reverb": False,
                    "noise_fusion": False,
                    "frame_interp": False,
                },
            ):
                dedup = cutter_logic.build_dedup_filters(1080, 1920, speed_factor=1.15)
        finally:
            cutter_logic.DEDUP_PRESET = old_preset

        self.assertIn("setpts=PTS/1.15", dedup["video_filters"])
        self.assertIn("atempo=1.15", dedup["audio_filters"])
        self.assertNotIn("1.29", dedup["video_filters"])

    def test_time_dedup_does_not_delete_the_next_compacted_clip(self) -> None:
        clips = [
            ("product", "肩线自然，轮廓很利落。", 0.0, 5.0, 50, 5.0, "版型显瘦"),
            ("product", "重复的肩线说明。", 0.5, 4.0, 40, 3.5, "版型显瘦"),
            ("product", "面料轻薄透气，夏天穿不闷。", 10.0, 14.0, 50, 4.0, "面料质感"),
        ]

        deduped = ai_clipper._dedup_clip_text_overlap(clips, None)

        self.assertEqual(
            [clip[1] for clip in deduped],
            [clips[0][1], clips[2][1]],
        )

    def test_selected_hook_style_replaces_size_reply_with_matching_opening(self) -> None:
        srt_text = (
            "1\n00:00:00,000 --> 00:00:02,500\n155斤穿XL码就可以。\n\n"
            "2\n00:00:02,500 --> 00:00:05,500\n这件上身肩线很利落，整个人看着更显瘦。\n\n"
            "3\n00:00:05,500 --> 00:00:09,500\n日常通勤穿也很有精神。"
        )
        clips = [
            ("hook", "155斤穿XL码就可以。", 0.0, 2.5, 50, 2.5, "尺寸长度"),
            ("product", "这件上身肩线很利落，整个人看着更显瘦。", 2.5, 5.5, 50, 3.0, "版型显瘦"),
            ("product", "日常通勤穿也很有精神。", 5.5, 9.5, 50, 4.0, "场景搭配"),
        ]

        refined = ai_clipper._refine_hook_by_dynamic_score(
            clips,
            srt_text,
            focus_hint="版型显瘦",
            ai_controls={"hook_style": "上身效果开头"},
        )

        self.assertEqual(refined[0][0], "hook")
        self.assertEqual(refined[0][1], clips[1][1])
        self.assertTrue(ai_clipper._hook_matches_selected_style(refined[0][1], "上身效果开头"))

    def test_secondary_pants_category_is_a_real_content_filter(self) -> None:
        self.assertEqual(ai_clipper._normalize_forced_category("裤子"), "裤子")
        clips = [
            ("product", "这件上衣肩线很利落。", 0.0, 4.0, 50, 4.0, "版型显瘦"),
            ("product", "这条裤子高腰直筒，通勤穿很利落。", 4.0, 8.0, 50, 4.0, "版型显瘦"),
        ]
        filtered = ai_clipper._post_filter_cross_category(
            clips,
            "这件上衣肩线很利落。这条裤子高腰直筒，通勤穿很利落。",
            None,
            preferred_cat="裤子",
        )

        self.assertEqual([clip[1] for clip in filtered], [clips[1][1]])

    def test_director_splits_long_product_at_frozen_short_sentence_boundaries(self) -> None:
        clip = ("product", "肩线很利落。面料很轻薄。通勤穿很干净。", 0.0, 9.0, 50, 9.0, "品质细节")
        entries = [
            (0.0, 3.0, "肩线很利落。"),
            (3.0, 6.0, "面料很轻薄。"),
            (6.0, 9.0, "通勤穿很干净。"),
        ]

        chunks = ai_clipper._split_director_overlong_product(clip, entries)

        self.assertEqual(len(chunks), 3)
        self.assertTrue(all(chunk[5] <= 5.2 for chunk in chunks))
        self.assertEqual([(chunk[2], chunk[3]) for chunk in chunks], [(0.0, 3.0), (3.0, 6.0), (6.0, 9.0)])

    def test_duration_fraction_inside_acceptance_margin_is_warning_not_failure(self) -> None:
        contract = selection_contracts.DurationContract.create(60, 1.0)
        clips = [
            ("hook", "这件上衣穿上很显精神。", 0.0, 3.0, 50, 3.0, "版型"),
            ("product", "肩线和轮廓都很利落，日常穿着很舒服。", 3.0, 66.1, 50, 63.1, "版型"),
            ("close", "这一身通勤穿很耐看。", 66.1, 70.1, 50, 4.0, "场景"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            60,
            8,
            duration_contract=contract,
        )
        self.assertFalse(audit["duration_long"])
        self.assertFalse(any("超过目标上限" in issue for issue in audit["issues"]))
        self.assertTrue(any("超过目标上限" in warning for warning in audit["warnings"]))

    def test_duration_overrun_is_warning_after_trim_attempts(self) -> None:
        contract = selection_contracts.DurationContract.create(60, 1.0)
        clips = [
            ("hook", "这件上衣穿上很显精神。", 0.0, 3.0, 50, 3.0, "版型"),
            ("product", "肩线和轮廓都很利落，日常穿着很舒服。", 3.0, 66.8, 50, 63.8, "版型"),
            ("close", "这一身通勤穿很耐看。", 66.8, 70.8, 50, 4.0, "场景"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            60,
            8,
            duration_contract=contract,
        )
        self.assertTrue(audit["duration_long"])
        self.assertFalse(any("超过目标上限" in issue for issue in audit["issues"]))
        self.assertTrue(any("超过目标上限" in warning for warning in audit["warnings"]))

    def test_ai_director_treats_subsecond_source_shortage_as_warning(self) -> None:
        contract = selection_contracts.DurationContract.create(90, 1.15)
        clips = [
            ("product", "完整安全卖点", 0.0, contract.source_min - 0.1, 50, contract.source_min - 0.1, "面料质感"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(
            clips,
            contract.ai_target_seconds,
            8,
            duration_contract=contract,
        )
        self.assertFalse(audit["duration_short"])
        self.assertFalse(any("低于目标下限" in issue for issue in audit["issues"]))
        self.assertTrue(any("编码误差" in warning for warning in audit["warnings"]))

    def test_local_duration_fit_reuses_safe_candidate_without_reordering_story(self) -> None:
        entries = [
            (0.0, 4.0, "穿上以后整个人很显精神。"),
            (4.0, 10.0, "肩线和版型会立即显得很利落。"),
            (10.0, 18.0, "面料轻薄透气，夏天穿也不会闷。"),
            (18.0, 30.0, "通勤和日常出门都可以直接搭配。"),
            (30.0, 34.0, "这一身日常穿很耐看。"),
        ]
        selected = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "版型"),
            ("product", entries[1][2], 4.0, 10.0, 50, 6.0, "版型"),
            ("close", entries[4][2], 30.0, 34.0, 50, 4.0, "场景"),
        ]
        fitted = ai_clipper._fit_director_duration_from_existing_candidates(
            selected,
            entries,
            30,
            duration_contract=selection_contracts.DurationContract.create(30, 1.0),
            allowed_candidate_ids={1, 2, 3, 4, 5},
            safe_inventory=[{"srt_index": index} for index in range(1, 6)],
        )

        self.assertEqual([clip[0] for clip in fitted], ["hook", "product", "product", "close"])
        self.assertEqual(fitted[0][1], selected[0][1])
        self.assertEqual(fitted[1][1], selected[1][1])
        self.assertEqual(fitted[-1][1], selected[-1][1])
        self.assertGreaterEqual(sum(float(clip[5]) for clip in fitted), 25.0)

    def test_local_duration_fit_prefers_grounded_primary_subject_but_keeps_ai_anchor_first(self) -> None:
        entries = [
            (0.0, 4.0, "这件上衣穿上以后肩线很利落。"),
            (4.0, 10.0, "肩部编织线把视觉重心往里收。"),
            (10.0, 22.0, "这件上衣的肩线向内收，所以整个人看起来更利落。"),
            (22.0, 34.0, "这条裤子的腰头平整，通勤穿不会勒肚子。"),
            (34.0, 38.0, "整套日常穿很耐看。"),
        ]
        selected = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "版型"),
            ("product", entries[1][2], 4.0, 10.0, 50, 6.0, "工艺"),
            ("close", entries[4][2], 34.0, 38.0, 50, 4.0, "场景"),
        ]
        review_bundle = content_review.ContentReviewBundle(
            "review-key",
            "digest",
            "上衣",
            "deepseek-v4-flash",
            (
                content_review.ContentCard(
                    3, "版型显瘦", "肩线内收", "说明上衣肩线如何修饰",
                    "原因解释", "肩线向内收", ("effect", "evidence"),
                    "independent", ("具体效果", "原因解释"), "main",
                    "上衣", "primary", "这件上衣",
                ),
                content_review.ContentCard(
                    4, "版型显瘦", "腰头平整", "说明裤子腰头不勒",
                    "具体效果", "不会勒肚子", ("effect",),
                    "independent", ("具体效果",), "reserve",
                    "裤子", "other", "这条裤子",
                ),
            ),
            24.0,
        )
        contract = selection_contracts.DurationContract.create(30, 1.0)
        safe_inventory = [{"srt_index": index} for index in range(1, 6)]

        fitted = ai_clipper._fit_director_duration_from_existing_candidates(
            selected,
            entries,
            30,
            duration_contract=contract,
            allowed_candidate_ids={1, 2, 3, 4, 5},
            review_bundle=review_bundle,
            safe_inventory=safe_inventory,
        )
        self.assertEqual([clip[1] for clip in fitted], [
            entries[0][2], entries[1][2], entries[2][2], entries[4][2],
        ])

        anchored = ai_clipper._fit_director_duration_from_existing_candidates(
            selected,
            entries,
            30,
            duration_contract=contract,
            expansion_plan=[{
                "priority": 1,
                "after_srt_indices": [2],
                "after_order": 2,
                "srt_indices": [4],
                "focus": "腰头说明",
            }],
            allowed_candidate_ids={1, 2, 3, 4, 5},
            review_bundle=review_bundle,
            safe_inventory=safe_inventory,
        )
        self.assertEqual([clip[1] for clip in anchored], [
            entries[0][2], entries[1][2], entries[3][2], entries[4][2],
        ])

    def test_mix_source_quota_repairs_then_warns_instead_of_failing(self) -> None:
        clips = [
            ("hook", "[V1]这件上衣穿上很显精神。", 0.0, 3.0, 50, 3.0, "版型"),
            ("product", "[V1]肩线和轮廓都很利落。", 3.0, 8.0, 50, 5.0, "版型"),
        ]
        requirements = {"[V1]": 1, "[V2]": 1}
        self.assertEqual(
            ai_clipper._director_source_quota_action(clips, requirements, 0, 2),
            "repair",
        )
        self.assertEqual(
            ai_clipper._director_source_quota_action(clips, requirements, 1, 2),
            "warn",
        )
        self.assertEqual(
            ai_clipper._director_source_quota_action(clips, {"[V1]": 1}, 0, 2),
            "satisfied",
        )

    def test_content_shortage_grace_accepts_safe_plan_without_changing_normal_contract(self) -> None:
        contract = selection_contracts.DurationContract.create(60, 1.15)
        self.assertFalse(contract.status(52.0)["accepted"])
        relaxed = contract.status(52.0, shortage_grace_seconds=5)
        self.assertTrue(relaxed["accepted"])
        self.assertTrue(relaxed["used_shortage_grace"])
        self.assertAlmostEqual(relaxed["relaxed_low"], 45.0)
        self.assertAlmostEqual(relaxed["relaxed_source_low"], 51.75)

    def test_director_shortage_grace_uses_final_seconds_after_speed_projection(self) -> None:
        contract = selection_contracts.DurationContract.create(60, 1.15)
        clips = [("hook", "食品口感很新鲜", 0.0, 52.0, 50, 52.0, "口感食欲")]
        normal = ai_clipper._director_duration_status(clips, 69, contract)
        relaxed = ai_clipper._director_duration_status(
            clips,
            69,
            contract,
            shortage_grace_seconds=5,
        )
        self.assertFalse(normal["accepted"])
        self.assertTrue(relaxed["accepted"])
        self.assertTrue(relaxed["used_shortage_grace"])

    def test_user_confirmed_preview_duration_is_warning_not_failure(self) -> None:
        clips = [("hook", "用户确认保留的完整片单", 0.0, 72.9, 50, 72.9, "场景搭配")]
        selected = cutter_logic._validate_selected_duration_contract(
            clips,
            90,
            1.0,
        )
        self.assertTrue(selected["underlength"])
        self.assertIn("低于建议下限", selected["duration_soft_warning"])
        actual_ok, actual = cutter_logic._validate_actual_duration_contract(72.9, 90)
        self.assertTrue(actual_ok)
        self.assertTrue(actual["underlength"])
        self.assertIn("低于建议下限", actual["duration_soft_warning"])

    def test_duration_grace_metadata_is_explicit_and_capped(self) -> None:
        self.assertEqual(cutter_logic._selection_shortage_grace_seconds({}), 0.0)
        self.assertEqual(
            cutter_logic._selection_shortage_grace_seconds({
                "duration_relaxation": {"applied": True, "grace_seconds": 99},
            }),
            5.0,
        )

    def test_verified_best_effort_policy_keeps_short_safe_output_as_warning(self) -> None:
        metadata = {
            "duration_relaxation": {
                "applied": True,
                "policy": "safe_best_effort_v1",
                "grace_seconds": 25.0,
                "standard_final_min": 50.0,
                "relaxed_final_min": 25.0,
            },
        }
        grace = cutter_logic._selection_shortage_grace_seconds(metadata)
        clips = [("product", "安全候选用尽后的最佳完整片单", 0.0, 28.75, 50, 28.75)]
        self.assertEqual(grace, 0.0)
        selected = cutter_logic._validate_selected_duration_contract(
            clips,
            60,
            1.15,
            shortage_grace_seconds=grace,
        )
        self.assertTrue(selected["underlength"])
        accepted, actual = cutter_logic._validate_actual_duration_contract(
            25.0,
            60,
            shortage_grace_seconds=grace,
        )
        self.assertTrue(accepted)
        self.assertTrue(actual["underlength"])

    def test_best_effort_policy_rejects_inconsistent_or_unsafe_metadata(self) -> None:
        for relaxation in (
            {
                "applied": True,
                "policy": "safe_best_effort_v1",
                "grace_seconds": 40.0,
                "standard_final_min": 50.0,
                "relaxed_final_min": 25.0,
            },
            {
                "applied": True,
                "policy": "safe_best_effort_v1",
                "grace_seconds": 50.0,
                "standard_final_min": 50.0,
                "relaxed_final_min": 0.0,
            },
        ):
            self.assertEqual(
                cutter_logic._selection_shortage_grace_seconds({
                    "duration_relaxation": relaxation,
                }),
                0.0,
            )

    def test_best_effort_metadata_is_not_reported_as_success(self) -> None:
        result = {
            "ok": True,
            "analysis_metadata": {
                "duration_relaxation": {
                    "applied": True,
                    "policy": "safe_best_effort_v1",
                    "projected_final_duration": 37.5,
                    "standard_final_min": 50.0,
                },
            },
        }
        detail = server._batch_best_effort_detail("短素材.mp4", result)
        self.assertIsNone(detail)
        summary = server._batch_summary_message(
            "智能成片完成",
            2,
            3,
            [{"code": "insufficient_safe_material"}],
            "个",
            [],
        )
        self.assertIn("成功 2/3", summary)
        self.assertIn("内容不足 1", summary)

        self.assertIsNone(server._batch_best_effort_detail("普通素材.mp4", {"ok": True}))
        self.assertIsNone(server._batch_best_effort_detail("普通素材.mp4", {
            "ok": True,
            "analysis_metadata": {
                "duration_relaxation": {
                    "applied": True,
                    "projected_final_duration": 37.5,
                    "standard_final_min": 50.0,
                },
            },
        }))

    def test_duration_overrun_output_is_reported_as_completed_warning(self) -> None:
        detail = server._batch_best_effort_detail("超时素材.mp4", {
            "ok": True,
            "duration_soft_warning": "成片78.7秒，超过建议上限75秒，已保留输出",
        })
        self.assertIsNotNone(detail)
        self.assertEqual(detail["code"], "duration_overrun_output")
        self.assertEqual(server._batch_best_effort_prefix(detail), "时长超出建议范围但已成片")
        summary = server._batch_summary_message(
            "智能成片完成", 3, 3, [], "个", [detail]
        )
        self.assertIn("成功 3/3", summary)
        self.assertIn("时长超出建议范围但已成片 1", summary)

    def test_duration_shortage_output_is_reported_as_completed_warning(self) -> None:
        detail = server._batch_best_effort_detail("短素材.mp4", {
            "ok": True,
            "duration_soft_kind": "under_target",
            "duration_soft_warning": "成片45.2秒，低于建议下限50秒，已保留输出",
        })
        self.assertIsNotNone(detail)
        self.assertEqual(detail["code"], "duration_shortage_output")
        self.assertEqual(server._batch_best_effort_prefix(detail), "时长低于建议范围但已成片")
        summary = server._batch_summary_message(
            "智能成片完成", 3, 3, [], "个", [detail]
        )
        self.assertIn("成功 3/3", summary)
        self.assertIn("时长低于建议范围但已成片 1", summary)

    def test_stale_best_effort_metadata_cannot_hide_short_duration_warning(self) -> None:
        metadata = ai_clipper._begin_analysis_metadata()
        metadata["duration_relaxation"] = {
            "applied": True,
            "policy": "safe_best_effort_v1",
            "grace_seconds": 5.0,
            "reason": "safe_candidates_exhausted",
            "projected_final_duration": 52.0 / 1.15,
            "standard_final_min": 50.0,
            "relaxed_final_min": 45.0,
        }

        public_metadata = ai_clipper.get_last_analysis_metadata()
        grace = cutter_logic._selection_shortage_grace_seconds(public_metadata)
        clips = [("product", "安全候选已经用尽后的完整片单", 0.0, 52.0, 50, 52.0)]
        self.assertEqual(grace, 0.0)
        selected = cutter_logic._validate_selected_duration_contract(
            clips,
            60,
            1.15,
            shortage_grace_seconds=grace,
        )
        self.assertTrue(selected["underlength"])

    def test_vocal_prefix_is_trimmed_but_semantic_connector_is_preserved(self) -> None:
        words = [
            {"text": "对", "start": 0.0, "end": 0.2},
            {"text": "而且", "start": 0.25, "end": 0.6},
            {"text": "它没有什么重量", "start": 0.65, "end": 1.8},
            {"text": "很轻", "start": 1.85, "end": 2.3},
        ]
        trimmed, removed = volcengine_asr._semantic_trim_weak_prefix(words)
        self.assertEqual(removed, ["对"])
        self.assertEqual(trimmed[0]["text"], "而且")
        self.assertAlmostEqual(trimmed[0]["start"], 0.25, places=2)

    def test_structure_allows_product_opening_when_no_real_hook_exists(self) -> None:
        entries = [
            (0.0, 3.0, "这件套装穿上特别轻松。"),
            (3.0, 8.0, "周末和朋友逛街很有松弛感。"),
            (8.0, 14.0, "面料轻薄透气，穿起来不会闷。"),
            (14.0, 18.0, "这一整套日常穿很耐看。"),
        ]
        parsed_after_invalid_hook = [
            ("product", entries[1][2], 3.0, 8.0, 50, 5.0, "场景搭配"),
            ("product", entries[2][2], 8.0, 14.0, 50, 6.0, "面料质感"),
            ("close", entries[3][2], 14.0, 18.0, 50, 4.0, "场景搭配"),
        ]
        stabilized = ai_clipper._stabilize_director_structure(
            parsed_after_invalid_hook,
            entries,
            {
                "allowed_hook_indices": [1],
                "ranked_hook_indices": [1],
                "requested_focus": "场景搭配",
            },
        )
        self.assertEqual([clip[0] for clip in stabilized], ["product", "product", "close"])
        self.assertEqual([clip[1] for clip in stabilized], [clip[1] for clip in parsed_after_invalid_hook])
        _safe, audit = ai_clipper._director_hard_audit(stabilized, 18, 8)
        self.assertFalse(any("Hook" in issue for issue in audit["issues"]))
        self.assertTrue(any("Product自然开场" in warning for warning in audit["warnings"]))

    def test_complete_story_repair_api_has_been_removed(self) -> None:
        self.assertFalse(hasattr(ai_clipper, "_director_repair_instruction"))

    def test_preview_split_parent_keeps_only_one_hook_role(self) -> None:
        raw = ("hook", "第一句完整开场第二句继续说明", 0.0, 4.0, 50, 4.0, "场景搭配")
        public = {
            "index": 0,
            "segments": [
                {"index": 0, "start": 0.0, "end": 1.2, "text": "第一句完整开场", "selected": True},
                {"index": 1, "start": 2.1, "end": 4.0, "text": "第二句继续说明", "selected": True},
            ],
        }
        groups = server._merge_selected_segments(public, raw, [0, 1])
        self.assertEqual([clip[0] for clip in groups], ["hook", "product"])

    def test_preview_exact_clause_hard_filter_catches_cta_and_asr_residue(self) -> None:
        clips = [
            ("product", "给我们关注点好哦", 0.0, 2.0, 50, 2.0, "互动"),
            ("product", "影子身高170体重asr", 2.0, 4.0, 50, 2.0, "尺寸"),
            ("product", "L腰围76全松紧，穿着很舒服", 4.0, 8.0, 50, 4.0, "尺寸长度"),
        ]
        filtered = server._hard_filter_preview_selection(clips, "test")
        self.assertEqual([clip[1] for clip in filtered], [clips[2][1]])
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

    def test_director_merges_only_dependent_fragments_and_keeps_complete_blocks_free(self) -> None:
        source = (
            "1\n00:00:10,000 --> 00:00:13,000\n你看她穿上以后整个人的气质\n\n"
            "2\n00:00:13,400 --> 00:00:18,000\n一般加上衣品一般就会显得整张脸有点浪费了。\n\n"
            "3\n00:00:25,000 --> 00:00:29,000\n八九月份穿这件薄长袖也没有问题。\n"
        )

        frozen = ai_clipper._freeze_director_candidates(source)
        entries = ai_clipper._parse_srt_entries_for_hook(frozen)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0][:2], (10.0, 18.0))
        self.assertIn("整个人的气质一般加上衣品一般", entries[0][2])
        self.assertEqual(entries[1][:2], (25.0, 29.0))
        self.assertEqual(entries[1][2], "八九月份穿这件薄长袖也没有问题。")
        self.assertEqual(ai_clipper._director_standalone_boundary_reason(entries[0][2]), "")
        self.assertEqual(ai_clipper._director_standalone_boundary_reason(entries[1][2]), "")

    def test_director_rejects_mid_clause_opening_and_unclosed_condition_as_standalone(self) -> None:
        mid_clause = "平庸的没有没有啊我很讨厌平庸和中庸两个字"
        unclosed_close = "我觉得既然大家来一趟"
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason(mid_clause),
            "开头承接上句",
        )
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason(unclosed_close),
            "结尾未说完",
        )

        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "close", "srt_indices": [1], "focus": "情绪感染", "reason": "错误收尾"},
            ], ensure_ascii=False),
            None,
            [(0.0, 4.0, unclosed_close)],
            set(),
            require_srt_indices=True,
        )
        self.assertEqual(clips, [])

    def test_hook_boundary_gate_rejects_tail_particle_and_freeze_rejoins_exact_word_tails(self) -> None:
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason(
                "吧？它一定不是靠身材去撑起来的。"
            ),
            "开头承接上句",
        )
        self.assertEqual(
            ai_clipper._hook_role_ineligibility_reason(
                "吧？它一定不是靠身材去撑起来的。"
            ),
            "开头承接上句",
        )
        rejected = ai_clipper._parse_ai_response(
            json.dumps([
                {
                    "clip_type": "hook",
                    "srt_indices": [1],
                    "focus": "版型显瘦",
                },
            ], ensure_ascii=False),
            None,
            [(0.0, 3.0, "吧？它一定不是靠身材去撑起来的。")],
            set(),
            require_srt_indices=True,
            allowed_hook_indices={1},
        )
        self.assertEqual(rejected, [])

        source = (
            "1\n00:00:00,000 --> 00:00:01,000\n聚\n\n"
            "2\n00:00:01,100 --> 00:00:04,000\n酯纤维的面料不显廉价。\n\n"
            "3\n00:00:05,000 --> 00:00:07,000\n侧面做了一个三角的立\n\n"
            "4\n00:00:07,100 --> 00:00:10,000\n体捏褶，腰线更利落。\n"
        )
        ai_clipper._begin_analysis_metadata()
        frozen = ai_clipper._freeze_director_candidates(source)
        entries = ai_clipper._parse_srt_entries_for_hook(frozen)

        self.assertEqual([entry[2] for entry in entries], [
            "聚酯纤维的面料不显廉价。",
            "侧面做了一个三角的立体捏褶，腰线更利落。",
        ])
        self.assertTrue(all(
            not ai_clipper._director_standalone_boundary_reason(entry[2])
            for entry in entries
        ))
        candidates = ai_clipper.get_last_analysis_metadata()["director_candidates"]
        self.assertTrue(all(candidate["hook_eligible"] for candidate in candidates))

    def test_auto_focus_respects_current_selling_points_before_global_weights(self) -> None:
        entries = [
            (0.0, 3.0, "这件衬衫修饰腰线，视觉上很显瘦。"),
            (3.2, 6.5, "通勤搭直筒裤去上班很利落。"),
            (6.7, 10.0, "周末出门配平底鞋也很轻松。"),
        ]
        source = (
            "00:00:00,000 --> 00:00:03,000\n这件衬衫修饰腰线，视觉上很显瘦。\n\n"
            "00:00:03,200 --> 00:00:06,500\n通勤搭直筒裤去上班很利落。\n\n"
            "00:00:06,700 --> 00:00:10,000\n周末出门配平底鞋也很轻松。"
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps([
                {"clip_type": "hook", "srt_indices": [2], "focus": "场景搭配", "reason": "通勤开场"},
                {"clip_type": "product", "srt_indices": [3], "focus": "场景搭配", "reason": "周末场景"},
                {"clip_type": "close", "srt_indices": [1], "focus": "版型显瘦", "reason": "补充版型"},
            ], ensure_ascii=False)}}]
        }).encode("utf-8")
        ai_clipper._begin_analysis_metadata()
        with (
            mock.patch.object(ai_clipper, "load_settings", return_value={"preference_weights": {"版型显瘦": 99.0}}),
            mock.patch.object(ai_clipper.urllib.request, "urlopen", return_value=response),
        ):
            clips = ai_clipper._call_ai(
                "key",
                "https://example.com/v1",
                "deepseek-test",
                source,
                None,
                srt_entries=entries,
                ai_controls={"selling_points": ["场景搭配"]},
                main_category="上衣",
            )

        metadata = ai_clipper.get_last_analysis_metadata()
        self.assertTrue(clips)
        self.assertEqual(metadata["preference_summary"]["used_label"], "场景搭配")
        self.assertEqual(metadata["preference_summary"]["allowed_labels"], ["场景搭配"])
        self.assertEqual(
            metadata["hook_candidate_summary"]["automatic_allowed_focuses"],
            ["场景搭配"],
        )

    def test_director_uses_verified_hook_package_as_the_only_hook_source(self) -> None:
        entries = [
            (0.0, 3.0, "这件短风衣立起来不会显得脖子短。"),
            (3.1, 6.0, "领子撑起来，视觉上把脖颈线条留得更利落。"),
            (6.1, 9.0, "短风衣搭牛仔裤也很干净。"),
        ]
        source = (
            "00:00:00,000 --> 00:00:03,000\n这件短风衣立起来不会显得脖子短。\n\n"
            "00:00:03,100 --> 00:00:06,000\n领子撑起来，视觉上把脖颈线条留得更利落。\n\n"
            "00:00:06,100 --> 00:00:09,000\n短风衣搭牛仔裤也很干净。"
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps([
                {"clip_type": "hook", "srt_indices": [1], "focus": "领型修饰", "reason": "视觉结果"},
                {"clip_type": "product", "srt_indices": [2], "focus": "领型修饰", "reason": "立刻证明"},
                {"clip_type": "close", "srt_indices": [3], "focus": "场景搭配", "reason": "场景收束"},
            ], ensure_ascii=False)}}]
        }).encode("utf-8")
        ai_clipper._begin_analysis_metadata()
        hook_package = {
            "hook_id": 1,
            "followup_id": 2,
            "topic": "版型显瘦",
            "reason": "领型直接解释脖颈比例",
            "hook_promise": "立起来不会显得脖子短",
            "proof_relation": "design_reason",
            "package_complete": True,
            "opening_tier": "B",
        }
        with (
            mock.patch.object(ai_clipper, "load_settings", return_value={}),
            mock.patch.object(ai_clipper.urllib.request, "urlopen", return_value=response) as request,
        ):
            clips = ai_clipper._call_ai(
                "key",
                "https://example.com/v1",
                "deepseek-test",
                source,
                None,
                srt_entries=entries,
                allowed_candidate_ids={1, 2, 3},
                content_review_hint="reviewed content",
                review_hook_pairs=[hook_package],
                review_hook_contract_kind="hook_package",
                review_hook_pairs_checked=True,
                review_hook_threads={
                    1: {
                        "topic": "版型显瘦",
                        "seed_followup_ids": [2],
                        "allowed_followup_ids": [2],
                    }
                },
            )

        self.assertEqual([clip[0] for clip in clips], ["hook", "product", "close"])
        self.assertEqual(clips[0][1], entries[0][2])
        payload = json.loads(request.call_args.args[0].data.decode("utf-8"))
        prompt = "\n".join(message["content"] for message in payload["messages"])
        self.assertIn("完整A/B HookPackage", prompt)
        self.assertIn("不得用普通候选或普通HookPair替换", prompt)
        self.assertIn("正文信息增量合同", prompt)
        self.assertEqual(
            ai_clipper.get_last_analysis_metadata()["hook_candidate_summary"]["review_hook_contract"],
            "hook_package",
        )

    def test_director_rejects_a_hook_package_without_its_same_topic_followup(self) -> None:
        entries = [
            (0.0, 3.0, "这件短风衣立起来不会显得脖子短。"),
            (3.1, 6.0, "领子撑起来，视觉上把脖颈线条留得更利落。"),
            (6.1, 9.0, "短风衣搭牛仔裤也很干净。"),
        ]
        source = (
            "00:00:00,000 --> 00:00:03,000\n这件短风衣立起来不会显得脖子短。\n\n"
            "00:00:03,100 --> 00:00:06,000\n领子撑起来，视觉上把脖颈线条留得更利落。\n\n"
            "00:00:06,100 --> 00:00:09,000\n短风衣搭牛仔裤也很干净。"
        )
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps([
                {"clip_type": "hook", "srt_indices": [1], "focus": "版型显瘦", "reason": "强开场"},
                {"clip_type": "product", "srt_indices": [3], "focus": "场景搭配", "reason": "错误跳题"},
            ], ensure_ascii=False)}}]
        }).encode("utf-8")
        ai_clipper._begin_analysis_metadata()
        with (
            mock.patch.object(ai_clipper, "load_settings", return_value={}),
            mock.patch.object(ai_clipper.urllib.request, "urlopen", return_value=response),
        ):
            clips = ai_clipper._call_ai(
                "key",
                "https://example.com/v1",
                "deepseek-test",
                source,
                None,
                srt_entries=entries,
                allowed_candidate_ids={1, 2, 3},
                review_hook_pairs=[{"hook_id": 1, "followup_id": 2, "topic": "版型显瘦"}],
                review_hook_contract_kind="hook_package",
                review_hook_threads={
                    1: {"topic": "版型显瘦", "seed_followup_ids": [2], "allowed_followup_ids": [2]}
                },
            )

        self.assertEqual(clips, [])

    def test_standalone_dangling_fragments_are_rejected_but_complete_group_is_kept(self) -> None:
        ai_clipper._begin_analysis_metadata()
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason("完整卖点说到一半呃"),
            "结尾语气残留",
        )
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason(
                "我的肩比我这根线多出来这么多呢，但是你会不会发现这个线像是我肩最宽的位置啊？"
            ),
            "",
        )
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason("吧对上班族日常都能穿"),
            "开头承接上句",
        )
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason("颜色它又是属于"),
            "结尾未说完",
        )
        entries = [
            (0.0, 3.0, "这件衬衫把腰线收得很自然，"),
            (3.1, 6.0, "也很显瘦，视觉比例更利落。"),
            (6.1, 8.0, "如果说给你做那种"),
            (8.1, 11.0, "纯色的话就会显得太普通。"),
        ]
        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "hook", "srt_indices": [2], "focus": "版型显瘦", "reason": "错误残句"},
                {"clip_type": "product", "srt_indices": [3], "focus": "颜色氛围", "reason": "错误条件残句"},
                {"clip_type": "product", "srt_indices": [1, 2], "focus": "版型显瘦", "reason": "完整表达"},
            ], ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
        )

        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0][2:4], (0.0, 6.0))
        self.assertIn("也很显瘦", clips[0][1])
        metadata = ai_clipper.get_last_analysis_metadata()
        provenance = next(iter(metadata["director_clip_provenance"].values()))
        self.assertEqual(provenance["candidate_indices"], [1, 2])
        self.assertEqual(provenance["reason"], "完整表达")

    def test_selected_word_tail_completion_uses_only_exact_adjacent_word_pair(self) -> None:
        clips = [
            ("product", "[V1] 这张脸有点浪费了的那种感", 10.0, 12.0, 50, 2.0, "情绪感染"),
        ]
        word_timings = [{
            "source_marker": "V1",
            "words": [
                {"text": "感", "start": 11.88, "end": 12.0},
                {"text": "觉", "start": 12.06, "end": 12.14},
            ],
        }, {
            "source_marker": "V2",
            "words": [
                {"text": "觉", "start": 12.04, "end": 12.10},
            ],
        }]

        repaired = ai_clipper._complete_selected_word_tail_boundaries(clips, word_timings)

        self.assertEqual(repaired[0][1], "[V1] 这张脸有点浪费了的那种感觉")
        self.assertEqual(repaired[0][3], 12.14)
        self.assertAlmostEqual(repaired[0][5], 2.14)

        delayed = ai_clipper._complete_selected_word_tail_boundaries(
            clips,
            [{
                "source_marker": "V1",
                "words": [{"text": "觉", "start": 12.25, "end": 12.35}],
            }],
        )
        self.assertEqual(delayed, clips)

    def test_final_selection_audit_uses_postprocessed_final_clips(self) -> None:
        ai_clipper._begin_analysis_metadata()
        hook = ("hook", "通勤穿这一件肩线很利落。", 0.0, 3.0, 50, 3.0, "场景搭配")
        product = ("product", "袖口走线很细致，日常穿也耐看。", 3.0, 8.0, 50, 5.0, "品质细节")
        ai_clipper._record_director_clip_provenance(
            hook,
            candidate_indices=[4],
            reason="通勤开场",
        )
        ai_clipper._record_director_clip_provenance(
            product,
            candidate_indices=[8],
            reason="工艺证明",
        )
        ai_clipper._record_hook_audit_event(
            "dynamic_score_replace",
            after=hook,
            details={"replacement_score": 42.0},
        )
        audit = cutter_logic._build_final_selection_audit(
            [hook, product],
            ai_clipper.get_last_analysis_metadata(),
        )

        self.assertEqual(audit["version"], "final_selection_audit_v1")
        self.assertEqual(audit["selected_duration"], 8.0)
        self.assertEqual(audit["final_clips"][0]["order"], 1)
        self.assertEqual(audit["final_clips"][0]["candidate_indices"], [4])
        self.assertEqual(audit["final_clips"][1]["reason"], "工艺证明")
        self.assertTrue(audit["final_clips"][0]["hook_postprocessed"])
        json.dumps(audit, ensure_ascii=False)

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

    def test_overlong_director_result_is_a_soft_warning(self) -> None:
        clips = [
            ("hook", "这件上衣很显精神", 0.0, 5.0, 50, 5.0, "效果"),
            ("product", "肩线自然而且整体轮廓很利落。", 5.0, 75.0, 50, 70.0, "版型"),
            ("close", "日常通勤穿这一身就很好看。", 75.0, 80.0, 50, 5.0, "场景"),
        ]
        _safe, audit = ai_clipper._director_hard_audit(clips, 60, 8)
        self.assertTrue(audit["duration_long"])
        self.assertFalse(any("超过目标上限70s" in issue for issue in audit["issues"]))
        self.assertTrue(any("超过目标上限70s" in warning for warning in audit["warnings"]))

    def test_director_duration_contract_splits_only_at_complete_frozen_candidates(self) -> None:
        entries = [
            (0.0, 4.0, "这件上衣上身很显精神。"),
            (4.0, 10.0, "肩线会把整个人的轮廓撑得更利落。"),
            (10.0, 17.0, "面料轻薄透气，夏天穿起来不会闷。"),
            (17.0, 22.0, "通勤出门直接搭配牛仔裤就很好看。"),
            (22.0, 26.0, "这一身日常穿很耐看。"),
        ]
        clips = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "版型显瘦"),
            (
                "product",
                "".join(item[2] for item in entries[1:4]),
                4.0,
                22.0,
                50,
                18.0,
                "穿着体验",
            ),
            ("close", entries[4][2], 22.0, 26.0, 50, 4.0, "场景搭配"),
        ]

        safe, audit = ai_clipper._director_hard_audit(
            clips,
            24,
            8,
            duration_contract=selection_contracts.DurationContract.create(24, 1.0),
            srt_entries=entries,
        )

        products = [clip for clip in safe if clip[0] == "product"]
        self.assertEqual([round(clip[5], 1) for clip in products], [6.0, 7.0, 5.0])
        self.assertTrue(all(clip[5] <= 8.2 for clip in products))
        self.assertEqual(audit["per_clip_duration"]["split_count"], 1)
        self.assertEqual(audit["per_clip_duration"]["removed_count"], 0)
        self.assertEqual(sum(clip[5] for clip in safe), 26.0)

    def test_director_duration_contract_preserves_unsplittable_complete_product(self) -> None:
        entries = [
            (0.0, 4.0, "这件上衣上身很显精神。"),
            (4.0, 21.0, "这是一段完整但过长的商品讲解，内容不能在冻结候选边界内拆开。"),
            (21.0, 25.0, "这一身日常穿很耐看。"),
        ]
        clips = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "版型显瘦"),
            ("product", entries[1][2], 4.0, 21.0, 50, 17.0, "品质细节"),
            ("close", entries[2][2], 21.0, 25.0, 50, 4.0, "场景搭配"),
        ]

        safe, audit = ai_clipper._director_hard_audit(
            clips,
            20,
            8,
            duration_contract=selection_contracts.DurationContract.create(20, 1.0),
            srt_entries=entries,
        )

        self.assertEqual([clip[0] for clip in safe], ["hook", "product", "close"])
        self.assertEqual([round(clip[5], 1) for clip in safe if clip[0] == "product"], [17.0])
        self.assertEqual(audit["per_clip_duration"]["split_count"], 0)
        self.assertEqual(audit["per_clip_duration"]["removed_count"], 0)
        self.assertEqual(audit["per_clip_duration"]["preserved_overlong_count"], 1)
        self.assertAlmostEqual(audit["per_clip_duration"]["preserved_overlong_duration"], 17.0)
        self.assertTrue(any("为避免截断已保留原句" in warning for warning in audit["warnings"]))

    def test_final_ai_selection_range_lock_bypasses_legacy_srt_expansion(self) -> None:
        preview_clip = (
            "product", "完整卖点。", 10.0, 16.0, 50.0, 6.0, "品质细节",
            {"preview_exact": True},
        )

        self.assertTrue(cutter_logic._clip_range_locked(preview_clip))
        self.assertTrue(cutter_logic._clip_range_locked(ai_selected=True))
        self.assertFalse(cutter_logic._clip_range_locked(("product", "普通片段", 0, 4, 50, 4, "")))

    def test_ai_expansion_skips_overlong_product_before_hard_audit(self) -> None:
        entries = [
            (0.0, 4.0, "这件上衣上身很显精神。"),
            (4.0, 15.0, "这是一段完整但超过单段节奏上限的商品讲解。"),
            (15.0, 20.0, "肩线向内收，视觉会更利落。"),
            (20.0, 24.0, "通勤穿这一身很耐看。"),
        ]
        clips = [
            ("hook", entries[0][2], 0.0, 4.0, 50, 4.0, "效果"),
            ("close", entries[3][2], 20.0, 24.0, 50, 4.0, "场景"),
        ]
        expanded = ai_clipper._apply_ai_expansion_plan(
            clips,
            [{
                "priority": 1,
                "after_srt_indices": [1],
                "after_order": 1,
                "srt_indices": [2],
                "focus": "长讲解",
            }],
            entries,
            24,
            duration_contract=selection_contracts.DurationContract.create(24, 1.0),
        )

        self.assertEqual(expanded, [])

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

    def test_ai_removal_priority_keeps_safe_partial_trim_when_overrun_remains(self) -> None:
        clips = [
            ("hook", "开场", 0.0, 5.0, 50, 5.0, "版型显瘦"),
            ("product", "承接开场", 5.0, 10.0, 50, 5.0, "版型显瘦"),
            ("product", "低价值补充", 10.0, 24.0, 50, 14.0, "场景搭配"),
            ("product", "核心证据", 24.0, 42.0, 50, 18.0, "面料质感"),
            ("close", "自然收尾", 42.0, 47.0, 50, 5.0, "场景搭配"),
        ]
        logs = []
        trimmed = ai_clipper._apply_ai_removal_priority(
            clips,
            [3],
            20,
            log_fn=logs.append,
        )

        self.assertEqual([clip[1] for clip in trimmed], ["开场", "承接开场", "核心证据", "自然收尾"])
        self.assertTrue(any("仍超过建议上限" in message for message in logs))

    def test_ai_removal_priority_cannot_break_mix_source_contract(self) -> None:
        clips = [
            ("hook", "[V1]开场", 0.0, 5.0, 50, 5.0, "效果"),
            ("product", "[V1]承接", 5.0, 10.0, 50, 5.0, "证据"),
            ("product", "[V2]另一来源证据", 10.0, 15.0, 50, 5.0, "面料"),
            ("product", "[V1]可删除补充", 15.0, 20.0, 50, 5.0, "场景"),
            ("close", "[V2]自然收尾", 20.0, 25.0, 50, 5.0, "场景"),
        ]
        trimmed = ai_clipper._apply_ai_removal_priority(
            clips,
            [3, 4],
            15,
            required_sources={"[V1]": 2, "[V2]": 2},
        )
        self.assertIn(clips[2][1], [clip[1] for clip in trimmed])
        self.assertNotIn(clips[3][1], [clip[1] for clip in trimmed])
        self.assertEqual(
            ai_clipper._director_missing_sources(trimmed, {"[V1]": 2, "[V2]": 2}),
            [],
        )
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

    def test_declared_trim_call_keywords_match_helper_signature(self) -> None:
        tree = ast.parse(inspect.getsource(ai_clipper.ai_analyze_clips))
        supported = set(inspect.signature(ai_clipper._apply_declared_trim_priorities).parameters)
        call_keywords = {
            keyword.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_apply_declared_trim_priorities"
            for keyword in node.keywords
            if keyword.arg is not None
        }

        self.assertTrue(call_keywords <= supported, call_keywords - supported)

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
        self.assertFalse(any("Hook未体现指定偏好" in issue for issue in audit["issues"]))
        self.assertFalse(any("Hook第二段未承接" in issue for issue in audit["issues"]))
        self.assertTrue(any("Hook未体现指定偏好" in warning for warning in audit["warnings"]))
        self.assertTrue(any("Hook第二段未承接" in warning for warning in audit["warnings"]))

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

    def test_completed_empty_hook_contract_forces_product_opening(self) -> None:
        entries = [
            (0.0, 3.0, "全松紧腰穿上不勒，裤子不挑人。"),
            (3.0, 7.0, "天丝亚麻成分让裤子更轻薄透气。"),
        ]
        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "hook", "srt_indices": [1], "focus": "版型显瘦"},
                {"clip_type": "product", "srt_indices": [2], "focus": "面料质感"},
            ], ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
            allowed_hook_indices=set(),
        )

        self.assertEqual([clip[0] for clip in clips], ["product"])
        self.assertEqual(clips[0][1], entries[1][2])

    def test_selection_duration_contract_keeps_overlong_output_as_warning(self) -> None:
        valid = [("product", "完整卖点", 0.0, 69.0, 50, 69.0)]
        result = cutter_logic._validate_selected_duration_contract(valid, 60, 1.15)
        self.assertAlmostEqual(result["projected_final"], 60.0, places=1)

        short = [("product", "完整卖点", 0.0, 46.3, 50, 46.3)]
        short_result = cutter_logic._validate_selected_duration_contract(short, 60, 1.15)
        self.assertTrue(short_result["underlength"])
        self.assertIn("低于建议下限", short_result["duration_soft_warning"])

        overlong = [("product", "完整卖点", 0.0, 137.1, 50, 137.1)]
        overlong_result = cutter_logic._validate_selected_duration_contract(
            overlong, 60, 1.15,
        )
        self.assertTrue(overlong_result["overlong"])
        self.assertIn("超过建议上限", overlong_result["duration_soft_warning"])

    def test_actual_duration_contract_keeps_both_out_of_range_outputs(self) -> None:
        self.assertTrue(cutter_logic._validate_actual_duration_contract(60.0, 60)[0])
        accepted_short, short = cutter_logic._validate_actual_duration_contract(44.6, 60)
        self.assertTrue(accepted_short)
        self.assertTrue(short["underlength"])
        self.assertEqual(short["duration_soft_kind"], "under_target")
        accepted, detail = cutter_logic._validate_actual_duration_contract(
            78.7, 60, duration_tolerance=15,
        )
        self.assertTrue(accepted)
        self.assertTrue(detail["overlong"])
        self.assertEqual(detail["high"], 75.0)
        self.assertIn("超过建议上限", detail["duration_soft_warning"])
        self.assertTrue(cutter_logic._validate_actual_duration_contract(124.1, 60)[0])

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

    def test_multi_industry_category_profiles_include_mother_baby_pet(self) -> None:
        self.assertEqual(ai_clipper._normalize_forced_category("宠物用品"), "母婴宠物")
        self.assertEqual(ai_clipper._feedback_category_bucket("宠物用品"), "mother_baby_pet")
        self.assertEqual(ai_clipper.infer_category_from_filename("猫粮宠物零食新品.mp4"), "母婴宠物")
        pet_overlay = ai_clipper._category_system_overlay("母婴宠物")
        self.assertIn("母婴宠物", pet_overlay)
        self.assertIn("宠物疾病治愈", pet_overlay)

    def test_unknown_future_category_uses_general_semantics_not_a_false_clothing_profile(self) -> None:
        self.assertIsNone(ai_clipper._normalize_forced_category("量子摆件"))
        self.assertEqual(ai_clipper._feedback_category_bucket("量子摆件"), "general")

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
        selected = cutter_logic._validate_selected_duration_contract(clips, 90, 1.15)
        self.assertTrue(selected["underlength"])
    def test_parser_extracts_prefixed_director_object_without_losing_expansion_plan(self) -> None:
        entries = [
            (0.0, 4.0, "这一件上身很有精神。"),
            (4.0, 8.0, "肩线利落，轮廓不会显得松垮。"),
        ]
        response = {
            "clips": [
                {"clip_type": "hook", "srt_indices": [1], "focus": "上身效果"},
                {"clip_type": "product", "srt_indices": [2], "focus": "版型显瘦"},
            ],
            "expansion_plan": [{
                "priority": 1,
                "srt_indices": [2],
                "after_srt_indices": [1],
                "reason": "补充肩线证据 {完整}",
            }],
        }
        content = "导演结果如下：\n```json\n" + json.dumps(response, ensure_ascii=False) + "\n```\n请按此执行。"
        ai_clipper._begin_analysis_metadata()
        clips = ai_clipper._parse_ai_response(
            content,
            None,
            entries,
            set(),
            require_srt_indices=True,
        )

        self.assertEqual([clip[1] for clip in clips], [entry[2] for entry in entries])
        plan = ai_clipper._analysis_metadata_context()["expansion_plan"]
        self.assertEqual(plan[0]["srt_indices"], [2])
        self.assertEqual(plan[0]["reason"], "补充肩线证据 {完整}")

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
    def test_multi_version_never_silently_falls_back_to_one_output(self) -> None:
        with mock.patch.object(ai_clipper, "is_enabled", return_value=False):
            result = cutter_logic.process_video_multi("sample.mp4", num_versions=2)

        self.assertFalse(result["ok"])
        self.assertEqual(result["requested_versions"], 2)
        self.assertEqual(result["produced_versions"], 0)
        self.assertEqual(result["outputs"], [])
        self.assertIn("未降级为单版本", result["error"])

    def test_mix_result_uses_declared_multi_version_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "mix.mp4"
            video.write_bytes(b"video")
            payload = server.MixPayload(video_paths=[str(video)], output_dir=str(root), versions=2)
            task_id = server._new_task("mix", "混剪")
            declared_outputs = [str(root / "mix_v1.mp4"), str(root / "mix_v2.mp4")]

            with (
                mock.patch(
                    "cutter_logic.process_video_mix",
                    return_value={"ok": True, "outputs": declared_outputs, "requested_versions": 2, "produced_versions": 2},
                ),
                mock.patch.object(server, "_ensure_feature_access"),
                mock.patch.object(server, "_pick_pip_asset", return_value=("", None)),
                mock.patch.object(server, "_archive_used_pip"),
                mock.patch.object(server, "_consume_trial"),
                mock.patch.object(server, "_record_output_history"),
            ):
                server._run_mix(task_id, payload)

            task = server._TASKS[task_id]
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["outputs"], declared_outputs)
            self.assertEqual(task["result_count"], 2)

    def test_mix_batch_counts_groups_separately_from_version_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            groups = []
            for index in range(1, 3):
                video = root / f"g{index}.mp4"
                video.write_bytes(b"video")
                groups.append(server.MixBatchGroup(name=f"第{index}组", video_paths=[str(video)]))
            payload = server.MixBatchPayload(groups=groups, output_dir=str(root), versions=2)
            task_id = server._new_task("mix", "批量混剪")
            calls = {"count": 0}

            def fake_mix(*_args, **_kwargs):
                calls["count"] += 1
                return {
                    "ok": True,
                    "outputs": [str(root / f"g{calls['count']}_v1.mp4"), str(root / f"g{calls['count']}_v2.mp4")],
                    "requested_versions": 2,
                    "produced_versions": 2,
                }

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
            self.assertEqual(task["batch_succeeded"], 2)
            self.assertEqual(task["result_count"], 4)
            self.assertEqual(len(task["outputs"]), 4)

    def test_payload_models_accept_optional_duration_tolerance(self) -> None:
        self.assertEqual(server.SmartCutPayload(duration_tolerance=15).duration_tolerance, 15)
        self.assertEqual(server.MixPayload(duration_tolerance=20).duration_tolerance, 20)
        self.assertIsNone(server.SmartCutPayload().duration_tolerance)
        self.assertIsNone(server.MixPayload().duration_tolerance)
        source = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('duration_tolerance: selectedDurationTolerance("sc")', source)
        self.assertIn('duration_tolerance: selectedDurationTolerance("mix")', source)

    def test_legacy_mix_batch_does_not_rethrow_one_group_failure(self) -> None:
        source = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        body = source.split("async function submitMixBatchLegacyQueue", 1)[1].split(
            "async function waitForTaskComplete",
            1,
        )[0]
        self.assertIn('await runPreflight("mix", singlePayload, "mix")', body)
        self.assertIn("组失败并跳过", body)
        self.assertNotIn("throw error;", body)

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
            payload = server.SmartCutPayload(
                video_paths=[str(item) for item in videos],
                output_dir=str(root),
                duration_tolerance=15,
            )
            task_id = server._new_task("smart-cut", "智能成片")
            seen_tolerances = []

            def fake_process(video_path, **kwargs):
                seen_tolerances.append(kwargs.get("duration_tolerance"))
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
            self.assertEqual(seen_tolerances, [15, 15, 15])

    def test_mix_worker_keeps_two_successes_after_third_group_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            groups = []
            for index in range(1, 4):
                video = root / f"g{index}.mp4"
                video.write_bytes(b"video")
                groups.append(server.MixBatchGroup(name=f"第{index}组", video_paths=[str(video)]))
            payload = server.MixBatchPayload(
                groups=groups,
                output_dir=str(root),
                duration_tolerance=20,
            )
            task_id = server._new_task("mix", "批量混剪")
            calls = {"count": 0}
            seen_tolerances = []

            def fake_mix(*_args, **kwargs):
                calls["count"] += 1
                seen_tolerances.append(kwargs.get("duration_tolerance"))
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
            self.assertEqual(seen_tolerances, [20, 20, 20])

    def test_mix_summary_fields_survive_output_history(self) -> None:
        failure_detail = server._batch_failure_detail("第3组", "有效内容不足：可用候选5条，最佳片单16.5秒，目标至少58秒")
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
            "batch_failure_details": [failure_detail],
        }
        records = server._output_history_records_from_task(task)
        self.assertEqual(len(records), 2)
        self.assertTrue(all(item["batch_done"] == 3 for item in records))
        self.assertTrue(all(item["batch_succeeded"] == 2 for item in records))
        self.assertTrue(all(item["batch_insufficient"] == 1 for item in records))
        self.assertTrue(all(
            item["batch_failure_details"] == [{
                "label": failure_detail["label"],
                "code": failure_detail["code"],
                "message": failure_detail["message"],
            }]
            for item in records
        ))

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

    def test_task_progress_exposes_structured_phase_and_batch_item_fields(self) -> None:
        task_id = server._new_task("smart-cut", "阶段测试")
        try:
            server._set_task_progress(task_id, 72, "裁剪片段", phase_current=7, phase_total=19)
            server._set_task(task_id, batch_total=3, batch_current=2)
            task = server._TASKS[task_id]
            self.assertEqual(task["phase"], "cut")
            self.assertEqual(task["phase_label"], "裁剪片段")
            self.assertEqual(task["phase_current"], 7)
            self.assertEqual(task["phase_total"], 19)
            self.assertEqual(task["item_total"], 3)
            self.assertEqual(task["item_current"], 2)
        finally:
            server._TASKS.pop(task_id, None)

    def test_asr_phase_wins_when_recognition_message_mentions_subtitles(self) -> None:
        phase, label = server._task_phase_from_message("正在使用本地语音识别并生成字幕")
        self.assertEqual(phase, "asr")
        self.assertEqual(label, "识别字幕")

    def test_bare_progress_marker_does_not_replace_subtitle_phase(self) -> None:
        task_id = server._new_task("smart-cut", "字幕阶段测试")
        try:
            log = server._task_log_fn(task_id, "smart-cut")
            log("[STEP] 📝 字幕处理中...")
            self.assertEqual(server._TASKS[task_id]["phase"], "subtitle")
            log("[PROGRESS] 0.65")
            self.assertEqual(server._TASKS[task_id]["phase"], "subtitle")
        finally:
            server._TASKS.pop(task_id, None)

    def test_batch_task_keeps_overall_and_current_item_progress_separate(self) -> None:
        task_id = server._new_task("smart-cut", "批量进度测试")
        try:
            server._set_task(task_id, batch_total=3, batch_done=1, batch_current=2)
            server._set_task_progress(task_id, 48, "裁剪片段", item_progress=64)
            task = server._TASKS[task_id]
            self.assertEqual(task["overall_percent"], 55)
            self.assertEqual(task["item_progress"], 64)

            server._set_task(task_id, batch_current=3)
            self.assertEqual(server._TASKS[task_id]["item_progress"], 0)
        finally:
            server._TASKS.pop(task_id, None)

    def test_batch_progress_uses_completed_materials_and_monotonic_current_item(self) -> None:
        task_id = server._new_task("smart-cut", "批量进度回归")
        try:
            server._set_task(task_id, batch_total=2, batch_done=1, batch_current=2)
            server._set_task_progress(task_id, 90, "去重处理", item_progress=90)
            self.assertEqual(server._TASKS[task_id]["overall_percent"], 95)

            # Subtitle extraction reports a lower internal scale, but it is
            # still part of the same second material and must not regress.
            server._set_task_progress(task_id, 65, "字幕处理", item_progress=65)
            task = server._TASKS[task_id]
            self.assertEqual(task["item_progress"], 90)
            self.assertEqual(task["overall_percent"], 95)

            server._set_task(task_id, batch_done=2, batch_current=0)
            self.assertEqual(server._TASKS[task_id]["overall_percent"], 100)
        finally:
            server._TASKS.pop(task_id, None)

    def test_worker_logs_are_bound_to_the_owning_task(self) -> None:
        task_id = server._new_task("mix", "日志绑定测试")
        last_id = server._LOG_SEQ
        try:
            server._run_task_worker(task_id, lambda: server.emit_log("warning", "当前素材失败，已继续下一个。", "mix"))
            events = server._snapshot_logs(last_id)
            event = next(item for item in events if item["message"] == "当前素材失败，已继续下一个。")
            self.assertEqual(event["task_id"], task_id)
            self.assertEqual(event["kind"], "warning")
            self.assertEqual(server._TASKS[task_id]["warnings"][-1]["message"], event["message"])
            restored = server.task_logs(task_id, after_id=last_id)
            self.assertEqual(restored["task_id"], task_id)
            self.assertEqual(restored["events"][-1]["id"], event["id"])
        finally:
            server._TASKS.pop(task_id, None)


if __name__ == "__main__":
    unittest.main()
