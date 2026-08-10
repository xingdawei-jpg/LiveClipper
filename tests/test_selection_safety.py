from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


ai_clipper = importlib.import_module("ai_clipper")
selection_safety = importlib.import_module("selection_safety")
content_review = importlib.import_module("content_review")


class LiveInteractionSafetyTests(unittest.TestCase):
    def test_reported_personal_size_variants_are_hard_rejected(self) -> None:
        samples = (
            "158的女孩子是S。但是我跟你说，那个小女人这个女生。",
            "我一。7米，我是大女。我是我1.7哦。",
            "105斤的你直接M码。",
            "160斤以内轻松驾驭，可以单买上衣。",
            "你子身高170，体重105，上身的东西看一下吧。",
            "姐妹有尺码问题抓紧问。",
            "1.6米98S码。好看吧。",
            "一米六九十八S码。",
            "那个不是说明天吗？啊，那我穿啥？",
            "粉色粉色粉色什么粉色姐妹，我们有粉色的衣服吗？今天没有吧。",
        )
        for text in samples:
            with self.subTest(text=text):
                self.assertTrue(selection_safety.live_interaction_or_size_response_reason(text))

    def test_objective_product_measurements_remain_available(self) -> None:
        for text in (
            "这件衣长158厘米，肩线向内收更显利落。",
            "尺码表做得清楚，腰围和裤长都标得明白。",
            "领口弹力足，日常穿脱不会勒脖子。",
        ):
            with self.subTest(text=text):
                self.assertFalse(selection_safety.live_interaction_or_size_response_reason(text))

    def test_frozen_director_candidates_never_include_personal_size_reply(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:03,000\n这件亚麻外套肩线向内收，穿上更显利落。\n\n"
            "2\n00:00:03,200 --> 00:00:06,600\n158的女孩子是S。但是我跟你说，那个小女人这个女生。\n\n"
            "3\n00:00:06,800 --> 00:00:09,800\n我一。7米，我是大女。我是我1.7哦。\n\n"
            "4\n00:00:10,000 --> 00:00:13,000\n色织从纱线源头定做，整件颜色更均匀。\n\n"
            "5\n00:00:13,200 --> 00:00:16,200\n高支亚麻摸起来细腻，夏天穿也透气。\n"
        )

        frozen = ai_clipper._freeze_director_candidates(source)

        self.assertNotIn("158的女孩子", frozen)
        self.assertNotIn("我一。7米", frozen)
        self.assertIn("高支亚麻摸起来细腻", frozen)

    def test_director_hard_audit_cannot_return_personal_size_as_hook(self) -> None:
        clips = [
            ("hook", "[V1] 158的女孩子是S，但是我跟你说我1.7米。", 0.0, 4.0, 50, 4.0),
            ("product", "[V1] 肩线向内收，视觉上更利落。", 4.0, 8.0, 50, 4.0),
            ("close", "[V1] 通勤和周末都很耐看。", 8.0, 11.0, 50, 3.0),
        ]
        logs: list[str] = []

        safe, audit = ai_clipper._director_hard_audit(clips, 10, 8, logs.append)

        self.assertFalse(any("158的女孩子" in str(clip[1]) for clip in safe))
        self.assertGreaterEqual(audit["hard_removed"], 1)
        self.assertTrue(any("个人尺码" in item for item in logs))

    def test_size_information_cannot_be_advertised_as_hook_candidate(self) -> None:
        candidates, _total = ai_clipper._collect_hook_candidates_from_entries(
            [
                (7, 0.0, 3.0, "1.6米98S码。好看吧。"),
                (19, 3.0, 6.0, "这个豹纹晕染上身显白，整个人气色更亮。"),
            ],
            hook_keywords=["好看", "显白"],
        )

        self.assertEqual([candidate[0] for candidate in candidates], [19])
        self.assertTrue(selection_safety.hook_ineligible_reason("尺码表从S到XL都齐全。"))
        self.assertFalse(selection_safety.hook_ineligible_reason("豹纹晕染上身显白，气色更亮。"))

    def test_live_demonstration_preamble_cannot_be_advertised_as_hook(self) -> None:
        text = "我自己啊我可能会这样穿，背面看一下，不挑人很舒服。"

        self.assertEqual(
            selection_safety.hook_ineligible_reason(text),
            "展示铺垫不可作Hook",
        )
        self.assertTrue(ai_clipper._is_bad_hook_candidate_text(text))
        self.assertFalse(content_review._reviewable_hook_text(text))

        viewer_preamble = "你这一套去穿呢也很松，你上班通勤穿这一套也可以的。"
        self.assertEqual(
            selection_safety.hook_ineligible_reason(viewer_preamble),
            "展示铺垫不可作Hook",
        )
        self.assertTrue(ai_clipper._is_bad_hook_candidate_text(viewer_preamble))
        self.assertFalse(content_review._reviewable_hook_text(viewer_preamble))

    def test_generic_live_preamble_cannot_be_hook(self) -> None:
        candidates, _total = ai_clipper._collect_hook_candidates_from_entries(
            [
                (1, 0.0, 3.0, "直接炸了吧，因为这个款式也拖欠你们特别久了。"),
                (2, 3.0, 7.0, "这个豹纹晕染上身显白，整个人气色更亮。"),
            ],
            hook_keywords=["炸了", "显白"],
        )

        self.assertEqual([candidate[0] for candidate in candidates], [2])
        self.assertTrue(ai_clipper._is_bad_hook_candidate_text("直接炸了吧，因为这个款式也拖欠你们特别久了。"))
        self.assertFalse(ai_clipper._is_bad_hook_candidate_text("这个豹纹晕染上身显白，整个人气色更亮。"))
        self.assertTrue(
            ai_clipper._is_bad_hook_candidate_text(
                "这件反正我建议大家夏天第一点这衣服你说显瘦吗"
            )
        )
        self.assertEqual(
            selection_safety.hook_ineligible_reason("很 duang 的"),
            "空泛口头语不可作Hook",
        )
        self.assertTrue(ai_clipper._is_bad_hook_candidate_text("很 duang 的"))
        self.assertTrue(
            ai_clipper._is_bad_hook_candidate_text(
                "很 duang 的这个裤子它不是纯亚麻，它是天丝亚麻。"
            )
        )

    def test_malformed_audience_benefit_fragment_cannot_be_hook_but_stays_body(self) -> None:
        malformed = "你们机洗水洗久穿久如新的整件衣服能够做到遮肉显瘦"
        product = "这件衣服机洗水洗以后依然很挺，久穿也不会变形。"
        clips = [
            ("hook", malformed, 0.0, 4.4, 50, 4.4),
            ("product", malformed, 4.5, 8.9, 50, 4.4),
            ("hook", product, 9.0, 13.0, 50, 4.0),
        ]

        self.assertTrue(ai_clipper._is_bad_hook_candidate_text(malformed))
        self.assertFalse(content_review._reviewable_hook_text(malformed))
        self.assertFalse(ai_clipper._is_bad_hook_candidate_text(product))

        safe = ai_clipper._filter_low_value_hook_clips(clips)

        self.assertEqual([clip[1] for clip in safe], [malformed, product])
        self.assertEqual([clip[0] for clip in safe], ["product", "hook"])
        report = ai_clipper._build_plan_quality_report(
            clips[:2],
            [(0.0, 4.4, malformed), (4.5, 8.9, malformed)],
        )
        self.assertEqual(report["status"], "fail")
        self.assertTrue(any("Hook不合格" in item for item in report["hard_failures"]))

    def test_delayed_try_on_promise_is_removed_as_live_interaction(self) -> None:
        text = "4号衬量搭配，你想小只，我等会儿穿给你看可以不？"
        source = (
            "1\n00:00:00,000 --> 00:00:03,000\n" + text + "\n\n"
            "2\n00:00:03,200 --> 00:00:06,200\n"
            "色织从纱线源头定做，整件颜色更均匀。\n"
        )

        self.assertEqual(
            selection_safety.live_interaction_or_size_response_reason(text),
            "直播互动回复",
        )
        self.assertEqual(selection_safety.hook_ineligible_reason(text), "直播互动回复")
        self.assertTrue(ai_clipper._is_bad_hook_candidate_text(text))
        self.assertNotIn(text, ai_clipper._freeze_director_candidates(source))

        safe, audit = ai_clipper._director_hard_audit(
            [
                ("hook", text, 0.0, 3.0, 50, 3.0),
                ("product", "色织从纱线源头定做，整件颜色更均匀。", 3.2, 6.2, 50, 3.0),
            ],
            6,
            8,
        )
        self.assertFalse(any(text in str(clip[1]) for clip in safe))
        self.assertGreaterEqual(audit["hard_removed"], 1)

    def test_audience_verdict_prompt_is_removed_as_live_interaction(self) -> None:
        text = "你们自己说吧，本期款好不好？行不行？"

        self.assertEqual(
            selection_safety.live_interaction_or_size_response_reason(text),
            "直播互动回复",
        )
        self.assertEqual(selection_safety.hook_ineligible_reason(text), "直播互动回复")
        self.assertTrue(ai_clipper._is_bad_hook_candidate_text(text))

    def test_plan_quality_fails_closed_for_invalid_hook(self) -> None:
        clips = [
            ("hook", "4号衬量搭配，你想小只，我等会儿穿给你看可以不？", 0.0, 3.0, 50, 3.0),
            ("product", "色织从纱线源头定做，整件颜色更均匀。", 3.2, 6.2, 50, 3.0),
        ]
        report = ai_clipper._build_plan_quality_report(
            clips,
            [
                (0.0, 3.0, clips[0][1]),
                (3.2, 6.2, clips[1][1]),
            ],
        )

        self.assertEqual(report["status"], "fail")
        self.assertTrue(report["hard_failures"])

        self.assertTrue(
            ai_clipper._is_bad_hook_candidate_text(
                "\u5450\uff0c\u548c\u7eb8\u7eb1\u3002\u7136\u540e\u8fd9\u79cd\u548c\u7eb8\u7eb1\uff0c\u5b83\u7684\u6574\u4e2a\u624b\u611f\u5f88\u597d\u3002"
            )
        )
        self.assertTrue(
            ai_clipper._is_bad_hook_candidate_text(
                "\u975e\u5e38\u975e\u5e38\u72e0\uff0c\u800c\u4e14\u9762\u6599\u4e5f\u4e0d\u5bb9\u6613\u52fe\u4e1d\u3002"
            )
        )
        safe, audit = ai_clipper._director_hard_audit(
            [
                ("hook", "直接炸了吧，因为这个款式也拖欠你们特别久了。", 0.0, 3.0, 50, 3.0),
                ("product", "这个豹纹晕染上身显白，整个人气色更亮。", 3.0, 7.0, 50, 4.0),
            ],
            7,
            8,
        )
        self.assertFalse(any(clip[0] == "hook" for clip in safe))
        self.assertGreaterEqual(audit["hard_removed"], 1)

    def test_structure_does_not_replace_a_size_hook_by_itself(self) -> None:
        clips = [
            ("hook", "[V1] 1.6米98S码。好看吧。", 0.0, 3.0, 50, 3.0),
            ("product", "[V1] 豹纹晕染上身显白，整个人气色更亮。", 3.0, 7.0, 50, 4.0),
            ("close", "[V1] 通勤和周末都很耐看。", 7.0, 10.0, 50, 3.0),
        ]
        entries = [
            (0.0, 3.0, "[V1] 1.6米98S码。好看吧。"),
            (3.0, 7.0, "[V1] 豹纹晕染上身显白，整个人气色更亮。"),
            (7.0, 10.0, "[V1] 通勤和周末都很耐看。"),
        ]

        stable = ai_clipper._stabilize_director_structure(
            clips,
            entries,
            {"ranked_hook_indices": [2]},
        )

        self.assertEqual(stable[0][0], "hook")
        self.assertIn("1.6", stable[0][1])
        self.assertEqual([clip[1] for clip in stable], [clip[1] for clip in clips])
        safe, audit = ai_clipper._director_hard_audit(stable, 10, 8)
        self.assertFalse(any(clip[0] == "hook" for clip in safe))
        self.assertFalse(any("Hook" in issue for issue in audit["issues"]))
        self.assertTrue(any("Product自然开场" in warning for warning in audit["warnings"]))

    def test_ai_opening_pair_replaces_only_the_opening_and_preserves_body_order(self) -> None:
        entries = [
            (0.0, 2.0, "[V1] 1.6米 98S码。好看吧。"),
            (2.2, 5.2, "[V1] 肩部黑色编织线把视觉重心向内收。"),
            (5.2, 8.8, "[V1] 穿上以后肩线会往里收，看起来更利落。"),
            (8.8, 12.0, "[V1] 通勤和周末都很耐看。"),
        ]
        body = [
            ("product", entries[1][2], 2.2, 5.2, 50, 3.0),
            ("close", entries[3][2], 8.8, 12.0, 50, 3.2),
        ]

        repaired = ai_clipper._apply_ai_opening_pair(
            body,
            {"hook_id": 3, "followup_id": 2},
            entries,
            allowed_candidate_ids={2, 3, 4},
        )

        self.assertIsNotNone(repaired)
        self.assertEqual([clip[0] for clip in repaired], ["hook", "product", "close"])
        self.assertEqual([clip[1] for clip in repaired], [entries[2][2], entries[1][2], entries[3][2]])

    def test_ai_opening_pair_rejects_size_or_interaction_even_when_model_declares_it(self) -> None:
        entries = [
            (0.0, 2.0, "[V1] 1.6米 98S码。好看吧。"),
            (2.2, 5.2, "[V1] 肩部黑色编织线把视觉重心向内收。"),
            (5.2, 8.8, "[V1] 穿上以后肩线会往里收，看起来更利落。"),
        ]
        body = [("product", entries[2][2], 5.2, 8.8, 50, 3.6)]

        repaired = ai_clipper._apply_ai_opening_pair(
            body,
            {"hook_id": 1, "followup_id": 2},
            entries,
            allowed_candidate_ids={1, 2, 3},
        )

        self.assertIsNone(repaired)

    def test_display_preamble_cannot_be_used_as_close(self) -> None:
        clips = [
            ("hook", "[V1] 全松紧腰穿上不勒，腿部活动很轻松。", 0.0, 3.0, 50, 3.0),
            ("product", "[V1] 腰头回弹好，久坐也不会卡肚子。", 3.0, 6.0, 50, 3.0),
            ("close", "[V1] 我自己可能会这样穿，背面看一下，不挑人很舒服。", 6.0, 10.0, 50, 4.0),
        ]

        safe, audit = ai_clipper._director_hard_audit(clips, 8, 8)

        self.assertEqual([clip[0] for clip in safe], ["hook", "product"])
        self.assertGreaterEqual(audit["hard_removed"], 1)

    def test_display_preamble_cannot_be_kept_as_product_filler(self) -> None:
        clips = [
            ("product", "[V1] 全松紧腰穿上不勒，腿部活动很轻松。", 0.0, 3.0, 50, 3.0),
            ("product", "[V1] 我自己可能会这样穿，背面看一下，不挑人很舒服。", 3.0, 7.0, 50, 4.0),
        ]

        safe, audit = ai_clipper._director_hard_audit(clips, 6, 8)

        self.assertEqual([clip[1] for clip in safe], [clips[0][1]])
        self.assertGreaterEqual(audit["hard_removed"], 1)


if __name__ == "__main__":
    unittest.main()
