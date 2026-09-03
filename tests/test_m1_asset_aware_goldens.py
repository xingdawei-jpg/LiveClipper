# -*- coding: utf-8 -*-
"""Source-only boundary tests for the Asset-Aware M1 golden runner."""

from __future__ import annotations

import unittest

from run_m1_asset_aware_goldens import _hard_safe_subtitles


class HardSafeM1InputTests(unittest.TestCase):
    def test_m1_receives_only_ledger_backed_raw_facts(self) -> None:
        rows = [
            {"candidate_id": 9, "start": 1.0, "end": 2.0, "text": "版型更安全"},
            {"candidate_id": 22, "start": 3.0, "end": 4.5, "text": "搭牛仔裤也好看"},
        ]
        self.assertEqual(_hard_safe_subtitles(rows), [
            {"id": 9, "start": 1.0, "end": 2.0, "text": "版型更安全"},
            {"id": 22, "start": 3.0, "end": 4.5, "text": "搭牛仔裤也好看"},
        ])


if __name__ == "__main__":
    unittest.main()
