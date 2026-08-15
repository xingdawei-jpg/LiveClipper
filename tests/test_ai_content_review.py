from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

content_review = importlib.import_module("content_review")
ai_clipper = importlib.import_module("ai_clipper")


def _inventory(*, duration: float = 10.0, mixed: bool = False) -> list[dict]:
    texts = [
        "穿上以后肩线会往里收，看起来更利落。",
        "肩部黑色编织线把视觉重心向内收。",
        "通勤搭西裤，周末搭牛仔裤都能穿。",
        "面料摸起来细腻，而且贴身不扎。",
    ]
    return [
        {
            "srt_index": index,
            "source": f"V{1 + (index % 2)}" if mixed else "V1",
            "duration_sec": duration,
            "text": text,
        }
        for index, text in enumerate(texts, 1)
    ]


def _review_payload(*, include_unknown: bool = False) -> dict:
    cards = [
        {
            "candidate_id": 1,
            "topic": "版型显瘦",
            "subtopic": "肩线内收",
            "buyer_value": "说明肩宽如何被修饰",
            "evidence_type": "具体效果",
            "evidence_quote": "肩线会往里收",
            "roles": ["effect"],
            "dependency": "independent",
            "quality_tags": ["具体效果", "人群明确"],
            "tier": "main",
        },
        {
            "candidate_id": 2,
            "topic": "工艺细节",
            "subtopic": "肩部编织线",
            "buyer_value": "解释肩线内收的设计原因",
            "evidence_type": "原因解释",
            "evidence_quote": "肩部黑色编织线",
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
            "evidence_type": "场景举例",
            "evidence_quote": "通勤搭西裤，周末搭牛仔裤",
            "roles": ["scene"],
            "dependency": "independent",
            "quality_tags": ["场景清晰"],
            "tier": "main",
        },
        {
            "candidate_id": 4,
            "topic": "面料质感",
            "subtopic": "贴身触感",
            "buyer_value": "说明贴身穿着不扎",
            "evidence_type": "穿着体验",
            "evidence_quote": "贴身不扎",
            "roles": ["product"],
            "dependency": "independent",
            "quality_tags": ["具体体验"],
            "tier": "reserve",
        },
    ]
    if include_unknown:
        cards[0]["public_score"] = 99
    return {
        "cards": cards,
        "hook_pairs": [
            {"hook_id": 1, "followup_id": 2, "topic": "旧合同", "reason": "应被忽略"},
        ],
        "unknown_top_level": "ignored",
    }

