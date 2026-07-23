from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))
server = importlib.import_module("server")


class PreviewWordEditingTests(unittest.TestCase):
    forbidden = "\u8fdd\u7981\u8bcd"

    def _public_clip(self):
        raw_clip = (
            "product",
            "[V2] \u597d\u770b\u8fdd\u7981\u8bcd\u7a7f\u8d77\u6765\u5f88\u663e\u7626",
            10.0,
            12.0,
            0,
            2.0,
            "",
            "C:/v2.mp4",
        )
        words = [
            {"index": 0, "text": "\u597d\u770b", "start": 10.0, "end": 10.35},
            {"index": 1, "text": "\u8fdd\u7981", "start": 10.35, "end": 10.65},
            {"index": 2, "text": "\u8bcd", "start": 10.65, "end": 10.82},
            {"index": 3, "text": "\u7a7f\u8d77\u6765", "start": 10.82, "end": 11.35},
            {"index": 4, "text": "\u5f88\u663e\u7626", "start": 11.35, "end": 12.0},
        ]
        with mock.patch.object(server, "_preview_forbidden_words", return_value=[self.forbidden]):
            server._preview_lock_forbidden_words(words)
            public = {
                "index": 0,
                "text": raw_clip[1],
                "source": raw_clip[7],
                "source_marker": "V2",
                "selected": True,
                "segments": [{
                    "index": 0,
                    "text": "\u597d\u770b\u8fdd\u7981\u8bcd\u7a7f\u8d77\u6765\u5f88\u663e\u7626",
                    "start": 10.0,
                    "end": 12.0,
                    "duration": 2.0,
                    "selected": True,
                    "word_timed": True,
                    "words": words,
                }],
            }
            server._preview_lock_unsafe_segments([public])
        return raw_clip, public

    def _preview_fixture(self):
        raw_clip, public = self._public_clip()
        return {
            "id": "preview-word-edit",
            "status": "ready",
            "candidate_raw_clips": [raw_clip],
            "candidate_clips": [public],
            "selection_draft": {
                "selected_indices": [0],
                "selected_segments": {"0": [0]},
                "selected_words": {"0": {"0": [0]}},
                "order": [0],
            },
        }

    def test_forbidden_phrase_is_locked_but_safe_words_remain_available(self):
        _raw_clip, public = self._public_clip()
        segment = public["segments"][0]
        self.assertTrue(segment["selected"])
        self.assertFalse(segment.get("selection_locked", False))
        self.assertTrue(segment["word_timed"])
        self.assertEqual([word["index"] for word in segment["words"]], [0, 1, 2, 3, 4])
        self.assertTrue(segment["words"][1]["selection_locked"])
        self.assertTrue(segment["words"][2]["selection_locked"])
        self.assertEqual(segment["words"][1]["blocked_reason"], "\u8fdd\u7981\u8bcd\uff1a" + self.forbidden)

    def test_normalizer_rejects_locked_words_and_keeps_explicit_empty_selection(self):
        preview = self._preview_fixture()
        draft = server._normalize_preview_selection_draft(
            preview,
            "mix",
            [0],
            {"0": [0]},
            {"0": {"0": [0, 1, 2, 4, 99]}},
            order=[0],
        )
        self.assertEqual(draft["selected_words"], {"0": {"0": [0, 4]}})
        empty = server._normalize_preview_selection_draft(
            preview,
            "mix",
            [0],
            {"0": [0]},
            {"0": {"0": []}},
            order=[0],
        )
        self.assertEqual(empty["selected_words"], {"0": {"0": []}})

    def test_word_gap_becomes_exact_safe_video_runs(self):
        raw_clip, public = self._public_clip()
        parts = server._merge_selected_segments(
            public,
            raw_clip,
            [0],
            {"0": [0, 1, 2, 3, 4]},
        )
        self.assertEqual([(round(part[2], 2), round(part[3], 2)) for part in parts], [(10.0, 10.35), (10.82, 12.0)])
        self.assertEqual([part[1] for part in parts], ["[V2] \u597d\u770b", "[V2] \u7a7f\u8d77\u6765\u5f88\u663e\u7626"])
        self.assertTrue(all("\u8fdd\u7981" not in part[1] and "\u8bcd" not in part[1] for part in parts))

    def test_explicit_empty_words_do_not_fall_back_to_saved_draft(self):
        preview = self._preview_fixture()
        handlers = [
            (server.SmartPreviewCutPayload, server.start_smart_from_preview),
            (server.MixPreviewCutPayload, server.start_mix_from_preview),
        ]
        for payload_type, handler in handlers:
            payload = payload_type(
                preview_id="preview-word-edit",
                selected_indices=[0],
                selected_segments={"0": [0]},
                selected_words={},
                order=[0],
            )
            with (
                mock.patch.object(server, "_ensure_scope_idle"),
                mock.patch.object(server, "_get_preview", return_value=preview),
                mock.patch.object(server, "_raise_preflight_errors"),
                mock.patch.object(server, "_apply_preview_payload_draft", return_value={}) as apply_draft,
                mock.patch.object(server, "_record_preview_selection_feedback"),
                mock.patch.object(server, "_new_task", return_value="task-word-edit"),
                mock.patch.object(server.threading, "Thread"),
            ):
                handler(payload)
            self.assertEqual(apply_draft.call_args.args[5], {})
            fields = getattr(payload, "model_fields_set", None)
            if fields is None:
                fields = getattr(payload, "__fields_set__", set())
            self.assertIn("selected_words", fields)


if __name__ == "__main__":
    unittest.main()
