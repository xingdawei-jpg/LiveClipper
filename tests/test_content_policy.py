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


def _policy(**overrides: object) -> dict[str, object]:
    policy = content_policy.default_content_policy()
    policy.update(overrides)
    return policy


class ContentPolicyTests(unittest.TestCase):
    def test_default_policy_remains_legacy_safe(self) -> None:
        policy = content_policy.normalize_content_policy({})

        self.assertEqual(policy["price"], "block")
        self.assertEqual(policy["cta"], "block")
        self.assertEqual(policy["size_interaction"], "block")
        self.assertEqual(policy["live_interaction"], "block")

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
