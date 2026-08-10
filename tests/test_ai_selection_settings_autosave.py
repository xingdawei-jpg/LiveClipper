from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web_client"))
sys.path.insert(0, str(ROOT / "app"))


server = importlib.import_module("server")


class AiSelectionSettingsAutosaveTests(unittest.TestCase):
    def test_selection_patch_preserves_unrelated_settings(self) -> None:
        existing = {
            "api_key": "do-not-rewrite",
            "asr_enabled": True,
            "content_review_mode": "off",
            "preference_weights": {"场景搭配": 1.0},
            "ai_rules": {
                "narrative": "旧叙事",
                "category_filter": True,
                "custom_text": "旧备注",
                "unknown": "保留",
            },
        }
        payload = server.AiSelectionSettingsPayload(
            content_review_mode="shadow",
            preference_weights={"场景搭配": 2.5},
            ai_rules={
                "category_filter": False,
                "content_policy": {"price": "allow"},
                "not_allowed": "ignore",
            },
        )

        with mock.patch.object(server, "_load_settings", return_value=existing), \
             mock.patch.object(server, "_save_settings", return_value=True) as save:
            result = server.save_ai_selection_settings(payload)

        saved = save.call_args.args[0]
        self.assertTrue(result["ok"])
        self.assertEqual(saved["api_key"], "do-not-rewrite")
        self.assertTrue(saved["asr_enabled"])
        self.assertEqual(saved["content_review_mode"], "shadow")
        self.assertEqual(saved["preference_weights"], {"场景搭配": 2.5})
        self.assertFalse(saved["ai_rules"]["category_filter"])
        self.assertEqual(saved["ai_rules"]["narrative"], "旧叙事")
        self.assertEqual(saved["ai_rules"]["unknown"], "保留")
        self.assertNotIn("not_allowed", saved["ai_rules"])
        self.assertEqual(saved["ai_rules"]["content_policy"]["price"], "allow")


if __name__ == "__main__":
    unittest.main()
