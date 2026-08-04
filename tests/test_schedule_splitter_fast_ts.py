from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

schedule_splitter = importlib.import_module("schedule_splitter")


class ScheduleSplitterFastTsTests(unittest.TestCase):
    def test_ts_extensions_require_normalization(self) -> None:
        for suffix in (".ts", ".TS", ".mts", ".m2ts"):
            self.assertTrue(schedule_splitter._needs_ts_normalization("source" + suffix))
        self.assertFalse(schedule_splitter._needs_ts_normalization("source.mp4"))

    def test_normalized_ts_is_reused_and_each_segment_uses_validated_fast_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ts"
            source.write_bytes(b"source")
            normalized_dir = root / "schedule-cache"
            normalized_dir.mkdir()
            normalized = normalized_dir / "normalized.mp4"
            normalized.write_bytes(b"normalized")
            output = root / "output"
            copy_calls = []

            def fake_copy(ffmpeg, start, duration, video, out_path):
                copy_calls.append((ffmpeg, start, duration, video, out_path))
                target = Path(out_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * 2048)
                return SimpleNamespace(returncode=0, stderr=b"")

            with (
                mock.patch.object(schedule_splitter, "_probe_durations", return_value=[100.0]),
                mock.patch.object(schedule_splitter, "_create_schedule_temp_dir", return_value=str(normalized_dir)),
                mock.patch.object(schedule_splitter, "_prepare_ts_source", return_value=str(normalized)) as prepare,
                mock.patch.object(schedule_splitter, "_probe_av_start_offset_seconds", side_effect=[0.147, 0.02, 0.02]),
                mock.patch.object(schedule_splitter, "_probe_media_duration_seconds", side_effect=[10.0, 10.0]),
                mock.patch.object(schedule_splitter, "_av_start_gap_seconds", side_effect=AssertionError("unexpected output probe")),
                mock.patch.object(schedule_splitter, "_fast_copy_segment", side_effect=fake_copy),
                mock.patch.object(schedule_splitter, "_sync_reencode_segment", side_effect=AssertionError("unexpected reencode")),
                mock.patch.object(schedule_splitter.subprocess, "run", side_effect=AssertionError("unexpected stream-copy cut")),
            ):
                results = schedule_splitter.extract_by_schedule(
                    [{"name": "product", "segments": [(0.0, 10.0), (20.0, 30.0)]}],
                    [str(source)],
                    str(output),
                    ffmpeg="ffmpeg",
                )

            self.assertEqual(len(results), 2)
            prepare.assert_called_once()
            self.assertEqual(len(copy_calls), 2)
            self.assertTrue(all(call[3] == str(normalized) for call in copy_calls))
            self.assertFalse(normalized_dir.exists())

    def test_fast_copy_offset_change_falls_back_to_sync_reencode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ts"
            source.write_bytes(b"source")
            normalized_dir = root / "schedule-cache"
            normalized_dir.mkdir()
            normalized = normalized_dir / "normalized.mp4"
            normalized.write_bytes(b"normalized")
            output = root / "output"

            def write_output(_ffmpeg, _start, _duration, _video, out_path, **_kwargs):
                Path(out_path).parent.mkdir(parents=True, exist_ok=True)
                Path(out_path).write_bytes(b"z" * 2048)
                return SimpleNamespace(returncode=0, stderr=b"")

            with (
                mock.patch.object(schedule_splitter, "_probe_durations", return_value=[100.0]),
                mock.patch.object(schedule_splitter, "_create_schedule_temp_dir", return_value=str(normalized_dir)),
                mock.patch.object(schedule_splitter, "_prepare_ts_source", return_value=str(normalized)),
                mock.patch.object(schedule_splitter, "_probe_av_start_offset_seconds", side_effect=[0.147, 0.2]),
                mock.patch.object(schedule_splitter, "_probe_media_duration_seconds", return_value=10.0),
                mock.patch.object(schedule_splitter, "_fast_copy_segment", side_effect=write_output) as fast_copy,
                mock.patch.object(schedule_splitter, "_sync_reencode_segment", side_effect=write_output) as reencode,
            ):
                results = schedule_splitter.extract_by_schedule(
                    [{"name": "product", "segments": [(0.0, 10.0)]}],
                    [str(source)],
                    str(output),
                    ffmpeg="ffmpeg",
                )

            self.assertEqual(len(results), 1)
            fast_copy.assert_called_once()
            reencode.assert_called_once()
            self.assertEqual(reencode.call_args.kwargs["av_start_offset"], 0.147)

    def test_failed_ts_normalization_uses_existing_sync_fallback_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ts"
            source.write_bytes(b"source")
            output = root / "output"

            def fake_reencode(_ffmpeg, _start, _duration, _video, out_path, **_kwargs):
                Path(out_path).write_bytes(b"y" * 2048)
                return SimpleNamespace(returncode=0, stderr=b"")

            with (
                mock.patch.object(schedule_splitter, "_probe_durations", return_value=[100.0]),
                mock.patch.object(schedule_splitter, "_prepare_ts_source", return_value=None),
                mock.patch.object(schedule_splitter, "_probe_av_start_offset_seconds", return_value=0.147),
                mock.patch.object(schedule_splitter, "_sync_reencode_segment", side_effect=fake_reencode) as reencode,
                mock.patch.object(schedule_splitter.subprocess, "run", side_effect=AssertionError("unexpected stream-copy cut")),
            ):
                results = schedule_splitter.extract_by_schedule(
                    [{"name": "product", "segments": [(0.0, 10.0)]}],
                    [str(source)],
                    str(output),
                    ffmpeg="ffmpeg",
                )

            self.assertEqual(len(results), 1)
            reencode.assert_called_once()

    def test_sync_reencode_resets_audio_and_video_from_the_same_seek_point(self) -> None:
        with mock.patch.object(
            schedule_splitter.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stderr=b""),
        ) as run:
            schedule_splitter._sync_reencode_segment(
                "ffmpeg",
                7200.25,
                65.0,
                "normalized.mp4",
                "product.mp4",
                av_start_offset=0.147,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-ss") + 1], "7200.25")
        self.assertEqual(command[command.index("-i") + 1], "normalized.mp4")
        self.assertIn("setpts=PTS-STARTPTS", command[command.index("-vf") + 1])
        self.assertIn("asetpts=PTS-STARTPTS-0.147000/TB", command[command.index("-af") + 1])
        self.assertIn("aresample=async=1:first_pts=0", command[command.index("-af") + 1])

    def test_fast_copy_uses_stream_copy_without_video_or_audio_filters(self) -> None:
        with mock.patch.object(
            schedule_splitter.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stderr=b""),
        ) as run:
            schedule_splitter._fast_copy_segment(
                "ffmpeg", 7200.25, 65.0, "normalized.mp4", "product.mp4"
            )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("-ss") + 1], "7200.25")
        self.assertEqual(command[command.index("-i") + 1], "normalized.mp4")
        self.assertEqual(command[command.index("-c") + 1], "copy")
        self.assertNotIn("-vf", command)
        self.assertNotIn("-af", command)

    def test_fast_copy_validation_rejects_duration_or_output_av_gap(self) -> None:
        with mock.patch.object(
            schedule_splitter, "_probe_av_start_offset_seconds", return_value=-0.017
        ), mock.patch.object(
            schedule_splitter, "_probe_media_duration_seconds", return_value=65.2
        ):
            ok, _detail = schedule_splitter._validate_fast_copy_segment(
                "product.mp4", "ffmpeg", 65.0, 0.147
            )
        self.assertTrue(ok)

        with mock.patch.object(
            schedule_splitter, "_probe_av_start_offset_seconds", return_value=-0.147
        ), mock.patch.object(
            schedule_splitter, "_probe_media_duration_seconds", return_value=65.0
        ):
            ok, detail = schedule_splitter._validate_fast_copy_segment(
                "product.mp4", "ffmpeg", 65.0, 0.147
            )
        self.assertFalse(ok)
        self.assertIn("切片音画起点相差", detail)

        with mock.patch.object(
            schedule_splitter, "_probe_av_start_offset_seconds", return_value=0.02
        ), mock.patch.object(
            schedule_splitter, "_probe_media_duration_seconds", return_value=74.0
        ):
            ok, detail = schedule_splitter._validate_fast_copy_segment(
                "product.mp4", "ffmpeg", 65.0, 0.147
            )
        self.assertFalse(ok)
        self.assertIn("切片时长偏差", detail)

    def test_audio_sync_filter_handles_audio_starting_before_video(self) -> None:
        self.assertEqual(
            schedule_splitter._audio_sync_filter(-0.069),
            "asetpts=PTS-STARTPTS+0.069000/TB,aresample=async=1:first_pts=0",
        )

    def test_prepare_ts_source_uses_stream_copy_and_validates_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ts"
            source.write_bytes(b"source" * 400)
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                Path(command[-1]).write_bytes(b"m" * 4096)
                return SimpleNamespace(returncode=0, stderr=b"")

            with (
                mock.patch.object(schedule_splitter, "_enough_normalize_space", return_value=True),
                mock.patch.object(schedule_splitter.subprocess, "run", side_effect=fake_run),
                mock.patch.object(
                    schedule_splitter,
                    "_probe_av_start_offset_seconds",
                    side_effect=[0.147, 0.147],
                ) as offset,
            ):
                normalized = schedule_splitter._prepare_ts_source(
                    str(source), str(root), "ffmpeg"
                )

            self.assertTrue(Path(normalized).is_file())
            self.assertEqual(len(commands), 1)
            self.assertIn("copy", commands[0])
            self.assertNotIn("libx264", commands[0])
            self.assertEqual(offset.call_count, 2)
            self.assertEqual(offset.call_args_list[0].args, (str(source), "ffmpeg"))
            self.assertEqual(offset.call_args_list[1].args, (normalized, "ffmpeg"))

    def test_prepare_ts_source_rejects_unverifiable_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ts"
            source.write_bytes(b"source" * 400)

            def fake_run(command, **_kwargs):
                Path(command[-1]).write_bytes(b"m" * 4096)
                return SimpleNamespace(returncode=0, stderr=b"")

            with (
                mock.patch.object(schedule_splitter, "_enough_normalize_space", return_value=True),
                mock.patch.object(schedule_splitter.subprocess, "run", side_effect=fake_run),
                mock.patch.object(
                    schedule_splitter,
                    "_probe_av_start_offset_seconds",
                    side_effect=[0.147, None],
                ),
            ):
                normalized = schedule_splitter._prepare_ts_source(
                    str(source), str(root), "ffmpeg"
                )

            self.assertIsNone(normalized)
            self.assertEqual(list(root.glob("normalized_*.mp4")), [])

    def test_temp_normalization_is_cleaned_when_cut_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.ts"
            source.write_bytes(b"source")
            cache_dir = root / "schedule-cache"
            cache_dir.mkdir()

            with (
                mock.patch.object(schedule_splitter, "_probe_durations", return_value=[100.0]),
                mock.patch.object(schedule_splitter, "_create_schedule_temp_dir", return_value=str(cache_dir)),
                mock.patch.object(schedule_splitter, "_prepare_ts_source", side_effect=RuntimeError("boom")),
            ):
                results = schedule_splitter.extract_by_schedule(
                    [{"name": "product", "segments": [(0.0, 10.0)]}],
                    [str(source)],
                    str(root / "output"),
                    ffmpeg="ffmpeg",
                )

            self.assertEqual(results, [])
            self.assertFalse(cache_dir.exists())


if __name__ == "__main__":
    unittest.main()
