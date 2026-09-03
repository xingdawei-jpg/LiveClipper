import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from asr_cache import write_metadata  # noqa: E402
from semantic_word_binder import (  # noqa: E402
    bind_candidates_by_semantic_srt,
    build_semantic_srt_word_timeline,
)


def _write_pair(root: Path, srt_text: str, words: list[dict]) -> Path:
    video = root / "source.mp4"
    srt = root / "source.srt"
    video.write_bytes(b"source-video")
    srt.write_text(srt_text, encoding="utf-8")
    (root / "source.words.json").write_text(
        json.dumps({"schema": "liveclipper.word-timings.v1", "provider": "volcengine", "segments": [{
            "text": "".join(word["text"] for word in words), "start": words[0]["start"], "end": words[-1]["end"], "words": words,
        }]}),
        encoding="utf-8",
    )
    write_metadata(video, srt, provider="volcengine", timing_precision="word")
    return srt


class SemanticWordBinderTests(unittest.TestCase):
    def test_binds_resegmented_rows_to_unique_contiguous_word_spans(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            srt = _write_pair(Path(temp), "1\n00:00:00,000 --> 00:00:01,000\n这个版型肩膀会往里收。\n\n2\n00:00:01,000 --> 00:00:02,000\n然后整个人很利落。\n", [
                {"text": text, "start": index * 0.1, "end": index * 0.1 + 0.08}
                for index, text in enumerate("这个版型肩膀会往里收然后整个人很利落")
            ])
            timeline = build_semantic_srt_word_timeline(srt)
            report = timeline.report()

        self.assertEqual(report["coverage"], 1.0)
        self.assertEqual(report["ambiguous"], 0)
        self.assertEqual(report["unmatched"], 0)
        self.assertEqual((timeline.spans[0].word_start_index, timeline.spans[0].word_end_index), (0, 9))
        self.assertEqual((timeline.spans[1].word_start_index, timeline.spans[1].word_end_index), (10, 17))

    def test_allows_format_and_number_normalization_but_not_semantic_substitution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            srt = _write_pair(root, "1\n00:00:00,000 --> 00:00:01,000\nS码一百。\n", [
                {"text": text, "start": index * 0.1, "end": index * 0.1 + 0.08}
                for index, text in enumerate("s码100")
            ])
            normalized = build_semantic_srt_word_timeline(srt)
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n这个真的特别显瘦。\n", encoding="utf-8")
            # The cache hash no longer verifies; turn off that independent
            # prerequisite to assert the text matcher itself never guesses.
            mismatch = build_semantic_srt_word_timeline(srt, require_managed_identity=False)

        self.assertEqual(normalized.spans[0].alignment_kind, "normalized")
        self.assertEqual(mismatch.spans[0].status, "unmatched")

    def test_candidate_trace_requires_contiguous_verified_srt_origins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            srt = _write_pair(Path(temp), "1\n00:00:00,000 --> 00:00:01,000\n第一句。\n\n2\n00:00:01,000 --> 00:00:02,000\n第二句。\n", [
                {"text": text, "start": index * 0.1, "end": index * 0.1 + 0.08}
                for index, text in enumerate("第一句第二句")
            ])
            timeline = build_semantic_srt_word_timeline(srt)
            bound, reasons = bind_candidates_by_semantic_srt((
                SimpleNamespace(candidate_id=9, origin_subtitle_ids=(1, 2), text="第一句第二句。"),
            ), timeline)

        self.assertEqual(tuple(bound), (9,))
        self.assertEqual(reasons, ())
        self.assertEqual(bound[9][0]["text"], "第")


if __name__ == "__main__":
    unittest.main()
