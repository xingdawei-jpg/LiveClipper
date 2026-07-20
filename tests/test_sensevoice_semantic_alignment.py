from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr = importlib.import_module("local_asr")
volcengine_asr = importlib.import_module("volcengine_asr")


def _timed_words(tokens: list[str], step: float = 0.5) -> list[dict[str, object]]:
    return [
        {"text": token, "start": index * step, "end": (index + 1) * step}
        for index, token in enumerate(tokens)
    ]


class SenseVoiceSemanticAlignmentTests(unittest.TestCase):
    def test_spurious_dot_between_chinese_and_digit_does_not_drop_sentence_punctuation(self) -> None:
        text = "\u8eab\u9ad8\u4e007\u7c73\u3002\u5efa\u8bae\u6309\u8eab\u9ad8\u9009\u3002"
        tokens = list("\u8eab\u9ad8\u4e00") + [".", "7"] + list("\u7c73\u5efa\u8bae\u6309\u8eab\u9ad8\u9009")
        source = [{"text": text, "start": 0.0, "end": 6.5, "words": _timed_words(tokens)}]

        semantic = volcengine_asr.build_semantic_segments(source)

        self.assertEqual(len(semantic), 2)
        self.assertEqual(semantic[0]["text"], "\u8eab\u9ad8\u4e007\u7c73\u3002")
        self.assertNotIn(".", semantic[0]["text"])

    def test_real_decimal_point_is_preserved(self) -> None:
        text = "\u8eab\u9ad81.7\u7c73\u3002\u5efa\u8bae\u6309\u8eab\u9ad8\u9009\u3002"
        tokens = list("\u8eab\u9ad8") + ["1", ".", "7"] + list("\u7c73\u5efa\u8bae\u6309\u8eab\u9ad8\u9009")
        source = [{"text": text, "start": 0.0, "end": 6.5, "words": _timed_words(tokens)}]

        semantic = volcengine_asr.build_semantic_segments(source)

        self.assertEqual(len(semantic), 2)
        self.assertIn("1.7", semantic[0]["text"])

    def test_new_sensevoice_sidecars_filter_only_spurious_dots(self) -> None:
        result = [{
            "text": "\u8eab\u9ad8\u4e007\u7c73\uff0c\u771f\u5b9e\u8eab\u9ad81.7\u7c73\u3002",
            "words": ["\u8eab", "\u9ad8", "\u4e00", ".", "7", "\u7c73", "\uff0c", "\u771f", "\u5b9e", "\u8eab", "\u9ad8", "1", ".", "7", "\u7c73", "\u3002"],
            "timestamp": [[index * 100, (index + 1) * 100] for index in range(16)],
        }]

        segments = local_asr._sensevoice_segments(result)
        words = "".join(word["text"] for word in segments[0]["words"])

        self.assertIn("\u4e007\u7c73", words)
        self.assertNotIn("\u4e00.7", words)
        self.assertIn("1.7", words)


if __name__ == "__main__":
    unittest.main()
