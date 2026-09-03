from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from run_m2_duration_depth_blind_eval import (  # noqa: E402
    CASE_ID,
    _duration_depth_sheet,
    build_duration_depth_packet,
)


def _manifest(*, duration: float, source: str = "source.srt") -> dict:
    return {
        "source_srt": source,
        "selector_result": {
            "status": "ok",
            "ranges": [{
                "parent_candidate_id": 12,
                "start": 1.0,
                "end": 1.0 + duration,
                "text": "完整商业表达。",
            }],
        },
    }


class DurationDepthBlindEvalTests(unittest.TestCase):
    def test_packet_is_anonymous_and_contains_both_depths(self) -> None:
        public, private = build_duration_depth_packet(
            _manifest(duration=20.0), _manifest(duration=40.0), seed="stable",
        )

        self.assertEqual(public["cases"][0]["case_id"], CASE_ID)
        self.assertNotIn("shorter_depth", str(public))
        self.assertNotIn("longer_depth", str(public))
        self.assertIn("shorter_depth", str(private))
        sheet = _duration_depth_sheet(public)
        self.assertEqual(len(sheet["variants"]), 2)
        self.assertIsNone(sheet["longer_version_adds_new_purchase_value"])

    def test_packet_refuses_cross_source_or_reversed_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "同一份源字幕"):
            build_duration_depth_packet(
                _manifest(duration=20.0, source="one.srt"),
                _manifest(duration=40.0, source="two.srt"),
                seed="x",
            )
        with self.assertRaisesRegex(ValueError, "更短"):
            build_duration_depth_packet(
                _manifest(duration=40.0), _manifest(duration=20.0), seed="x",
            )


if __name__ == "__main__":
    unittest.main()
