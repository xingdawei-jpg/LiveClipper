from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr = importlib.import_module("local_asr")


class LocalAsrPunctuationTokenTests(unittest.TestCase):
    def test_punctuation_tokens_stay_in_text_but_not_word_timing_sidecar(self) -> None:
        result = [{
            "text": "这个版型很好。",
            "words": ["这", "个", "版", "型", "很", "好", "。"],
            "timestamp": [[index * 100, (index + 1) * 100] for index in range(7)],
        }]

        segments = local_asr._sensevoice_segments(result)

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "这个版型很好。")
        self.assertEqual("".join(word["text"] for word in segments[0]["words"]), "这个版型很好")
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["end"], 0.6)


if __name__ == "__main__":
    unittest.main()
