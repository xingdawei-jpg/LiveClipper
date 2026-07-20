from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr_quality = importlib.import_module("local_asr_quality")


class SenseVoiceFashionContextEdgeTests(unittest.TestCase):
    def test_punctuation_spaces_and_repeated_yarn_count_still_correct(self) -> None:
        text = "色织。    芝麻和那种常规染色麻。一定要看枝数，枝数高。"
        spoken = "色织芝麻和那种常规染色麻一定要看枝数枝数高"
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

        self.assertEqual(count, 3)
        self.assertEqual(
            corrected[0]["text"],
            "色织。    色织麻和那种常规染色麻。一定要看支数，支数高。",
        )
        self.assertEqual(
            "".join(word["text"] for word in corrected[0]["words"]),
            "色织色织麻和那种常规染色麻一定要看支数支数高",
        )


if __name__ == "__main__":
    unittest.main()
