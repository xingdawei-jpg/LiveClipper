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
sys.path.insert(0, str(ROOT / "web_client"))
server = importlib.import_module("server")
ai_clipper = importlib.import_module("ai_clipper")


class KeywordPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = {
            "clip_keywords": {"hook": ["原始钩子"]},
            "forbidden_phrases": ["原始违禁"],
            "filler_words": ["原始废话"],
            "negative_signals": ["原始负面"],
            "preference_keywords": {"版型显瘦": ["原始偏好"]},
            "detail_keywords": ["原始细节"],
            "preference_weights": {"版型显瘦": 1.5},
        }
        self.effective = copy.deepcopy(self.raw)
        self.effective["hook_keywords"] = ["原始钩子"]
        self.effective["forbidden_phrases"] = ["原始违禁", "硬安全词"]
        self.effective["preference_keywords"]["口感食欲"] = ["系统默认词"]
        self.effective["_source"] = "runtime-only"

    def test_explicit_changes_preserve_unedited_raw_vocabulary(self) -> None:
        payload = {"changes": {"filler_words": ["新的废话"]}}
        with mock.patch.object(server, "_load_keyword_config", return_value=copy.deepcopy(self.raw)):
            saved = server._normalize_keyword_payload(payload)

        self.assertEqual(saved["filler_words"], ["新的废话"])
        self.assertEqual(saved["clip_keywords"], self.raw["clip_keywords"])
        self.assertEqual(saved["forbidden_phrases"], self.raw["forbidden_phrases"])
        self.assertEqual(saved["preference_keywords"], self.raw["preference_keywords"])
        self.assertEqual(saved["preference_weights"], self.raw["preference_weights"])
        self.assertNotIn("hook_keywords", saved)
        self.assertNotIn("_source", saved)

    def test_legacy_full_payload_ignores_runtime_fields(self) -> None:
        payload = {
            "forbidden_phrases": ["新的违禁"],
            "hook_keywords": ["运行时字段不得落盘"],
            "unknown_runtime_field": {"ignored": True},
        }
        with mock.patch.object(server, "_load_keyword_config", return_value=copy.deepcopy(self.raw)):
            saved = server._normalize_keyword_payload(payload)

        self.assertEqual(saved["forbidden_phrases"], ["新的违禁"])
        self.assertEqual(saved["clip_keywords"], self.raw["clip_keywords"])
        self.assertNotIn("hook_keywords", saved)
        self.assertNotIn("unknown_runtime_field", saved)

    def test_save_writes_only_changed_field_and_returns_effective_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "keywords.json"
            with (
                mock.patch.object(server, "_safe_user_child", return_value=target),
                mock.patch.object(server, "_load_keyword_config", return_value=copy.deepcopy(self.raw)),
                mock.patch.object(server, "_load_effective_keyword_config", return_value=copy.deepcopy(self.effective)),
                mock.patch.object(server, "_clear_ai_keyword_cache") as clear_cache,
                mock.patch.object(server, "emit_log"),
            ):
                result = server.save_keywords({"changes": {"filler_words": ["新的废话"]}})

            persisted = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(result["saved_fields"], ["filler_words"])
            self.assertEqual(persisted["filler_words"], ["新的废话"])
            self.assertEqual(persisted["preference_keywords"], self.raw["preference_keywords"])
            self.assertNotIn("hook_keywords", persisted)
            self.assertNotIn("口感食欲", persisted["preference_keywords"])
            clear_cache.assert_called_once()

    def test_empty_changes_are_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "keywords.json"
            with (
                mock.patch.object(server, "_safe_user_child", return_value=target),
                mock.patch.object(server, "_load_effective_keyword_config", return_value=copy.deepcopy(self.effective)),
                mock.patch.object(server, "emit_log"),
            ):
                result = server.save_keywords({"changes": {}})

            self.assertEqual(result["saved_fields"], [])
            self.assertFalse(target.exists())
            self.assertEqual(result["message"], "词库没有改动")

    def test_atomic_writer_leaves_valid_json_without_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "keywords.json"
            server._write_json_file(target, {"forbidden_phrases": ["价格"]})

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"forbidden_phrases": ["价格"]})
            self.assertEqual(list(target.parent.glob(f".{target.name}.*.tmp")), [])

    def test_frontend_sends_a_changes_patch_not_effective_state(self) -> None:
        source = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function collectKeywordChanges()", source)
        self.assertIn("body: JSON.stringify({ changes })", source)
        self.assertNotIn("...state.keywordConfig,\n    clip_keywords", source)

    def test_corrupted_user_vocabulary_falls_back_to_the_bundled_template(self) -> None:
        damaged = {
            "forbidden_phrases": ["broken\u00c3\u00a7", "broken\u00c2\u00a9"],
            "clip_keywords": {"hook": ["broken\u00c3\u00a7"]},
        }
        default = {"forbidden_phrases": ["默认违禁"], "clip_keywords": {"hook": ["默认钩子"]}}

        self.assertFalse(ai_clipper.is_keyword_config_usable(damaged))
        with mock.patch.object(ai_clipper, "_keyword_file_paths", return_value=("default", "user")):
            with mock.patch.object(ai_clipper, "_load_keyword_file", side_effect=[default, damaged]):
                effective = ai_clipper.load_keywords()
        self.assertIn("默认违禁", effective["forbidden_phrases"])
        self.assertNotIn("broken\u00c3\u00a7", effective["forbidden_phrases"])
        self.assertEqual(effective["_source"], "default")

    def test_server_does_not_persist_edits_on_top_of_a_corrupted_vocabulary(self) -> None:
        damaged = {"forbidden_phrases": ["broken\u00c3\u00a7"]}
        default = {"forbidden_phrases": ["默认违禁"], "filler_words": ["默认废话"]}
        with mock.patch.object(server, "_read_json_file", side_effect=[default, damaged]):
            self.assertEqual(server._load_keyword_config(), default)


if __name__ == "__main__":
    unittest.main()