class ContentReviewValidationTests(unittest.TestCase):
    def _validate(self, payload: dict, inventory=None, required_sources=None):
        inventory = inventory or _inventory()
        return content_review._validate_bundle(
            payload,
            inventory=inventory,
            cache_key="cache-key",
            candidate_digest="digest",
            category="上衣",
            model="deepseek-v4-flash",
            required_sources=required_sources,
        )

    def test_normal_response_ignores_unknown_fields_and_keeps_real_ids(self) -> None:
        inventory = _inventory()
        original = json.loads(json.dumps(inventory, ensure_ascii=False))
        bundle = self._validate(_review_payload(include_unknown=True), inventory)
        self.assertEqual([card.candidate_id for card in bundle.cards], [1, 2, 3, 4])
        self.assertEqual(len(bundle.hook_pairs), 1)
        self.assertEqual((bundle.hook_pairs[0].hook_id, bundle.hook_pairs[0].followup_id), (1, 2))
        self.assertEqual(inventory, original)
        self.assertNotIn("public_score", bundle.cards[0].to_dict())
        self.assertEqual(bundle.cards[0].evidence_quote, "肩线会往里收")

    def test_plain_feature_list_cannot_reopen_a_fallback_hook_pair(self) -> None:
        inventory = _inventory()
        inventory[0]["text"] = (
            "\u50cf\u8fd9\u79cd\u9886\u578b\u7684\uff0c\u5b83\u6bd4\u8f83\u7ecf\u5178\uff0c"
            "\u53ef\u4ee5\u7acb\u9886\uff0c\u4e5f\u53ef\u4ee5\u7ffb\u9886\u7a7f\u3002"
        )

        bundle = self._validate(_review_payload(), inventory)

        self.assertEqual(bundle.hook_pairs, ())

    def test_neutral_material_evidence_cannot_be_promoted_to_hook_pair(self) -> None:
        inventory = _inventory()
        inventory[0]["text"] = (
            "150克纯棉面料是挺括和柔软度适中，非常刚刚好的那种，"
            "不会让你觉得又硬又塌。"
        )
        inventory[1]["text"] = "你看它属于有型，而且还比较耐皱。"

        bundle = self._validate(_review_payload(), inventory)

        self.assertEqual(bundle.hook_pairs, ())

    def test_hook_package_becomes_director_contract_when_grounded_and_strong(self) -> None:
        payload = _review_payload()
        payload["hook_pairs"] = []
        payload["hook_packages"] = [{
            "hook_id": 1,
            "followup_id": 2,
            "topic": "版型显瘦",
            "reason": "肩部编织线解释视觉重心为什么会向内收",
            "hook_promise": "肩线会往里收",
            "proof_relation": "design_reason",
            "package_complete": True,
            "semantic_signals": ["result"],
            "opening_tier": "B",
        }]

        bundle = self._validate(payload)
        package = bundle.hook_packages[0]

        self.assertEqual(package.hook_promise, "肩线会往里收")
        self.assertEqual(package.proof_relation, "design_reason")
        self.assertTrue(package.package_complete)
        self.assertEqual(package.opening_tier, "B")
        self.assertIn("result", package.semantic_signals)
        self.assertEqual(bundle.hook_pairs, ())
        hook_records, threads, source = bundle.director_hook_contract()
        self.assertEqual(source, "hook_package")
        self.assertEqual(hook_records[0]["hook_id"], 1)
        self.assertEqual(threads[1]["seed_followup_ids"], [2])
        self.assertEqual(set(threads[1]["allowed_followup_ids"]), {2})
        self.assertEqual(bundle.hook_candidate_ids, {1})
        self.assertIn("HookPackage", bundle.director_hint())
        self.assertEqual(bundle.summary("shadow")["hook_package_complete_count"], 1)
        self.assertEqual(bundle.summary("on")["director_hook_contract"], "hook_package")
        self.assertIn("hook_packages", bundle.to_dict())

    def test_tier_c_hook_package_does_not_displace_safe_pair_fallback(self) -> None:
        payload = _review_payload()
        payload["hook_packages"] = [{
            "hook_id": 1,
            "followup_id": 2,
            "topic": "版型显瘦",
            "reason": "肩部编织线解释视觉重心为什么会向内收",
            "hook_promise": "肩线会往里收",
            "proof_relation": "design_reason",
            "package_complete": True,
            "semantic_signals": ["result"],
            "opening_tier": "C",
        }]

        bundle = self._validate(payload)
        hook_records, threads, source = bundle.director_hook_contract()

        self.assertEqual(source, "hook_pair")
        self.assertEqual(hook_records[0]["hook_id"], 1)
        self.assertEqual(threads[1]["seed_followup_ids"], [2])

    def test_hook_pair_and_package_require_executable_segment_durations(self) -> None:
        payload = _review_payload()
        payload["hook_packages"] = [{
            "hook_id": 1,
            "followup_id": 2,
            "topic": "版型显瘦",
            "reason": "肩部编织线解释视觉重心为什么会向内收",
            "hook_promise": "肩线会往里收",
            "proof_relation": "design_reason",
            "package_complete": True,
            "semantic_signals": ["result"],
            "opening_tier": "B",
        }]

        inventory = _inventory(duration=8.1)
        for index, item in enumerate(inventory):
            item["start"] = index * 8.1
            item["end"] = (index + 1) * 8.1
        bundle = self._validate(payload, inventory)

        self.assertEqual(bundle.hook_pairs, ())
        self.assertEqual(bundle.hook_packages, ())

    def test_unrelated_good_selling_point_is_not_a_complete_hook_package(self) -> None:
        inventory = _inventory()
        inventory[0]["text"] = "哇，这件西装真的好帅，像明星机场穿搭。"
        inventory[1]["text"] = "面料摸起来细腻，而且贴身不扎。"
        payload = _review_payload()
        payload["hook_pairs"] = []
        payload["hook_packages"] = [{
            "hook_id": 1,
            "followup_id": 2,
            "topic": "流行趋势",
            "reason": "面料很好",
            "hook_promise": "真的好帅，像明星机场穿搭",
            "proof_relation": "material_evidence",
            "package_complete": True,
            "semantic_signals": ["emotion", "style_projection"],
            "opening_tier": "A",
        }]

        bundle = self._validate(payload, inventory)

        self.assertEqual(bundle.hook_packages, ())

    def test_incomplete_or_ungrounded_hook_package_is_rejected(self) -> None:
        payload = _review_payload()
        payload["hook_pairs"] = []
        payload["hook_packages"] = [{
            "hook_id": 1,
            "followup_id": 2,
            "topic": "版型显瘦",
            "reason": "下一句解释原因",
            "hook_promise": "不存在的营销承诺",
            "proof_relation": "design_reason",
            "package_complete": False,
            "semantic_signals": ["result"],
            "opening_tier": "B",
        }]

        self.assertEqual(self._validate(payload).hook_packages, ())

    def test_delivery_signal_is_an_honest_text_proxy(self) -> None:
        self.assertEqual(
            content_review._textual_delivery_signal("哇，这件西装真的很帅！我的菜真的我的菜。"),
            "textual_high",
        )
        self.assertEqual(
            content_review._textual_delivery_signal("这件上衣肩线向内收，看起来更利落。"),
            "unknown",
        )

    def test_emotional_reaction_can_be_a_diagnostic_package_only_when_proved(self) -> None:
        inventory = _inventory()
        inventory[0]["text"] = "很帅，真的很帅，我的菜真的我的菜。"
        inventory[1]["text"] = "你看这个肩往外走，下面又是松的，整个型特别利落。"
        payload = _review_payload()
        payload["hook_pairs"] = []
        payload["hook_packages"] = [{
            "hook_id": 1,
            "followup_id": 2,
            "topic": "流行趋势",
            "reason": "肩线和廓形直接证明利落风格",
            "hook_promise": "很帅，真的很帅",
            "proof_relation": "identity_projection",
            "package_complete": True,
            "semantic_signals": ["emotion", "style_projection"],
            "opening_tier": "A",
        }]

        bundle = self._validate(payload, inventory)

        self.assertEqual(len(bundle.hook_packages), 1)
        self.assertEqual(bundle.hook_packages[0].opening_tier, "A")
        self.assertEqual(bundle.hook_pairs, ())

    def test_hook_thread_keeps_seed_proof_and_same_topic_extensions(self) -> None:
        payload = _review_payload()
        payload["cards"][2]["topic"] = "版型显瘦"
        payload["cards"][2]["subtopic"] = "肩线修饰后的搭配效果"
        bundle = self._validate(payload)

        thread = bundle.hook_thread_contract()[1]

        self.assertEqual(thread["topic"], "版型显瘦")
        self.assertEqual(thread["seed_followup_ids"], [2])
        self.assertEqual(set(thread["allowed_followup_ids"]), {2, 3})

    def test_evidence_grounding_tolerates_only_punctuation_and_spacing(self) -> None:
        source = "通勤搭西裤，周末搭牛仔裤都能穿。"
        self.assertEqual(
            content_review._grounded_evidence_quote("通勤搭西裤 周末搭牛仔裤", source),
            "通勤搭西裤，周末搭牛仔裤",
        )
        self.assertEqual(
            content_review._grounded_evidence_quote("度假搭西裤周末搭牛仔裤", source),
            "",
        )

    def test_compact_array_response_is_supported(self) -> None:
        payload = _review_payload()
        payload["cards"] = [
            [
                card["candidate_id"], card["topic"], card["subtopic"],
                card["buyer_value"], card["evidence_type"], card["evidence_quote"], card["roles"],
                card["dependency"], card["quality_tags"], card["tier"],
            ]
            for card in payload["cards"]
        ]
        payload["hook_pairs"] = [
            [pair["hook_id"], pair["followup_id"], pair["topic"], pair["reason"]]
            for pair in payload["hook_pairs"]
        ]
        bundle = self._validate(payload)
        self.assertEqual([card.candidate_id for card in bundle.cards], [1, 2, 3, 4])
        self.assertEqual(len(bundle.hook_pairs), 1)

    def test_invalid_hook_pairs_are_ignored(self) -> None:
        payload = _review_payload()
        payload["hook_pairs"] = [
            {"hook_id": 99, "followup_id": 1},
        ]
        bundle = self._validate(payload)
        self.assertEqual(bundle.hook_pairs, ())
        self.assertTrue(all("hook" not in card.roles for card in bundle.cards))

    def test_hook_pairs_must_reference_real_reviewed_cards_and_be_safe(self) -> None:
        inventory = _inventory()
        inventory[0]["text"] = "我一米六体重98穿S码"
        payload = _review_payload()
        payload["hook_pairs"] = [
            {"hook_id": 1, "followup_id": 2, "topic": "版型显瘦", "reason": "个人尺码"},
            {"hook_id": 2, "followup_id": 2, "topic": "版型显瘦", "reason": "同一编号"},
            {"hook_id": 2, "followup_id": 3, "topic": "版型显瘦", "reason": "有效"},
            {"hook_id": 2, "followup_id": 3, "topic": "版型显瘦", "reason": "重复"},
            {"hook_id": 2, "followup_id": 99, "topic": "版型显瘦", "reason": "未知编号"},
        ]
        bundle = self._validate(payload, inventory)
        # Candidate #2 is safe body material, but does not carry an opening
        # mechanism by itself. Safety alone must not create a fallback Hook.
        self.assertEqual(bundle.hook_pairs, ())
    def test_invalid_and_duplicate_ids_never_enter_cards(self) -> None:
        payload = _review_payload()
        payload["cards"].insert(1, dict(payload["cards"][0]))
        payload["cards"].insert(2, {
            **payload["cards"][0], "candidate_id": 99,
        })
        bundle = self._validate(payload)
        ids = [card.candidate_id for card in bundle.cards]
        self.assertEqual(ids, [1, 2, 3, 4])
        self.assertNotIn(99, ids)

    def test_candidate_not_present_in_safe_inventory_cannot_reenter(self) -> None:
        payload = _review_payload()
        payload["cards"].append({**payload["cards"][0], "candidate_id": 99})
        bundle = self._validate(payload)
        self.assertNotIn(99, bundle.allowed_candidate_ids)

    def test_structural_transcript_fragment_cannot_enter_review_pool(self) -> None:
        self.assertTrue(content_review._reviewable_candidate_text("\u54c7\uff0c\u8fd9\u53e5\u6709\u5b8c\u6574\u7684\u5546\u54c1\u4fe1\u606f"))
        self.assertFalse(content_review._reviewable_candidate_text("X, broken transcript fragment"))
        self.assertFalse(
            content_review._reviewable_hook_text(
                "很 duang 的这个裤子它不是纯亚麻，它是天丝亚麻。"
            )
        )
        self.assertFalse(content_review._reviewable_candidate_text("很 duang 的"))
        self.assertFalse(
            content_review._reviewable_candidate_text(
                "我自己可能会这样穿，背面看一下，不挑人很舒服。"
            )
        )
        self.assertFalse(content_review._reviewable_candidate_text("\u9762\u6599\u5f88\u8584\uff0c\u522b\u5435\u522b\u5435"))
        self.assertFalse(content_review._reviewable_hook_text("\u7136\u540e\u8fd9\u4ef6\u8863\u670d\u5f88\u663e\u7626"))
        self.assertTrue(content_review._reviewable_hook_text("\u8fd9\u4ef6\u8863\u670d\u7a7f\u4e0a\u5f88\u663e\u7626"))
        malformed_hook = "\u590f\u5929\u51fa\u6bdb\u8863\uff0c\u8fd9\u4e2a\u548c\u6bdb\u8863\u3002"
        self.assertTrue(content_review._reviewable_candidate_text(malformed_hook))
        self.assertFalse(content_review._reviewable_hook_text(malformed_hook))
        self.assertFalse(content_review._reviewable_hook_text("\u4e0d\u642d\u8fb9\u7684\uff0c\u5b83\u5176\u5b9e\u662f\u7eb1\u7ebf\u3002"))

    def test_chatty_doubt_opening_cannot_be_a_reviewed_hook(self) -> None:
        self.assertFalse(
            content_review._reviewable_hook_text(
                "\u5582\u3002\u6211\u5176\u5b9e\u4e5f\u5f88\u8d28\u7591\u7684\uff0c\u4f46\u662f\u6211\u8ddf\u4f60\u4eec\u8bb2\u3002"
            )
        )
        self.assertFalse(
            content_review._reviewable_hook_text(
                "\u4f60\u4eec\u7ec6\u54c1\u4e00\u4e0b\uff0c\u4f60\u770b\u5b83\u6240\u6709\u7684\u5305\u7ebf\u7f1d\u7ebf\u3002"
            )
        )
        self.assertTrue(
            content_review._reviewable_hook_text(
                "\u80a9\u7ebf\u5411\u5185\u6536\uff0c\u7a7f\u4e0a\u540e\u770b\u8d77\u6765\u66f4\u5229\u843d\u3002"
            )
        )

        inventory = _inventory()
        inventory[3]["text"] = "X, broken transcript fragment"
        payload = _review_payload()
        payload["hook_pairs"] = [
            {"hook_id": 1, "followup_id": 2},
            {"hook_id": 2, "followup_id": 3},
            {"hook_id": 3, "followup_id": 1},
        ]
        bundle = self._validate(payload, inventory)
        self.assertNotIn(4, bundle.allowed_candidate_ids)

    def test_unusable_transcript_price_and_body_measurement_never_enter_cards(self) -> None:
        inventory = _inventory()
        inventory[0]["text"] = "\u4ed6\u9ebb\u5dfe\u8ddf\u80a0\u6e29\u67d4\u8f6f\u4e86\u7136\u540e\u8fd8\u6709\u4e00\u70b9"
        inventory[1]["text"] = "\u4f60\u5b50\u8eab\u9ad8170\uff0c\u4f53\u91cd105\uff0c\u4e0a\u8eab\u7684\u4e1c\u897f\u770b\u4e00\u4e0b\u5427"
        inventory[2]["text"] = "\u9762\u6599\u6210\u672c210\uff0c\u6240\u4ee5\u4e0d\u4fbf\u5b9c"
        inventory[3]["text"] = "\u80a9\u7ebf\u5411\u5185\u6536\uff0c\u89c6\u89c9\u66f4\u5229\u843d"

        bundle = self._validate(_review_payload(), inventory)

        self.assertEqual(bundle.allowed_candidate_ids, {4})
        self.assertEqual([card.candidate_id for card in bundle.cards], [4])

    def test_unreviewed_safe_candidate_is_not_promoted_to_a_reserve_card_for_duration(self) -> None:
        inventory = _inventory()
        inventory.append(
            {
                "srt_index": 5,
                "source": "V1",
                "duration_sec": 10.0,
                "text": "\u4f60\u4eec\u6765\u770b\u4e00\u4e0b\u5427\uff0c\u4eca\u5929\u6d3b\u52a8\u8fd8\u4f1a\u6709\u7684\u3002",
            }
        )

        bundle = self._validate(_review_payload(), inventory)

        self.assertEqual([card.candidate_id for card in bundle.cards], [1, 2, 3, 4])
        self.assertNotIn(5, bundle.allowed_candidate_ids)

    def test_short_grounded_review_pool_remains_a_valid_quality_contract(self) -> None:
        inventory = _inventory(duration=2.0)
        payload = _review_payload()
        payload["cards"] = [payload["cards"][0]]

        bundle = self._validate(payload, inventory)

        self.assertEqual(bundle.allowed_candidate_ids, {1})
        self.assertEqual(bundle.retained_duration, 2.0)

    def test_missing_or_ungrounded_quote_binds_original_and_keeps_ai_semantics(self) -> None:
        inventory = _inventory(duration=60.0)
        payload = _review_payload()
        payload["cards"][0]["topic"] = "\u573a\u666f\u642d\u914d"
        payload["cards"][0]["subtopic"] = "\u5ea6\u5047\u6c1b\u56f4"
        payload["cards"][0]["buyer_value"] = "\u63cf\u8ff0\u5ea6\u5047\u573a\u666f"
        payload["cards"][0]["evidence_quote"] = "\u539f\u5b57\u5e55\u4e2d\u4e0d\u5b58\u5728"
        bundle = self._validate(payload, inventory)
        bound = bundle.card_map()[1]
        self.assertEqual(bound.evidence_quote, inventory[0]["text"][:120])
        self.assertEqual(bound.topic, "\u573a\u666f\u642d\u914d")
        self.assertEqual(bound.subtopic, "\u5ea6\u5047\u6c1b\u56f4")
        self.assertEqual(bound.buyer_value, "\u63cf\u8ff0\u5ea6\u5047\u573a\u666f")
        self.assertIn("\u539f\u6587\u7ed1\u5b9a", bound.quality_tags)
    def test_all_missing_quotes_are_bound_to_original_candidates(self) -> None:
        inventory = _inventory(duration=60.0)
        payload = _review_payload()
        for card in payload["cards"]:
            card.pop("evidence_quote", None)
        bundle = self._validate(payload, inventory)
        by_id = {item["srt_index"]: item["text"] for item in inventory}
        self.assertEqual(bundle.allowed_candidate_ids, set(by_id))
        self.assertTrue(
            all(card.evidence_quote in by_id[card.candidate_id] for card in bundle.cards)
        )
        self.assertTrue(
            all("\u539f\u6587\u7ed1\u5b9a" in card.quality_tags for card in bundle.cards)
        )
    def test_compact_cards_without_quotes_keep_distinct_ai_semantics(self) -> None:
        inventory = [
            {
                "srt_index": index,
                "source": "V1",
                "start": float(index * 10),
                "end": float(index * 10 + 10),
                "duration_sec": 10.0,
                "text": f"\u8fd9\u4ef6\u8863\u670d\u7b2c{index}\u5904\u7248\u578b\u7ec6\u8282\u80fd\u4fee\u9970\u80a9\u578b\u548c\u8eab\u5f62",
                "safe": True,
            }
            for index in range(1, 31)
        ]
        payload = {
            "cards": [
                [
                    index, "\u7248\u578b\u663e\u7626", f"\u7ec6\u8282{index}", f"\u8d2d\u4e70\u4ef7\u503c{index}",
                    "\u5177\u4f53\u6548\u679c", ["effect"], "independent", ["\u5177\u4f53\u6548\u679c"], "main",
                ]
                for index in range(1, 11)
            ]
        }
        bundle = self._validate(payload, inventory)
        self.assertEqual(bundle.allowed_candidate_ids, set(range(1, 11)))
        self.assertEqual(bundle.retained_duration, 100.0)
        self.assertTrue(all("\u539f\u6587\u7ed1\u5b9a" in card.quality_tags for card in bundle.cards))
    def test_mixed_source_requirements_use_original_source_ids(self) -> None:
        inventory = _inventory(mixed=True)
        bundle = self._validate(
            _review_payload(), inventory, required_sources={"V1": 1, "V2": 1}
        )
        by_id = {item["srt_index"]: item["source"] for item in inventory}
        self.assertEqual({by_id[card.candidate_id] for card in bundle.cards}, {"V1", "V2"})

    def test_missing_mixed_source_fails_closed_to_caller(self) -> None:
        inventory = _inventory(mixed=True)
        payload = _review_payload()
        with self.assertRaisesRegex(content_review.ContentReviewError, "缺少混剪来源"):
            self._validate(payload, inventory, required_sources={"V1": 1, "V3": 1})


    def test_topic_aliases_are_folded_into_stable_families(self) -> None:
        self.assertEqual(
            content_review._normalize_topic("\u989c\u8272\u663e\u767d"),
            "\u989c\u8272\u6c1b\u56f4",
        )
        self.assertEqual(
            content_review._normalize_topic("\u9762\u6599\u6210\u5206"),
            "\u9762\u6599\u8d28\u611f",
        )
        self.assertEqual(
            content_review._normalize_topic("\u6301\u5986\u6548\u679c"),
            "\u6301\u5986\u6548\u679c",
        )

    def test_apparel_taxonomy_separates_try_on_style_and_actual_trend(self) -> None:
        self.assertEqual(content_review._normalize_topic("\u4e0a\u8eab\u53cd\u5dee"), "\u4e0a\u8eab\u6548\u679c")
        self.assertEqual(content_review._normalize_topic("\u5b66\u9662\u7f8e\u5f0f\u98ce\u683c"), "\u98ce\u683c\u5b9a\u4f4d")
        self.assertEqual(content_review._normalize_topic("\u4eca\u5e74\u6d41\u884c\u5b66\u9662\u98ce"), "\u6d41\u884c\u8d8b\u52bf")
        self.assertEqual(
            content_review._topic_from_candidate_text("\u6302\u7740\u5e73\u5e73\u65e0\u5947\uff0c\u4e0a\u8eab\u4e4b\u540e\u6574\u4e2a\u4eba\u5c31\u7cbe\u81f4\u4e86\u3002"),
            "\u4e0a\u8eab\u6548\u679c",
        )
        self.assertEqual(
            content_review._topic_from_candidate_text("\u7f8e\u5f0f\u5b66\u9662\u611f\u5f88\u4fcf\u76ae\uff0c\u65e5\u5e38\u7a7f\u4e5f\u4e0d\u5938\u5f20\u3002"),
            "\u98ce\u683c\u5b9a\u4f4d",
        )
    def test_content_card_reconciles_decisive_apparel_topic_conflicts(self) -> None:
        def normalize(reported_topic: str, text: str, subtopic: str = ""):
            return content_review._normalize_card(
                {
                    "candidate_id": 1,
                    "topic": reported_topic,
                    "subtopic": subtopic,
                    "buyer_value": "\u5177\u4f53\u5356\u70b9",
                    "evidence_type": "\u5b9e\u6d4b\u8bc1\u636e",
                    "evidence_quote": text,
                    "roles": ["effect"],
                    "dependency": "independent",
                    "quality_tags": ["\u5177\u4f53\u6548\u679c"],
                    "tier": "main",
                },
                {1},
                {1: {"text": text}},
            )

        self.assertEqual(
            normalize("\u7a7f\u7740\u4f53\u9a8c", "\u6302\u7740\u5f88\u666e\u901a\uff0c\u4e0a\u8eab\u540e\u53cd\u800c\u5f88\u7cbe\u81f4").topic,
            "\u4e0a\u8eab\u6548\u679c",
        )
        self.assertEqual(
            normalize("\u6d41\u884c\u8d8b\u52bf", "\u8fd9\u4ef6\u4e0d\u4e0a\u8eab\u7a7f\uff0c\u4f60\u4e0d\u4f1a\u77e5\u9053\u5b83\u7684\u9b45\u529b").topic,
            "\u4e0a\u8eab\u6548\u679c",
        )
        self.assertEqual(
            normalize("\u6d41\u884c\u8d8b\u52bf", "\u8fd9\u6761\u88d9\u5b50\u662f\u7f8e\u5f0f\u5b66\u9662\u611f\uff0c\u7a7f\u8d77\u6765\u5f88\u51cf\u9f84").topic,
            "\u98ce\u683c\u5b9a\u4f4d",
        )
        self.assertEqual(
            normalize("\u7a7f\u7740\u4f53\u9a8c", "\u8fd9\u4e2a\u6b3e\u5f88\u8584\uff0c\u590f\u5929\u5230\u65e9\u79cb\u90fd\u80fd\u7a7f").topic,
            "\u573a\u666f\u642d\u914d",
        )
        self.assertEqual(
            normalize("\u98ce\u683c\u5b9a\u4f4d", "\u4eca\u5e74\u6d41\u884c\u7684\u5b66\u9662\u98ce\u5c31\u662f\u8fd9\u79cd").topic,
            "\u6d41\u884c\u8d8b\u52bf",
        )
        self.assertEqual(
            normalize("\u6d41\u884c\u8d8b\u52bf", "\u7a7f\u4e0a\u663e\u5f97\u4eba\u5f88\u9ad8\u667a\u5546\u9ad8\u5b66\u5386").topic,
            "\u98ce\u683c\u5b9a\u4f4d",
        )
        self.assertEqual(
            normalize("\u7a7f\u7740\u4f53\u9a8c", "\u4e0a\u8eab\u5f88\u8212\u670d\uff0c\u900f\u6c14\u4e0d\u95f7").topic,
            "\u7a7f\u7740\u4f53\u9a8c",
        )

    def test_legacy_style_words_are_not_retained_as_trend_keywords(self) -> None:
        migrated = ai_clipper._migrate_legacy_trend_style_keywords({
            "\u6d41\u884c\u8d8b\u52bf": ["\u6d41\u884c", "\u97e9\u7cfb", "\u5c0f\u9999\u98ce", "\u8d8b\u52bf"],
        })
        self.assertEqual(migrated["\u6d41\u884c\u8d8b\u52bf"], ["\u6d41\u884c", "\u8d8b\u52bf"])
        self.assertEqual(migrated["\u98ce\u683c\u5b9a\u4f4d"], ["\u97e9\u7cfb", "\u5c0f\u9999\u98ce"])
    def test_all_main_response_is_retiered_by_topic_and_source(self) -> None:
        inventory = [
            {
                "srt_index": index,
                "source": f"V{1 + index % 2}",
                "duration_sec": 8.0,
                "text": f"\u8fd9\u662f\u7b2c{index}\u6761\u5b8c\u6574\u7684\u5177\u4f53\u5546\u54c1\u4fe1\u606f",
            }
            for index in range(1, 13)
        ]
        topics = [
            "\u989c\u8272\u663e\u767d", "\u989c\u8272\u63a8\u8350", "\u989c\u8272\u8d28\u611f",
            "\u4eae\u8272\u6548\u679c", "\u80a4\u8272\u63d0\u4eae", "\u8272\u8c03\u6c1b\u56f4",
            "\u9762\u6599\u6210\u5206", "\u9762\u6599\u624b\u611f", "\u9762\u6599\u539a\u8584",
            "\u83b1\u8d5b\u5c14\u542b\u91cf", "\u4eb2\u80a4\u900f\u6c14", "\u9762\u6599\u5782\u5760",
        ]
        payload = {
            "cards": [
                [
                    index, topics[index - 1], f"\u5b50\u4e3b\u9898{index}",
                    f"\u5177\u4f53\u8d2d\u4e70\u4ef7\u503c{index}", "\u5177\u4f53\u8bc1\u636e",
                    f"\u7b2c{index}\u6761\u5b8c\u6574\u7684\u5177\u4f53\u5546\u54c1\u4fe1\u606f",
                    ["evidence"], "independent", ["\u5177\u4f53\u6548\u679c"], "main",
                ]
                for index in range(1, 13)
            ],
            "hook_pairs": [
                [1, 2, "\u989c\u8272\u663e\u767d", "\u627f\u63a5"],
                [3, 4, "\u989c\u8272\u63a8\u8350", "\u627f\u63a5"],
                [7, 8, "\u9762\u6599\u6210\u5206", "\u627f\u63a5"],
                [9, 10, "\u9762\u6599\u624b\u611f", "\u627f\u63a5"],
            ],
        }
        bundle = self._validate(
            payload, inventory, required_sources={"V1": 1, "V2": 1}
        )
        self.assertEqual(len(bundle.cards), 12)
        self.assertGreater(sum(card.tier == "reserve" for card in bundle.cards), 0)
        main_counts = {}
        source_topic_counts = {}
        source_by_id = {item["srt_index"]: item["source"] for item in inventory}
        for card in bundle.cards:
            if card.tier != "main":
                continue
            main_counts[card.topic] = main_counts.get(card.topic, 0) + 1
            key = (source_by_id[card.candidate_id], card.topic)
            source_topic_counts[key] = source_topic_counts.get(key, 0) + 1
        self.assertTrue(all(count <= 4 for count in main_counts.values()))
        self.assertTrue(all(count <= 2 for count in source_topic_counts.values()))
        # This fixture intentionally contains generic body text only. It still
        # exercises card tier balancing, but must not manufacture HookPairs.
        self.assertEqual(bundle.hook_pairs, ())
