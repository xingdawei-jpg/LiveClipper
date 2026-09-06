from __future__ import annotations

import copy
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from director_wire_schema import compact_director_wire_payload, expand_director_wire_payload


class DirectorWireSchemaTests(unittest.TestCase):
    def test_round_trip_preserves_casting_facts(self) -> None:
        payload = {"strategies": [{"strategy_id": "S1", "chapter_packets": [{
            "chapter_id": "C1",
            "beats": [{"beat_function": "proof", "subtitle_ids": [101], "source_seconds": 2.5,
                       "product_relation": "main_product", "subject_product": "衬衫",
                       "subject_product_type": "top", "product_evidence_ids": [101],
                       "supports_main_product": ""}],
            "alternative_beats": [{"beat_function": "styling", "subtitle_ids": [102],
                                   "product_relation": "styling_support", "subject_product": "衬衫",
                                   "subject_product_type": "top", "product_evidence_ids": [102],
                                   "supports_main_product": "通勤搭配", "replaces_beat_id": "B1"}],
        }]}]}
        encoded = compact_director_wire_payload(payload)
        self.assertEqual(encoded["products"], [{"name": "衬衫", "type": "top"}])
        self.assertEqual(expand_director_wire_payload(encoded), payload)

    def test_invalid_product_reference_is_rejected(self) -> None:
        payload = compact_director_wire_payload({"beats": [{"beat_function": "proof", "subtitle_ids": [1]}]})
        payload = copy.deepcopy(payload)
        payload["packet"]["beats"][0]["product_ref"] = 9
        with self.assertRaises(ValueError):
            expand_director_wire_payload(payload)


if __name__ == "__main__":
    unittest.main()
