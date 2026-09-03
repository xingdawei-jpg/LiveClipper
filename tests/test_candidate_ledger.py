from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
candidate_ledger = importlib.import_module("candidate_ledger")
ai_clipper = importlib.import_module("ai_clipper")


class CandidateLedgerTests(unittest.TestCase):
    def test_merge_and_filter_keep_auditable_lineage(self) -> None:
        ledger = candidate_ledger.CandidateLedger()
        ledger.seed("semantic", [
            (0.0, 2.0, "[V1] 你看这个肩线"),
            (2.0, 4.0, "[V1] 会往里面收"),
            (5.0, 7.0, "[V1] 面料摸起来很细腻"),
        ])
        ledger.transition(
            "boundary_merge",
            [
                ("product", "[V1] 你看这个肩线会往里面收", 0.0, 4.0),
                ("product", "[V1] 面料摸起来很细腻", 5.0, 7.0),
            ],
            reason_code="semantic_boundary_merge",
        )
        ledger.transition(
            "quality_gate",
            [("product", "[V1] 你看这个肩线会往里面收", 0.0, 4.0)],
            reason_code="candidate_quality_gate",
        )

        data = ledger.to_dict()
        merge_events = [event for event in data["events"] if event["action"] == "MERGE"]
        drops = [event for event in data["events"] if event["action"] == "DROP"]

        self.assertEqual(len(merge_events), 1)
        self.assertEqual(len(merge_events[0]["parents"]), 2)
        self.assertTrue(any(event["stage"] == "quality_gate" for event in drops))
        self.assertEqual(data["stages"][-1]["active_count"], 1)

    def test_membership_annotations_do_not_change_active_candidates(self) -> None:
        ledger = candidate_ledger.CandidateLedger()
        ledger.seed("semantic", [
            (0.0, 2.0, "[V1] 第一条"),
            (2.0, 4.0, "[V1] 第二条"),
        ])
        ledger.transition(
            "frozen",
            [
                {"candidate_id": 1, "source": "V1", "start": 0.0, "end": 2.0, "text": "[V1] 第一条"},
                {"candidate_id": 2, "source": "V1", "start": 2.0, "end": 4.0, "text": "[V1] 第二条"},
            ],
            reason_code="candidate_freeze",
        )
        ledger.mark_membership(
            "content_review",
            {1},
            action="ANNOTATE",
            reason_code="review_card",
            excluded_action="ANNOTATE",
            excluded_reason_code="unreviewed_safe_reserve",
        )

        data = ledger.to_dict()
        stage = data["stages"][-1]
        annotated = [event for event in data["events"] if event["stage"] == "content_review"]

        self.assertEqual(stage["active_count"], 2)
        self.assertEqual(stage["selected_count"], 1)
        self.assertEqual({event["reason_code"] for event in annotated}, {"review_card", "unreviewed_safe_reserve"})

    def test_freeze_pipeline_records_its_real_filter_stages(self) -> None:
        srt = """1
00:00:00,000 --> 00:00:02,000
[V1] 你看这个肩线会往里面收。

2
00:00:02,100 --> 00:00:04,000
[V1] 所以视觉上会更利落。
"""
        ledger = candidate_ledger.CandidateLedger()
        ledger.seed("semantic_input", ai_clipper._parse_srt_entries_for_hook(srt))

        frozen = ai_clipper._freeze_director_candidates(
            srt,
            candidate_ledger=ledger,
        )
        data = ledger.to_dict()
        stages = {stage["stage"] for stage in data["stages"]}

        self.assertIn("frozen_candidate_contract", stages)
        self.assertIn("candidate_quality_gate", stages)
        self.assertIn("你看这个肩线", frozen)

    def test_freeze_exposes_explicit_semantic_ancestors_after_a_real_merge(self) -> None:
        srt = """1
00:00:00,000 --> 00:00:01,000
[V1] 因为

2
00:00:01,050 --> 00:00:03,500
[V1] 肩线往里面收，所以视觉上更显瘦。

3
00:00:04,000 --> 00:00:06,000
[V1] 面料也有筋骨，不会软塌。
"""
        ledger = candidate_ledger.CandidateLedger()
        ledger.seed("semantic_input", ai_clipper._parse_srt_entries_for_hook(srt))

        ai_clipper._freeze_director_candidates(srt, candidate_ledger=ledger)

        origins = ledger.frozen_candidate_origins()
        self.assertEqual(origins[1], (1, 2))
        self.assertEqual(origins[2], (3,))
        final_nodes = [
            node for node in ledger.to_dict()["nodes"]
            if node["candidate_id"] == 1
        ]
        self.assertEqual(final_nodes[-1]["origin_subtitle_ids"], [1, 2])

    def test_commercial_asset_annotations_do_not_change_active_candidates(self) -> None:
        ledger = candidate_ledger.CandidateLedger()
        ledger.seed("semantic_input", [(0.0, 2.0, "[V1] 肩线往里面收。")])
        ledger.transition(
            "frozen_candidate_contract",
            [{
                "candidate_id": 1,
                "source": "V1",
                "start": 0.0,
                "end": 2.0,
                "text": "[V1] 肩线往里面收。",
            }],
            reason_code="immutable_candidate_contract",
        )
        ledger.annotate_commercial_assets("commercial_asset_annotation", [{
            "candidate_id": 1,
            "subject_context": {"product_focus": "same_product", "confidence": "medium"},
            "asset_role": "design_explanation",
            "story_permission": "main_story",
            "evidence_source": "asr",
            "reason": "肩线解释版型。",
        }])

        self.assertEqual(len(ledger.to_dict()["commercial_assets"]), 1)
        self.assertEqual(ledger.commercial_assets()[1].asset_role, "design_explanation")
        self.assertEqual(ledger.to_dict()["stages"][-1]["active_count"], 1)

    def test_quality_rejection_keeps_explicit_ancestor_on_its_drop_node(self) -> None:
        srt = """1
00:00:00,000 --> 00:00:02,000
[V1] 肌励感很强。

2
00:00:02,100 --> 00:00:04,500
[V1] 肩线往里面收，所以视觉更利落。

3
00:00:04,600 --> 00:00:07,000
[V1] 前短后长的设计会更显比例。

4
00:00:07,100 --> 00:00:09,500
[V1] 面料有筋骨，单穿也不会软塌。
"""
        ledger = candidate_ledger.CandidateLedger()
        ledger.seed("semantic_input", ai_clipper._parse_srt_entries_for_hook(srt))

        ai_clipper._freeze_director_candidates(srt, candidate_ledger=ledger)

        data = ledger.to_dict()
        node_by_id = {node["node_id"]: node for node in data["nodes"]}
        quality_drops = [
            event for event in data["events"]
            if event["stage"] == "candidate_quality_gate" and event["action"] == "DROP"
        ]
        self.assertTrue(quality_drops)
        rejected = node_by_id[quality_drops[0]["node_id"]]
        self.assertEqual(rejected["origin_subtitle_ids"], [1])


if __name__ == "__main__":
    unittest.main()
