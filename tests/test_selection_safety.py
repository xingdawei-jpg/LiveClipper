from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


ai_clipper = importlib.import_module("ai_clipper")
selection_safety = importlib.import_module("selection_safety")


class LiveInteractionSafetyTests(unittest.TestCase):
    def test_reported_personal_size_variants_are_hard_rejected(self) -> None:
        samples = (
            "158的女孩子是S。但是我跟你说，那个小女人这个女生。",
            "我一。7米，我是大女。我是我1.7哦。",
            "105斤的你直接M码。",
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

    def test_structure_replaces_a_size_hook_with_a_real_opening(self) -> None:
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
        self.assertIn("豹纹晕染", stable[0][1])
        self.assertFalse(any("1.6米98S码" in str(clip[1]) and clip[0] == "hook" for clip in stable))


if __name__ == "__main__":
    unittest.main()
