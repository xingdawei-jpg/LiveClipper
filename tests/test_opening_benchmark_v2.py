# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for item in (str(ROOT), str(ROOT / "app")):
    if item not in sys.path:
        sys.path.insert(0, item)

from run_opening_benchmark_v2 import DEFAULT_DATASET, build_prompt, evaluate_case, load_dataset, summarize  # noqa: E402


class OpeningBenchmarkV2Tests(unittest.TestCase):
    def test_dataset_has_requested_task_mix_and_explicit_provisional_labels(self) -> None:
        dataset = load_dataset(DEFAULT_DATASET)
        cases = dataset["cases"]
        self.assertEqual(len(cases), 8)
        self.assertEqual(sum(len(item["candidates"]) for item in cases), 21)
        self.assertEqual(sum(item["task"] == "pair_choice" for item in cases), 3)
        self.assertEqual(sum(item["task"] == "rank_three" for item in cases), 2)
        self.assertEqual(sum(item["task"] == "reject_all" for item in cases), 3)
        self.assertIn("provisional", dataset["label_status"])

    def test_ranking_checks_complete_order_and_pairwise_accuracy(self) -> None:
        case = {
            "case_id": "rank", "task": "rank_three", "expected_verdict": "select",
            "expected_order": ["a", "b", "c"],
            "candidates": [{"id": "a", "label": "v1_gold"}, {"id": "b", "label": "v1_gold"}, {"id": "c", "label": "v1_gold"}],
        }
        result = evaluate_case(case, {"verdict": "select", "selected_candidate_id": "a", "ranking": ["a", "c", "b"]})
        self.assertTrue(result["ranking_valid"])
        self.assertTrue(result["top1_correct"])
        self.assertEqual(result["ranking_pair_accuracy"], 0.6667)

    def test_reject_metrics_do_not_confuse_selection_with_success(self) -> None:
        case = {
            "case_id": "reject", "task": "reject_all", "expected_verdict": "no_publishable_opening_found",
            "expected_order": [], "candidates": [{"id": "a", "label": "v1_gold"}, {"id": "b", "label": "v1_gold"}],
        }
        result = evaluate_case(case, {"verdict": "select", "selected_candidate_id": "a", "ranking": ["a", "b"]})
        self.assertTrue(result["selected_reject"])
        metrics = summarize([result])
        self.assertEqual(metrics["reject_recall"], 0.0)
        self.assertEqual(metrics["false_publish_rate"], 1.0)

    def test_prompt_is_ranking_first_and_all_bad_rejection_only(self) -> None:
        dataset = load_dataset(DEFAULT_DATASET)
        prompt = build_prompt(dataset["cases"][0])
        self.assertIn("二选一", prompt)
        self.assertIn("只有两个都不合格才拒绝", prompt)
        self.assertIn("ranking", prompt)
