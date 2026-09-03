from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
for item in (str(ROOT), str(ROOT / "app")):
    if item not in sys.path:
        sys.path.insert(0, item)

from ai_director_experiment import (  # noqa: E402
    AI_DIRECTOR_MODE_EXPERIMENTAL,
    controlled_experiment_enabled,
    experimental_output_policy,
    M2_PLANNER_MODE_HEAVY_DIRECTOR,
    M2_PLANNER_MODE_LEGACY,
    M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT,
    M2_PLANNER_MODE_LITE_CHAPTER_COMPRESSION_EXPERIMENT,
    M2_PLANNER_MODE_LITE_FINAL_EDITOR_EXPERIMENT,
    controlled_planner_mode,
    planner_mode_version,
    resolve_ai_director_mode,
    resolve_m2_planner_mode,
)


class AiDirectorExperimentTests(unittest.TestCase):
    def test_missing_or_invalid_mode_fails_closed_to_legacy(self) -> None:
        self.assertEqual(resolve_ai_director_mode({}), "legacy")
        self.assertEqual(resolve_ai_director_mode({"ai_director_mode": "unsafe"}), "legacy")
        self.assertFalse(controlled_experiment_enabled({}))

    def test_only_experimental_mode_enables_controlled_runner(self) -> None:
        settings = {"ai_director_mode": AI_DIRECTOR_MODE_EXPERIMENTAL}
        self.assertTrue(controlled_experiment_enabled(settings))
        self.assertFalse(controlled_experiment_enabled({"ai_director_mode": "production"}))

    def test_policy_can_render_but_never_exposes_or_publishes(self) -> None:
        policy = experimental_output_policy(technical_ok=True)
        self.assertTrue(policy["output_allowed"])
        self.assertFalse(policy["user_preview_allowed"])
        self.assertFalse(policy["formal_export_allowed"])
        self.assertFalse(policy["publication_allowed"])

    def test_planner_mode_is_legacy_by_default_and_only_experimental_when_enabled(self) -> None:
        self.assertEqual(resolve_m2_planner_mode({}), M2_PLANNER_MODE_LEGACY)
        self.assertEqual(resolve_m2_planner_mode({"m2_planner_mode": "unknown"}), M2_PLANNER_MODE_LEGACY)
        self.assertEqual(
            resolve_m2_planner_mode({"m2_planner_mode": M2_PLANNER_MODE_HEAVY_DIRECTOR}),
            M2_PLANNER_MODE_HEAVY_DIRECTOR,
        )
        self.assertEqual(
            controlled_planner_mode({"m2_planner_mode": M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT}),
            M2_PLANNER_MODE_LEGACY,
        )
        enabled = {
            "ai_director_mode": AI_DIRECTOR_MODE_EXPERIMENTAL,
            "m2_planner_mode": M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT,
        }
        self.assertEqual(controlled_planner_mode(enabled), M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT)
        self.assertEqual(planner_mode_version(M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT), "commerce_lite_v2.1")
        self.assertEqual(
            controlled_planner_mode({
                "ai_director_mode": "experimental",
                "m2_planner_mode": M2_PLANNER_MODE_LITE_CHAPTER_COMPRESSION_EXPERIMENT,
            }),
            M2_PLANNER_MODE_LITE_CHAPTER_COMPRESSION_EXPERIMENT,
        )
        self.assertEqual(
            planner_mode_version(M2_PLANNER_MODE_LITE_CHAPTER_COMPRESSION_EXPERIMENT),
            "commerce_lite_m2.4_chapter_compression",
        )
        self.assertEqual(
            controlled_planner_mode({
                "ai_director_mode": "experimental",
                "m2_planner_mode": M2_PLANNER_MODE_LITE_FINAL_EDITOR_EXPERIMENT,
            }),
            M2_PLANNER_MODE_LITE_FINAL_EDITOR_EXPERIMENT,
        )
        self.assertEqual(
            planner_mode_version(M2_PLANNER_MODE_LITE_FINAL_EDITOR_EXPERIMENT),
            "commerce_m2_narrative_beat_casting_p0_5a3_a4_v3",
        )
