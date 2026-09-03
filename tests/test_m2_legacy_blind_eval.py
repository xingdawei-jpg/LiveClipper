from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from run_m2_legacy_blind_eval import _comparison_variants, _legacy_variant  # noqa: E402


class M2LegacyBlindEvalTests(unittest.TestCase):
    def test_comparison_requires_a_valid_m2_plan(self) -> None:
        with self.assertRaisesRegex(ValueError, "M2 plan invalid"):
            _comparison_variants("case", {"plan_valid": False}, [("hook", "开头", 0, 2)])

    def test_comparison_builds_two_same_case_variants(self) -> None:
        legacy, m2 = _comparison_variants(
            "case",
            {
                "plan_valid": True,
                "selected_candidates": [
                    {"candidate_id": 7, "start": 0.0, "end": 2.0, "text": "M2开头"},
                ],
            },
            [("hook", "Legacy开头", 0.0, 2.0, 50, 2.0, "")],
        )
        self.assertEqual(legacy.variant_id, "legacy:case")
        self.assertEqual(m2.variant_id, "m2:case")
        self.assertEqual(legacy.clips[0].text, "Legacy开头")
        self.assertEqual(m2.clips[0].text, "M2开头")

    def test_legacy_variant_requires_playable_clip_ranges(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid clip"):
            _legacy_variant("case", [("hook", "", 0.0, 0.0)])


if __name__ == "__main__":
    unittest.main()
