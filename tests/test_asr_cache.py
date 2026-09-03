from __future__ import annotations

import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

import asr_cache
import local_asr_quality
import stt
import volcengine_asr


class AsrCacheTests(unittest.TestCase):
    def test_local_asr_persists_transcript_and_word_assets_beside_source_video(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "中文素材.mp4"
            video.write_bytes(b"video-data")
            temp_audio_root = root / "temporary-audio"

            def fake_transcribe(_audio, srt_output, **_kwargs):
                destination = Path(srt_output)
                destination.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n完整口播\n", encoding="utf-8",
                )
                destination.with_suffix(".words.json").write_text(
                    json.dumps({"segments": []}), encoding="utf-8",
                )
                return True

            with mock.patch.object(stt.tempfile, "gettempdir", return_value=str(temp_audio_root)), \
                 mock.patch.object(stt, "extract_audio", return_value=True), \
                 mock.patch.object(stt, "transcribe_local_audio_to_srt", side_effect=fake_transcribe):
                generated = stt.generate_srt(str(video))

            srt = video.with_suffix(".srt")
            self.assertEqual(Path(generated), srt)
            self.assertTrue(srt.is_file())
            self.assertTrue(srt.with_suffix(".words.json").is_file())
            self.assertTrue(srt.with_suffix(".asr-cache.json").is_file())
            self.assertTrue(asr_cache.inspect_cache(video, srt)["valid"])

            stt.cleanup_srt(str(srt))
            self.assertTrue(srt.is_file(), "successful clipping must not delete source-side ASR assets")

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

    def test_old_managed_sensevoice_cache_requests_text_pipeline_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"video-data")
            srt.write_text("subtitle", encoding="utf-8")
            asr_cache.write_metadata(video, srt, provider="sensevoice", timing_precision="word")
            metadata_path = asr_cache.metadata_path(srt)
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            del payload["asr"]["text_pipeline_revision"]
            metadata_path.write_text(json.dumps(payload), encoding="utf-8")
            result = asr_cache.inspect_cache(video, srt)
        self.assertFalse(result["valid"])
        self.assertTrue(result["managed"])
        self.assertEqual(result["reason"], "sensevoice_text_pipeline_changed")

    def test_refreshed_sensevoice_cache_becomes_verified_without_audio_decode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"video-data")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n旧字幕\n", encoding="utf-8")
            words = [
                {"text": char, "start": index * 0.2, "end": (index + 1) * 0.2}
                for index, char in enumerate("透薄三伏天随便穿")
            ]
            volcengine_asr.write_word_timing_sidecar(
                srt,
                [{"text": "透薄，三伏天随便穿。", "start": 0.0, "end": len(words) * 0.2, "words": words}],
                provider="sensevoice",
            )
            asr_cache.write_metadata(video, srt, provider="sensevoice", timing_precision="word")
            metadata_path = asr_cache.metadata_path(srt)
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            del payload["asr"]["text_pipeline_revision"]
            metadata_path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(
                asr_cache.inspect_cache(video, srt)["reason"],
                "sensevoice_text_pipeline_changed",
            )
            self.assertTrue(local_asr_quality.refresh_managed_sensevoice_transcript(srt)["refreshed"])
            asr_cache.write_metadata(video, srt, provider="sensevoice", timing_precision="word")
            result = asr_cache.inspect_cache(video, srt)

        self.assertTrue(result["valid"])
        self.assertEqual(result["reason"], "verified")


if __name__ == "__main__":
    unittest.main()
