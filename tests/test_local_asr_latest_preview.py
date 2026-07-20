from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr_quality = importlib.import_module("local_asr_quality")


class LocalAsrLatestPreviewTests(unittest.TestCase):
    def test_latest_preview_errors_are_corrected_without_changing_range(self) -> None:
        text = (
            "这个麻着个料子有肌励感，高枝的亚麻树质非常高，"
            "对撞衫这个事情比较ca的，森侣禅意，整个的版然后很非常适合。"
        )
        spoken = text.replace("，", "").replace("。", "")
        words = [
            {"text": char, "start": index * 0.1, "end": (index + 1) * 0.1}
            for index, char in enumerate(spoken)
        ]

        corrected, count = local_asr_quality.apply_domain_corrections([{
            "text": text,
            "start": 0.0,
            "end": len(spoken) * 0.1,
            "words": words,
        }])

        self.assertGreaterEqual(count, 7)
        self.assertEqual(
            corrected[0]["text"],
            "这个麻质料子有肌理感，高支的亚麻支数非常高，"
            "对撞衫这个事情比较care的，僧侣禅意，整个版型然后非常适合。",
        )
        self.assertAlmostEqual(corrected[0]["words"][0]["start"], 0.0)
        self.assertAlmostEqual(corrected[0]["words"][-1]["end"], len(spoken) * 0.1)

    def test_punctuation_repair_moves_audience_word_to_following_sentence(self) -> None:
        text = "我觉得会对这件衣服一眼心动女生。大概率很多自己做老板的。"
        spoken = text.replace("。", "")
        words = [
            {"text": char, "start": index * 0.1, "end": (index + 1) * 0.1}
            for index, char in enumerate(spoken)
        ]

        corrected, _ = local_asr_quality.apply_domain_corrections([{
            "text": text,
            "start": 0.0,
            "end": len(spoken) * 0.1,
            "words": words,
        }])

        self.assertEqual(
            corrected[0]["text"],
            "我觉得会对这件衣服一眼心动。女生大概率很多自己做老板的。",
        )
        self.assertEqual("".join(item["text"] for item in corrected[0]["words"]), spoken)


if __name__ == "__main__":
    unittest.main()