class ContentReviewSubjectPriorityTests(unittest.TestCase):
    def _validate(self, payload: dict, inventory=None, required_sources=None):
        return content_review._validate_bundle(
            payload,
            inventory=inventory or _inventory(),
            cache_key="cache-key",
            candidate_digest="digest",
            category="\u4e0a\u8863",
            model="deepseek-v4-flash",
            required_sources=required_sources,
        )

    def test_subject_relation_is_grounded_demoted_and_blocks_other_product_hook(self) -> None:
        inventory = [
            {
                "srt_index": 1,
                "source": "V1",
                "duration_sec": 5.0,
                "text": "这件上衣的肩线向内收，穿上整个人更利落。",
            },
            {
                "srt_index": 2,
                "source": "V1",
                "duration_sec": 5.0,
                "text": "这条裤子的腰头很平整，通勤穿不勒肚子。",
            },
        ]
        payload = {
            "cards": [
                {
                    "candidate_id": 1,
                    "topic": "版型显瘦",
                    "subtopic": "肩线内收",
                    "buyer_value": "解释上衣肩线如何收窄视觉",
                    "evidence_type": "原因解释",
                    "evidence_quote": "肩线向内收",
                    "roles": ["effect", "evidence"],
                    "dependency": "independent",
                    "quality_tags": ["具体效果", "原因解释"],
                    "tier": "main",
                    "primary_subject": "上衣",
                    "target_relation": "primary",
                    "subject_evidence": "这件上衣",
                },
                {
                    "candidate_id": 2,
                    "topic": "版型显瘦",
                    "subtopic": "腰头平整",
                    "buyer_value": "说明裤子腰头不勒",
                    "evidence_type": "具体效果",
                    "evidence_quote": "不勒肚子",
                    "roles": ["effect"],
                    "dependency": "independent",
                    "quality_tags": ["具体效果"],
                    "tier": "main",
                    "primary_subject": "裤子",
                    "target_relation": "other",
                    "subject_evidence": "这条裤子",
                },
            ],
            "hook_pairs": [[2, 1, "版型显瘦", "错误地把裤子当上衣Hook"]],
        }

        bundle = content_review._validate_bundle(
            payload,
            inventory=inventory,
            cache_key="cache-key",
            candidate_digest="digest",
            category="上衣",
            model="deepseek-v4-flash",
            required_sources=None,
            main_product="上衣",
        )

        cards = bundle.card_map()
        self.assertEqual(cards[1].target_relation, "primary")
        self.assertEqual(cards[2].target_relation, "other")
        self.assertEqual(cards[2].tier, "reserve")
        self.assertEqual(bundle.hook_pairs, ())
        self.assertLess(
            bundle.director_hint().index("#01"),
            bundle.director_hint().index("#02"),
        )
        self.assertEqual(bundle.summary("on")["primary_subject_count"], 1)
        self.assertEqual(bundle.summary("on")["other_subject_count"], 1)

    def test_ungrounded_subject_metadata_is_downgraded_without_dropping_safe_card(self) -> None:
        payload = _review_payload()
        payload["cards"] = [payload["cards"][0]]
        payload["cards"][0].update({
            "primary_subject": "裙子",
            "target_relation": "other",
            "subject_evidence": "肩线会往里收",
        })
        bundle = self._validate(payload)

        self.assertEqual(bundle.allowed_candidate_ids, {1})
        self.assertEqual(bundle.cards[0].target_relation, "unknown")
        self.assertEqual(bundle.cards[0].primary_subject, "")
        self.assertEqual(bundle.cards[0].subject_evidence, "")

    def test_compact_card_subject_fields_are_backward_compatible_and_grounded(self) -> None:
        payload = _review_payload()
        card = payload["cards"][0]
        payload["cards"] = [[
            card["candidate_id"], card["topic"], card["subtopic"],
            card["buyer_value"], card["evidence_type"], card["evidence_quote"],
            card["roles"], card["dependency"], card["quality_tags"], card["tier"],
            "肩线", "primary", "肩线会往里收",
        ]]
        payload["hook_pairs"] = []
        bundle = self._validate(payload)

        self.assertEqual(bundle.cards[0].primary_subject, "肩线")
        self.assertEqual(bundle.cards[0].target_relation, "primary")
        self.assertEqual(bundle.cards[0].subject_evidence, "肩线会往里收")


