from __future__ import annotations

from array import array
import importlib
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

local_asr = importlib.import_module("local_asr")
chunking = importlib.import_module("local_asr_chunking")


def _write_pcm_wave(path: Path, duration: float, active_ranges: list[tuple[float, float]]) -> int:
    sample_rate = 1000
    total = int(round(duration * sample_rate))
    samples = array("h")
    for index in range(total):
        second = index / sample_rate
        active = any(start <= second < end for start, end in active_ranges)
        samples.append(3000 if active and index % 8 < 4 else (-3000 if active else 0))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(samples.tobytes())
    return total


class LocalAsrChunkingTests(unittest.TestCase):
    def test_pause_chunks_are_contiguous_and_prefer_real_silence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "pause.wav"
            total_frames = _write_pcm_wave(
                audio,
                24.0,
                [(0.0, 3.0), (3.7, 7.0), (7.7, 12.0), (12.8, 16.0), (16.7, 24.0)],
            )
            chunks = chunking.build_pause_aware_audio_chunks(audio)

            self.assertGreaterEqual(len(chunks), 3)
            self.assertEqual(chunks[0].start, 0.0)
            self.assertAlmostEqual(chunks[-1].end, 24.0, places=3)
            self.assertTrue(all(chunk.duration <= 10.001 for chunk in chunks))
            self.assertTrue(all(right.start == left.end for left, right in zip(chunks, chunks[1:])))
            self.assertTrue(all(chunk.boundary_reason != "hard_limit" for chunk in chunks[:-1]))

            copied_frames = 0
            for index, chunk in enumerate(chunks, 1):
                output = Path(temp_dir) / f"chunk-{index}.wav"
                chunking.write_audio_chunk(audio, output, chunk)
                with wave.open(str(output), "rb") as reader:
                    copied_frames += reader.getnframes()
            self.assertEqual(copied_frames, total_frames)

    def test_continuous_audio_uses_hard_limit_without_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "continuous.wav"
            _write_pcm_wave(audio, 25.0, [(0.0, 25.0)])
            chunks = chunking.build_pause_aware_audio_chunks(audio)

        self.assertEqual(
            [(chunk.start, chunk.end) for chunk in chunks],
            [(0.0, 7.0), (7.0, 14.0), (14.0, 21.0), (21.0, 25.0)],
        )
        self.assertEqual(
            [chunk.boundary_reason for chunk in chunks],
            ["hard_limit", "hard_limit", "hard_limit", "source_end"],
        )

    def test_standard_profile_remains_available_for_offline_ab_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            audio = Path(temp_dir) / "continuous-standard.wav"
            _write_pcm_wave(audio, 25.0, [(0.0, 25.0)])
            chunks = chunking.build_pause_aware_audio_chunks(
                audio,
                target_seconds=chunking.STANDARD_TARGET_CHUNK_SECONDS,
                max_seconds=chunking.STANDARD_MAX_CHUNK_SECONDS,
            )

        self.assertEqual(
            [(chunk.start, chunk.end) for chunk in chunks],
            [(0.0, 9.0), (9.0, 18.0), (18.0, 25.0)],
        )

    def test_chunked_inference_offsets_words_back_to_source_timeline(self) -> None:
        chunks = [
            chunking.AudioChunk(0.0, 5.0, "pause"),
            chunking.AudioChunk(5.0, 10.0, "source_end"),
        ]

        class FakeModel:
            def __init__(self) -> None:
                self.inputs: list[str] = []

            def generate(self, **kwargs):
                self.inputs.append(kwargs["input"])
                return [{
                    "text": "这件衣服很好。",
                    "timestamp": [[index * 100, (index + 1) * 100] for index in range(6)],
                }]

        model = FakeModel()
        with (
            mock.patch.object(local_asr, "build_pause_aware_audio_chunks", return_value=chunks),
            mock.patch.object(local_asr, "write_audio_chunk"),
        ):
            segments, chunk_count, hard_count = local_asr._sensevoice_pause_aware_segments(
                model,
                "source.wav",
            )

        self.assertEqual((chunk_count, hard_count), (2, 0))
        self.assertEqual(len(model.inputs), 2)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["start"], 0.0)
        self.assertEqual(segments[0]["end"], 0.6)
        self.assertEqual(segments[1]["start"], 5.0)
        self.assertEqual(segments[1]["end"], 5.6)
        self.assertEqual(segments[1]["words"][0]["start"], 5.0)
        self.assertEqual(segments[1]["words"][-1]["end"], 5.6)


if __name__ == "__main__":
    unittest.main()
