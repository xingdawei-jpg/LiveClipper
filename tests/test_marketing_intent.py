from __future__ import annotations

import copy
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


marketing_intent = importlib.import_module("marketing_intent")
content_review = importlib.import_module("content_review")
ai_clipper = importlib.import_module("ai_clipper")


def _inventory() -> list[dict]:
    return [
        {
            "srt_index": 1,
            "source": "V1",
            "start": 0.0,
            "end": 5.0,
            "duration_sec": 5.0,
            "story_block_id": "SB-V1-001",
            "continuity_group_id": "CG-V1-001",
            "text": "穿上以后整个人看起来更利落，也更有自己的风格。",
        },
        {
            "srt_index": 2,
            "source": "V1",
            "start": 5.0,
            "end": 10.0,
            "duration_sec": 5.0,
            "story_block_id": "SB-V1-001",
            "continuity_group_id": "CG-V1-001",
            "text": "肩部的黑色编织线把视觉重心往里收，所以肩线看起来更窄。",
        },
        {
            "srt_index": 3,
            "source": "V1",
            "start": 10.0,
            "end": 15.0,
            "duration_sec": 5.0,
            "story_block_id": "SB-V1-001",
            "continuity_group_id": "CG-V1-001",
            "text": "通勤搭西装，周末搭牛仔裤，都能穿得很有精神。",
        },
        {
            "srt_index": 4,
            "source": "V1",
            "start": 24.0,
            "end": 29.0,
            "duration_sec": 5.0,
            "story_block_id": "SB-V1-001",
            "continuity_group_id": "CG-V1-002",
            "text": "领口的包边做得很平整，贴身穿也不会磨脖子。",
        },
        {
            "srt_index": 5,
            "source": "V2",
            "start": 0.0,
            "end": 5.0,
            "duration_sec": 5.0,
            "story_block_id": "SB-V2-001",
            "continuity_group_id": "CG-V2-001",
            "text": "另一件的颜色在阳光下会显得更有层次。",
        },
    ]


def _cards() -> list[dict]:
    return [
        {"candidate_id": 1, "roles": ("effect",), "evidence_type": "具体效果", "evidence_quote": "更利落"},
        {"candidate_id": 2, "roles": ("evidence",), "evidence_type": "原因解释", "evidence_quote": "视觉重心往里收"},
        {"candidate_id": 3, "roles": ("scene",), "evidence_type": "场景例子", "evidence_quote": "通勤搭西装"},
        {"candidate_id": 4, "roles": ("evidence",), "evidence_type": "工艺细节", "evidence_quote": "包边做得很平整"},
        {"candidate_id": 5, "roles": ("effect",), "evidence_type": "颜色效果", "evidence_quote": "更有层次"},
    ]


def _payload() -> dict:
    return {
        "marketing_intents": [
            [1, "identity_expression", "肩线内收让整体气质更利落", "整个人看起来更利落"],
            [2, "product_proof", "肩部编织线将视觉重心向内收", "视觉重心往里收"],
            [3, "ownership_scene", "通勤和休闲都能直接搭配", "通勤搭西装，周末搭牛仔裤"],
        ],
        "narrative_arcs": [
            [1, [2], [3], "identity_expression", "肩线细节解释利落感，再落到使用场景"],
        ],
    }


