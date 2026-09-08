from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT))

import stt
from web_client import server


class LocalASRRuntimeRecoveryTests(unittest.TestCase):
    def test_one_shot_worker_promotes_memory_failure_and_hides_mac_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker = root / "worker.py"
            output = root / "result.srt"
            worker.write_text(
                "import json\n"
                "print('Notice: ffmpeg is not installed.', flush=True)\n"
                "print('# brew install ffmpeg # mac', flush=True)\n"
                "print(json.dumps({'type':'log','message':"
                "'SenseVoice model unavailable: DefaultCPUAllocator: not enough memory'}), flush=True)\n"
                "print(json.dumps({'type':'result','ok':False}), flush=True)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            logs: list[str] = []
            with mock.patch.dict(
                os.environ, {"LIVECLIPPER_LOCAL_ASR_WORKER": str(worker)}, clear=False,
            ):
                with self.assertRaises(stt.LocalASRFailure) as raised:
                    stt.transcribe_local_audio_to_srt("audio.wav", str(output), logs.append)

        self.assertEqual(raised.exception.code, "local_asr_out_of_memory")
        self.assertIn("虚拟内存不足", str(raised.exception))
        self.assertFalse(any("brew install" in line for line in logs))
        self.assertFalse(output.exists())

    def test_worker_adds_frozen_ffmpeg_directory_to_path(self) -> None:
        module_path = ROOT / "web_client" / "tools" / "local_asr_worker.py"
        spec = importlib.util.spec_from_file_location("local_asr_worker_recovery_test", module_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            ffmpeg_dir = root / "ffmpeg"
            ffmpeg_dir.mkdir()
            executable = ffmpeg_dir / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
            executable.write_bytes(b"test")
            with (
                mock.patch.object(module.sys, "_MEIPASS", str(root), create=True),
                mock.patch.dict(module.os.environ, {"PATH": "C:\\Windows"}, clear=False),
            ):
                resolved = module._configure_ffmpeg_path()
                first_path = module.os.environ["PATH"].split(os.pathsep)[0]

        self.assertEqual(Path(resolved), executable.resolve())
        self.assertEqual(Path(first_path), ffmpeg_dir.resolve())

    def test_server_preserves_actionable_memory_failure(self) -> None:
        failure = stt.LocalASRFailure("本机内存不足", code="local_asr_out_of_memory")
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "source.ts"
            video.write_bytes(b"video")
            with (
                mock.patch.object(server, "_load_settings", return_value={"asr_enabled": False}),
                mock.patch.object(server, "emit_log"),
                mock.patch.object(stt, "generate_srt", side_effect=failure),
            ):
                with self.assertRaises(stt.LocalASRFailure) as raised:
                    server._ensure_srt(video, "smart-cut")

        self.assertEqual(raised.exception.code, "local_asr_out_of_memory")


if __name__ == "__main__":
    unittest.main()
