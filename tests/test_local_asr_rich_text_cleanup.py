from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr = importlib.import_module("local_asr")
volcengine_asr = importlib.import_module("volcengine_asr")


class LocalAsrRichTextCleanupTests(unittest.TestCase):
    def test_spaced_rich_tags_and_duplicate_punctuation_are_removed(self) -> None:
        raw = (
            "< | zh | > < | NEUTRAL | >这个板型很好，。"
            "< | withi tn | >身高是1. 7米啊？，价格629."
        )

        cleaned = local_asr._clean_text(raw)

        self.assertEqual(cleaned, "这个板型很好。身高是1.7米啊？价格629。")

    def test_cleaned_sentence_text_aligns_with_spoken_tokens(self) -> None:
        raw = "< | zh | >这个版型很好，。身高是1. 7米，价格534. 7。"
        text = local_asr._clean_text(raw)
        words = list("这个版型很好身高是1") + ["."] + list("7米价格534") + ["."] + list("7")
        expected_plain = "".join(volcengine_asr._semantic_plain_text(word) for word in words)

        self.assertEqual(
            volcengine_asr._semantic_plain_text(text),
            expected_plain,
        )


if __name__ == "__main__":
    unittest.main()
