from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import asr_cache


class AsrCacheTests(unittest.TestCase):
    def test_user_srt_without_metadata_remains_usable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"video")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n", encoding="utf-8")
            result = asr_cache.inspect_cache(video, srt)
        self.assertTrue(result["valid"])
        self.assertFalse(result["managed"])

    def test_managed_cache_rejects_replaced_source_at_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"a" * 4096)
            srt.write_text("subtitle", encoding="utf-8")
            asr_cache.write_metadata(
                video,
                srt,
                provider="sensevoice",
                model="iic/SenseVoiceSmall",
                timing_precision="word",
            )
            self.assertTrue(asr_cache.inspect_cache(video, srt)["valid"])
            video.write_bytes(b"b" * 4096)
            result = asr_cache.inspect_cache(video, srt)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "source_changed")

    def test_managed_cache_binds_srt_and_word_sidecar_as_one_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            words = root / "source.words.json"
            video.write_bytes(b"video-data")
            srt.write_text("subtitle", encoding="utf-8")
            words.write_text(json.dumps({"segments": []}), encoding="utf-8")
            asr_cache.write_metadata(
                video,
                srt,
                provider="sensevoice",
                timing_precision="word",
            )
            words.write_text(json.dumps({"segments": [1]}), encoding="utf-8")
            result = asr_cache.inspect_cache(video, srt)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "words_changed")


if __name__ == "__main__":
    unittest.main()
