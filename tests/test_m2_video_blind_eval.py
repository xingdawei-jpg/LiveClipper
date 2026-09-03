from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from run_m2_video_blind_eval import legacy_ranges  # noqa: E402
from run_m3_selector_render import write_output_timeline_srt  # noqa: E402


class M2VideoBlindEvalTests(unittest.TestCase):
    def test_legacy_ranges_preserve_recorded_order_and_boundaries(self) -> None:
        ranges = legacy_ranges((
            ["hook", "第一个片段。", 3.0, 5.0],
            ["product", "第二个片段。", 9.0, 11.5],
        ))

        self.assertEqual([item["text"] for item in ranges], ["第一个片段。", "第二个片段。"])
        self.assertEqual([(item["start"], item["end"]) for item in ranges], [(3.0, 5.0), (9.0, 11.5)])

    def test_legacy_ranges_refuse_incomplete_recording(self) -> None:
        with self.assertRaisesRegex(ValueError, "内容不完整"):
            legacy_ranges((["hook", "", 0.0, 2.0],))

    def test_output_subtitles_are_rebased_to_the_concatenated_video_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            subtitle = Path(temp_dir) / "output.srt"
            write_output_timeline_srt([
                {"start": 117.0, "end": 119.5, "text": "第一段。"},
                {"start": 9.0, "end": 11.0, "text": "第二段。"},
            ], subtitle)
            content = subtitle.read_text(encoding="utf-8")

        self.assertIn("00:00:00,000 --> 00:00:02,500", content)
        self.assertIn("00:00:02,500 --> 00:00:04,500", content)


if __name__ == "__main__":
    unittest.main()