class ContentReviewDirectorContractTests(unittest.TestCase):
    def test_completed_empty_hook_review_rejects_a_director_hook_but_keeps_body(self) -> None:
        entries = [
            (0.0, 3.0, "150克纯棉面料挺括柔软适中，不会又硬又塌。"),
            (3.0, 6.0, "它属于有型，而且还比较耐皱。"),
        ]
        response = json.dumps([
            {"clip_type": "hook", "srt_indices": [1]},
            {"clip_type": "product", "srt_indices": [2]},
        ])

        clips = ai_clipper._parse_ai_response(
            response,
            None,
            entries,
            require_srt_indices=True,
            # An empty set differs from None: content review completed and
            # explicitly left no verified Hook source for this task.
            allowed_hook_indices=set(),
            allowed_candidate_indices={1, 2},
        )

        self.assertEqual([clip[0] for clip in clips], ["product"])
        self.assertEqual(clips[0][1], entries[1][2])

    def test_review_hook_pair_requires_its_immediate_followup(self) -> None:
        entries = [
            (0.0, 2.0, "\u8fd9\u4ef6\u4e0a\u8eab\u80a9\u7ebf\u4f1a\u5411\u5185\u6536\uff0c\u770b\u8d77\u6765\u66f4\u5229\u843d\u3002"),
            (2.0, 4.0, "\u80a9\u90e8\u7684\u9ed1\u8272\u7f16\u7ec7\u7ebf\u628a\u89c6\u89c9\u91cd\u5fc3\u5411\u5185\u6536\u3002"),
            (4.0, 6.0, "\u901a\u52e4\u642d\u897f\u88c5\uff0c\u5468\u672b\u642d\u725b\u4ed4\u88e4\u90fd\u80fd\u7a7f\u3002"),
        ]
        invalid = json.dumps([
            {"clip_type": "hook", "srt_indices": [1]},
            {"clip_type": "product", "srt_indices": [3]},
        ])
        self.assertEqual(
            ai_clipper._parse_ai_response(
                invalid,
                None,
                entries,
                require_srt_indices=True,
                allowed_hook_indices={1},
                allowed_candidate_indices={1, 2, 3},
                required_hook_followups={1: 2},
            ),
            [],
        )
        valid = json.dumps([
            {"clip_type": "hook", "srt_indices": [1]},
            {"clip_type": "product", "srt_indices": [2]},
        ])
        clips = ai_clipper._parse_ai_response(
            valid,
            None,
            entries,
            require_srt_indices=True,
            allowed_hook_indices={1},
            allowed_candidate_indices={1, 2, 3},
            required_hook_followups={1: 2},
        )
        self.assertEqual([clip[0] for clip in clips], ["hook", "product"])

    def test_review_hook_pair_accepts_any_verified_followup_for_the_same_hook(self) -> None:
        entries = [
            (0.0, 2.0, "这条裙子不怎么挑人，肩宽和小肚子都能修饰。"),
            (2.0, 4.0, "有袖子，不会显得大手臂很粗。"),
            (4.0, 6.0, "下摆留有余量，肚子大也能自然遮住。"),
            (6.0, 8.0, "面料摸起来柔软，贴身不会扎。"),
        ]
        contract = {1: {2, 3}}
        for followup_id in (2, 3):
            with self.subTest(followup_id=followup_id):
                response = json.dumps([
                    {"clip_type": "hook", "srt_indices": [1]},
                    {"clip_type": "product", "srt_indices": [followup_id]},
                ])
                clips = ai_clipper._parse_ai_response(
                    response,
                    None,
                    entries,
                    require_srt_indices=True,
                    allowed_hook_indices={1},
                    allowed_candidate_indices={1, 2, 3, 4},
                    required_hook_followups=contract,
                )
                self.assertEqual([clip[0] for clip in clips], ["hook", "product"])

        logs: list[str] = []
        invalid = json.dumps([
            {"clip_type": "hook", "srt_indices": [1]},
            {"clip_type": "product", "srt_indices": [4]},
        ])
        self.assertEqual(
            ai_clipper._parse_ai_response(
                invalid,
                logs.append,
                entries,
                require_srt_indices=True,
                allowed_hook_indices={1},
                allowed_candidate_indices={1, 2, 3, 4},
                required_hook_followups=contract,
            ),
            [],
        )
        self.assertTrue(any("要求紧接 #2 或 #3" in line for line in logs))

    def test_hook_thread_allows_a_same_topic_extension_not_just_seed_proof(self) -> None:
        entries = [
            (0.0, 2.0, "这条裙子肩宽和小肚子都能修饰，看起来更利落。"),
            (2.0, 4.0, "肩部黑色编织线把视觉重心向内收。"),
            (4.0, 6.0, "下摆留有余量，肚子大也能自然遮住。"),
        ]
        response = json.dumps([
            {"clip_type": "hook", "srt_indices": [1]},
            {"clip_type": "product", "srt_indices": [3]},
        ])

        clips = ai_clipper._parse_ai_response(
            response,
            None,
            entries,
            require_srt_indices=True,
            allowed_hook_indices={1},
            allowed_candidate_indices={1, 2, 3},
            hook_followup_threads={
                1: {
                    "topic": "版型显瘦",
                    "seed_followup_ids": [2],
                    "allowed_followup_ids": [2, 3],
                }
            },
        )

        self.assertEqual([clip[0] for clip in clips], ["hook", "product"])
        self.assertEqual([clip[1] for clip in clips], [entries[0][2], entries[2][2]])

    def test_hook_thread_mismatch_is_a_quality_warning_not_a_parse_failure(self) -> None:
        entries = [
            (0.0, 2.0, "这条裙子肩宽和小肚子都能修饰，看起来更利落。"),
            (2.0, 4.0, "肩部黑色编织线把视觉重心向内收。"),
            (4.0, 6.0, "面料摸起来柔软，贴身不会扎。"),
        ]
        response = json.dumps([
            {"clip_type": "hook", "srt_indices": [1]},
            {"clip_type": "product", "srt_indices": [3]},
        ])
        logs: list[str] = []

        clips = ai_clipper._parse_ai_response(
            response,
            logs.append,
            entries,
            require_srt_indices=True,
            allowed_hook_indices={1},
            allowed_candidate_indices={1, 2, 3},
            hook_followup_threads={
                1: {
                    "topic": "版型显瘦",
                    "seed_followup_ids": [2],
                    "allowed_followup_ids": [2],
                }
            },
        )

        self.assertEqual([clip[0] for clip in clips], ["hook", "product"])
        self.assertTrue(any("Hook主题承接提示" in line for line in logs))


class ContentReviewCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.user_dir = Path(self.temp_dir.name)
        self.user_patch = mock.patch.object(content_review, "_user_data_dir", return_value=self.user_dir)
        self.user_patch.start()
        self.addCleanup(self.user_patch.stop)

    @staticmethod
    def _response() -> str:
        return json.dumps(_review_payload(), ensure_ascii=False)

    def _review(self, **overrides):
        kwargs = {
            "api_key": "key",
            "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash",
            "inventory": _inventory(),
            "candidate_digest": "digest",
            "category": "上衣",
            "main_product": "拼接长袖T恤",
            "avoid": ["不要价格"],
        }
        kwargs.update(overrides)
        return content_review.review_candidates(**kwargs)

    def test_format_error_retries_once_and_only_caches_success(self) -> None:
        with mock.patch.object(
            content_review, "_post_review_request", side_effect=["not-json", self._response()]
        ) as request:
            bundle = self._review()
        self.assertEqual(request.call_count, 2)
        self.assertFalse(bundle.cache_hit)
        self.assertTrue((content_review.content_review_cache_dir() / f"{bundle.cache_key}.json").is_file())

    def test_missing_evidence_quote_does_not_retry(self) -> None:
        payload = _review_payload()
        for card in payload["cards"]:
            card.pop("evidence_quote", None)
        response = json.dumps(payload, ensure_ascii=False)
        with mock.patch.object(content_review, "_post_review_request", return_value=response) as request:
            bundle = self._review(inventory=_inventory(duration=60.0))
        self.assertEqual(request.call_count, 1)
        self.assertTrue(
            all("\u539f\u6587\u7ed1\u5b9a" in card.quality_tags for card in bundle.cards)
        )
    def test_cache_hit_skips_review_api_for_changed_preference_or_duration(self) -> None:
        with mock.patch.object(content_review, "_post_review_request", return_value=self._response()) as request:
            first = self._review()
            second = self._review()
        self.assertEqual(request.call_count, 1)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)

    def test_empty_broad_review_gets_one_focused_hook_pair_repair_and_caches_it(self) -> None:
        broad_payload = _review_payload()
        broad_payload["hook_pairs"] = []
        with mock.patch.object(
            content_review,
            "_post_review_request",
            return_value=json.dumps(broad_payload, ensure_ascii=False),
        ):
            bundle = self._review()

        repair_payload = {"hook_pairs": [[1, 2, "版型显瘦", "下一句解释肩线内收"]]}
        with mock.patch.object(
            content_review,
            "_post_review_request",
            return_value=json.dumps(repair_payload, ensure_ascii=False),
        ) as request:
            repaired = content_review.repair_hook_pairs(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                inventory=_inventory(),
                bundle=bundle,
                category="上衣",
                main_product="T恤",
            )
        self.assertEqual(request.call_count, 1)
        self.assertTrue(repaired.hook_pair_reviewed)
        self.assertEqual(
            [(pair.hook_id, pair.followup_id) for pair in repaired.hook_pairs],
            [(1, 2)],
        )

        with mock.patch.object(content_review, "_post_review_request") as request:
            cached = self._review()
            repeat = content_review.repair_hook_pairs(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                inventory=_inventory(),
                bundle=cached,
                category="上衣",
                main_product="T恤",
            )
        request.assert_not_called()
        self.assertTrue(cached.cache_hit)
        self.assertTrue(repeat.hook_pair_reviewed)
        self.assertEqual([(pair.hook_id, pair.followup_id) for pair in repeat.hook_pairs], [(1, 2)])

    def test_empty_focused_repair_is_cached_without_repeating_the_api_call(self) -> None:
        broad_payload = _review_payload()
        broad_payload["hook_pairs"] = []
        with mock.patch.object(
            content_review,
            "_post_review_request",
            return_value=json.dumps(broad_payload, ensure_ascii=False),
        ):
            bundle = self._review()
        with mock.patch.object(
            content_review,
            "_post_review_request",
            return_value=json.dumps({"hook_pairs": []}),
        ) as request:
            repaired = content_review.repair_hook_pairs(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                inventory=_inventory(),
                bundle=bundle,
            )
        self.assertEqual(request.call_count, 1)
        self.assertTrue(repaired.hook_pair_reviewed)
        self.assertEqual(repaired.hook_pairs, ())
        with mock.patch.object(content_review, "_post_review_request") as request:
            repeated = content_review.repair_hook_pairs(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                inventory=_inventory(),
                bundle=repaired,
            )
        request.assert_not_called()
        self.assertEqual(repeated, repaired)

    def test_cache_key_changes_for_contract_category_product_avoid_or_model(self) -> None:
        base = content_review.build_cache_key("d", "上衣", "T恤", ["价格"], "m")
        variants = {
            content_review.build_cache_key("d2", "上衣", "T恤", ["价格"], "m"),
            content_review.build_cache_key("d", "裤子", "T恤", ["价格"], "m"),
            content_review.build_cache_key("d", "上衣", "衬衫", ["价格"], "m"),
            content_review.build_cache_key("d", "上衣", "T恤", ["关注"], "m"),
            content_review.build_cache_key("d", "上衣", "T恤", ["价格"], "m2"),
        }
        self.assertEqual(len(variants), 5)
        self.assertNotIn(base, variants)

    def test_corrupt_cache_is_ignored_and_replaced(self) -> None:
        key = content_review.build_cache_key(
            "digest", "上衣", "拼接长袖T恤", ["不要价格"], "deepseek-v4-flash"
        )
        path = content_review.content_review_cache_dir() / f"{key}.json"
        path.parent.mkdir(parents=True)
        path.write_text("{broken", encoding="utf-8")
        with mock.patch.object(content_review, "_post_review_request", return_value=self._response()) as request:
            bundle = self._review()
        self.assertEqual(request.call_count, 1)
        self.assertFalse(bundle.cache_hit)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], content_review.CONTENT_REVIEW_VERSION)

    def test_atomic_write_leaves_no_temp_file(self) -> None:
        with mock.patch.object(content_review, "_post_review_request", return_value=self._response()):
            self._review()
        self.assertEqual(list(content_review.content_review_cache_dir().glob("*.tmp")), [])

    def test_expired_cache_is_removed(self) -> None:
        root = content_review.content_review_cache_dir()
        root.mkdir(parents=True)
        old = root / "old.json"
        old.write_text("{}", encoding="utf-8")
        old_time = time.time() - (content_review.CONTENT_REVIEW_CACHE_DAYS + 1) * 86400
        os.utime(old, (old_time, old_time))
        content_review._cleanup_cache()
        self.assertFalse(old.exists())


