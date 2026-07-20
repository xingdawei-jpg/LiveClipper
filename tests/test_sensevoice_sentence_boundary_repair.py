from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

ai_clipper = importlib.import_module("ai_clipper")
local_asr_quality = importlib.import_module("local_asr_quality")
volcengine_asr = importlib.import_module("volcengine_asr")


def _timed_characters(text: str, step: float = 0.12) -> list[dict[str, float | str]]:
    return [
        {"text": char, "start": index * step, "end": (index + 1) * step}
        for index, char in enumerate(text)
    ]


class SenseVoiceSentenceBoundaryRepairTests(unittest.TestCase):
    def test_false_punctuation_and_observed_word_errors_are_repaired_before_segmentation(self) -> None:
        text = (
            "首先。这个世界上有非常多的各种。高端面料？但是大家不穿。"
            "因为它是。亚麻的。这个你们只有收到面料的那一刻。才能感觉到好不好。"
            "当然好的面料不是为富人而单。单独为富人准备的料子。"
            "这个衣服就是属于。其实会喜欢的人很多，因为这件衣服其实。成本的确贵。"
            "怕缩水17？麻呢，它其实是按克重收费的。不要热水洗，你坏，你冷水洗。"
            "别冲动小飞，买的是进口吗？如果是边捡的麻就会扎。"
        )
        punctuation = "，。！？!?；;：:、 "
        spoken = "".join(char for char in text if char not in punctuation)
        segment = {
            "text": text,
            "start": 0.0,
            "end": len(spoken) * 0.12,
            "words": _timed_characters(spoken),
        }

        corrected, count = local_asr_quality.apply_domain_corrections([segment])
        semantic = volcengine_asr.build_semantic_segments(corrected)
        repaired = corrected[0]["text"]

        self.assertGreaterEqual(count, 13)
        self.assertIn("首先，这个世界上有非常多的各种高端面料，但是大家不穿。", repaired)
        self.assertIn("因为它是，亚麻的。", repaired)
        self.assertIn("收到面料的那一刻，才能感觉到好不好。", repaired)
        self.assertIn("不是为富人而单独为富人准备的料子。", repaired)
        self.assertIn("这个衣服就是属于，其实会喜欢的人很多", repaired)
        self.assertIn("因为这件衣服其实，成本的确贵。", repaired)
        self.assertIn("怕缩水？亚麻呢", repaired)
        self.assertIn("你换冷水洗", repaired)
        self.assertIn("别冲动消费", repaired)
        self.assertIn("进口麻", repaired)
        self.assertIn("便宜的麻", repaired)
        rendered = "".join(item["text"] for item in semantic)
        for fragment in ("各种。高端", "那一刻。才能", "衣服其实。成本", "而单。单独"):
            self.assertNotIn(fragment, rendered)

    def test_word_timing_prevents_proportional_prefix_cut(self) -> None:
        text = "那这个世界上的好面料就真的没有人碰了"
        words = _timed_characters(text, step=0.1)
        clip = ("product", text, 0.0, len(text) * 0.1, 50.0, len(text) * 0.1)
        word_timings = [{"text": text, "start": 0.0, "end": len(text) * 0.1, "words": words}]
        cleaned_srt = f"1\n00:00:00,000 --> 00:00:05,000\n{text}\n"

        trimmed = ai_clipper._trim_filler_start(
            [clip],
            cleaned_srt,
            word_timings=word_timings,
        )

        self.assertEqual(trimmed[0][1], text)
        self.assertEqual(trimmed[0][2], 0.0)


if __name__ == "__main__":
    unittest.main()
