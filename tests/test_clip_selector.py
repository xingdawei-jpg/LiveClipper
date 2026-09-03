from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from clip_selector import (  # noqa: E402
    assess_candidate_materializability,
    audit_materialization_fidelity,
    bind_candidate_words_by_origin,
    materialize_narrative_plan,
)
from story_planner import NarrativeBeat, NarrativePlan, PlanningCandidate  # noqa: E402


def _candidate(
    candidate_id: int,
    text: str,
    *,
    start: float = 0.0,
    end: float = 4.0,
) -> PlanningCandidate:
    return PlanningCandidate(candidate_id, "V1", start, end, text, (candidate_id,))


def _beat(chapter_id: str, candidate_id: int) -> NarrativeBeat:
    return NarrativeBeat(
        source_role="proof",
        narrative_role="development",
        goal="讲清这一章",
        candidate_evidence=(candidate_id,),
        required=True,
        target_seconds=3.0,
        selection_instruction="只用这个已批准候选",
        chapter_id=chapter_id,
    )


def _plan(*candidates: PlanningCandidate) -> NarrativePlan:
    return NarrativePlan(
        "S1", "商业故事", 12.0,
        tuple(_beat(f"C{index}", candidate.candidate_id) for index, candidate in enumerate(candidates, 1)),
        "ok", 0.0, (), (), True,
        selected_candidates=tuple(candidates),
    )


