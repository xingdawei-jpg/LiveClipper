from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))


ai_clipper = importlib.import_module("ai_clipper")
candidate_quality = importlib.import_module("candidate_quality")
content_policy = importlib.import_module("content_policy")
content_review = importlib.import_module("content_review")


def _policy(**overrides: object) -> dict[str, object]:
    policy = content_policy.default_content_policy()
    policy.update(overrides)
    return policy


class ContentPolicyTests(unittest.TestCase):
    def test_default_policy_remains_legacy_safe(self) -> None:
        policy = content_policy.normalize_content_policy({})

        self.assertEqual(policy["price"], "block")
        self.assertEqual(policy["cta"], "block")
        self.assertEqual(policy["inventory_pressure"], "block")
        self.assertEqual(policy["source_claim"], "block")
        self.assertEqual(policy["social_proof"], "block")
        self.assertEqual(policy["after_sale"], "block")
        self.assertEqual(policy["size_interaction"], "block")
        self.assertEqual(policy["live_interaction"], "block")

    def test_run_avoid_is_a_restrictive_snapshot_not_a_saved_setting_change(self) -> None:
        saved = _policy(price="allow", inventory_pressure="allow")

        snapshot = content_policy.apply_run_avoid_overrides(saved, ["价格", "库存"])

        self.assertEqual(snapshot["price"], "block")
        self.assertEqual(snapshot["inventory_pressure"], "block")
        self.assertEqual(saved["price"], "allow")
        self.assertEqual(saved["inventory_pressure"], "allow")

    def test_inventory_pressure_is_independent_from_cta_policy(self) -> None:
        clip = ("product", "这批库存不多，喜欢的尽快看。", 0.0, 3.0, 0, 3.0)

        self.assertEqual(
            ai_clipper._filter_price_and_cta([clip], content_policy=_policy(cta="allow", inventory_pressure="block")),
            [],
        )
        self.assertEqual(
            ai_clipper._filter_price_and_cta([clip], content_policy=_policy(cta="block", inventory_pressure="allow")),
            [clip],
        )

    def test_price_and_cta_can_be_retained_when_explicitly_allowed(self) -> None:
        clips = [
            ("product", "这件面料成本210，所以细节会更扎实。", 0.0, 4.0, 0, 4.0),
            ("product", "今天上链接，喜欢的直接拍。", 4.1, 7.0, 0, 2.9),
        ]
        policy = _policy(price="allow", cta="allow")

        self.assertFalse(
            candidate_quality.candidate_quality_flags(clips[0][1], content_policy=policy)
        )
        retained = ai_clipper._filter_price_and_cta(clips, content_policy=policy)

        self.assertEqual(retained, clips)

    def test_live_price_change_with_bare_amount_honors_price_policy(self) -> None:
        clip = (
            "product",
            "来，我们这个给大家开卫衣改价本来是290。",
            0.0,
            4.0,
            0,
            4.0,
        )
        blocked = _policy(price="block")
        allowed = _policy(price="allow")

        self.assertTrue(candidate_quality.candidate_quality_flags(clip[1], content_policy=blocked))
        self.assertFalse(candidate_quality.candidate_quality_flags(clip[1], content_policy=allowed))
        self.assertEqual(ai_clipper._filter_price_and_cta([clip], content_policy=blocked), [])
        self.assertEqual(ai_clipper._filter_price_and_cta([clip], content_policy=allowed), [clip])

    def test_isolated_bare_price_sentence_honors_price_policy(self) -> None:
        clip = (
            "product",
            "你看这个上身正面也显瘦。179。",
            0.0,
            4.0,
            0,
            4.0,
        )

        self.assertEqual(
            ai_clipper._filter_price_and_cta([clip], content_policy=_policy(price="block")),
            [],
        )
        self.assertEqual(
            ai_clipper._filter_price_and_cta([clip], content_policy=_policy(price="allow")),
            [clip],
        )

    def test_measurement_number_is_not_an_isolated_price_sentence(self) -> None:
        clip = (
            "product",
            "模特身高179cm，穿起来长度刚好到脚踝。",
            0.0,
            4.0,
            0,
            4.0,
        )

        self.assertEqual(
            ai_clipper._filter_price_and_cta([clip], content_policy=_policy(price="block")),
            [clip],
        )

    def test_source_social_proof_and_after_sale_follow_task_policy(self) -> None:
        clips = [
            ("product", "这件是原厂同款，肩线往里收，视觉更利落。", 0.0, 4.0, 0, 4.0),
            ("product", "全公司都留了一件，因为这个颜色上身特别显白。", 4.1, 8.1, 0, 4.0),
            ("product", "收到不喜欢可以退，但你先看它的垂感怎么修饰腿型。", 8.2, 12.2, 0, 4.0),
        ]
        allowed = _policy(source_claim="allow", social_proof="allow", after_sale="allow")

        self.assertEqual(ai_clipper._filter_price_and_cta(clips, content_policy=allowed), clips)
        self.assertEqual(
            ai_clipper._filter_price_and_cta(clips, content_policy=_policy()),
            [],
        )

    def test_policy_only_body_content_cannot_be_promoted_to_hook(self) -> None:
        clip = ("hook", "原厂同款的肩线往里收，穿起来更利落。", 0.0, 4.0, 0, 4.0)
        policy = _policy(source_claim="body_only")

        body_safe = ai_clipper._filter_price_and_cta([clip], content_policy=policy)
        hook_safe = ai_clipper._filter_hook_ineligible_clips(body_safe, content_policy=policy)

        self.assertEqual(body_safe, [clip])
        self.assertEqual(hook_safe, [])

    def test_content_review_cache_isolated_by_content_policy(self) -> None:
        blocked = _policy(source_claim="block", social_proof="block")
        allowed = _policy(source_claim="allow", social_proof="allow")

        blocked_key = content_review.build_cache_key(
            "digest", "上衣", "西装", [], "deepseek-v4-flash", content_policy=blocked
        )
        allowed_key = content_review.build_cache_key(
            "digest", "上衣", "西装", [], "deepseek-v4-flash", content_policy=allowed
        )

        self.assertNotEqual(blocked_key, allowed_key)
        self.assertEqual(
            allowed_key,
            content_review.build_cache_key(
                "digest", "上衣", "西装", [], "deepseek-v4-flash", content_policy=allowed
            ),
        )

    def test_director_audit_uses_explicit_policy_not_saved_defaults(self) -> None:
        clips = [
            ("product", "这件是原厂同款，肩线往里收，视觉更利落。", 0.0, 4.0, 0, 4.0),
            ("product", "全公司都留了一件，因为上身显白。", 4.1, 8.1, 0, 4.0),
        ]
        policy = _policy(source_claim="allow", social_proof="allow")

        safe, _audit = ai_clipper._director_hard_audit(
            clips,
            8.0,
            5.0,
            content_policy=policy,
        )

        self.assertEqual(safe, clips)

    def test_final_audit_only_flags_cta_when_task_policy_blocks_it(self) -> None:
        sequence = [
            {
                "clip_type": "product",
                "text": "这个肩型很利落，我推荐大家直接拍。",
                "duration_sec": 4.0,
            }
        ]

        blocked = content_review._final_objective_issues(sequence, _policy(cta="block"))
        allowed = content_review._final_objective_issues(sequence, _policy(cta="allow"))

        self.assertTrue(any("CTA" in issue for issue in blocked))
        self.assertFalse(any("CTA" in issue for issue in allowed))

    def test_body_only_content_never_promotes_to_hook(self) -> None:
        price = "今天到手价199，但这件肩线向内收，视觉更利落。"
        size_reply = "我身高160体重98，穿S码正合适。"
        policy = _policy(price="body_only", size_interaction="allow")
        clips = [
            ("hook", price, 0.0, 4.0, 0, 4.0),
            ("product", price, 4.1, 8.0, 0, 3.9),
            ("hook", size_reply, 8.1, 11.0, 0, 2.9),
            ("product", size_reply, 11.1, 14.0, 0, 2.9),
        ]

        body_safe = ai_clipper._filter_price_and_cta(clips, content_policy=policy)
        hook_safe = ai_clipper._filter_hook_ineligible_clips(
            body_safe,
            content_policy=policy,
        )

        self.assertEqual(
            [(clip[0], clip[1]) for clip in hook_safe],
            [("product", price), ("product", size_reply)],
        )

    def test_custom_block_overrides_broad_allow(self) -> None:
        policy = _policy(
            price="allow",
            custom_rules=[{"text": "内部口令", "action": "block"}],
        )
        clips = [
            ("product", "内部口令报出来之前，先看这件版型。", 0.0, 3.0, 0, 3.0),
            ("product", "这件肩部结构会把视觉重心往里收。", 3.1, 6.1, 0, 3.0),
        ]

        retained = ai_clipper._filter_price_and_cta(clips, content_policy=policy)

        self.assertEqual(retained, [clips[1]])

    def test_fixed_platform_or_legal_forbidden_word_is_not_overridable(self) -> None:
        self.assertTrue(
            ai_clipper._is_safety_blocked_text(
                "这个面料能治疗皮肤问题。",
                forbidden_words=["治疗"],
                content_policy=_policy(price="allow", cta="allow"),
            )
        )

    def test_frozen_candidate_policy_preserves_body_but_not_hook_eligibility(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:03,000\n"
            "今天到手价199，但肩线向内收，视觉更利落。\n\n"
            "2\n00:00:03,100 --> 00:00:06,000\n"
            "我身高160体重98，穿S码正合适。\n\n"
            "3\n00:00:06,100 --> 00:00:09,100\n"
            "高支亚麻摸起来细腻，夏天穿也透气。\n"
        )
        frozen = ai_clipper._freeze_director_candidates(
            source,
            content_policy=_policy(price="body_only", size_interaction="allow"),
        )

        self.assertIn("今天到手价199", frozen)
        self.assertIn("我身高160体重98", frozen)
        self.assertTrue(
            ai_clipper._is_safety_blocked_text(
                "今天到手价199，但肩线向内收，视觉更利落。",
                content_policy=_policy(price="body_only", size_interaction="allow"),
                role="hook",
            )
        )

    def test_task_policy_overrides_saved_policy_without_mutating_it(self) -> None:
        saved = _policy(price="block", cta="block")
        requested = _policy(price="allow", cta="body_only")

        with mock.patch.object(ai_clipper, "_load_ai_rules", return_value={"content_policy": saved}):
            merged = ai_clipper._merge_ai_rules({"content_policy": requested})

        self.assertEqual(merged["content_policy"]["price"], "allow")
        self.assertEqual(merged["content_policy"]["cta"], "body_only")
        self.assertEqual(saved["price"], "block")
        self.assertTrue(
            ai_clipper._is_safety_blocked_text(
                "我身高160体重98，穿S码正合适。",
                content_policy=_policy(price="body_only", size_interaction="allow"),
                role="hook",
            )
        )


if __name__ == "__main__":
    unittest.main()
