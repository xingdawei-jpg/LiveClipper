from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))


cutter_logic = importlib.import_module("cutter_logic")
server = importlib.import_module("server")


class SubtitleStyleSettingsTests(unittest.TestCase):
    def test_settings_payload_uses_requested_subtitle_defaults(self) -> None:
        payload = server.SettingsPayload()
        self.assertEqual(payload.subtitle_font_family, "思源粗宋")
        self.assertEqual(payload.subtitle_font_color, "white")
        self.assertEqual(payload.subtitle_text_effect, "shadow")
        self.assertEqual(payload.subtitle_opacity, 70)
        self.assertEqual(payload.subtitle_blur, 10)

    def test_style_normalization_rejects_unknown_values(self) -> None:
        values = {
            "subtitle_font_family": "untrusted font",
            "subtitle_font_color": "teal",
            "subtitle_text_effect": "glow",
            "subtitle_opacity": 200,
            "subtitle_blur": -5,
        }
        server._normalize_subtitle_style_settings(values)
        self.assertEqual(values["subtitle_font_family"], "思源粗宋")
        self.assertEqual(values["subtitle_font_color"], "white")
        self.assertEqual(values["subtitle_text_effect"], "shadow")
        self.assertEqual(values["subtitle_opacity"], 100)
        self.assertEqual(values["subtitle_blur"], 0)

    def test_ass_style_applies_color_opacity_shadow_and_blur(self) -> None:
        style = {
            "font_name": "Test Font",
            "color_hex": "FFFFFF",
            "effect": "shadow",
            "opacity": 70,
            "blur": 10,
        }
        with tempfile.TemporaryDirectory() as tmp:
            ass = Path(tmp) / "subtitle.ass"
            cutter_logic._write_mapped_subtitle_ass(
                str(ass), [{"start": 0, "end": 1, "text": "字幕"}],
                1080, 1920, "Test Font", 52, 300, style=style,
            )
            rendered = ass.read_text(encoding="utf-8-sig")

        self.assertIn("Style: Default,Test Font,52,&H4DFFFFFF", rendered)
        self.assertIn(",0,2,2,20,20,300,1", rendered)
        self.assertIn(r"{\blur10}字幕", rendered)

    def test_ass_outline_replaces_shadow_and_drawtext_fallback_keeps_appearance(self) -> None:
        style = {
            "font_name": "Test Font",
            "color_hex": "FF3B30",
            "effect": "outline",
            "opacity": 70,
            "blur": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            ass = Path(tmp) / "subtitle.ass"
            cutter_logic._write_mapped_subtitle_ass(
                str(ass), [{"start": 0, "end": 1, "text": "字幕"}],
                1080, 1920, "Test Font", 52, 300, style=style,
            )
            rendered = ass.read_text(encoding="utf-8-sig")

        self.assertIn("Style: Default,Test Font,52,&H4D303BFF", rendered)
        self.assertIn(",3,0,2,20,20,300,1", rendered)
        fallback = cutter_logic._subtitle_drawtext_style(style)
        self.assertIn(":fontcolor=0xFF3B30:alpha=0.70", fallback)
        self.assertIn(":borderw=3:bordercolor=black@0.70", fallback)


if __name__ == "__main__":
    unittest.main()
