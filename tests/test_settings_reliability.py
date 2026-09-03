from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))


ai_clipper = importlib.import_module("ai_clipper")
server = importlib.import_module("server")


class SettingsReliabilityTests(unittest.TestCase):
    def test_save_creates_missing_parent_and_writes_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "ai_settings.json"
            with mock.patch("config.SETTINGS_PATH", str(target)):
                self.assertTrue(ai_clipper.save_settings({"api_key": "secret", "enabled": True}))

            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["api_key"], "secret")
            self.assertTrue(saved["enabled"])

    def test_save_quarantines_invalid_existing_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ai_settings.json"
            target.write_text('{"api_key":', encoding="utf-8")

            with mock.patch("config.SETTINGS_PATH", str(target)):
                self.assertTrue(ai_clipper.save_settings({"api_key": "replacement"}))

            saved = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(saved["api_key"], "replacement")
            backups = list(Path(tmp).glob("ai_settings.json.invalid-*.bak"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), '{"api_key":')

    def test_save_failure_exposes_safe_reason_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "ai_settings.json"
            with mock.patch("config.SETTINGS_PATH", str(target)), mock.patch.object(
                ai_clipper.os, "replace", side_effect=PermissionError("secret-token")
            ), mock.patch("traceback.print_exc"):
                self.assertFalse(ai_clipper.save_settings({"api_key": "secret-token"}))

            error = ai_clipper.get_last_settings_save_error()
            self.assertIn("不可写", error)
            self.assertNotIn("secret-token", error)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_api_returns_readable_save_error(self) -> None:
        payload = server.SettingsPayload()
        with mock.patch.object(server, "_save_settings", return_value=False), mock.patch.object(
            ai_clipper, "get_last_settings_save_error", return_value="用户数据目录不可写"
        ), self.assertRaises(server.HTTPException) as raised:
            server.save_settings(payload)

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(raised.exception.detail, "设置保存失败：用户数据目录不可写")

    def test_planner_mode_save_is_experimental_only_for_lite_and_legacy_is_fail_closed(self) -> None:
        values = {"m2_planner_mode": "lite_director_experiment"}
        with mock.patch.object(ai_clipper, "save_settings", return_value=True) as save:
            self.assertTrue(server._save_settings(values))
        saved = save.call_args.args[0]
        self.assertEqual(saved["m2_planner_mode"], "lite_director_experiment")
        self.assertEqual(saved["ai_director_mode"], "experimental")

        values = {"m2_planner_mode": "not-a-mode"}
        with mock.patch.object(ai_clipper, "save_settings", return_value=True) as save:
            self.assertTrue(server._save_settings(values))
        saved = save.call_args.args[0]
        self.assertEqual(saved["m2_planner_mode"], "legacy")
        self.assertEqual(saved["ai_director_mode"], "legacy")


if __name__ == "__main__":
    unittest.main()
