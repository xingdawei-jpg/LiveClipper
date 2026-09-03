import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from run_m3_new_golden_plan_fidelity import (  # noqa: E402
    _select_m1_hero,
    _write_approved_selector_render_manifest,
)


class M3NewGoldenPlanFidelityTests(unittest.TestCase):
    def test_hero_selection_is_explicit_or_preserves_m1_order_among_high_stories(self) -> None:
        strategies = (
            SimpleNamespace(strategy_id="S1", story_priority="medium"),
            SimpleNamespace(strategy_id="S2", story_priority="high"),
            SimpleNamespace(strategy_id="S3", story_priority="high"),
        )
        selected, reason = _select_m1_hero(strategies)
        explicit, explicit_reason = _select_m1_hero(strategies, "S3")

        self.assertEqual((selected.strategy_id, reason), ("S2", "first_m1_high_strategy_in_model_order"))
        self.assertEqual((explicit.strategy_id, explicit_reason), ("S3", "explicit_strategy_id"))

    def test_explicit_unknown_hero_is_not_silently_replaced(self) -> None:
        with self.assertRaisesRegex(ValueError, "not found"):
            _select_m1_hero((SimpleNamespace(strategy_id="S1", story_priority="high"),), "S9")

    def test_render_handoff_exists_only_after_quality_gate_and_m3_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_srt = root / "source.srt"
            source_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n原话\n", encoding="utf-8")
            plan = SimpleNamespace(total_seconds=1.0, target_duration=40.0, duration_plan=None)
            selector = {
                "status": "ok",
                "ranges": [{"start": 0.0, "end": 1.0, "duration": 1.0, "text": "原话"}],
            }
            blocked = _write_approved_selector_render_manifest(
                output_dir=root,
                case_id="caramel",
                source_srt=str(source_srt),
                plan=plan,
                selector_result=selector,
                quality_gate={"passed": False},
                approved=False,
            )
            manifest_path = _write_approved_selector_render_manifest(
                output_dir=root,
                case_id="caramel",
                source_srt=str(source_srt),
                plan=plan,
                selector_result=selector,
                quality_gate={"passed": True},
                approved=True,
            )

            self.assertIsNone(blocked)
            self.assertTrue(manifest_path and manifest_path.is_file())
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["purchase_journey_quality_render_gate"]["passed"])
            self.assertEqual(payload["selector_result"]["ranges"][0]["text"], "原话")


if __name__ == "__main__":
    unittest.main()
