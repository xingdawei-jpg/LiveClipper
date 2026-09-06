from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

schedule_splitter = importlib.import_module("schedule_splitter")


class ScheduleSplitterFastTsTests(unittest.TestCase):
    def test_independent_anchors_share_preview_and_fast_export_without_filling_gaps(self) -> None:
        videos = ["first.mp4", "later.mp4"]
        groups = [{"name": "product", "segments": [(90, 220)]}]
        with mock.patch.object(schedule_splitter, "_probe_durations", return_value=[100, 100]):
            covered, timeline = schedule_splitter.build_schedule_coverage(
                groups, videos, video_start_offsets={videos[0]: 0, videos[1]: 200})
        record = covered[0]["ranges"][0]
        self.assertEqual(record["status"], "partial")
        self.assertEqual(record["missing_duration"], 100)
        expected = [(part["video_path"], part["file_start"], part["duration"]) for part in record["parts"]]
        self.assertEqual(expected, [("first.mp4", 90, 10), ("later.mp4", 0, 20)])
        calls = []

        def copy(_ffmpeg, start, duration, video, output):
            calls.append((video, start, duration))
            Path(output).write_bytes(b"x" * 2048)
            return SimpleNamespace(returncode=0, stderr=b"")

        with tempfile.TemporaryDirectory() as output, mock.patch.object(schedule_splitter, "_fast_copy_segment", side_effect=copy), mock.patch.object(schedule_splitter, "_probe_durations", side_effect=AssertionError("export must use preview timeline")):
            results = schedule_splitter.extract_by_schedule(groups, videos, output, fast_copy=True, source_timeline=timeline)
        self.assertEqual(calls, expected)
        self.assertEqual(len(results), 2)

    def test_overlaps_are_exported_once_and_short_ranges_remain_available(self) -> None:
        timeline = [
            {"video": "one.mp4", "name": "one.mp4", "start": 1800, "end": 3601.32},
            {"video": "two.mp4", "name": "two.mp4", "start": 3600, "end": 5400},
        ]
        parts = schedule_splitter._timeline_parts(2159, 4456, timeline)
        self.assertEqual([(p["file_start"], p["file_end"]) for p in parts], [(359, 1800), (0, 856)])
        self.assertEqual(sum(p["duration"] for p in parts), 2297)
        calls = []

        def copy(_ffmpeg, start, duration, video, output):
            calls.append((video, start, duration))
            Path(output).write_bytes(b"x" * 2048)
            return SimpleNamespace(returncode=0, stderr=b"")

        with tempfile.TemporaryDirectory() as output, mock.patch.object(schedule_splitter, "_fast_copy_segment", side_effect=copy):
            schedule_splitter.extract_by_schedule([{"name": "short", "segments": [(3602, 3607)]}], [], output, fast_copy=True, source_timeline=timeline)
        self.assertEqual(calls, [("two.mp4", 2, 5)])

    def test_clock_time_basis_keeps_two_part_clock_values_and_crosses_midnight(self) -> None:
        self.assertEqual(
            schedule_splitter._parse_schedule_time_value("12:20", time_basis="relative"),
            12 * 60 + 20,
        )
        self.assertEqual(
            schedule_splitter._parse_schedule_time_value("12:20", time_basis="clock"),
            12 * 3600 + 20 * 60,
        )
        schedule = [
            {"name": "daytime", "start_offset": 12 * 3600 + 20 * 60 + 4, "end_offset": 12 * 3600 + 45 * 60 + 15},
            {"name": "overnight", "start_offset": 23 * 3600 + 55 * 60, "end_offset": 15 * 60},
        ]
        schedule_splitter.normalize_clock_schedule_to_live_offsets(
            schedule,
            datetime(2026, 8, 5, 12, 19, 0),
        )
        self.assertEqual((schedule[0]["start_offset"], schedule[0]["end_offset"]), (64, 1575))
        self.assertEqual((schedule[1]["start_offset"], schedule[1]["end_offset"]), (41760, 42960))

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

    def test_product_fast_mode_skips_ts_normalization_validation_and_reencode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.ts"
            second = root / "second.ts"
            first.write_bytes(b"source")
            second.write_bytes(b"source")
            output = root / "output"
            commands = []

            def fake_run(command, **_kwargs):
                commands.append(command)
                target = Path(command[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * 2048)
                return SimpleNamespace(returncode=0, stderr=b"")

            with (
                mock.patch.object(schedule_splitter, "_probe_durations", return_value=[30.0, 30.0]),
                mock.patch.object(
                    schedule_splitter,
                    "_prepare_ts_source",
                    side_effect=AssertionError("fast mode must not normalize TS"),
                ),
                mock.patch.object(
                    schedule_splitter,
                    "_probe_av_start_offset_seconds",
                    side_effect=AssertionError("fast mode must not inspect output timing"),
                ),
                mock.patch.object(
                    schedule_splitter,
                    "_sync_reencode_segment",
                    side_effect=AssertionError("fast mode must not reencode"),
                ),
                mock.patch.object(schedule_splitter.subprocess, "run", side_effect=fake_run),
            ):
                results = schedule_splitter.extract_by_schedule(
                    [{"name": "product", "segments": [(5.0, 55.0)]}],
                    [str(first), str(second)],
                    str(output),
                    ffmpeg="ffmpeg",
                    fast_copy=True,
                )

            self.assertEqual(len(results), 2)
            self.assertTrue(all(item["cut_mode"] == "fast-copy" for item in results))
            self.assertEqual(len(commands), 2)
            for command in commands:
                self.assertLess(command.index("-ss"), command.index("-i"))
                self.assertEqual(command[command.index("-c") + 1], "copy")
                self.assertNotIn("libx264", command)

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
