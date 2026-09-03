from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from run_m3_new_golden_plan_fidelity import _run_case  # noqa: E402
from commercial_analyzer import Strategy  # noqa: E402


class M3MaterializationPreflightTests(unittest.TestCase):
    def test_unverified_word_sidecars_block_before_m1_and_persist_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "source.mp4"
            srt = root / "source.srt"
            output_dir = root / "artifacts"
            video.write_bytes(b"source")
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n肩宽女生穿这个版型会显瘦。\n",
                encoding="utf-8",
            )
            with mock.patch(
                "run_m3_new_golden_plan_fidelity.analyze_commercial_story"
            ) as m1_call:
                with self.assertRaisesRegex(ValueError, "no materializable hard-safe candidates"):
                    _run_case(
                        "unverified_source",
                        settings={},
                        output_dir=output_dir,
                        target_duration=45.0,
                        strategy_id="",
                        m2_replan_attempts=0,
                        opening_quality_review=False,
                        source_definition={
                            "label": "source",
                            "product": "测试商品",
                            "srt": str(srt),
                        },
                    )

            self.assertFalse(m1_call.called)
            preflight_paths = list(output_dir.glob("*.materialization_preflight.json"))
            self.assertEqual(len(preflight_paths), 1)
            preflight = json.loads(preflight_paths[0].read_text(encoding="utf-8"))
            self.assertEqual(preflight["reason"], "no_materializable_hard_safe_candidates")
            self.assertEqual(preflight["word_lineage_bound_candidate_count"], 0)
            self.assertEqual(
                preflight["binder"]["source_identity"]["reason"],
                "user_or_legacy_srt",
            )

    def test_sentence_preview_explicitly_bypasses_word_sidecar_and_m3(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            srt = root / "source.srt"
            output_dir = root / "artifacts"
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:02,000\n穿上正面很显瘦。\n\n"
                "2\n00:00:02,100 --> 00:00:04,100\n肩线会往里面收。\n\n"
                "3\n00:00:04,200 --> 00:00:06,200\n夏天穿着也很透气。\n",
                encoding="utf-8",
            )
            strategy = Strategy.from_dict({
                "strategy_id": "S1",
                "director_plan_role": "primary",
                "director_title": "显瘦又透气",
                "core_desire": "穿得显瘦又舒服",
                "opening_promise": "先看显瘦结果",
                "chapter_packets": [
                    {"chapter_id": "C1", "chapter_kind": "result", "title": "结果", "buyer_advance": "显瘦", "coverage": "required", "beats": [{"beat_id": "B1", "beat_function": "result", "source_span": {"start_id": 1, "end_id": 1}, "verbatim": "穿上正面很显瘦。"}]},
                    {"chapter_id": "C2", "chapter_kind": "mechanism", "title": "原因", "buyer_advance": "肩线内收", "coverage": "required", "beats": [{"beat_id": "B2", "beat_function": "mechanism", "source_span": {"start_id": 2, "end_id": 2}, "verbatim": "肩线会往里面收。"}]},
                    {"chapter_id": "C3", "chapter_kind": "comfort", "title": "体验", "buyer_advance": "夏天透气", "coverage": "recommended", "beats": [{"beat_id": "B3", "beat_function": "experience", "source_span": {"start_id": 3, "end_id": 3}, "verbatim": "夏天穿着也很透气。"}]},
                ],
            }, index=1)

            case = _run_case(
                "sentence_preview",
                settings={"content_policy": {}},
                output_dir=output_dir,
                target_duration=60.0,
                strategy_id="",
                m2_replan_attempts=0,
                opening_quality_review=False,
                commerce_lite_final_editor=True,
                m1_strategy_override=strategy,
                director_strategy_contract={
                    "single_ai_director_packet": True,
                    "sentence_preview_without_m3": True,
                },
                skip_m3_materialization=True,
                source_definition={
                    "label": "source",
                    "product": "测试商品",
                    "srt": str(srt),
                },
            )

            self.assertTrue(case["passed"])
            self.assertEqual(case["m3_selection_result"]["status"], "not_run_sentence_preview")
            self.assertEqual(case["m3_plan_fidelity_audit"]["status"], "not_run_sentence_preview")
            self.assertIsNone(case["approved_selector_render_manifest"])
            self.assertEqual(len(case["m2_plan"]["selected_candidates"]), 3)


if __name__ == "__main__":
    unittest.main()
