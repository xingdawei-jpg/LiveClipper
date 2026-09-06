import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import ai_clipper
from content_policy import default_content_policy
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source


class DirectorCastingInventoryTests(unittest.TestCase):
    def test_context_beats_keep_exact_boundaries_and_still_enforce_content_policy(self):
        entries = [
            (0.0, 2.34, "它是属于看着是属于瘦，但它有料，"),
            (3.0, 5.82, "就是你越胖，你穿这种版，你反而能把你的肉藏好。"),
            (6.0, 8.0, "肩线往里收，视觉上看起来更利落。"),
            (9.0, 11.0, "今天只要99元，赶紧下单。"),
            (12.0, 14.0, "这个禁用描述不能进入成片。"),
        ]
        with patch.object(ai_clipper, "load_keywords", return_value={"forbidden_phrases": ["禁用描述"]}):
            normal = ai_clipper._director_safe_candidate_inventory(entries)
            casting = ai_clipper._director_safe_candidate_inventory(
                entries, content_policy=default_content_policy(), allow_context_dependent_beats=True,
            )
        self.assertEqual([row["srt_index"] for row in normal], [3])
        self.assertEqual([row["srt_index"] for row in casting], [1, 2, 3])
        self.assertEqual([(r["start"], r["end"], r["text"]) for r in casting], entries[:3])

    def test_production_ledger_keeps_context_beats_only_for_casting(self):
        srt = (
            "1\n00:00:00,000 --> 00:00:02,340\n它是属于看着是属于瘦，但它有料，\n\n"
            "2\n00:00:03,000 --> 00:00:05,000\n肩线往里收，视觉上看起来更利落。\n\n"
            "3\n00:00:06,000 --> 00:00:08,000\n今天只要99元，赶紧下单。\n"
        )
        with tempfile.TemporaryDirectory() as directory, patch.object(
            ai_clipper, "load_keywords", return_value={"forbidden_phrases": []}
        ):
            path = Path(directory) / "source.srt"
            path.write_text(srt, encoding="utf-8")
            legacy = _asset_ledger_for_source("test", source_srt=str(path))
            casting = _asset_ledger_for_source("test", source_srt=str(path), allow_context_dependent_beats=True)
        self.assertEqual([r["candidate_id"] for r in legacy["assets"]], [2])
        self.assertEqual([r["candidate_id"] for r in casting["assets"]], [1, 2])
        self.assertEqual(casting["assets"][0]["end"], 2.34)


if __name__ == "__main__":
    unittest.main()
