from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from ai_cost_ledger import ai_cost_ledger_scope, extract_usage, generate_ai_cost_reports, record_ai_call


class AiCostLedgerTests(unittest.TestCase):
    def test_reasoning_is_a_subset_of_output_and_missing_is_not_zero(self) -> None:
        for reasoning in (None, 0, 24000, 40000):
            with self.subTest(reasoning=reasoning):
                usage = {"prompt_tokens": 100, "completion_tokens": 32000}
                if reasoning is not None:
                    usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
                result = extract_usage({"usage": usage})
                self.assertEqual(result["total_tokens"], 32100)
                self.assertEqual(result["reasoning_tokens"], reasoning)
                self.assertEqual(result["non_reasoning_output_tokens"],
                                 32000 - reasoning if reasoning is not None and reasoning <= 32000 else None)

    def test_truncation_diagnostics_preserve_usage_without_response_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.jsonl"
            record_ai_call(
                module="commercial_analyzer", stage="Director_beat_casting", model="deepseek-v4-flash",
                request_payload={"max_tokens": 32000, "reasoning_effort": "low"},
                response_payload={
                    "choices": [{"finish_reason": "length", "message": {
                        "content": "private incomplete JSON", "reasoning_content": "private reasoning",
                    }}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 32000,
                              "completion_tokens_details": {"reasoning_tokens": 24000}},
                },
                success=False, error_type="output_truncated", ledger_path=ledger,
                request_started_at="2026-09-07T03:59:59+00:00",
            )
            raw = ledger.read_text(encoding="utf-8")
            self.assertNotIn("private", raw)
            report, _ = generate_ai_cost_reports(ledger_path=ledger)
            self.assertEqual(report["tokens"]["known_total_tokens"], 32100)
            self.assertEqual(report["tokens"]["known_reasoning_tokens"], 24000)
            self.assertEqual(report["tokens"]["known_non_reasoning_output_tokens"], 8000)
            self.assertEqual(report["records"]["failed_requests"], 1)
            self.assertEqual(report["records"]["reasoning_usage_missing_requests"], 0)
            diagnostic = report["output_diagnostics"][0]
            self.assertEqual(diagnostic["finish_reason"], "length")
            self.assertEqual(diagnostic["output_limit_tokens"], 32000)
            self.assertEqual(diagnostic["request_started_at"], "2026-09-07T03:59:59+00:00")
            self.assertEqual(diagnostic["model"], "deepseek-v4-flash")
            self.assertEqual(diagnostic["input_tokens"], 100)
            self.assertEqual(diagnostic["reasoning_characters"], len("private reasoning"))

    def test_extracts_openai_usage_and_nested_cached_tokens(self) -> None:
        usage = extract_usage({
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "prompt_tokens_details": {"cached_tokens": 40},
            }
        })
        self.assertEqual(usage["input_tokens"], 120)
        self.assertEqual(usage["output_tokens"], 30)
        self.assertEqual(usage["cached_input_tokens"], 40)
        self.assertEqual(usage["total_tokens"], 150)

    def test_does_not_invent_usage_when_provider_omits_it(self) -> None:
        usage = extract_usage({"choices": [{"message": {"content": "ok"}}]})
        self.assertFalse(usage["usage_available"])
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["total_tokens"])

    def test_reports_stages_retries_and_exact_input_reuse_without_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ledger = root / "ledger.jsonl"
            payload = {
                "messages": [
                    {"role": "system", "content": "system instruction"},
                    {"role": "user", "content": "private source text"},
                ]
            }
            response = {"usage": {"prompt_tokens": 100, "completion_tokens": 25}}
            record_ai_call(
                module="commercial_analyzer",
                stage="M1_story_discovery",
                model="test-model",
                request_payload=payload,
                response_payload=response,
                success=True,
                task_id="task-1",
                ledger_path=ledger,
            )
            record_ai_call(
                module="commercial_analyzer",
                stage="M1_story_discovery",
                model="test-model",
                request_payload=payload,
                response_payload=response,
                success=True,
                task_id="task-1",
                retry=True,
                ledger_path=ledger,
            )
            report, cache_report = generate_ai_cost_reports(ledger_path=ledger, output_dir=root / "report")
            self.assertEqual(report["records"]["total_requests"], 2)
            self.assertEqual(report["records"]["retry_count"], 1)
            self.assertEqual(report["tokens"]["known_total_tokens"], 250)
            self.assertEqual(report["by_stage"][0]["stage"], "M1_story_discovery")
            self.assertEqual(report["top_duplicate_chains"][0]["count"], 2)
            self.assertEqual(cache_report["exact_input_reuse_candidates"][0]["count"], 2)
            raw = ledger.read_text(encoding="utf-8")
            self.assertNotIn("private source text", raw)
            self.assertTrue((root / "report" / "ai_cost_report.json").exists())
            self.assertTrue((root / "report" / "cache_candidate_report.json").exists())
            self.assertEqual(len([json.loads(line) for line in raw.splitlines()]), 2)

    def test_scope_supplies_task_and_session_and_reports_session_slice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.jsonl"
            with ai_cost_ledger_scope(task_id="case-a", session_id="run-a"):
                record_ai_call(
                    module="story_planner", stage="M2_story_planner", model="test-model",
                    request_payload={"messages": []}, response_payload={"usage": {"total_tokens": 9}},
                    success=True, ledger_path=ledger,
                )
            record_ai_call(
                module="story_planner", stage="M2_story_planner", model="test-model",
                request_payload={"messages": []}, response_payload={"usage": {"total_tokens": 7}},
                success=True, ledger_path=ledger,
            )
            report, _ = generate_ai_cost_reports(ledger_path=ledger, session_id="run-a", task_id="case-a")
            self.assertEqual(report["records"]["total_requests"], 1)
            self.assertEqual(report["tokens"]["known_total_tokens"], 9)
            record = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual((record["task_id"], record["session_id"]), ("case-a", "run-a"))

    def test_retry_scope_marks_every_call_in_a_retried_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ledger = Path(temp_dir) / "ledger.jsonl"
            with ai_cost_ledger_scope(task_id="task-a", session_id="attempt-2", retry=True):
                record_ai_call(
                    module="commercial_analyzer", stage="Director_story_contract", model="test-model",
                    request_payload={"messages": []}, response_payload={"usage": {"total_tokens": 5}},
                    success=True, ledger_path=ledger,
                )
            report, _ = generate_ai_cost_reports(ledger_path=ledger, task_id="task-a")
            self.assertEqual(report["records"]["retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
