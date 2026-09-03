# -*- coding: utf-8 -*-
from __future__ import annotations
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for item in (str(ROOT), str(ROOT / "app")):
    if item not in sys.path:
        sys.path.insert(0, item)

from run_opening_benchmark_v1 import DEFAULT_DATASET, load_dataset  # noqa: E402
from run_opening_gate_benchmark_v1 import build_gate_prompt, evaluate_gate, summarize  # noqa: E402


class OpeningGateBenchmarkV1Tests(unittest.TestCase):
    def test_confirmed_dataset_has_ten_gold_units(self) -> None:
        dataset = load_dataset(DEFAULT_DATASET)
        candidates = [candidate for case in dataset["cases"] for candidate in case["candidates"]]
        self.assertEqual(dataset["label_status"], "human_confirmed_v1")
        self.assertEqual(len(candidates), 10)
        self.assertTrue(all(candidate["gold"] for candidate in candidates))

    def test_gate_metrics_make_false_publish_visible(self) -> None:
        rejected = evaluate_gate(
            {"candidate_id": "bad", "gold": True, "decision": "reject", "failure_reason": ["context_dependent"]},
            {"publishable": True, "reason_codes": [], "confidence": 0.9},
        )
        kept = evaluate_gate(
            {"candidate_id": "good", "gold": True, "decision": "keep", "failure_reason": []},
            {"publishable": True, "reason_codes": [], "confidence": 0.9},
        )
        metrics = summarize([rejected, kept])
        self.assertEqual(metrics["reject_recall"], 0.0)
        self.assertEqual(metrics["false_publish_rate"], 1.0)
        self.assertEqual(metrics["false_publish_candidates"], ["bad"])

    def test_gate_prompt_never_requests_alternative_generation(self) -> None:
        prompt = build_gate_prompt({"context": "测试"}, {"text": "固定开场"})
        self.assertIn("只做发布判断", prompt)
        self.assertNotIn("推荐另一个", prompt)
