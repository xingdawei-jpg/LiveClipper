from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from run_m123_pre_shadow import (  # noqa: E402
    _evaluation_ranges,
    _planner_mode_flags,
    _quality_review,
    _write_planner_manifest,
    write_run_summary,
)
from ai_director_experiment import M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT  # noqa: E402


class PreShadowRunnerTests(unittest.TestCase):
    def test_human_opening_and_legacy_comparison_remain_hard_gates(self) -> None:
        review = _quality_review({
            "m2_plan": {"plan_valid": True, "duration_assessment": {"status": "full"}},
            "m3_selection_result": {"status": "ok"},
            "m3_plan_fidelity_audit": {"passed": True},
            "m2_story_consumption_audit": {"passed": True},
            "semantic_binder": {"coverage": 1.0, "ambiguous": 0, "unmatched": 0},
        }, legacy_status="legacy_run_log_not_supplied")
        self.assertEqual(review["technical_status"], "passed")
        self.assertEqual(review["status"], "blocked")
        self.assertTrue(review["commercial_quality_pending"])
        self.assertIn("opening_quality_human_review_required", review["reasons"])
        self.assertIn("legacy_comparison_unavailable", review["reasons"])

    def test_controlled_experiment_renders_for_human_review_without_publication(self) -> None:
        review = _quality_review({
            "m2_plan": {"plan_valid": True, "duration_assessment": {"status": "full"}},
            "m3_selection_result": {"status": "ok"},
            "m3_plan_fidelity_audit": {"passed": True},
            "m2_story_consumption_audit": {"passed": True},
            "semantic_binder": {"coverage": 1.0, "ambiguous": 0, "unmatched": 0},
        }, legacy_status="legacy_run_log_not_supplied", controlled_experiment=True)
        self.assertEqual(review["status"], "ready_for_human_review")
        self.assertEqual(review["story_quality"], "experimental")
        self.assertEqual(review["opening_quality"], "human_review_required")
        self.assertFalse(review["commercial_ready"])
        self.assertTrue(review["diagnostic_render_allowed"])
        self.assertFalse(review["experimental_output_policy"]["user_preview_allowed"])
        self.assertFalse(review["experimental_output_policy"]["publication_allowed"])

    def test_controlled_experiment_is_explicitly_recorded_in_summary(self) -> None:
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = write_run_summary(
                root, "experiment-1", target_duration=45, render_requested=True,
                controlled_experiment=True,
            )
            summary = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(summary["controlled_experiment"])
            self.assertIn("controlled_experiment", summary["mode"])

    def test_selector_lineage_is_adapted_for_blind_evaluation(self) -> None:
        values = _evaluation_ranges([{
            "parent_candidate_id": 17, "start": 1.0, "end": 2.0, "text": "完整表达",
        }])
        self.assertEqual(values, [{"candidate_id": 17, "start": 1.0, "end": 2.0, "text": "完整表达"}])

    def test_summary_recovers_all_completed_case_statuses(self) -> None:
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            for case_id in ("jccc_deep_roast_hoodie", "shanjie_plaid_splice"):
                target = root / case_id / "run-1"
                target.mkdir(parents=True)
                (target / "status.json").write_text(
                    json.dumps({"status": "blocked", "reasons": ["gate"], "render": {}}), encoding="utf-8"
                )
            path = write_run_summary(root, "run-1", target_duration=45, render_requested=False)
            summary = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([item["case_id"] for item in summary["cases"]], [
                "jccc_deep_roast_hoodie", "shanjie_plaid_splice",
            ])

    def test_lite_manifest_changes_only_the_m2_composition_label(self) -> None:
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.assertEqual(_planner_mode_flags(M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT), (False, True))
            path = _write_planner_manifest(
                root,
                run_id="lite-1",
                planner_mode=M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT,
                controlled_experiment=True,
                source_info={"case_id": "source-1", "source_srt": "input.srt", "source_video": "input.mp4"},
            )
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["planner_mode"], M2_PLANNER_MODE_LITE_DIRECTOR_EXPERIMENT)
            self.assertEqual(manifest["planner_version"], "commerce_lite_v2.1")
            self.assertEqual(manifest["unchanged_components"], ["M1", "hard_safe_candidates", "candidate_ledger", "semantic_binder", "M3"])
            self.assertFalse(manifest["user_preview_allowed"])
            self.assertFalse(manifest["publication_allowed"])


if __name__ == "__main__":
    unittest.main()
