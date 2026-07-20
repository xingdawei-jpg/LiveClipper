from __future__ import annotations

import importlib
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

        fake_funasr = types.SimpleNamespace(AutoModel=fake_auto_model)
        with mock.patch.dict(sys.modules, {"funasr": fake_funasr}):
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

        fake_funasr = types.SimpleNamespace(AutoModel=fake_auto_model)
        with mock.patch.dict(sys.modules, {"funasr": fake_funasr}):
            with mock.patch.object(local_asr, "_sensevoice_model_dir", return_value="C:\\models\\SenseVoice"):
                local_asr._SENSEVOICE_MODEL = None
                local_asr._SENSEVOICE_PUNCTUATION = False
                loaded = local_asr._load_sensevoice()

        self.assertIs(loaded, sentinel)
        self.assertFalse(local_asr._SENSEVOICE_PUNCTUATION)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("punc_model", calls[1])
        local_asr._SENSEVOICE_MODEL = None

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

    def test_shared_audio_transcriber_prefers_sensevoice_without_whisper(self) -> None:
        calls = []

        def fake_sensevoice(audio_path, srt_output, log_fn=None):
            calls.append((audio_path, srt_output))
            return True

        fake_local_asr = types.SimpleNamespace(sensevoice_to_srt=fake_sensevoice)
        with mock.patch.dict(sys.modules, {"local_asr": fake_local_asr}):
            with mock.patch.object(stt, "transcribe_to_srt") as whisper_transcribe:
                recognized = stt.transcribe_local_audio_to_srt(
                    "final.wav",
                    "final.srt",
                    asr_engine="sensevoice",
                )

        self.assertTrue(recognized)
        self.assertEqual(calls, [("final.wav", "final.srt")])
        whisper_transcribe.assert_not_called()

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
                        {"local_asr_engine": "sensevoice", "whisper_model": "small"},
                        logs.append,
                    )

        self.assertEqual(segments, [{"start": 1.2, "end": 2.8, "text": "亚麻肤感很舒服"}])
        self.assertEqual(transcribe.call_args.kwargs["asr_engine"], "sensevoice")
        self.assertEqual(transcribe.call_args.kwargs["whisper_model"], "small")
        self.assertTrue(any("本地 SenseVoice" in message for message in logs))

    def test_final_subtitle_stage_no_longer_constructs_whisper_directly(self) -> None:
        source = inspect.getsource(cutter_logic._add_subtitles_final)

        self.assertNotIn("from faster_whisper", source)
        self.assertNotIn("_run_whisper", source)
        self.assertIn("_run_local_asr", source)


if __name__ == "__main__":
    unittest.main()
