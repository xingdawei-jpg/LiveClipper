from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))


server = importlib.import_module("server")


class CategoryIsolationTests(unittest.TestCase):
    def test_compact_allow_is_not_overruled_by_removed_legacy_avoid(self) -> None:
        from content_policy import apply_run_avoid_overrides
        for payload_type in (server.SmartCutPayload, server.MixPayload):
            payload = payload_type(ai_controls={
                "controls_version": "director-controls-v2", "goal": "穿着体验",
                "content_policy": {"price": "allow", "cta": "allow", "size_interaction": "body_only"},
                "avoid": ["价格", "尺码"], "hook_style": "价格开头", "selling_points": ["显瘦"],
                "preference_weights": {"版型": 3}, "supporting_products": "block",
            })
            controls = server._payload_ai_controls(payload)
            self.assertNotIn("hook_style", controls)
            self.assertNotIn("selling_points", controls)
            self.assertEqual(controls["preference_weights"], {})
            self.assertIn("搭配其他品", controls["avoid"])
            policy = apply_run_avoid_overrides(controls["content_policy"], controls["avoid"])
            self.assertEqual(policy["price"], "allow")
            self.assertEqual(policy["size_interaction"], "body_only")

    def test_explicit_identity_survives_stale_cross_vertical_secondary(self) -> None:
        payload = server.SmartCutPayload(primary_category="服饰内衣", ai_controls={
            "secondary_category": "美容护肤", "leaf_category": "面膜", "main_product": "白衬衫",
        })
        controls = server._payload_ai_controls(payload)
        self.assertEqual(controls["main_product"], "白衬衫")
        self.assertNotIn("leaf_category", controls)

    def test_first_level_category_overrides_stale_legacy_category(self) -> None:
        payload = server.SmartCutPayload(
            primary_category="生鲜",
            category="上衣",
            ai_controls={"primary_category": "服饰内衣", "secondary_category": "女装"},
        )

        self.assertEqual(server._force_category_for_payload(payload), "食品/生鲜")
        self.assertEqual(server._preview_category_for_payload(payload), "食品/生鲜")
        self.assertEqual(server._preferred_category_for_payload(payload, {"main_category": "上衣"}), "食品/生鲜")
        self.assertEqual(server._payload_ai_controls(payload), {"primary_category": "生鲜"})

    def test_matching_secondary_category_is_preserved(self) -> None:
        payload = server.SmartCutPayload(
            primary_category="美妆",
            category="上衣",
            ai_controls={
                "primary_category": "服饰内衣",
                "secondary_category": "美容护肤",
                "leaf_category": "面膜",
                "main_product": "补水面膜",
            },
        )

        controls = server._payload_ai_controls(payload)
        self.assertEqual(server._force_category_for_payload(payload), "美妆护肤")
        self.assertEqual(controls["primary_category"], "美妆")
        self.assertEqual(controls["secondary_category"], "美容护肤")
        self.assertEqual(controls["leaf_category"], "面膜")
        self.assertEqual(controls["main_product"], "补水面膜")


if __name__ == "__main__":
    unittest.main()
