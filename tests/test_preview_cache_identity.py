import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT / "app", ROOT / "web_client"):
    value = str(folder)
    if value not in sys.path:
        sys.path.insert(0, value)

import ai_clipper
import server


class PreviewCacheIdentityTests(unittest.TestCase):
    def test_config_signature_excludes_api_key(self) -> None:
        with mock.patch.object(ai_clipper, "load_settings", return_value={
            "api_key": "must-not-enter-cache-key",
            "model": "deepseek-v4-flash",
            "base_url": "https://example.invalid",
            "style_profile_strength": "strong",
            "content_review_mode": "off",
            "ai_rules": {"time_coherence": True},
        }):
            signature = server._preview_selection_config_signature()

        self.assertNotIn("api_key", signature["settings"])
        self.assertNotIn("must-not-enter-cache-key", repr(signature))
        self.assertEqual(signature["settings"]["style_profile_strength"], "strong")

    def test_preview_key_changes_with_pipeline_config_and_word_timing(self) -> None:
        payload = SimpleNamespace(
            target_duration=60,
            primary_category="服饰内衣",
            category="上衣",
            focus_hint="版型显瘦",
            ai_controls={"main_product": "防晒衬衫"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "source.mp4"
            srt = video.with_suffix(".srt")
            words = Path(str(srt) + ".words.json")
            video.write_bytes(b"video")
            srt.write_text("subtitle", encoding="utf-8")
            words.write_text("[]", encoding="utf-8")

            with mock.patch.object(
                server,
                "_preview_selection_config_signature",
                return_value={"pipeline": "one"},
            ):
                first = server._preview_cache_key("smart", [video], payload)
            with mock.patch.object(
                server,
                "_preview_selection_config_signature",
                return_value={"pipeline": "two"},
            ):
                second = server._preview_cache_key("smart", [video], payload)
            self.assertNotEqual(first, second)

            words.write_text('[{"text":"新"}]', encoding="utf-8")
            with mock.patch.object(
                server,
                "_preview_selection_config_signature",
                return_value={"pipeline": "two"},
            ):
                third = server._preview_cache_key("smart", [video], payload)
            self.assertNotEqual(second, third)


if __name__ == "__main__":
    unittest.main()
