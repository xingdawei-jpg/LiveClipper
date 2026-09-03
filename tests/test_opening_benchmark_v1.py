# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for item in (str(ROOT), str(ROOT / "app")):
    if item not in sys.path:
        sys.path.insert(0, item)

from run_opening_benchmark_v1 import DEFAULT_DATASET, build_prompt, evaluate_case, load_dataset, summarize  # noqa: E402


class OpeningBenchmarkV1Tests(unittest.TestCase):
    def test_seed_dataset_has_rejection_and_top1_pressure_cases(self) -> None:
        dataset = load_dataset(DEFAULT_DATASET)
        cases = dataset["cases"]
        self.assertEqual(len(cases), 5)
        self.assertEqual(sum(item["expected_verdict"] == "no_publishable_opening_found" for item in cases), 3)
        self.assertEqual(sum(item["expected_verdict"] == "select" for item in cases), 2)
        self.assertEqual(sum(len(item["candidates"]) for item in cases), 10)

    def test_evaluation_separates_top1_and_no_publishable(self) -> None:
        reject_case = {
            "case_id": "reject", "expected_verdict": "no_publishable_opening_found",
            "candidates": [{"candidate_id": "a", "decision": "reject"}],
        }
        select_case = {
            "case_id": "select", "expected_verdict": "select", "expected_candidate_id": "a",
            "candidates": [{"candidate_id": "a", "decision": "keep"}, {"candidate_id": "b", "decision": "reject"}],
        }
        rejected = evaluate_case(reject_case, {"verdict": "no_publishable_opening_found", "candidate_decisions": [{"candidate_id": "a", "decision": "reject"}]})
        selected = evaluate_case(select_case, {"verdict": "select", "selected_candidate_id": "a", "candidate_decisions": [{"candidate_id": "a", "decision": "keep"}, {"candidate_id": "b", "decision": "reject"}]})
        metrics = summarize([rejected, selected])
        self.assertTrue(rejected["no_publishable_correct"])
        self.assertTrue(selected["top1_correct"])
        self.assertEqual(metrics["unit_label_accuracy"], 1.0)

    def test_selected_reject_rate_uses_editorial_labels_not_model_self_report(self) -> None:
        case = {
            "case_id": "wrong-publish", "expected_verdict": "no_publishable_opening_found",
            "candidates": [{"candidate_id": "a", "decision": "reject"}],
        }
        result = evaluate_case(case, {
            "verdict": "select", "selected_candidate_id": "a",
            "candidate_decisions": [{"candidate_id": "a", "decision": "keep"}],
        })
        metrics = summarize([result])
        self.assertEqual(metrics["selected_reject_count"], 1)
        self.assertEqual(metrics["selected_reject_rate"], 1.0)

    def test_prompt_requires_selection_or_honest_rejection_not_rewrite(self) -> None:
        dataset = load_dataset(DEFAULT_DATASET)
        prompt = build_prompt(dataset["cases"][0])
        self.assertIn("绝不生成、改写、拼接", prompt)
        self.assertIn("no_publishable_opening_found", prompt)