class ContentReviewIntegrationTests(unittest.TestCase):
    def test_environment_mode_overrides_hidden_setting(self) -> None:
        with mock.patch.dict(os.environ, {content_review.CONTENT_REVIEW_ENV: "shadow"}):
            self.assertEqual(content_review.resolve_review_mode({"content_review_mode": "on"}), "shadow")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(content_review.resolve_review_mode({}), "off")

    def test_parser_rejects_review_pool_ids_without_changing_original_mapping(self) -> None:
        entries = [
            (0.0, 4.0, "原始一号字幕。"),
            (4.0, 8.0, "不应进入的二号字幕。"),
            (8.0, 12.0, "原始三号字幕。"),
        ]
        clips = ai_clipper._parse_ai_response(
            json.dumps([
                {"clip_type": "hook", "srt_indices": [1], "focus": "版型显瘦"},
                {"clip_type": "product", "srt_indices": [2], "focus": "其他"},
                {"clip_type": "product", "srt_indices": [3], "focus": "面料质感"},
            ], ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
            allowed_hook_indices={1},
            allowed_candidate_indices={1, 3},
        )
        self.assertEqual([clip[1] for clip in clips], [entries[0][2], entries[2][2]])
        self.assertEqual([(clip[2], clip[3]) for clip in clips], [(0.0, 4.0), (8.0, 12.0)])
    def test_expansion_plan_rejects_items_outside_review_pool(self) -> None:
        entries = [
            (0.0, 4.0, "一号开头。"),
            (4.0, 8.0, "二号池外备用。"),
            (8.0, 12.0, "三号池内备用。"),
        ]
        response = {
            "clips": [{"clip_type": "hook", "srt_indices": [1], "focus": "版型显瘦"}],
            "expansion_plan": [
                {"priority": 1, "srt_indices": [2], "after_srt_indices": [1]},
                {"priority": 2, "srt_indices": [3], "after_srt_indices": [1]},
            ],
        }
        ai_clipper._parse_ai_response(
            json.dumps(response, ensure_ascii=False),
            None,
            entries,
            set(),
            require_srt_indices=True,
            allowed_hook_indices={1},
            allowed_candidate_indices={1, 3},
        )
        plan = ai_clipper._analysis_metadata_context()["expansion_plan"]
        self.assertEqual([item["srt_indices"] for item in plan], [[3]])

    def test_expansion_cannot_reintroduce_an_unreviewed_candidate(self) -> None:
        entries = [
            (0.0, 3.0, "[V1] 肩线向内收，视觉上更利落。"),
            (3.0, 6.0, "[V1] 高支亚麻摸起来细腻，贴身不扎。"),
            (6.0, 9.0, "[V1] 姐妹们活动还有的，明天再来看。"),
        ]
        clips = [
            ("product", entries[0][2], 0.0, 3.0, 50, 3.0, "版型显瘦"),
            ("product", entries[1][2], 3.0, 6.0, 50, 3.0, "面料质感"),
        ]
        plan = [{
            "priority": 1,
            "after_srt_indices": [2],
            "srt_indices": [3],
            "focus": "无关直播话术",
        }]

        expanded = ai_clipper._apply_ai_expansion_plan(
            clips,
            plan,
            entries,
            12,
            allowed_candidate_ids={1, 2},
        )

        self.assertEqual(expanded, [])


    def test_off_mode_never_calls_reviewer(self) -> None:
        settings = {
            "api_key": "key", "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash", "content_review_mode": "off",
        }
        srt = "1\n00:00:00,000 --> 00:01:05,000\n这件衣服肩线清楚而且完整说明版型效果。\n"
        safe_inventory = [{"srt_index": 1, "source": "V1", "duration_sec": 65.0, "text": "完整卖点"}]
        with mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=safe_inventory), \
             mock.patch.object(content_review, "review_candidates") as reviewer, \
             mock.patch.object(ai_clipper, "_call_ai", side_effect=RuntimeError("director-called")):
            with self.assertRaisesRegex(RuntimeError, "director-called"):
                ai_clipper.ai_analyze_clips(srt, target_duration=60)
        reviewer.assert_not_called()

    def test_task_shadow_mode_overrides_saved_off_mode(self) -> None:
        settings = {
            "api_key": "key", "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash", "content_review_mode": "off",
        }
        srt = "1\n00:00:00,000 --> 00:01:05,000\n这件衣服肩线清晰而且完整说明版型效果。\n"
        safe_inventory = [{"srt_index": 1, "source": "V1", "duration_sec": 65.0, "text": "完整卖点"}]

        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=safe_inventory), \
             mock.patch.object(content_review, "review_candidates", side_effect=RuntimeError("reviewed")), \
             mock.patch.object(ai_clipper, "_call_ai", side_effect=RuntimeError("director-called")):
            with self.assertRaisesRegex(RuntimeError, "director-called"):
                ai_clipper.ai_analyze_clips(
                    srt,
                    target_duration=60,
                    ai_controls={"content_review_mode": "shadow"},
                )

        metadata = ai_clipper.get_last_analysis_metadata()
        self.assertEqual(metadata["content_review_summary"]["mode"], "shadow")
        self.assertEqual(metadata["content_review_summary"]["mode_source"], "task")

    def test_on_mode_passes_reviewed_ids_and_review_failure_falls_back(self) -> None:
        settings = {
            "api_key": "key", "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash", "content_review_mode": "on",
        }
        srt = "1\n00:00:00,000 --> 00:01:05,000\n这件衣服肩线清楚而且完整说明版型效果。\n"
        card = content_review.ContentCard(
            1, "版型显瘦", "肩线", "修饰肩宽", "具体效果", "肩线清楚",
            ("effect",), "independent", ("具体效果",), "main",
        )
        bundle = content_review.ContentReviewBundle(
            "key", "digest", "上衣", "deepseek-v4-flash", (card,), 65.0,
        )
        def capture_reviewed(*_args, **kwargs):
            self.assertEqual(kwargs["allowed_candidate_ids"], {1})
            raise RuntimeError("reviewed-director")

        with mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=[{"srt_index": 1, "source": "V1", "duration_sec": 65.0, "text": "完整卖点"}]), \
             mock.patch.object(content_review, "review_candidates", return_value=bundle), \
             mock.patch.object(ai_clipper, "_call_ai", side_effect=capture_reviewed):
            with self.assertRaisesRegex(RuntimeError, "reviewed-director"):
                ai_clipper.ai_analyze_clips(srt, target_duration=60)

        def capture_fallback(*_args, **kwargs):
            self.assertEqual(kwargs["allowed_candidate_ids"], {1})
            raise RuntimeError("fallback-director")

        with mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=[{"srt_index": 1, "source": "V1", "duration_sec": 65.0, "text": "完整卖点"}]), \
             mock.patch.object(content_review, "review_candidates", side_effect=ValueError("bad-review")), \
             mock.patch.object(ai_clipper, "_call_ai", side_effect=capture_fallback):
            with self.assertRaisesRegex(RuntimeError, "fallback-director"):
                ai_clipper.ai_analyze_clips(srt, target_duration=60)

    def test_on_mode_keeps_a_viable_reviewed_pool_when_only_target_duration_is_short(self) -> None:
        settings = {
            "api_key": "key", "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash", "content_review_mode": "on",
        }
        srt = (
            "1\n00:00:00,000 --> 00:00:10,000\n这件衣服肩线清楚而且完整说明版型效果。\n\n"
            "2\n00:00:10,000 --> 00:00:52,000\n面料很轻薄，夏天贴身穿也不会闷。\n\n"
            "3\n00:00:52,000 --> 00:01:34,000\n肩部线条向内收，视觉上更利落。\n"
        )
        card = content_review.ContentCard(
            1, "版型显瘦", "肩线", "修饰肩宽", "具体效果", "肩线清楚",
            ("effect",), "independent", ("具体效果",), "main",
        )
        bundle = content_review.ContentReviewBundle(
            "key", "digest", "上衣", "deepseek-v4-flash", (card,), 10.0,
        )

        def capture_reviewed(*_args, **kwargs):
            self.assertEqual(kwargs["allowed_candidate_ids"], {1, 2, 3})
            self.assertIn("审稿池时长不足", kwargs["content_review_hint"])
            raise RuntimeError("review-priority-safe-reserve-director")

        with mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=[
                 {"srt_index": 1, "source": "V1", "duration_sec": 10.0, "text": "完整卖点"},
                 {"srt_index": 2, "source": "V1", "duration_sec": 42.0, "text": "完整面料卖点"},
                 {"srt_index": 3, "source": "V1", "duration_sec": 42.0, "text": "完整版型卖点"},
             ]), \
             mock.patch.object(content_review, "review_candidates", return_value=bundle), \
             mock.patch.object(ai_clipper, "_call_ai", side_effect=capture_reviewed):
            with self.assertRaisesRegex(RuntimeError, "review-priority-safe-reserve-director"):
                ai_clipper.ai_analyze_clips(srt, target_duration=90)

    def test_on_mode_reviews_a_short_source_before_the_director_runs(self) -> None:
        settings = {
            "api_key": "key", "base_url": "https://example.com/v1",
            "model": "deepseek-v4-flash", "content_review_mode": "on",
        }
        srt = "1\n00:00:00,000 --> 00:00:12,000\n这件衣服肩线清楚而且完整说明版型效果。\n"
        card = content_review.ContentCard(
            1, "版型显瘦", "肩线", "修饰肩宽", "具体效果", "肩线清楚",
            ("effect",), "independent", ("具体效果",), "main",
        )
        bundle = content_review.ContentReviewBundle(
            "key", "digest", "上衣", "deepseek-v4-flash", (card,), 12.0,
        )

        with mock.patch.object(ai_clipper, "load_settings", return_value=settings), \
             mock.patch.object(ai_clipper, "_director_safe_candidate_inventory", return_value=[{"srt_index": 1, "source": "V1", "duration_sec": 12.0, "text": "完整卖点"}]), \
             mock.patch.object(content_review, "review_candidates", return_value=bundle), \
             mock.patch.object(ai_clipper, "_call_ai") as director:
            self.assertEqual(ai_clipper.ai_analyze_clips(srt, target_duration=60), [])
        director.assert_not_called()
        metadata = ai_clipper.get_last_analysis_metadata()
        self.assertEqual(metadata["selection_failure"]["code"], "insufficient_safe_material")
        self.assertFalse(metadata["selection_result"]["ok"])
        self.assertIn("可用安全内容仅12.0秒原片", ai_clipper.selection_failure_message(metadata))

    def test_reviewed_ten_seconds_never_becomes_a_successful_ninety_second_plan(self) -> None:
        contract = ai_clipper.DurationContract.create(90, 1.0)
        inventory = [
            {"srt_index": 1, "source": "V1", "duration_sec": 10.0, "text": "肩线向内收，视觉更利落。"},
        ]
        policy = ai_clipper._content_review_candidate_policy({1}, 10.0, inventory, contract)

        self.assertEqual(policy["allowed_candidate_ids"], {1})
        self.assertFalse(policy["reviewed_pool_covers_contract"])
        self.assertFalse(policy["safe_pool_covers_contract"])
        failure = ai_clipper._record_insufficient_safe_material(
            candidate_count=1,
            safe_candidate_duration=10.0,
            duration_contract=contract,
        )
        metadata = ai_clipper.get_last_analysis_metadata()

        self.assertEqual(failure["code"], "insufficient_safe_material")
        self.assertFalse(metadata["selection_result"]["ok"])
        self.assertEqual(metadata["selection_result"]["status"], "insufficient_safe_material")
        self.assertIn("无法完成90秒目标", ai_clipper.selection_failure_message(metadata))

    def test_safe_reserve_inventory_cannot_reintroduce_hard_exclusions(self) -> None:
        entries = [
            (0.0, 8.0, "肩线向内收，视觉上更利落。"),
            (8.0, 16.0, "面料成本210元，所以这个价位很划算。"),
            (16.0, 24.0, "姐妹点点关注，等会儿给大家上链接。"),
            (24.0, 32.0, "我身高160体重105，穿M码刚刚好。"),
            (32.0, 40.0, "姐妹你报个尺码，我给你看一下。"),
            (40.0, 48.0, "麻巾跟肠温柔软了然后还很舒服。"),
            (48.0, 56.0, "面料轻薄透气，夏天贴身穿也不会闷。"),
        ]
        ai_clipper._begin_analysis_metadata()
        with mock.patch.object(
            ai_clipper,
            "_content_policy_value",
            return_value=ai_clipper.default_content_policy(),
        ):
            safe = ai_clipper._director_safe_candidate_inventory(entries, record_metrics=True)
        safety = ai_clipper.get_last_analysis_metadata()["candidate_safety_summary"]

        self.assertEqual([item["srt_index"] for item in safe], [1, 7])
        joined = " ".join(item["text"] for item in safe)
        self.assertNotIn("成本", joined)
        self.assertNotIn("关注", joined)
        self.assertNotIn("身高", joined)
        self.assertNotIn("尺码", joined)
        self.assertNotIn("麻巾", joined)
        self.assertEqual(safety["before_count"], 7)
        self.assertEqual(safety["after_count"], 2)
        self.assertEqual(safety["removed_count"], 5)


