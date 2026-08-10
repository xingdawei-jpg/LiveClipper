from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

ai_clipper = importlib.import_module("ai_clipper")
content_policy = importlib.import_module("content_policy")


def _word_timing_segment(text: str, step: float = 0.2) -> list[dict[str, object]]:
    spoken = "".join(char for char in text if char not in "，。！？!?；;：:、 ")
    return [{
        "text": text,
        "start": 0.0,
        "end": len(spoken) * step,
        "words": [
            {"text": char, "start": index * step, "end": (index + 1) * step}
            for index, char in enumerate(spoken)
        ],
    }]


class CandidateQualityIntegrationTests(unittest.TestCase):
    def test_frozen_candidate_contract_excludes_obvious_garble(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:03,000\n整个的版然后很非常适合。\n\n"
            "2\n00:00:03,200 --> 00:00:06,200\n白搭白搭绿还蛮干净的。\n\n"
            "3\n00:00:06,400 --> 00:00:09,400\n高支亚麻手感更加细腻。\n\n"
            "4\n00:00:09,600 --> 00:00:12,600\n这件外套从夏天可以穿到秋天。\n\n"
            "5\n00:00:12,800 --> 00:00:15,800\n交错门襟让细节更有层次。\n\n"
            "6\n00:00:16,000 --> 00:00:19,000\n织法更密上身感觉也更柔软。\n"
        )

        frozen = ai_clipper._freeze_director_candidates(source)

        self.assertNotIn("整个的版", frozen)
        self.assertNotIn("白搭白搭绿", frozen)
        self.assertIn("高支亚麻手感更加细腻", frozen)

    def test_word_timed_fragment_is_trimmed_to_complete_question(self) -> None:
        text = "而且亚麻的。哎，你们有没有发现今年大衣里面都有亚麻。"
        timings = _word_timing_segment(text)
        spoken_length = len(timings[0]["words"])
        clips = [("product", text, 0.0, spoken_length * 0.2, 0, spoken_length * 0.2)]

        repaired = ai_clipper._trim_filler_start(clips, "", word_timings=timings)

        self.assertEqual(repaired[0][1], "你们有没有发现今年大衣里面都有亚麻")
        self.assertAlmostEqual(repaired[0][2], len("而且亚麻的哎") * 0.2)

    def test_connector_is_trimmed_only_when_the_remainder_stays_complete(self) -> None:
        text = "而且这件亚麻外套夏到秋都能穿。"
        timings = _word_timing_segment(text)
        spoken_length = len(timings[0]["words"])
        clips = [("product", text, 0.0, spoken_length * 0.2, 0, spoken_length * 0.2)]

        repaired = ai_clipper._trim_filler_start(clips, "", word_timings=timings)

        expected = "".join(str(word["text"]) for word in timings[0]["words"][2:])
        self.assertEqual(repaired[0][1], expected)
        self.assertAlmostEqual(repaired[0][2], 0.4)

    def test_price_announcement_asr_variant_is_excluded_from_safe_inventory(self) -> None:
        inventory = ai_clipper._director_safe_candidate_inventory(
            [
                (0.0, 4.0, "经开价喽，这套搭个小草帽就是度假风。"),
                (4.0, 8.0, "肩线往里收，视觉上看起来更利落。"),
            ],
            content_policy=content_policy.default_content_policy(),
        )

        self.assertEqual([item["srt_index"] for item in inventory], [2])

    def test_short_conjunction_fragment_is_excluded_from_safe_inventory(self) -> None:
        inventory = ai_clipper._director_safe_candidate_inventory([
            (0.0, 4.0, "但它这根线往里一挪，视觉上更清楚。"),
            (4.0, 8.0, "肩线往里收，视觉上看起来更利落。"),
        ])

        self.assertEqual([item["srt_index"] for item in inventory], [2])

    def test_purchase_conditioned_gift_is_excluded_from_safe_inventory(self) -> None:
        inventory = ai_clipper._director_safe_candidate_inventory([
            (0.0, 5.0, "你买这一身还有洗衣袋给你，然后再讲面料。"),
            (5.0, 9.0, "不起球也可以机洗，日常打理更省心。"),
        ])

        self.assertEqual([item["srt_index"] for item in inventory], [2])

    def test_direct_gift_promotion_is_excluded_from_safe_inventory(self) -> None:
        inventory = ai_clipper._director_safe_candidate_inventory([
            (0.0, 5.0, "我们会赠送洗衣袋给你，套洗衣袋去洗就好。"),
            (5.0, 9.0, "再生纤维素纤维的手感更软，也不容易起球。"),
        ])

        self.assertEqual([item["srt_index"] for item in inventory], [2])


if __name__ == "__main__":
    unittest.main()
