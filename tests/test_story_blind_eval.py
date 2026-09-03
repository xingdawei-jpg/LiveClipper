from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from story_blind_eval import (  # noqa: E402
    EvaluationVariant,
    build_blind_packet,
    empty_rating_sheet,
    score_rating_sheet,
)


def _variant(name: str, text: str) -> EvaluationVariant:
    return EvaluationVariant.from_mapping({
        "variant_id": name,
        "clips": [
            {"candidate_id": 1, "start": 0.0, "end": 2.5, "text": text},
            {"candidate_id": 2, "start": 2.5, "end": 5.0, "text": "后一句立即给出证明。"},
        ],
    })


class StoryBlindEvaluationTests(unittest.TestCase):
    def test_public_packet_hides_generator_identity_and_is_seed_stable(self) -> None:
        cases = {"jianzhi": (_variant("manual", "人工片单开头"), _variant("m2", "M2片单开头"))}
        first_packet, first_key = build_blind_packet(cases, seed="2026-08-20")
        second_packet, second_key = build_blind_packet(cases, seed="2026-08-20")

        self.assertEqual(first_packet, second_packet)
        self.assertEqual(first_key, second_key)
        public_text = str(first_packet)
        self.assertNotIn("manual", public_text)
        self.assertNotIn("m2", public_text)
        self.assertEqual([item["label"] for item in first_packet["cases"][0]["variants"]], ["A", "B"])

    def test_packet_requires_a_real_comparison(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least two variants"):
            build_blind_packet({"jianzhi": (_variant("m2", "M2片单开头"),)}, seed="x")

    def test_completed_rating_sheet_reveals_scores_only_through_private_key(self) -> None:
        packet, key = build_blind_packet(
            {"jianzhi": (_variant("manual", "人工片单开头"), _variant("m2", "M2片单开头"))},
            seed="x",
        )
        sheet = empty_rating_sheet(packet)
        for case in sheet["cases"]:
            for variant in case["variants"]:
                variant["scores"] = {
                    "hook": 2,
                    "payoff": 2,
                    "progression": 1,
                    "focus": 1,
                    "purchase_reason": 2,
                    "naturalness": 1,
                }
                variant["max_issue"] = "transition_jump"
        scored = score_rating_sheet(sheet, key)

        self.assertEqual(len(scored["variants"]), 2)
        self.assertEqual({item["average_per_case"] for item in scored["variants"]}, {9.0})
        self.assertEqual(
            {item["diagnostics"]["transition_jump"] for item in scored["variants"]}, {1}
        )

    def test_unknown_diagnostic_is_rejected_without_affecting_the_score_contract(self) -> None:
        packet, key = build_blind_packet(
            {"jianzhi": (_variant("legacy", "旧版开头"), _variant("m2", "新版开头"))},
            seed="x",
        )
        sheet = empty_rating_sheet(packet)
        for case in sheet["cases"]:
            for variant in case["variants"]:
                variant["scores"] = {criterion["id"]: 1 for criterion in packet["criteria"]}
                variant["max_issue"] = "not_a_valid_issue"
        with self.assertRaisesRegex(ValueError, "unknown diagnostic"):
            score_rating_sheet(sheet, key)


if __name__ == "__main__":
    unittest.main()
