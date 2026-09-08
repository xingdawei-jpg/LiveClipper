import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from commercial_analyzer import _expand_cast_sentence_groups, AnalyzerError, _casting_execution_contract, director_delivery_duration_range

class DirectorFormatTests(unittest.TestCase):
    def payload(self, ids):
        return {"strategies": [{"strategy_id": "S1", "chapter_packets": [{"chapter_id": "C2", "beats": [{"subtitle_ids": ids, "product_relation": "main_product", "product_evidence_ids": [1], "source_seconds": 99}]}]}]}

    def test_group_expansion_preserves_sequence_metadata_and_real_duration(self):
        payload = self.payload([3, 1, 2])
        rows = [{"id": i, "start": i * 10, "end": i * 10 + i} for i in range(1, 4)]
        result = _expand_cast_sentence_groups(payload, rows)
        beats = payload["strategies"][0]["chapter_packets"][0]["beats"]
        self.assertEqual(result["expanded_groups"], 1)
        self.assertEqual([b["subtitle_ids"] for b in beats], [[3], [1], [2]])
        self.assertEqual([b["source_seconds"] for b in beats], [3, 1, 2])
        self.assertTrue(all(b["product_relation"] == "main_product" for b in beats))
        self.assertTrue(all(b["product_evidence_ids"] == [1] for b in beats))
        before = copy.deepcopy(payload)
        self.assertEqual(_expand_cast_sentence_groups(payload, rows)["expanded_groups"], 0)
        self.assertEqual(payload, before)

    def test_unknown_blocked_and_invalid_ids_are_not_silently_removed(self):
        rows = [{"id": i, "start": 0, "end": 2} for i in [1, 2]]
        for ids in ([1, 999], [1, 2], [1, True], [1, 1.2], [1, "x"], []):
            with self.subTest(ids=ids), self.assertRaises(AnalyzerError):
                _expand_cast_sentence_groups(self.payload(ids), rows, [1])

    def test_duplicate_ids_are_preserved_for_existing_content_audit(self):
        payload = self.payload([1, 1])
        _expand_cast_sentence_groups(payload, [{"id": 1, "start": 0, "end": 2}])
        self.assertEqual([b["subtitle_ids"] for b in payload["strategies"][0]["chapter_packets"][0]["beats"]], [[1], [1]])

    def test_cumulative_budgets_reach_source_target(self):
        story = {"strategies": [{"strategy_id": "S1", "chapter_packets": [{"chapter_id": f"C{i}", "source_budget_seconds": i * 5} for i in range(1, 4)]}]}
        contract = _casting_execution_contract(story, duration_range=director_delivery_duration_range(60, output_speed_factor=1.15))
        chapters = contract["strategies"][0]["chapters"]
        self.assertAlmostEqual(chapters[-1]["budget_end"], 69, places=1)
        self.assertEqual(chapters[1]["budget_end"], round(sum(c["budget"] for c in chapters[:2]), 3))
