from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from selection_continuity import annotate_continuity_groups


class SelectionContinuityTests(unittest.TestCase):
    def test_annotations_preserve_order_and_group_adjacent_source_ranges(self) -> None:
        clips = [
            {"index": 2, "source": "C:/video-a.mp4", "start": 20.0, "end": 24.0},
            {"index": 0, "source": "C:/video-a.mp4", "start": 24.5, "end": 28.0},
            {"index": 3, "source": "C:/video-b.mp4", "start": 1.0, "end": 5.0},
        ]

        result = annotate_continuity_groups(clips)

        self.assertEqual([clip["index"] for clip in result], [2, 0, 3])
        self.assertEqual(result[0]["continuity_group"], result[1]["continuity_group"])
        self.assertNotEqual(result[1]["continuity_group"], result[2]["continuity_group"])
        self.assertEqual(result[1]["transition_reason"], "same_source_continuation")
        self.assertEqual(result[2]["transition_reason"], "source_change")
        self.assertEqual(result[0]["continuity_size"], 2)

    def test_overlap_and_reverse_time_are_explicit_breaks(self) -> None:
        clips = [
            {"source": "C:/video.mp4", "start": 10.0, "end": 15.0},
            {"source": "C:/video.mp4", "start": 14.0, "end": 18.0},
            {"source": "C:/video.mp4", "start": 2.0, "end": 5.0},
        ]

        result = annotate_continuity_groups(clips)

        self.assertEqual(result[1]["transition_reason"], "source_overlap")
        self.assertEqual(result[2]["transition_reason"], "time_reverse")
        self.assertTrue(result[1]["continuity_break_before"])
        self.assertTrue(result[2]["continuity_break_before"])


if __name__ == "__main__":
    unittest.main()

