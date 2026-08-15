from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

ai_clipper = importlib.import_module("ai_clipper")


class ManualPreferenceDirectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = [
            (0.0, 3.0, "这个面料夏天穿起来很清爽"),
            (3.0, 6.0, "垂感很好而且摸起来不扎"),
            (6.0, 9.0, "上身以后整个人比例会更好"),
        ]
        self.review = SimpleNamespace(cards=(
            SimpleNamespace(candidate_id=1, topic="面料质感", tier="main"),
            SimpleNamespace(candidate_id=2, topic="面料质感", tier="reserve"),
            SimpleNamespace(candidate_id=3, topic="版型显瘦", tier="main"),
        ))

    def test_manual_direction_freezes_real_body_evidence_without_forcing_hook(self) -> None:
        contract = ai_clipper._manual_focus_mainline_contract(
            "面料质感",
            self.entries,
            allowed_candidate_ids={1, 2, 3},
            review_bundle=self.review,
            target_duration=20,
        )

        self.assertTrue(contract["enabled"])
        self.assertEqual(contract["topic"], "面料质感")
        self.assertEqual(contract["main_candidate_ids"], [1])
        self.assertEqual(contract["reserve_candidate_ids"], [2])
        self.assertEqual(contract["required_product_count"], 2)
        prompt = ai_clipper._manual_focus_mainline_prompt(contract)
        self.assertIn("Hook仍先选最强且已兑现的开场", prompt)
        self.assertIn("#1", prompt)
        self.assertIn("#2", prompt)

    def test_final_plan_that_ignores_available_manual_direction_is_detected(self) -> None:
        contract = ai_clipper._manual_focus_mainline_contract(
            "面料质感",
            self.entries,
            allowed_candidate_ids={1, 2, 3},
            review_bundle=self.review,
            target_duration=20,
        )
        ignored = [
            ("hook", "这个上身真的很显比例", 0.0, 3.0, 50, 3.0, "上身效果"),
            ("product", self.entries[2][2], 6.0, 9.0, 50, 3.0, "版型显瘦"),
        ]
        status = ai_clipper._manual_focus_mainline_status(ignored, contract, self.entries)

        self.assertTrue(status["checked"])
        self.assertFalse(status["met"])
        self.assertEqual(status["selected_product_count"], 0)

    def test_final_plan_counts_manual_direction_in_body_not_hook(self) -> None:
        contract = ai_clipper._manual_focus_mainline_contract(
            "面料质感",
            self.entries,
            allowed_candidate_ids={1, 2, 3},
            review_bundle=self.review,
            target_duration=20,
        )
        selected = [
            ("hook", "这个上身真的很显比例", 6.0, 9.0, 50, 3.0, "上身效果"),
            ("product", self.entries[0][2], 0.0, 3.0, 50, 3.0, "面料质感"),
            ("product", self.entries[1][2], 3.0, 6.0, 50, 3.0, "面料质感"),
        ]
        status = ai_clipper._manual_focus_mainline_status(selected, contract, self.entries)

        self.assertTrue(status["met"])
        self.assertEqual(status["selected_product_count"], 2)
        self.assertTrue(status["front_window_met"])

    def test_manual_direction_without_safe_evidence_does_not_make_up_a_contract(self) -> None:
        contract = ai_clipper._manual_focus_mainline_contract(
            "场景搭配",
            self.entries,
            allowed_candidate_ids={1, 2, 3},
            review_bundle=self.review,
            target_duration=20,
        )

        self.assertFalse(contract["enabled"])
        self.assertEqual(contract["reason"], "no_safe_focus_evidence")


if __name__ == "__main__":
    unittest.main()