class ContentReviewSemanticBindingTests(unittest.TestCase):
    def test_final_clip_provenance_keeps_reviewed_semantics_without_mutating_clip(self) -> None:
        clip = (
            "product",
            "肩线往里收，视觉上整个人会更利落。",
            10.0,
            16.0,
            50.0,
            6.0,
            "面料质感",
        )
        card = content_review.ContentCard(
            1,
            "版型显瘦",
            "肩线内收修饰肩宽",
            "让上半身线条更利落",
            "上身效果",
            "肩线往里收",
            ("effect", "evidence"),
            "independent",
            ("具体效果",),
            "main",
            "上衣",
            "primary",
            "肩线往里收",
        )
        bundle = content_review.ContentReviewBundle(
            "semantic-key", "digest", "上衣", "deepseek-v4-flash", (card,), 6.0,
        )
        entries = [(10.0, 16.0, clip[1])]

        ai_clipper._begin_analysis_metadata()
        ai_clipper._record_director_clip_provenance(
            clip,
            candidate_indices=[1],
            reason="导演选择肩线证据",
        )
        attached = ai_clipper._attach_content_review_semantics([clip], bundle, entries)
        metadata = ai_clipper.get_last_analysis_metadata()
        provenance = next(iter(metadata["director_clip_provenance"].values()))

        self.assertEqual(attached, 1)
        self.assertEqual(clip[1], entries[0][2])
        self.assertEqual(clip[2:4], (10.0, 16.0))
        self.assertEqual(provenance["content_semantics"]["topic"], "版型显瘦")
        self.assertEqual(provenance["content_semantics"]["subtopic"], "肩线内收修饰肩宽")
        self.assertEqual(provenance["content_semantics"]["candidate_ids"], [1])
        self.assertEqual(metadata["content_review_summary"]["selected_semantic_count"], 1)


