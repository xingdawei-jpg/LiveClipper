import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from asr_cache import write_metadata  # noqa: E402
from run_m3_golden_source_identity import assess_source_identity  # noqa: E402


class M3GoldenSourceIdentityTests(unittest.TestCase):
    def test_cache_backed_pair_is_selected_even_when_direct_row_binding_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            words = root / "source.words.json"
            video.write_bytes(b"video")
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n第一句。\n2\n00:00:01,000 --> 00:00:02,000\n第二句。\n", encoding="utf-8")
            words.write_text(json.dumps({"segments": [{
                "text": "第一句。第二句。",
                "words": [{"text": "第一句", "start": 0.0, "end": 1.0}, {"text": "第二句", "start": 1.0, "end": 2.0}],
            }]}), encoding="utf-8")
            write_metadata(video, srt, provider="sensevoice", timing_precision="word")

            result = assess_source_identity(srt)

        self.assertTrue(result["source_identity_verified"])
        self.assertTrue(result["selected_as_m3_source_golden"])
        self.assertFalse(result["direct_row_identity"])
        self.assertFalse(result["ready_for_m3_plan_fidelity"])


if __name__ == "__main__":
    unittest.main()
