from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

candidate_quality = importlib.import_module("candidate_quality")


def _tokens(text: str, step: float = 0.1) -> list[dict[str, object]]:
    return [
        {"text": char, "norm": char, "start": index * step, "end": (index + 1) * step}
        for index, char in enumerate(text)
    ]


class CandidateQualityTests(unittest.TestCase):
    def test_word_exact_trim_removes_dangling_clause_before_complete_question(self) -> None:
        text = "而且亚麻的哎你们有没有发现今年大衣都有亚麻"

        repair = candidate_quality.leading_fragment_trim(_tokens(text))

        self.assertIsNotNone(repair)
        self.assertEqual(repair["prefix"], "而且亚麻的哎")
        self.assertAlmostEqual(repair["boundary"], len("而且亚麻的哎") * 0.1)

    def test_normal_material_sentence_is_not_trimmed(self) -> None:
        self.assertIsNone(candidate_quality.leading_fragment_trim(_tokens("而且亚麻面料穿着很透气")))

    def test_trim_boundary_uses_next_kept_word_start_across_a_timing_gap(self) -> None:
        tokens = _tokens("而且亚麻的哎你们有没有发现今年大衣都有亚麻")
        kept_index = len("而且亚麻的哎")
        tokens[kept_index]["start"] = float(tokens[kept_index - 1]["end"]) + 0.03

        repair = candidate_quality.leading_fragment_trim(tokens)

        self.assertIsNotNone(repair)
        self.assertAlmostEqual(repair["boundary"], tokens[kept_index]["start"])

    def test_only_high_confidence_garble_is_filtered(self) -> None:
        clips = [
            ("product", "这个面料穿起来很柔软", 0.0, 3.0, 0, 3.0),
            ("product", "这件感兴趣的是夏天的吗", 3.0, 6.0, 0, 3.0),
            ("product", "白搭白搭绿还蛮干净的", 6.0, 9.0, 0, 3.0),
            ("product", "支数越高织法越密", 9.0, 12.0, 0, 3.0),
            ("product", "高支亚麻手感更细腻", 12.0, 15.0, 0, 3.0),
            ("product", "这件外套夏到秋都能穿", 15.0, 18.0, 0, 3.0),
        ]

        filtered = candidate_quality.filter_candidate_clips(clips)

        self.assertEqual(
            [clip[1] for clip in filtered],
            [clips[0][1], clips[3][1], clips[4][1], clips[5][1]],
        )
        self.assertEqual(filtered[0], clips[0])

    def test_unusable_transcript_residue_and_cost_quotes_are_filtered(self) -> None:
        clips = [
            ("product", "\u8fd9\u4e2a\u9762\u6599\u7a7f\u8d77\u6765\u5f88\u67d4\u8f6f", 0.0, 3.0, 0, 3.0),
            ("product", "\u4ed6\u9ebb\u5dfe\u8ddf\u80a0\u6e29\u67d4\u8f6f\u4e86\u7136\u540e\u8fd8\u6709\u4e00\u70b9", 3.0, 6.0, 0, 3.0),
            ("product", "\u5728\u8fd9\u91cc\u3002\u6574\u4e2a\u95e8\u895f\u505a\u5230\u4f60\u770b\u5b83\u8fd9\u6837\u7684", 6.0, 9.0, 0, 3.0),
            ("product", "\u8fd9\u4ef6\u4e0d\u884c\u54e6\uff0c\u5bf9\uff0c\u98ce\u683c\u4e0d\u5927", 9.0, 12.0, 0, 3.0),
            ("product", "\u9762\u6599\u6210\u672c210\uff0c\u6240\u4ee5\u4e0d\u4fbf\u5b9c", 12.0, 15.0, 0, 3.0),
            ("product", "\u61c2\u8d27\u7684\u4eba\u76f4\u63a5\u79d2\u5e26\uff0c\u4e0d\u7528\u6307\u671b\u4fbf\u5b9c", 15.0, 18.0, 0, 3.0),
            ("product", "\u5b83\u4e0b\u610f\u8bc6\u4e2d\u8fd8\u86ee\u597d\u770b\u7684", 18.0, 21.0, 0, 3.0),
            ("product", "\u90a3\u62dc\u62dc\u4e9a\u9ebb\u8fd8\u662f\u5f88\u7ec6\u817b", 21.0, 24.0, 0, 3.0),
            ("product", "200\u65a4\u5185\u6211\u5e94\u8be5\u6ca1\u6bdb\u75c5", 24.0, 27.0, 0, 3.0),
            ("product", "\u9ad8\u652f\u4e9a\u9ebb\u624b\u611f\u66f4\u7ec6\u817b", 27.0, 30.0, 0, 3.0),
            ("product", "\u80a9\u7ebf\u5411\u5185\u6536\uff0c\u89c6\u89c9\u66f4\u5229\u843d", 30.0, 33.0, 0, 3.0),
            ("product", "\u8272\u7ec7\u7eb1\u7ebf\u8ba9\u989c\u8272\u66f4\u5747\u5300", 33.0, 36.0, 0, 3.0),
        ]

        filtered = candidate_quality.filter_candidate_clips(clips)

        self.assertEqual([clip[1] for clip in filtered], [clips[0][1], clips[9][1], clips[10][1], clips[11][1]])

    def test_joined_transcript_residue_and_personal_try_on_claims_are_rejected(self) -> None:
        unusable = [
            "它其实偏深绿，在这里。整个门襟做了拼接色系。",
            "你们也能发现整件衣服唉好多毛边。",
            "你看毛边。袖口的毛边。",
            "麻它是植物的根茎它要多道层层筛选。",
            "这种亚麻纱很难。",
            "这衣服将近3米，真的假的，我计算机按一下。",
            "200斤内我应该没毛病。",
        ]

        for text in unusable:
            with self.subTest(text=text):
                self.assertTrue(candidate_quality.candidate_quality_flags(text))

        self.assertFalse(candidate_quality.candidate_quality_flags("高克重粗织亚麻，纹理更立体。"))


if __name__ == "__main__":
    unittest.main()
