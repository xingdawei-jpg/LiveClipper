from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr = importlib.import_module("local_asr")
review = importlib.import_module("local_asr_review")


def _segment(text: str, start: float = 10.0, end: float = 13.0) -> dict:
    return {
        "text": text,
        "start": start,
        "end": end,
        "words": [
            {"text": "这", "start": start, "end": start + 0.3},
            {"text": "个", "start": start + 0.3, "end": start + 0.6},
            {"text": "料", "start": start + 0.6, "end": start + 0.9},
        ],
    }


class LocalAsrReviewTests(unittest.TestCase):
    def test_known_transcript_error_is_flagged_without_editing_local_text(self) -> None:
        source = [_segment("这个料子非常舒服，但料子非。")]

        reviewed, report = review.review_segments(source, retry_enabled=False)

        self.assertEqual(reviewed, source)
        self.assertEqual(report["initial"]["flagged_count"], 1)
        self.assertEqual(report["retry"]["requested_count"], 0)
        self.assertEqual(report["final"]["flagged_count"], 1)

    def test_unfinished_tail_is_detected_without_changing_timing(self) -> None:
        source = [_segment("这个版型夏天穿也很好看而且", end=14.0)]

        findings = review.assess_segments(source)

        self.assertEqual(findings[0]["reasons"], ["unfinished_tail"])
        self.assertEqual(findings[0]["start"], 10.0)
        self.assertEqual(findings[0]["end"], 14.0)

    def test_valid_word_timed_retry_replaces_only_the_flagged_window(self) -> None:
        source = [
            _segment("这个料子非。", start=10.0, end=13.0),
            _segment("版型穿起来很显瘦。", start=14.0, end=17.0),
        ]
        calls = []

        def retry(finding: dict) -> list[dict]:
            calls.append(finding)
            return [{
                "text": "这个料子非常轻薄。",
                "start": 10.1,
                "end": 12.8,
                "words": [
                    {"text": "这", "start": 10.1, "end": 10.3},
                    {"text": "个", "start": 10.3, "end": 10.5},
                    {"text": "料子", "start": 10.5, "end": 11.1},
                    {"text": "非常", "start": 11.1, "end": 11.7},
                    {"text": "轻薄", "start": 11.7, "end": 12.8},
                ],
            }]

        reviewed, report = review.review_segments(source, retry_enabled=True, retry_callback=retry)

        self.assertEqual(len(calls), 1)
        self.assertEqual(reviewed[0]["text"], "这个料子非常轻薄。")
        self.assertEqual(reviewed[0]["asr_source"], "cloud_retry")
        self.assertEqual(reviewed[1], source[1])
        self.assertEqual(report["retry"]["replaced_count"], 1)
        self.assertEqual(report["final"]["flagged_count"], 0)

    def test_retry_without_word_timing_keeps_original_segment(self) -> None:
        source = [_segment("这个料子非。")]

        reviewed, report = review.review_segments(
            source,
            retry_enabled=True,
            retry_callback=lambda _finding: [{"text": "这个料子非常轻薄。", "start": 10.0, "end": 12.0}],
        )

        self.assertEqual(reviewed, source)
        self.assertEqual(report["retry"]["replaced_count"], 0)
        self.assertEqual(report["retry"]["outcomes"][0]["outcome"], "kept_local")

    def test_cloud_retry_requires_dedicated_opt_in_or_explicit_on_mode(self) -> None:
        configured = {
            "volc_tos_ak": "ak",
            "volc_tos_sk": "sk",
            "volc_api_key": "key",
            "asr_enabled": False,
            "asr_preset": "火山引擎",
            "local_asr_quality_retry_enabled": False,
        }

        self.assertEqual(review.cloud_retry_eligibility(configured, "auto"), (False, "quality_retry_disabled"))
        self.assertEqual(review.cloud_retry_eligibility(configured, "on"), (True, "enabled"))
        configured["local_asr_quality_retry_enabled"] = True
        self.assertEqual(review.cloud_retry_eligibility(configured, "auto"), (True, "enabled"))

    def test_dedicated_retry_opt_in_is_wired_through_settings_contract(self) -> None:
        server_source = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        page_source = (ROOT / "web_client" / "frontend" / "index.html").read_text(encoding="utf-8")
        app_source = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('"local_asr_quality_retry_enabled": False', server_source)
        self.assertIn("local_asr_quality_retry_enabled: bool = False", server_source)
        self.assertIn('id="s-local-asr-quality-retry-enabled"', page_source)
        self.assertIn('local_asr_quality_retry_enabled: "s-local-asr-quality-retry-enabled"', app_source)

    def test_quality_report_writes_atomically_without_transcript_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            srt = Path(temp_dir) / "clip.srt"
            report = {"schema": review.QUALITY_SCHEMA, "final": {"flagged_count": 1}}

            target = review.write_quality_report(srt, report)

            self.assertEqual(target, Path(temp_dir) / "clip.asr_quality.json")
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload, report)
            self.assertFalse(list(Path(temp_dir).glob("*.tmp")))

    def test_local_entrypoint_uses_retry_only_for_valid_replacement(self) -> None:
        source = [_segment("这个料子非。")]
        replacement = [{
            "text": "这个料子非常轻薄。",
            "start": 10.1,
            "end": 12.8,
            "words": [
                {"text": "这个", "start": 10.1, "end": 10.5},
                {"text": "料子", "start": 10.5, "end": 11.0},
                {"text": "非常轻薄", "start": 11.0, "end": 12.8},
            ],
        }]
        settings = {
            "volc_tos_ak": "ak",
            "volc_tos_sk": "sk",
            "volc_api_key": "key",
            "asr_enabled": True,
            "asr_preset": "火山引擎",
            "local_asr_quality_retry_enabled": True,
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            srt = Path(temp_dir) / "clip.srt"
            logs = []
            with (
                mock.patch.object(local_asr, "_local_asr_review_settings", return_value=settings),
                mock.patch.object(local_asr, "_retry_sensevoice_window_with_volcengine", return_value=replacement) as retry,
                mock.patch.dict(os.environ, {"LIVECLIPPER_LOCAL_ASR_REVIEW_MODE": "auto"}, clear=False),
            ):
                reviewed = local_asr._review_sensevoice_segments(source, "source.wav", str(srt), logs.append)

            self.assertEqual(reviewed[0]["text"], "这个料子非常轻薄。")
            retry.assert_called_once()
            self.assertTrue(srt.with_suffix(".asr_quality.json").is_file())
            self.assertTrue(any("局部复核完成" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
