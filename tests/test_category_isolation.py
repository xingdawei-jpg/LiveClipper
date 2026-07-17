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
