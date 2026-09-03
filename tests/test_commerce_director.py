import os
import sys
import unittest
from types import SimpleNamespace


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from commerce_director import (  # noqa: E402
    APPAREL_45S_COVERAGE_PROFILE,
    audit_commerce_story_coverage,
    parse_commerce_story_plan,
)
from story_planner import _parse_beats  # noqa: E402


def _plan_payload(*, available=()):
    values = []
    for beat_id in (
        "identity", "difference", "problem_or_benefit", "visible_result",
        "proof", "scene_or_audience", "trust",
    ):
        values.append({
            "beat_id": beat_id,
            "availability": "available" if beat_id in available else "insufficient_evidence",
            "rationale": "来自现有资产的认知任务。",
        })
    return {
        "opening": {"goal": "建立购买问题", "promise": "解释产品为什么值得继续看"},
        "beats": values,
        "coverage_status": "covered",
    }


class CommerceDirectorTests(unittest.TestCase):
    def test_profile_is_cognitive_coverage_not_keyword_filter(self) -> None:
        plan = parse_commerce_story_plan(
            _plan_payload(available={"identity", "difference", "problem_or_benefit", "visible_result", "proof"}),
            strategy_id="S1", profile=APPAREL_45S_COVERAGE_PROFILE,
        )
        self.assertEqual(plan.coverage_status, "covered")
        self.assertEqual([beat.beat_id for beat in plan.beats], [
            "identity", "difference", "problem_or_benefit", "visible_result", "proof", "scene_or_audience", "trust",
        ])
        self.assertFalse(plan.to_dict()["selection_boundary"]["contains_candidate_ids"])

    def test_missing_evidence_is_preserved_instead_of_invented(self) -> None:
        plan = parse_commerce_story_plan(
            _plan_payload(available={"identity", "difference"}), strategy_id="S1",
        )
        self.assertEqual(plan.coverage_status, "insufficient_evidence")
        self.assertEqual(plan.missing_required_beat_ids, ("problem_or_benefit", "visible_result", "proof"))

    def test_selector_fields_are_rejected(self) -> None:
        raw = _plan_payload(available={"identity", "difference", "problem_or_benefit", "visible_result", "proof"})
        raw["beats"][0]["candidate_ids"] = [101]
        with self.assertRaisesRegex(ValueError, "must not select"):
            parse_commerce_story_plan(raw, strategy_id="S1")

    def test_coverage_audit_reports_mapping_without_repair(self) -> None:
        commerce = parse_commerce_story_plan(
            _plan_payload(available={"identity", "difference", "problem_or_benefit", "visible_result", "proof"}),
            strategy_id="S1",
        )
        m2 = SimpleNamespace(beats=(
            SimpleNamespace(chapter_id="C1", commerce_beat_id="identity"),
            SimpleNamespace(chapter_id="C2", commerce_beat_id="difference"),
            SimpleNamespace(chapter_id="C3", commerce_beat_id="problem_or_benefit"),
            SimpleNamespace(chapter_id="C4", commerce_beat_id="visible_result"),
        ))
        audit = audit_commerce_story_coverage(m2, commerce)
        self.assertFalse(audit["passed"])
        self.assertEqual(audit["missing_materialization_beat_ids"], ["proof"])
        self.assertEqual(audit["enforcement"], "experiment_report_only_no_local_repair_or_candidate_filter")

    def test_m2_chapter_keeps_semantic_coverage_mapping(self) -> None:
        beat = _parse_beats({"chapters": [{
            "chapter_id": "C1", "candidate_ids": [101], "commerce_beat_id": "difference",
        }]})[0]
        self.assertEqual(beat.commerce_beat_id, "difference")
        self.assertEqual(beat.to_dict()["commerce_beat_id"], "difference")


if __name__ == "__main__":
    unittest.main()
