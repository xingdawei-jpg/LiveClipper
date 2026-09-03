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


class ReviewedNarrativeOpportunityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = [
            (0.0, 3.0, "哇，这件西装真的好帅，像明星机场穿搭。"),
            (3.0, 6.0, "你看这个肩往外走，下面又是松的，整个型特别利落。"),
            (6.0, 9.0, "通勤搭西裤，周末搭牛仔裤都能穿。"),
        ]
        self.thread = {
            1: {
                "topic": "风格定位",
                "allowed_followup_ids": {2},
                "seed_followup_ids": {2},
            }
        }
        self.opportunity = {
            "narrative_id": "ARC-01",
            "hook_id": 1,
            "followup_id": 2,
            "topic": "风格定位",
            "hook_promise": "真的好帅，像明星机场穿搭",
            "proof_relation": "identity_projection",
            "opening_support_ids": [3],
            "next_topics": ["场景搭配"],
        }

    def test_contract_keeps_only_real_hook_proof_and_support_ids(self) -> None:
        contract = ai_clipper._normalize_review_narrative_opportunities(
            [self.opportunity],
            srt_entry_map={index: entry for index, entry in enumerate(self.entries, 1)},
            hook_threads=self.thread,
            allowed_candidate_ids={1, 2, 3},
        )

        self.assertEqual(len(contract), 1)
        self.assertEqual(contract[0]["opening_support_ids"], [3])
        self.assertIn("ARC-01", ai_clipper._review_narrative_prompt(contract))
        self.assertIn("不必把整条视频锁死", ai_clipper._review_narrative_prompt(contract))

    def test_invalid_or_out_of_thread_proof_never_enters_contract(self) -> None:
        invalid = {**self.opportunity, "followup_id": 3}
        contract = ai_clipper._normalize_review_narrative_opportunities(
            [invalid],
            srt_entry_map={index: entry for index, entry in enumerate(self.entries, 1)},
            hook_threads=self.thread,
            allowed_candidate_ids={1, 2, 3},
        )

        self.assertEqual(contract, [])

    def test_final_status_accepts_only_the_matching_opening_pair(self) -> None:
        selected = [
            ("hook", self.entries[0][2], 0.0, 3.0, 50, 3.0, "风格定位"),
            ("product", self.entries[1][2], 3.0, 6.0, 50, 3.0, "风格定位"),
            ("product", self.entries[2][2], 6.0, 9.0, 50, 3.0, "场景搭配"),
        ]
        status = ai_clipper._review_narrative_status(
            selected,
            [self.opportunity],
            self.entries,
            {"selected_narrative_id": "ARC-01"},
        )

        self.assertTrue(status["checked"])
        self.assertTrue(status["opening_matches"])
        self.assertEqual(status["selected_id"], "ARC-01")

    def test_wrong_declared_arc_is_observed_not_locally_rearranged(self) -> None:
        selected = [
            ("product", self.entries[2][2], 6.0, 9.0, 50, 3.0, "场景搭配"),
            ("product", self.entries[1][2], 3.0, 6.0, 50, 3.0, "风格定位"),
        ]
        status = ai_clipper._review_narrative_status(
            selected,
            [self.opportunity],
            self.entries,
            {"selected_narrative_id": "ARC-01"},
        )

        self.assertTrue(status["checked"])
        self.assertFalse(status["opening_matches"])
        self.assertEqual(status["reason"], "declared_arc_does_not_match_opening")


if __name__ == "__main__":
    unittest.main()
