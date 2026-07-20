from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr_quality = importlib.import_module("local_asr_quality")


class LocalAsrObservedCorrectionTests(unittest.TestCase):
    def test_observed_fashion_live_errors_are_corrected_with_source_ranges(self) -> None:
        text = "我们家app不小建议卡马王小，因为西装交底门经理更像森绿，而且是高知数亚麻"
        spoken = text.replace("，", "")
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

        self.assertEqual(count, 5)
        self.assertEqual(
            corrected[0]["text"],
            "我们家衣服不小建议尺码往小，因为西装交叠门襟领更像僧侣，而且是高支数亚麻",
        )
        self.assertEqual(
            "".join(word["text"] for word in corrected[0]["words"]),
            "我们家衣服不小建议尺码往小因为西装交叠门襟领更像僧侣而且是高支数亚麻",
        )
        app_start = spoken.index("app") * 0.1
        app_end = app_start + 0.3
        clothing_token = next(word for word in corrected[0]["words"] if word["text"] == "衣服")
        self.assertAlmostEqual(clothing_token["start"], app_start)
        self.assertAlmostEqual(clothing_token["end"], app_end)


if __name__ == "__main__":
    unittest.main()
