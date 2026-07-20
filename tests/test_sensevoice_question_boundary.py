from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

volcengine_asr = importlib.import_module("volcengine_asr")


class SenseVoiceQuestionBoundaryTests(unittest.TestCase):
    def test_confirmation_trim_never_breaks_dui_bu_dui_question(self) -> None:
        text = "嗯，对不对？然后门襟是交错单粒扣。"
        tokens = list("嗯对不对然后门襟是交错单粒扣")
        words = [
            {"text": token, "start": index * 0.1, "end": (index + 1) * 0.1}
            for index, token in enumerate(tokens)
        ]

        semantic = volcengine_asr.build_semantic_segments([{
            "text": text,
            "start": 0.0,
            "end": len(tokens) * 0.1,
            "words": words,
        }])

        self.assertTrue(semantic)
        self.assertTrue(semantic[0]["text"].startswith("对不对"))
        self.assertFalse(semantic[0]["text"].startswith("不对"))


if __name__ == "__main__":
    unittest.main()