class ClipSelectorTests(unittest.TestCase):
    def test_trims_only_a_leading_vocal_and_keeps_exact_word_boundaries(self) -> None:
        candidate = _candidate(11, "嗯，这件上身很显精神。", end=3.4)
        result = materialize_narrative_plan(_plan(candidate), {
            11: (
                {"text": "嗯", "start": 0.0, "end": 0.18},
                {"text": "这件", "start": 0.26, "end": 0.62},
                {"text": "上身", "start": 0.66, "end": 1.02},
                {"text": "很显精神", "start": 1.08, "end": 2.05},
            ),
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(result.ranges), 1)
        selected = result.ranges[0]
        self.assertEqual(selected.text, "这件上身很显精神。")
        self.assertEqual((selected.start, selected.end), (0.26, 2.05))
        self.assertEqual(selected.boundary_kind, "leading_vocal_trim")
        self.assertEqual(selected.origin_subtitle_ids, (11,))

    def test_missing_word_timing_keeps_an_already_complete_candidate(self) -> None:
        candidate = _candidate(12, "前短后长穿起来比例更好。", start=5.0, end=8.2)
        result = materialize_narrative_plan(_plan(candidate), {})

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.ranges[0].boundary_kind, "whole_candidate_no_word_timing")
        self.assertEqual(result.ranges[0].text, candidate.text)

    def test_completed_colloquial_question_is_not_misclassified_as_a_tail_particle(self) -> None:
        candidate = _candidate(
            168,
            "我的肩比我这根线多出来这么多呢，但是你会不会发现这个线像是我肩最宽的位置啊？",
            start=565.649,
            end=570.85,
        )
        result = materialize_narrative_plan(_plan(candidate), {})

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.ranges[0].boundary_kind, "whole_candidate_no_word_timing")
        self.assertEqual(result.ranges[0].text, candidate.text)

    def test_ambiguous_multiple_sentences_are_preserved_not_arbitrarily_reselected(self) -> None:
        candidate = _candidate(13, "肩线很利落。面料也有筋骨。", end=4.2)
        result = materialize_narrative_plan(_plan(candidate), {
            13: (
                {"text": "肩线", "start": 0.0, "end": 0.4},
                {"text": "很利落", "start": 0.45, "end": 1.1},
                {"text": "面料", "start": 1.25, "end": 1.65},
                {"text": "也有筋骨", "start": 1.7, "end": 2.4},
            ),
        })

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.ranges[0].boundary_kind, "whole_candidate_multiple_complete_units")
        self.assertEqual(result.ranges[0].text, candidate.text)
        self.assertEqual((result.ranges[0].start, result.ranges[0].end), (0.0, 2.4))

    def test_incomplete_candidate_returns_structured_blocker_instead_of_a_label_fragment(self) -> None:
        candidate = _candidate(14, "整个人的气质", end=1.8)
        result = materialize_narrative_plan(_plan(candidate), {
            14: ({"text": "整个人的气质", "start": 0.0, "end": 1.8},),
        })

        self.assertEqual(result.status, "selector_blocked")
        self.assertEqual(result.ranges, ())
        self.assertEqual(result.blocked[0].code, "trim_would_break_semantic_unit")

    def test_selector_cannot_materialize_candidate_outside_m2_approval(self) -> None:
        candidate = _candidate(15, "这件穿起来很有型。")
        plan = NarrativePlan(
            "S1", "商业故事", 8.0, (_beat("C1", 999),),
            "ok", 0.0, (), (), True, selected_candidates=(candidate,),
        )
        result = materialize_narrative_plan(plan, {})

        self.assertEqual(result.status, "selector_blocked")
        self.assertEqual(result.blocked[0].code, "candidate_not_approved_by_m2")

    def test_selector_preserves_m2_chapter_and_candidate_order(self) -> None:
        first = _candidate(16, "开场有反差。", start=0.0, end=2.0)
        second = _candidate(17, "后面马上给证据。", start=2.0, end=4.0)
        result = materialize_narrative_plan(_plan(first, second), {})

        self.assertEqual(result.status, "ok")
        self.assertEqual(
            [(item.chapter_id, item.parent_candidate_id) for item in result.ranges],
            [("C1", 16), ("C2", 17)],
        )

    def test_word_binding_requires_verified_source_text_not_timestamp_overlap(self) -> None:
        candidate = _candidate(18, "这件上身很显精神。")
        binding = bind_candidate_words_by_origin(
            (candidate,),
            ({"id": 18, "start": 0.0, "end": 2.0, "text": "这件上身很显精神。"},),
            ({"start": 20.0, "end": 22.0, "text": "这件上身很显精神。", "words": (
                {"text": "这件", "start": 20.0, "end": 20.4},
                {"text": "上身", "start": 20.5, "end": 20.9},
                {"text": "很显精神", "start": 21.0, "end": 22.0},
            )},),
        )
        self.assertEqual(tuple(binding.words_by_candidate), (18,))
        self.assertEqual(binding.unbound_reasons, ())

    def test_word_binding_refuses_sidecar_text_mismatch(self) -> None:
        candidate = _candidate(19, "这件上身很显精神。")
        binding = bind_candidate_words_by_origin(
            (candidate,),
            ({"id": 19, "text": "这件上身很显精神。"},),
            ({"text": "这件面料很舒服。", "words": ()},),
        )
        self.assertEqual(binding.words_by_candidate, {})
        self.assertEqual(binding.unbound_reasons, ("sidecar_text_mismatch_at_source:19",))

    def test_selector_blocks_obvious_asr_tail_when_no_word_sidecar_is_available(self) -> None:
        candidate = _candidate(20, "往人群当中一站，就是那种没有刻意打扮过的精。")
        result = materialize_narrative_plan(_plan(candidate), {})

        self.assertEqual(result.status, "selector_blocked")
        self.assertEqual(result.ranges, ())
        self.assertEqual(result.blocked[0].code, "candidate_not_complete")
        self.assertEqual(result.blocked[0].detail, "词尾残缺")

    def test_selector_blocks_unretained_transition_clause(self) -> None:
        candidate = _candidate(21, "显得人特别有精神之外，它是让你整个人穿上显背薄的。")
        result = materialize_narrative_plan(_plan(candidate), {})

        self.assertEqual(result.status, "selector_blocked")
        self.assertEqual(result.blocked[0].detail, "转折承接未保留")

    def test_materializability_preflight_exposes_a_boundary_blocker_without_selecting_a_replacement(self) -> None:
        candidate = _candidate(22, "整个人的气质")

        result = assess_candidate_materializability(
            candidate,
            ({"text": "整个人的气质", "start": 0.0, "end": 1.0},),
        )

        self.assertEqual(result.parent_candidate_id, 22)
        self.assertEqual(result.code, "trim_would_break_semantic_unit")

    def test_fidelity_audit_requires_exact_m2_order_and_ledger_identity(self) -> None:
        first = _candidate(31, "先讲清肩线为什么显窄。", start=1.0, end=2.0)
        second = _candidate(32, "再讲度假时穿得更放松。", start=3.0, end=4.0)
        plan = _plan(first, second)
        result = materialize_narrative_plan(plan, {
            31: ({"text": "先讲清肩线为什么显窄", "start": 1.1, "end": 1.9},),
            32: ({"text": "再讲度假时穿得更放松", "start": 3.1, "end": 3.9},),
        })
        audit = audit_materialization_fidelity(
            plan,
            result,
            (
                {"candidate_id": 31, "start": 1.0, "end": 2.0, "text": first.text},
                {"candidate_id": 32, "start": 3.0, "end": 4.0, "text": second.text},
            ),
            require_word_boundaries=True,
        )

        self.assertTrue(audit["passed"])
        self.assertTrue(audit["no_story_rewrite"])
        self.assertTrue(audit["complete_plan_materialized"])
        self.assertEqual(audit["candidate_ledger_identity_error_ids"], [])

    def test_fidelity_audit_does_not_pass_sentence_timing_as_word_exact(self) -> None:
        candidate = _candidate(33, "完整的购买理由。")
        plan = _plan(candidate)
        result = materialize_narrative_plan(plan, {})
        audit = audit_materialization_fidelity(
            plan,
            result,
            ({"candidate_id": 33, "start": 0.0, "end": 4.0, "text": candidate.text},),
            require_word_boundaries=True,
        )

        self.assertFalse(audit["passed"])
        self.assertIn("verified_word_boundaries_required", audit["issues"])


if __name__ == "__main__":
    unittest.main()
