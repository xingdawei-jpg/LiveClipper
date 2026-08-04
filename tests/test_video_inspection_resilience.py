from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))

server = importlib.import_module("server")


class VideoInspectionResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        server._VIDEO_INFO_CACHE.clear()

    def tearDown(self) -> None:
        server._VIDEO_INFO_CACHE.clear()

    def test_frontend_retries_transient_failures_and_exposes_manual_retry(self) -> None:
        source = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('if (action === "retry-video-inspection") retryVideoInspection', source)
        self.assertIn("inspectVideoList(targetId, currentLines, true);", source)
        self.assertIn("item?.retryable && !item?.valid", source)
        self.assertIn('title="重新检测"', source)

    def test_timeout_is_retryable_and_is_not_cached(self) -> None:
        probe_payload = {
            "format": {"duration": "12.5"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
                {"codec_type": "audio"},
            ],
        }
        completed = subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=json.dumps(probe_payload).encode("utf-8"),
            stderr=b"",
        )

        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "clip.mp4"
            video.write_bytes(b"video fixture")
            with mock.patch.object(server, "_ffprobe_cmd", return_value="ffprobe"), mock.patch.object(
                server.subprocess,
                "run",
                side_effect=[subprocess.TimeoutExpired(["ffprobe", str(video)], 20), completed],
            ) as run_probe:
                failed = server._probe_video_info(str(video))
                recovered = server._probe_video_info(str(video))
                cached = server._probe_video_info(str(video))

        self.assertFalse(failed["valid"])
        self.assertTrue(failed["retryable"])
        self.assertEqual(failed["error_code"], "probe_timeout")
        self.assertEqual(failed["message"], "视频检测超时，可点击重新检测")
        self.assertNotIn("Command", failed["message"])
        self.assertTrue(recovered["valid"])
        self.assertEqual(recovered["duration"], 12.5)
        self.assertEqual(recovered["resolution"], "1920x1080")
        self.assertTrue(recovered["has_audio"])
        self.assertEqual(cached, recovered)
        self.assertEqual(run_probe.call_count, 2)

    def test_ffprobe_failure_is_not_reused_as_a_permanent_invalid_result(self) -> None:
        failed_probe = subprocess.CompletedProcess(args=["ffprobe"], returncode=1, stdout=b"", stderr=b"busy")
        valid_probe = subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=json.dumps(
                {
                    "format": {"duration": "3"},
                    "streams": [{"codec_type": "video", "width": 720, "height": 1280, "r_frame_rate": "25/1"}],
                }
            ).encode("utf-8"),
            stderr=b"",
        )

        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "clip.mp4"
            video.write_bytes(b"video fixture")
            with mock.patch.object(server, "_ffprobe_cmd", return_value="ffprobe"), mock.patch.object(
                server.subprocess, "run", side_effect=[failed_probe, valid_probe]
            ) as run_probe:
                failed = server._probe_video_info(str(video))
                recovered = server._probe_video_info(str(video))

        self.assertFalse(failed["valid"])
        self.assertTrue(failed["retryable"])
        self.assertEqual(failed["error_code"], "probe_failed")
        self.assertTrue(recovered["valid"])
        self.assertEqual(run_probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
