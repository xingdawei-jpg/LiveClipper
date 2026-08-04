from __future__ import annotations

import importlib
import io
import inspect
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

cutter_logic = importlib.import_module("cutter_logic")
local_asr = importlib.import_module("local_asr")
local_asr_quality = importlib.import_module("local_asr_quality")
stt = importlib.import_module("stt")
volcengine_asr = importlib.import_module("volcengine_asr")


def _timed_characters(text: str, step: float = 0.1) -> list[dict[str, float | str]]:
    return [
        {"text": char, "start": round(index * step, 3), "end": round((index + 1) * step, 3)}
        for index, char in enumerate(text)
    ]


class LocalAsrQualityTests(unittest.TestCase):
    def test_context_bound_fashion_corrections_preserve_each_token_time(self) -> None:
        source = "这个板型很好高织数色只面料小个字也可以穿显受效果遮月效果"
        segment = {
            "text": source + "？？？",
            "start": 0.0,
            "end": len(source) * 0.1,
            "words": _timed_characters(source),
        }

        corrected, count = local_asr_quality.apply_domain_corrections([segment])

        self.assertEqual(count, 6)
        self.assertEqual(
            corrected[0]["text"],
            "这个版型很好高支数色织面料小个子也可以穿显瘦效果遮肉效果？",
        )
        self.assertEqual(
            "".join(word["text"] for word in corrected[0]["words"]),
            "这个版型很好高支数色织面料小个子也可以穿显瘦效果遮肉效果",
        )
        for before, after in zip(segment["words"], corrected[0]["words"]):
            self.assertEqual((after["start"], after["end"]), (before["start"], before["end"]))

    def test_user_exact_correction_can_merge_tokens_without_inventing_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            corrections = Path(temp_dir) / "corrections.json"
            corrections.write_text(
                json.dumps({"replacements": {"艾克斯艾斯": "XS"}}, ensure_ascii=False),
                encoding="utf-8",
            )
            segment = {
                "text": "艾克斯艾斯码",
                "start": 0.0,
                "end": 0.6,
                "words": _timed_characters("艾克斯艾斯码"),
            }
            with mock.patch.dict(os.environ, {"LIVECLIPPER_ASR_CORRECTIONS": str(corrections)}):
                corrected, count = local_asr_quality.apply_domain_corrections([segment])

        self.assertEqual(count, 1)
        self.assertEqual(corrected[0]["text"], "XS码")
        self.assertEqual([word["text"] for word in corrected[0]["words"]], ["XS", "码"])
        self.assertEqual((corrected[0]["words"][0]["start"], corrected[0]["words"][0]["end"]), (0.0, 0.5))
        self.assertEqual((corrected[0]["words"][1]["start"], corrected[0]["words"][1]["end"]), (0.5, 0.6))

    def test_model_loader_enables_punctuation_and_uses_shorter_vad(self) -> None:
        calls = []
        sentinel = object()

        def fake_auto_model(**kwargs):
            calls.append(kwargs)
            return sentinel

        fake_auto_module = types.ModuleType("funasr.auto.auto_model")
        fake_auto_module.AutoModel = fake_auto_model
        fake_auto_package = types.ModuleType("funasr.auto")
        fake_funasr = types.ModuleType("funasr")
        with mock.patch.dict(sys.modules, {
            "funasr": fake_funasr,
            "funasr.auto": fake_auto_package,
            "funasr.auto.auto_model": fake_auto_module,
        }):
            with mock.patch.object(local_asr, "_register_sensevoice_components"):
                with mock.patch.object(local_asr, "_sensevoice_model_dir", return_value="C:\\models\\SenseVoice"):
                    local_asr._SENSEVOICE_MODEL = None
                    local_asr._SENSEVOICE_PUNCTUATION = False
                    loaded = local_asr._load_sensevoice()

        self.assertIs(loaded, sentinel)
        self.assertTrue(local_asr._SENSEVOICE_PUNCTUATION)
        self.assertEqual(calls[0]["punc_model"], "ct-punc")
        self.assertEqual(calls[0]["vad_kwargs"]["max_single_segment_time"], 15000)
        local_asr._SENSEVOICE_MODEL = None

    def test_model_loader_falls_back_when_punctuation_model_is_unavailable(self) -> None:
        calls = []
        sentinel = object()

        def fake_auto_model(**kwargs):
            calls.append(kwargs)
            if kwargs.get("punc_model"):
                raise RuntimeError("punc unavailable")
            return sentinel

        fake_auto_module = types.ModuleType("funasr.auto.auto_model")
        fake_auto_module.AutoModel = fake_auto_model
        fake_auto_package = types.ModuleType("funasr.auto")
        fake_funasr = types.ModuleType("funasr")
        with mock.patch.dict(sys.modules, {
            "funasr": fake_funasr,
            "funasr.auto": fake_auto_package,
            "funasr.auto.auto_model": fake_auto_module,
        }):
            with mock.patch.object(local_asr, "_register_sensevoice_components"):
                with mock.patch.object(local_asr, "_sensevoice_model_dir", return_value="C:\\models\\SenseVoice"):
                    local_asr._SENSEVOICE_MODEL = None
                    local_asr._SENSEVOICE_PUNCTUATION = False
                    loaded = local_asr._load_sensevoice()

        self.assertIs(loaded, sentinel)
        self.assertFalse(local_asr._SENSEVOICE_PUNCTUATION)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("punc_model", calls[1])
        local_asr._SENSEVOICE_MODEL = None

    def test_model_dir_reuses_modelscope_snapshot_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot = Path(temp_dir) / "models" / "iic--SenseVoiceSmall" / "snapshots" / "master"
            snapshot.mkdir(parents=True)
            (snapshot / "model.pt").write_bytes(b"model")
            with mock.patch.dict(os.environ, {"MODELSCOPE_CACHE": temp_dir}, clear=False):
                model_dir = local_asr._cached_sensevoice_model_dir()

        self.assertEqual(Path(model_dir), snapshot)

    def test_modelscope_work_redirects_windowed_progress_streams(self) -> None:
        observed = {}

        def fake_modelscope_operation():
            observed["stdout"] = sys.stdout
            observed["stderr"] = sys.stderr
            return "done"

        result = local_asr._run_modelscope_quietly(fake_modelscope_operation)

        self.assertEqual(result, "done")
        self.assertIsNot(observed["stdout"], sys.stdout)
        self.assertIsNot(observed["stderr"], sys.stderr)

    def test_registry_load_tolerates_frozen_modules_without_source_text(self) -> None:
        tables = types.SimpleNamespace(
            tokenizer_classes={"SentencepiecesTokenizer": object, "CharTokenizer": object},
            frontend_classes={"WavFrontend": object, "WavFrontendOnline": object},
            encoder_classes={"SenseVoiceEncoderSmall": object, "FSMN": object, "SANMEncoder": object},
            model_classes={"SenseVoiceSmall": object, "FsmnVADStreaming": object, "CTTransformer": object},
            specaug_classes={"SpecAugLFR": object},
        )
        fake_funasr = types.ModuleType("funasr")
        fake_register = types.ModuleType("funasr.register")
        fake_register.tables = tables
        imported = []

        def fake_import(module_name):
            imported.append(module_name)
            # Simulate FunASR's class decorator consulting source locations
            # while importing a module from a PyInstaller bytecode archive.
            self.assertEqual(local_asr.inspect.getsourcelines(object)[1], 0)
            return types.ModuleType(module_name)

        with mock.patch.dict(sys.modules, {"funasr": fake_funasr, "funasr.register": fake_register}):
            with mock.patch.object(local_asr.inspect, "getsourcelines", side_effect=OSError("no source")):
                with mock.patch.object(local_asr.importlib, "import_module", side_effect=fake_import):
                    local_asr._register_sensevoice_components()

        self.assertEqual(imported, list(local_asr._SENSEVOICE_REGISTRATION_MODULES))

    def test_srt_is_resegmented_but_sidecar_keeps_corrected_ctc_tokens(self) -> None:
        spoken = "这个板型很好小个字也可以穿"
        punctuated = "这个板型很好。小个字也可以穿。"
        timestamps = [[index * 400, (index + 1) * 400] for index in range(len(spoken))]

        class FakeModel:
            def __init__(self):
                self.kwargs = None

            def generate(self, **kwargs):
                self.kwargs = kwargs
                return [{"text": punctuated, "timestamp": timestamps}]

        model = FakeModel()
        local_asr._SENSEVOICE_MODEL = model
        local_asr._SENSEVOICE_PUNCTUATION = True
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                srt_path = Path(temp_dir) / "quality.srt"
                self.assertTrue(local_asr.sensevoice_to_srt("fake.wav", str(srt_path)))
                srt = srt_path.read_text(encoding="utf-8")
                sidecar = json.loads((Path(temp_dir) / "quality.words.json").read_text(encoding="utf-8"))
        finally:
            local_asr._SENSEVOICE_MODEL = None
            local_asr._SENSEVOICE_PUNCTUATION = False

        self.assertFalse(model.kwargs["merge_vad"])
        self.assertNotIn("merge_length_s", model.kwargs)
        self.assertTrue(model.kwargs["output_timestamp"])
        self.assertIn("这个版型很好。", srt)
        self.assertIn("小个子也可以穿。", srt)
        self.assertIn("\n2\n", srt)
        self.assertEqual(sidecar["provider"], "sensevoice")
        self.assertEqual(
            "".join(word["text"] for word in sidecar["segments"][0]["words"]),
            "这个版型很好小个子也可以穿",
        )
        self.assertEqual(sidecar["segments"][0]["words"][0]["start"], 0.0)
        self.assertEqual(sidecar["segments"][0]["words"][-1]["end"], len(spoken) * 0.4)

    def test_mix_cache_copies_srt_and_its_word_timing_sidecar_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_srt = root / "generated.srt"
            destination_srt = root / "video.srt"
            source_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n测试\n", encoding="utf-8")
            source_sidecar = root / "generated.words.json"
            source_sidecar.write_text(
                json.dumps({"provider": "sensevoice", "segments": [{"words": _timed_characters("测试")}]}),
                encoding="utf-8",
            )

            copied_timing = cutter_logic._copy_srt_with_word_timing_sidecar(source_srt, destination_srt)

            self.assertTrue(copied_timing)
            self.assertEqual(destination_srt.read_text(encoding="utf-8"), source_srt.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads((root / "video.words.json").read_text(encoding="utf-8")),
                json.loads(source_sidecar.read_text(encoding="utf-8")),
            )

    def test_mix_cache_removes_stale_word_timing_when_source_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_srt = root / "generated.srt"
            destination_srt = root / "video.srt"
            source_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n新字幕\n", encoding="utf-8")
            stale_sidecar = root / "video.words.json"
            stale_sidecar.write_text("{}", encoding="utf-8")

            copied_timing = cutter_logic._copy_srt_with_word_timing_sidecar(source_srt, destination_srt)

            self.assertFalse(copied_timing)
            self.assertFalse(stale_sidecar.exists())

    def test_mix_sensevoice_fallback_keeps_srt_and_word_timing_together(self) -> None:
        source = inspect.getsource(cutter_logic.process_video_mix)

        self.assertIn("_copy_srt_with_word_timing_sidecar(", source)
        self.assertIn("source_video=original_vp", source)
        self.assertIn("provider=\"sensevoice\"", source)
        self.assertIn("cancel_event=cancel_event", source)
        self.assertNotIn("shutil.copy2(_temp, _sc)", source)

    def test_shared_audio_transcriber_uses_isolated_sensevoice_worker(self) -> None:
        with mock.patch.object(stt, "_run_local_asr_worker", return_value=True) as worker:
            recognized = stt.transcribe_local_audio_to_srt(
                "final.wav",
                "final.srt",
                asr_engine="sensevoice",
            )

        self.assertTrue(recognized)
        worker.assert_called_once_with(
            "final.wav",
            "final.srt",
            log_fn=None,
            cancel_event=None,
        )

    def test_shared_audio_transcriber_returns_failure_without_an_engine_fallback(self) -> None:
        logs = []
        with mock.patch.object(stt, "_run_local_asr_worker", return_value=False):
            recognized = stt.transcribe_local_audio_to_srt("final.wav", "final.srt", logs.append)

        self.assertFalse(recognized)

    def test_source_worker_streams_logs_and_keeps_word_timing_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker_script = root / "fake_local_asr_worker.py"
            output_srt = root / "result.srt"
            worker_script.write_text(
                "\n".join((
                    "import json, sys",
                    "from pathlib import Path",
                    "target = Path(sys.argv[2])",
                    "target.write_text('1\\n00:00:00,000 --> 00:00:01,000\\n测试\\n', encoding='utf-8')",
                    "target.with_suffix('.words.json').write_text(json.dumps({'provider': 'sensevoice'}), encoding='utf-8')",
                    "print('internal progress 100%', flush=True)",
                    "print(json.dumps({'type': 'log', 'message': 'worker log'}), flush=True)",
                    "print(json.dumps({'type': 'result', 'ok': True}), flush=True)",
                )),
                encoding="utf-8",
            )
            logs = []
            with mock.patch.dict(
                os.environ,
                {"LIVECLIPPER_LOCAL_ASR_WORKER": str(worker_script)},
                clear=False,
            ):
                recognized = stt.transcribe_local_audio_to_srt(
                    "fake.wav",
                    str(output_srt),
                    logs.append,
                )

            self.assertTrue(recognized)
            self.assertTrue(output_srt.is_file())
            self.assertTrue(output_srt.with_suffix(".words.json").is_file())
            self.assertTrue(any("worker log" in message for message in logs))
            self.assertFalse(any("internal progress" in message for message in logs))
            self.assertTrue(any("模型内存已释放" in message for message in logs))

    def test_task_scoped_worker_reuses_one_process_for_multiple_final_subtitles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker_script = root / "serve_local_asr_worker.py"
            first_srt = root / "first.srt"
            second_srt = root / "second.srt"
            worker_script.write_text(
                "\n".join((
                    "import json, sys",
                    "from pathlib import Path",
                    "assert sys.argv[1:] == ['--serve']",
                    "for raw in sys.stdin:",
                    "    request = json.loads(raw)",
                    "    if request.get('command') == 'shutdown':",
                    "        break",
                    "    target = Path(request['srt_output'])",
                    "    target.write_text('1\\n00:00:00,000 --> 00:00:01,000\\n测试\\n', encoding='utf-8')",
                    "    print(json.dumps({'type': 'log', 'id': request['id'], 'message': 'worker reused'}), flush=True)",
                    "    print(json.dumps({'type': 'result', 'id': request['id'], 'ok': True}), flush=True)",
                )),
                encoding="utf-8",
            )
            logs = []
            with mock.patch.dict(
                os.environ,
                {"LIVECLIPPER_LOCAL_ASR_WORKER": str(worker_script)},
                clear=False,
            ):
                session = stt.LocalASRWorkerSession()
                try:
                    self.assertTrue(session.transcribe("first.wav", str(first_srt), log_fn=logs.append))
                    self.assertTrue(session.transcribe("second.wav", str(second_srt), log_fn=logs.append))
                finally:
                    session.close(logs.append)

            self.assertTrue(first_srt.is_file())
            self.assertTrue(second_srt.is_file())
            self.assertEqual(sum("正在启动可复用" in message for message in logs), 1)
            self.assertEqual(sum("worker reused" in message for message in logs), 2)
            self.assertTrue(any("模型内存已释放" in message for message in logs))

    def test_failed_worker_removes_stale_srt_and_word_timing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            worker_script = root / "failed_local_asr_worker.py"
            output_srt = root / "stale.srt"
            output_srt.write_text("stale", encoding="utf-8")
            output_srt.with_suffix(".words.json").write_text("{}", encoding="utf-8")
            worker_script.write_text(
                "import json\n"
                "print(json.dumps({'type': 'result', 'ok': False}), flush=True)\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {"LIVECLIPPER_LOCAL_ASR_WORKER": str(worker_script)},
                clear=False,
            ):
                recognized = stt.transcribe_local_audio_to_srt("fake.wav", str(output_srt))

            self.assertFalse(recognized)
            self.assertFalse(output_srt.exists())
            self.assertFalse(output_srt.with_suffix(".words.json").exists())

    def test_worker_timeout_terminates_process_and_reports_memory_release(self) -> None:
        class FakeProcess:
            def __init__(self) -> None:
                self.stdout = io.StringIO("")
                self.killed = False

            def poll(self):
                return 1 if self.killed else None

            def kill(self) -> None:
                self.killed = True

            def wait(self, timeout=None):
                return 1

        process = FakeProcess()
        logs = []
        with tempfile.TemporaryDirectory() as temp_dir:
            output_srt = Path(temp_dir) / "timeout.srt"
            with (
                mock.patch.object(stt, "_local_asr_worker_script", return_value=Path("worker.py")),
                mock.patch.object(stt, "_local_asr_worker_command", return_value=["worker"]),
                mock.patch.object(stt.subprocess, "Popen", return_value=process),
                mock.patch.object(stt.time, "monotonic", side_effect=[0.0, 61.0]),
                mock.patch.dict(os.environ, {"LIVECLIPPER_LOCAL_ASR_TIMEOUT_SECONDS": "60"}, clear=False),
            ):
                recognized = stt.transcribe_local_audio_to_srt(
                    "fake.wav",
                    str(output_srt),
                    logs.append,
                )

        self.assertFalse(recognized)
        self.assertTrue(process.killed)
        self.assertTrue(any("识别超时" in message for message in logs))
        self.assertTrue(any("模型内存已释放" in message for message in logs))

    def test_frozen_worker_command_reenters_desktop_tool_runner(self) -> None:
        worker = Path("C:/bundle/tools/local_asr_worker.py")
        with mock.patch.object(stt.sys, "frozen", True, create=True):
            with mock.patch.object(stt.sys, "executable", "C:/LiveClipper/LiveClipperWeb.exe"):
                command = stt._local_asr_worker_command(worker, "audio.wav", "result.srt")

        self.assertEqual(command[0], "C:/LiveClipper/LiveClipperWeb.exe")
        self.assertEqual(command[1], "--liveclipper-run-tool")
        self.assertEqual(command[2], str(worker))

    def test_desktop_package_includes_local_asr_worker_script(self) -> None:
        spec_source = (ROOT / "web_client" / "liveclipper_web.spec").read_text(encoding="utf-8")
        manifest_source = (ROOT / "tools" / "build_update_manifest.py").read_text(encoding="utf-8")

        self.assertIn('"local_asr_worker.py"', spec_source)
        self.assertIn('WEB_DIR / "tools" / "local_asr_worker.py"', manifest_source)

    def test_final_subtitle_stage_uses_shared_local_asr_and_word_timing(self) -> None:
        semantic = [{"start": 1.2, "end": 2.8, "text": "亚麻肤感很舒服", "words": []}]
        logs = []
        with tempfile.TemporaryDirectory() as temp_dir:
            local_srt = Path(temp_dir) / "final_local_asr.srt"

            def fake_transcribe(audio_path, srt_output, **kwargs):
                Path(srt_output).write_text("", encoding="utf-8")
                return True

            with mock.patch.object(stt, "transcribe_local_audio_to_srt", side_effect=fake_transcribe) as transcribe:
                with mock.patch.object(volcengine_asr, "load_word_timing_sidecar", return_value=semantic):
                    segments = cutter_logic._final_subtitle_local_asr_segments(
                        "final.wav",
                        temp_dir,
                        {},
                        logs.append,
                    )

        self.assertEqual(segments, [{"start": 1.2, "end": 2.8, "text": "亚麻肤感很舒服"}])
        self.assertEqual(
            transcribe.call_args.kwargs,
            {"log_fn": logs.append, "cancel_event": None},
        )
        self.assertTrue(any("本地 SenseVoice" in message for message in logs))

    def test_final_subtitle_stage_uses_shared_sensevoice_entrypoint(self) -> None:
        source = inspect.getsource(cutter_logic._add_subtitles_final)

        self.assertNotIn("from faster_whisper", source)
        self.assertIn("_run_local_asr", source)

    def test_final_subtitle_ai_repair_preserves_final_asr_timestamps(self) -> None:
        raw_segments = [
            {"start": 1.25, "end": 2.5, "text": "板型很显受"},
            {"start": 3.0, "end": 4.25, "text": "小个字也能穿"},
        ]
        repaired = cutter_logic._apply_final_subtitle_text_repairs(
            raw_segments,
            "[90.00-91.00] 版型很显瘦\n[92.00-93.00] 小个子也能穿",
        )

        self.assertEqual(
            repaired,
            [
                {"start": 1.25, "end": 2.5, "text": "版型很显瘦"},
                {"start": 3.0, "end": 4.25, "text": "小个子也能穿"},
            ],
        )

    def test_final_subtitle_ai_repair_rejects_missing_lines(self) -> None:
        raw_segments = [
            {"start": 1.0, "end": 2.0, "text": "第一句"},
            {"start": 2.0, "end": 3.0, "text": "第二句"},
        ]

        repaired = cutter_logic._apply_final_subtitle_text_repairs(
            raw_segments,
            "[1.00-2.00] 只返回了一句",
        )

        self.assertIsNone(repaired)

    def test_smart_cut_final_subtitles_always_re_recognize_assembled_audio(self) -> None:
        source = inspect.getsource(cutter_logic.process_video)

        self.assertIn("重新识别最终成片音频", source)
        self.assertIn("_add_subtitles_final(", source)
        self.assertNotIn("_burn_mapped_subtitles_final(", source)

    def test_local_asr_entrypoint_has_no_whisper_fallback(self) -> None:
        source = inspect.getsource(stt.transcribe_local_audio_to_srt)

        self.assertNotIn("transcribe_to_srt", source)
        self.assertNotIn("faster_whisper", inspect.getsource(stt))


if __name__ == "__main__":
    unittest.main()