class MarketingIntentValidationTests(unittest.TestCase):
    def test_grounded_arc_is_strong_and_does_not_mutate_frozen_inventory(self) -> None:
        inventory = _inventory()
        original = copy.deepcopy(inventory)

        bundle = marketing_intent.build_marketing_intent_bundle(
            _payload(), cards=_cards(), inventory=inventory, candidate_digest="digest"
        )

        self.assertEqual(inventory, original)
        self.assertEqual([item.candidate_id for item in bundle.intents], [1, 2, 3])
        self.assertEqual(len(bundle.arcs), 1)
        arc = bundle.arcs[0]
        self.assertEqual(arc.continuity, "strong")
        self.assertEqual(arc.product_scope, "inferred_same_product")
        self.assertTrue(arc.activation_eligible)
        self.assertEqual([(item.candidate_id, item.strength) for item in arc.proof_links], [(2, "direct")])

    def test_manual_main_product_locks_scope_without_claiming_candidate_metadata(self) -> None:
        bundle = marketing_intent.build_marketing_intent_bundle(
            _payload(), cards=_cards(), inventory=_inventory(), candidate_digest="digest", main_product="亨利领上衣"
        )

        self.assertEqual(bundle.arcs[0].product_scope, "locked")
        self.assertTrue(bundle.arcs[0].activation_eligible)

    def test_arc_without_direct_proof_is_rejected_not_promoted(self) -> None:
        payload = _payload()
        payload["marketing_intents"].append(
            [4, "product_proof", "另一处细节说明", "袖口做了收束"]
        )
        payload["narrative_arcs"] = [
            [1, [4], [], "identity_expression", "只是在同一故事区间的另一个细节"],
        ]

        bundle = marketing_intent.build_marketing_intent_bundle(
            payload, cards=_cards(), inventory=_inventory(), candidate_digest="digest"
        )

        self.assertEqual(bundle.arcs, ())
        self.assertEqual(bundle.rejections[0].reason, "no_direct_proof")

    def test_arc_proof_must_be_explicit_product_proof_intent(self) -> None:
        payload = _payload()
        payload["marketing_intents"] = [
            item for item in payload["marketing_intents"] if item[0] != 2
        ]

        bundle = marketing_intent.build_marketing_intent_bundle(
            payload,
            cards=_cards(),
            inventory=_inventory(),
            candidate_digest="digest",
            main_product="上衣",
        )

        self.assertEqual(bundle.arcs, ())
        self.assertEqual(
            bundle.rejections[0].reason,
            "proof_has_no_product_evidence_intent",
        )

    def test_product_proof_cannot_open_a_marketing_arc(self) -> None:
        payload = _payload()
        payload["marketing_intents"] = [
            item for item in payload["marketing_intents"] if item[0] != 3
        ]
        payload["marketing_intents"].append(
            [3, "product_proof", "通勤和休闲都能直接搭配", "通勤搭西装，周末搭牛仔裤"]
        )
        payload["narrative_arcs"] = [
            [2, [3], [], "product_proof", "把肩线细节当成弧开头"],
        ]

        bundle = marketing_intent.build_marketing_intent_bundle(
            payload, cards=_cards(), inventory=_inventory(), candidate_digest="digest"
        )

        self.assertEqual(bundle.arcs, ())
        self.assertEqual(
            bundle.rejections[0].reason,
            "product_proof_cannot_open_arc",
        )

    def test_unknown_candidates_and_ungrounded_openings_are_rejected(self) -> None:
        payload = _payload()
        payload["marketing_intents"].append([99, "product_distinction", "编造的候选", "不存在"])
        payload["narrative_arcs"] = [
            [99, [2], [], "product_distinction", "不存在"],
            [1, [99], [], "identity_expression", "不存在"],
        ]

        bundle = marketing_intent.build_marketing_intent_bundle(
            payload, cards=_cards(), inventory=_inventory(), candidate_digest="digest"
        )

        self.assertEqual([item.candidate_id for item in bundle.intents], [1, 2, 3])
        self.assertEqual(bundle.arcs, ())
        self.assertEqual({item.reason for item in bundle.rejections}, {"unknown_or_unreviewed_candidate"})

    def test_missing_marketing_fields_are_a_compatible_noop(self) -> None:
        bundle = marketing_intent.build_marketing_intent_bundle(
            {"cards": []}, cards=_cards(), inventory=_inventory(), candidate_digest="digest"
        )

        self.assertFalse(bundle.response_present)
        self.assertEqual(bundle.intents, ())
        self.assertEqual(bundle.arcs, ())

    def test_marketing_schema_is_only_added_for_shadow_review(self) -> None:
        _system, normal_prompt = content_review._review_prompts(
            _inventory()[:2],
            category="上衣",
            main_product="",
            avoid=[],
            required_sources=None,
            format_retry=False,
            include_marketing_intent=False,
        )
        _system, shadow_prompt = content_review._review_prompts(
            _inventory()[:2],
            category="上衣",
            main_product="",
            avoid=[],
            required_sources=None,
            format_retry=False,
            include_marketing_intent=True,
        )

        self.assertNotIn("marketing_intents", normal_prompt)
        self.assertNotIn("story_block_id", normal_prompt)
        self.assertIn("marketing_intents", shadow_prompt)
        self.assertIn("narrative_arcs", shadow_prompt)
        self.assertNotEqual(
            content_review.build_cache_key("d", "上衣", "", [], "m"),
            content_review.build_cache_key("d", "上衣", "", [], "m", include_marketing_intent=True),
        )

    def test_content_review_serializes_marketing_bundle_without_changing_card_contract(self) -> None:
        inventory = _inventory()[:3]
        review_cards = [
            {
                "candidate_id": 1,
                "topic": "版型显瘦",
                "subtopic": "肩线内收",
                "buyer_value": "说明肩线内收的视觉效果",
                "evidence_type": "具体效果",
                "evidence_quote": "整个人看起来更利落",
                "roles": ["effect"],
                "dependency": "independent",
                "quality_tags": ["具体效果"],
                "tier": "main",
            },
            {
                "candidate_id": 2,
                "topic": "工艺细节",
                "subtopic": "肩部编织线",
                "buyer_value": "解释肩线内收的设计原因",
                "evidence_type": "原因解释",
                "evidence_quote": "视觉重心往里收",
                "roles": ["evidence"],
                "dependency": "independent",
                "quality_tags": ["原因解释"],
                "tier": "main",
            },
            {
                "candidate_id": 3,
                "topic": "场景搭配",
                "subtopic": "通勤休闲",
                "buyer_value": "给出两类直接可用的搭配",
                "evidence_type": "场景例子",
                "evidence_quote": "通勤搭西装，周末搭牛仔裤",
                "roles": ["scene"],
                "dependency": "independent",
                "quality_tags": ["场景清晰"],
                "tier": "reserve",
            },
        ]
        data = {"cards": review_cards, "hook_pairs": [], **_payload()}

        bundle = content_review._validate_bundle(
            data,
            inventory=inventory,
            cache_key="cache-key",
            candidate_digest="digest",
            category="上衣",
            model="deepseek-v4-flash",
            required_sources=None,
        )

        self.assertEqual(bundle.allowed_candidate_ids, {1, 2, 3})
        self.assertEqual(bundle.marketing_intent.summary()["eligible_arc_count"], 1)
        saved = bundle.to_dict()
        self.assertIn("marketing_intent", saved)
        self.assertEqual(saved["marketing_intent"]["arcs"][0]["candidate_ids"], [1, 2, 3])
        restored = content_review._validate_bundle(
            saved,
            inventory=inventory,
            cache_key="cache-key",
            candidate_digest="digest",
            category="上衣",
            model="deepseek-v4-flash",
            required_sources=None,
        )
        self.assertEqual(restored.marketing_intent.summary()["eligible_arc_count"], 1)

    def test_shadow_records_join_final_selection_and_manual_feedback(self) -> None:
        bundle = marketing_intent.build_marketing_intent_bundle(
            _payload(), cards=_cards(), inventory=_inventory(), candidate_digest="digest"
        )
        manifest = {"digest": "selection-digest", "items": [{"clip_id": "c1"}]}
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "marketing_arc_shadow.jsonl"
            with mock.patch.object(marketing_intent, "marketing_intent_shadow_path", return_value=path):
                run_key = marketing_intent.append_shadow_observation(
                    bundle=bundle,
                    review_cache_key="review-cache",
                    mode="shadow",
                    category="上衣",
                    selection_manifest=manifest,
                )
                self.assertTrue(run_key)
                self.assertTrue(marketing_intent.append_manual_feedback_shadow(
                    run_key, {"preview_id": "preview-1", "kept_segment_count": 3, "role_samples": {}}
                ))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([row["event"] for row in rows], ["legacy_final_selection", "manual_preview_selection"])
        self.assertTrue(all(row["run_key"] == run_key for row in rows))

    def test_shadow_review_never_narrows_the_director_candidate_contract(self) -> None:
        settings = {
            "api_key": "key",
            "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash",
            "content_review_mode": "shadow",
        }
        card = content_review.ContentCard(
            1, "版型显瘦", "肩线", "修饰肩宽", "具体效果", "肩线清楚",
            ("effect",), "independent", ("具体效果",), "main",
        )
        bundle = content_review.ContentReviewBundle(
            "review-key", "digest", "上衣", "deepseek-v4-flash", (card,), 10.0,
            marketing_intent=marketing_intent.MarketingIntentBundle("digest", response_present=True),
        )
        srt = (
            "1\n00:00:00,000 --> 00:00:32,000\n肩线向内收，穿上后视觉更利落。\n\n"
            "2\n00:00:32,000 --> 00:01:05,000\n面料轻薄透气，贴身穿也不会闷。\n"
        )
        safe_inventory = [
            {"srt_index": 1, "source": "V1", "duration_sec": 32.0, "text": "肩线向内收，穿上后视觉更利落。"},
            {"srt_index": 2, "source": "V1", "duration_sec": 33.0, "text": "面料轻薄透气，贴身穿也不会闷。"},
        ]

        def capture_director(*_args, **kwargs):
            self.assertEqual(kwargs["allowed_candidate_ids"], {1, 2})
            self.assertEqual(kwargs["content_review_hint"], "")
            raise RuntimeError("shadow-director-called")

        with mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=safe_inventory), \
             mock.patch.object(content_review, "review_candidates", return_value=bundle) as reviewer, \
             mock.patch.object(ai_clipper, "_call_ai", side_effect=capture_director):
            with self.assertRaisesRegex(RuntimeError, "shadow-director-called"):
                ai_clipper.ai_analyze_clips(srt, target_duration=60)

        self.assertTrue(reviewer.call_args.kwargs["include_marketing_intent"])


if __name__ == "__main__":
    unittest.main()
