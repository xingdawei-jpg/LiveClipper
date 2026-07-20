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


if __name__ == "__main__":
    unittest.main()