class FinalSequenceReviewTests(unittest.TestCase):
    def _selected(self) -> list[dict]:
        return [
            {
                "order": 1,
                "clip_type": "hook",
                "srt_indices": [1],
                "source": "V1",
                "duration_sec": 10.0,
                "focus": "版型显瘦",
                "text": _inventory()[0]["text"],
            },
            {
                "order": 2,
                "clip_type": "product",
                "srt_indices": [2],
                "source": "V1",
                "duration_sec": 10.0,
                "focus": "工艺细节",
                "text": _inventory()[1]["text"],
            },
        ]

    def test_final_review_pass_keeps_director_sequence(self) -> None:
        response = json.dumps(
            {"status": "pass", "issues": [], "clips": []},
            ensure_ascii=False,
        )
        with mock.patch.object(
            content_review, "_post_review_request", return_value=response
        ) as request:
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                category="上衣",
                preference="版型显瘦",
                duration_low=20.0,
                duration_high=50.0,
            )
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.clips, ())
        self.assertEqual(request.call_count, 1)

    def test_final_review_retries_when_revised_main_list_is_too_short(self) -> None:
        short_response = json.dumps({
            "status": "revise",
            "issues": ["replace weak hook"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [2]},
            ],
            "expansion_plan": [
                {
                    "priority": 1,
                    "after_srt_indices": [2],
                    "srt_indices": [3],
                }
            ],
        }, ensure_ascii=False)
        corrected_response = json.dumps({
            "status": "revise",
            "issues": ["replace weak hook"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [2]},
                {"clip_type": "close", "srt_indices": [3]},
            ],
            "expansion_plan": [
                {
                    "priority": 1,
                    "after_srt_indices": [2],
                    "srt_indices": [4],
                }
            ],
        }, ensure_ascii=False)
        with mock.patch.object(
            content_review,
            "_post_review_request",
            side_effect=[short_response, corrected_response],
        ) as request:
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=30.0,
                duration_high=50.0,
            )
        self.assertEqual(result.status, "revise")
        self.assertEqual(len(result.clips), 3)
        self.assertEqual(request.call_count, 2)
        self.assertIn("clips\u4e3b\u7247\u5355\u4ec5", request.call_args_list[1].args[4])

    def test_final_review_cannot_replace_the_reviewed_hook_pair(self) -> None:
        invalid = json.dumps({
            "status": "revise",
            "issues": ["replace hook"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [3]},
                {"clip_type": "product", "srt_indices": [2]},
            ],
        }, ensure_ascii=False)
        corrected = json.dumps({
            "status": "revise",
            "issues": ["keep reviewed opening"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [2]},
            ],
        }, ensure_ascii=False)
        with mock.patch.object(
            content_review,
            "_post_review_request",
            side_effect=[invalid, corrected],
        ) as request:
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=20.0,
                duration_high=50.0,
                hook_pairs=[{"hook_id": 1, "followup_id": 2}],
            )
        self.assertEqual(result.clips[0]["srt_indices"], [1])
        self.assertEqual(result.clips[1]["srt_indices"], [2])
        self.assertEqual(request.call_count, 2)

    def test_final_review_accepts_any_verified_followup_for_the_same_hook(self) -> None:
        response = json.dumps({
            "status": "revise",
            "issues": ["use another verified proof"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [3]},
            ],
        }, ensure_ascii=False)
        with mock.patch.object(
            content_review, "_post_review_request", return_value=response
        ) as request:
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=20.0,
                duration_high=50.0,
                hook_pairs=[
                    {"hook_id": 1, "followup_id": 2},
                    {"hook_id": 1, "followup_id": 3},
                ],
            )
        self.assertEqual([item["srt_indices"] for item in result.clips[:2]], [[1], [3]])
        self.assertEqual(request.call_count, 1)

    def test_final_review_cannot_escape_strict_fallback_hook_ids(self) -> None:
        invalid = json.dumps({
            "status": "revise",
            "issues": ["replace hook"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [3]},
                {"clip_type": "product", "srt_indices": [2]},
            ],
        }, ensure_ascii=False)
        corrected = json.dumps({
            "status": "revise",
            "issues": ["keep safe fallback"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [2]},
            ],
        }, ensure_ascii=False)
        with mock.patch.object(
            content_review,
            "_post_review_request",
            side_effect=[invalid, corrected],
        ) as request:
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=20.0,
                duration_high=50.0,
                allowed_hook_ids={1},
            )
        self.assertEqual(result.clips[0]["srt_indices"], [1])
        self.assertEqual(request.call_count, 2)

    def test_final_review_accepts_ai_anchored_expansion_for_small_deficit(self) -> None:
        response = json.dumps({
            "status": "revise",
            "issues": ["replace weak hook"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [2]},
                {"clip_type": "close", "srt_indices": [3]},
            ],
            "expansion_plan": [
                {
                    "priority": 1,
                    "after_srt_indices": [2],
                    "srt_indices": [4],
                }
            ],
        }, ensure_ascii=False)
        with mock.patch.object(
            content_review,
            "_post_review_request",
            return_value=response,
        ) as request:
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=35.0,
                duration_high=50.0,
            )
        self.assertEqual(result.status, "revise")
        self.assertEqual(len(result.expansion_plan), 1)
        self.assertEqual(request.call_count, 1)

    def test_final_review_retries_pass_when_objective_issue_is_preflagged(self) -> None:
        selected = self._selected()
        selected[0]["text"] = "\u60f3\u8981\u663e\u7626\u7684"
        pass_response = json.dumps(
            {"status": "pass", "issues": [], "clips": []},
            ensure_ascii=False,
        )
        corrected_response = json.dumps({
            "status": "revise",
            "issues": ["\u66ff\u6362\u5f31Hook"],
            "clips": [
                {"clip_type": "hook", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [2]},
            ],
            "expansion_plan": [
                {
                    "priority": 1,
                    "after_srt_indices": [2],
                    "srt_indices": [3],
                }
            ],
        }, ensure_ascii=False)
        with mock.patch.object(
            content_review,
            "_post_review_request",
            side_effect=[pass_response, corrected_response],
        ) as request:
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=selected,
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=20.0,
                duration_high=50.0,
            )
        self.assertEqual(result.status, "revise")
        self.assertEqual(request.call_count, 2)

    def test_final_review_revision_is_full_grounded_list(self) -> None:
        response = json.dumps({
            "status": "revise",
            "issues": ["原Hook只是展示铺垫"],
            "clips": [
                {
                    "clip_type": "hook",
                    "srt_indices": [1],
                    "focus": "版型显瘦",
                    "reason": "独立效果",
                    "trim_priority": 0,
                },
                {
                    "clip_type": "product",
                    "srt_indices": [2],
                    "focus": "工艺细节",
                    "reason": "解释效果",
                    "trim_priority": 0,
                },
                {
                    "clip_type": "close",
                    "srt_indices": [3],
                    "focus": "场景搭配",
                    "reason": "自然收束",
                    "trim_priority": 0,
                },
            ],
            "expansion_plan": [
                {
                    "priority": 1,
                    "after_srt_indices": [2],
                    "after_order": 2,
                    "srt_indices": [4],
                    "focus": "\u9762\u6599\u8d28\u611f",
                    "reason": "\u65f6\u957f\u4e0d\u8db3\u65f6\u8865\u5165",
                },
            ],
        }, ensure_ascii=False)
        with mock.patch.object(
            content_review, "_post_review_request", return_value=response
        ):
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=20.0,
                duration_high=50.0,
            )
        self.assertEqual(result.status, "revise")
        self.assertEqual(
            [item["srt_indices"] for item in result.clips],
            [[1], [2], [3]],
        )
        self.assertEqual(len(result.expansion_plan), 1)
        self.assertEqual(result.expansion_plan[0]["srt_indices"], [4])
        self.assertEqual(result.expansion_plan[0]["after_srt_indices"], [2])

    def test_final_review_normalizes_missing_hook_label_on_first_clip(self) -> None:
        response = json.dumps({
            "status": "revise",
            "issues": ["\u5f00\u5934\u9700\u66f4\u5f3a"],
            "clips": [
                {"clip_type": "product", "srt_indices": [1]},
                {"clip_type": "product", "srt_indices": [2]},
                {"clip_type": "close", "srt_indices": [3]},
            ],
        }, ensure_ascii=False)
        with mock.patch.object(content_review, "_post_review_request", return_value=response):
            result = content_review.review_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                inventory=_inventory(),
                allowed_candidate_ids={1, 2, 3, 4},
                duration_low=20.0,
                duration_high=50.0,
            )
        self.assertEqual(result.clips[0]["clip_type"], "hook")
        self.assertIn("\u9996\u6bb5Hook\u7c7b\u578b\u5df2\u5f52\u4e00", result.issues)
    def test_final_review_rejects_unknown_duplicate_or_noncontiguous_ids(self) -> None:
        invalid_responses = [
            {
                "status": "revise",
                "issues": ["未知编号"],
                "clips": [
                    {"clip_type": "hook", "srt_indices": [99]},
                    {"clip_type": "product", "srt_indices": [2]},
                ],
            },
            {
                "status": "revise",
                "issues": ["重复编号"],
                "clips": [
                    {"clip_type": "hook", "srt_indices": [1]},
                    {"clip_type": "product", "srt_indices": [1]},
                ],
            },
            {
                "status": "revise",
                "issues": ["跳号"],
                "clips": [
                    {"clip_type": "hook", "srt_indices": [1, 3]},
                    {"clip_type": "product", "srt_indices": [2]},
                ],
            },
        ]
        for payload in invalid_responses:
            with self.subTest(payload=payload["issues"][0]), mock.patch.object(
                content_review,
                "_post_review_request",
                return_value=json.dumps(payload, ensure_ascii=False),
            ):
                with self.assertRaises(content_review.ContentReviewError):
                    content_review.review_final_sequence(
                        api_key="key",
                        base_url="https://example.com/v1",
                        model="deepseek-v4-flash",
                        selected_sequence=self._selected(),
                        inventory=_inventory(),
                        allowed_candidate_ids={1, 2, 3, 4},
                    )

    def test_cta_asr_variants_are_hard_blocked_without_blocking_photography(self) -> None:
        self.assertTrue(ai_clipper._is_safety_blocked_text("姐妹们赶紧去来拍"))
        self.assertTrue(ai_clipper._is_safety_blocked_text("喜欢就拍这一套"))
        self.assertTrue(ai_clipper._is_safety_blocked_text("这套我推荐你们拍的点是它很藏肉"))
        self.assertNotIn("CTA拍单变体", ai_clipper._content_safety_pattern_matches("这套很适合去拍照"))
        self.assertFalse(ai_clipper._is_safety_blocked_text("这套我很推荐拍照穿"))

class FinalSequenceAuditTests(unittest.TestCase):
    def _selected(self) -> list[dict]:
        return [
            {
                "order": 1,
                "clip_type": "hook",
                "srt_indices": [1],
                "duration_sec": 3.6,
                "focus": "版型显瘦",
                "text": "穿上以后肩线会往里收，看起来更利落。",
            },
            {
                "order": 2,
                "clip_type": "product",
                "srt_indices": [2],
                "duration_sec": 3.0,
                "focus": "工艺细节",
                "text": "肩部黑色编织线把视觉重心向内收。",
            },
        ]

    def test_audit_is_read_only_and_ignores_a_model_rewrite_payload(self) -> None:
        response = json.dumps(
            {
                "status": "flag",
                "issues": ["开头不够具体"],
                "opening_issue": True,
                "clips": [{"clip_type": "hook", "srt_indices": [99]}],
                "expansion_plan": [{"srt_indices": [99]}],
            },
            ensure_ascii=False,
        )
        with mock.patch.object(content_review, "_post_review_request", return_value=response) as request:
            result = content_review.audit_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                hook_pairs=[{"hook_id": 1, "followup_id": 2}],
            )

        self.assertEqual(result.status, "flag")
        self.assertTrue(result.opening_issue)
        self.assertFalse(hasattr(result, "clips"))
        self.assertNotIn("expansion_plan", request.call_args.args[3])
        self.assertNotIn("安全候选", request.call_args.args[4])

    def test_audit_receives_flexible_hook_thread_not_only_seed_pair(self) -> None:
        response = json.dumps(
            {"status": "pass", "issues": [], "opening_issue": False},
            ensure_ascii=False,
        )
        with mock.patch.object(content_review, "_post_review_request", return_value=response) as request:
            result = content_review.audit_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=self._selected(),
                hook_pairs=[{"hook_id": 1, "followup_id": 2}],
                hook_threads={
                    1: {
                        "topic": "版型显瘦",
                        "seed_followup_ids": [2],
                        "allowed_followup_ids": [2, 3],
                    }
                },
            )

        self.assertEqual(result.status, "pass")
        self.assertIn("Hook主题线程", request.call_args.args[4])
        self.assertIn("#1[版型显瘦]->#2/#3", request.call_args.args[4])

    def test_audit_forces_flag_when_local_check_detects_display_preamble(self) -> None:
        selected = self._selected()
        selected[0]["text"] = "想搭长裤的啊，给你看一眼长裤就这么搭。"
        response = json.dumps({"status": "pass", "issues": [], "opening_issue": False}, ensure_ascii=False)

        with mock.patch.object(content_review, "_post_review_request", return_value=response):
            result = content_review.audit_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=selected,
            )

        self.assertEqual(result.status, "flag")
        self.assertTrue(result.opening_issue)
        self.assertIn("展示铺垫", " ".join(result.issues))

    def test_audit_forces_flag_for_malformed_audience_hook(self) -> None:
        selected = self._selected()
        selected[0]["text"] = "你们机洗水洗久穿久如新的整件衣服能够做到遮肉显瘦"
        response = json.dumps({"status": "pass", "issues": [], "opening_issue": False}, ensure_ascii=False)

        with mock.patch.object(content_review, "_post_review_request", return_value=response):
            result = content_review.audit_final_sequence(
                api_key="key",
                base_url="https://example.com/v1",
                model="deepseek-v4-flash",
                selected_sequence=selected,
            )

        self.assertEqual(result.status, "flag")
        self.assertTrue(result.opening_issue)
        self.assertIn("Hook人称残句", " ".join(result.issues))


if __name__ == "__main__":
    unittest.main()
