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

    def test_frozen_candidates_join_a_specific_incomplete_material_clause(self) -> None:
        source = (
            "1\n00:00:10,000 --> 00:00:14,000\n"
            "它的面料不是单纯的棉，它做的是。\n\n"
            "2\n00:00:14,700 --> 00:00:18,000\n"
            "加棉，它会跟常规的卫衣不太一样。\n"
        )

        frozen = ai_clipper._freeze_director_candidates(source)
        entries = ai_clipper._parse_srt_entries_for_hook(frozen)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], 10.0)
        self.assertEqual(entries[0][1], 18.0)
        self.assertIn("它做的是。加棉", entries[0][2])

    def test_frozen_candidates_join_a_specific_unfinished_morning_scene(self) -> None:
        source = (
            "1\n00:00:10,000 --> 00:00:14,000\n"
            "露腿穿的话就比较方便，因为你不管想底下搭什么，早上一觉睡醒。\n\n"
            "2\n00:00:14,500 --> 00:00:18,000\n"
            "穿个这个衣服一穿，直接出门。\n"
        )

        frozen = ai_clipper._freeze_director_candidates(source)
        entries = ai_clipper._parse_srt_entries_for_hook(frozen)

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], 10.0)
        self.assertEqual(entries[0][1], 18.0)
        self.assertIn("早上一觉睡醒。穿个这个衣服", entries[0][2])

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

    def test_exact_product_context_excludes_foreign_garment_thread_only(self) -> None:
        clips = [
            ("product", "这件连帽卫衣露腿穿很方便，早上直接套上就能出门。", 0.0, 4.0, 0, 4.0),
            ("product", "还有就是一件牛仔外套，这个复古花色非常少见。", 4.2, 8.2, 0, 4.0),
            ("product", "纽扣是不规则工艺，所以看起来不会太普通。", 8.3, 12.3, 0, 4.0),
            ("product", "你看裤子吧，绑着比例感会更好。", 12.5, 16.0, 0, 3.5),
            ("product", "卫衣搭这种尖头靴，整体会更利落。", 16.2, 20.0, 0, 3.8),
            ("product", "这件连帽卫衣前短后长，单穿也有比例。", 20.2, 24.2, 0, 4.0),
        ]

        filtered = ai_clipper._filter_candidate_clips_by_exact_product_context(
            clips,
            main_product="连帽卫衣",
        )

        self.assertEqual(ai_clipper.infer_main_product_from_filename("抽绳廓形连帽卫衣.mp4"), "连帽卫衣")
        self.assertEqual(
            [clip[1] for clip in filtered],
            [clips[0][1], clips[4][1], clips[5][1]],
        )
        self.assertEqual([(clip[2], clip[3]) for clip in filtered], [(0.0, 4.0), (16.2, 20.0), (20.2, 24.2)])

    def test_subject_gate_excludes_clear_cross_garment_behavior_without_rewriting_kept_candidates(self) -> None:
        source = (
            "1\n00:00:00,000 --> 00:00:03,000\n这条牛仔裤的腰头很平整，坐下也不会勒肚子。\n\n"
            "2\n00:00:03,200 --> 00:00:06,200\n这件可以露腿和搭裤子两种穿法。\n\n"
            "3\n00:00:06,400 --> 00:00:09,400\n袖口太巨大了，挽起来会更有感觉。\n\n"
            "4\n00:00:10,000 --> 00:00:13,000\n这条裤子的侧缝往前收，腿会显得更直。\n"
        )

        ai_clipper._begin_analysis_metadata()
        frozen = ai_clipper._freeze_director_candidates(source, main_product="裤子")
        entries = ai_clipper._parse_srt_entries_for_hook(frozen)

        self.assertEqual(
            [(start, end) for start, end, _text in entries],
            [(0.0, 3.0), (10.0, 13.0)],
        )
        self.assertNotIn("露腿和搭裤子两种穿法", frozen)
        self.assertNotIn("袖口太巨大", frozen)
        self.assertIn("侧缝往前收", frozen)

    def test_subject_unknown_keeps_normal_story_roles(self) -> None:
        entries = [
            (0.0, 3.0, "这条牛仔裤的侧缝往前收，腿会显得更直。"),
            (30.0, 33.0, "面料摸起来很细腻，垂感也很自然。"),
        ]
        logs: list[str] = []
        clips = [
            ("hook", entries[1][2], 30.0, 33.0, 50, 3.0),
            ("product", entries[1][2], 30.0, 33.0, 50, 3.0),
            ("product", entries[0][2], 0.0, 3.0, 50, 3.0),
            ("close", entries[1][2], 30.0, 33.0, 50, 3.0),
        ]

        ai_clipper._begin_analysis_metadata()
        safe, audit = ai_clipper._director_hard_audit(
            clips,
            9,
            8,
            logs.append,
            srt_entries=entries,
            main_product="裤子",
        )

        self.assertEqual([clip[1] for clip in safe], [clip[1] for clip in clips])
        self.assertEqual(audit["subject_roles"]["removed_count"], 0)
        self.assertFalse(any("主体角色硬质检" in item for item in logs))

    def test_supporting_subject_stays_body_only_until_primary_anchor(self) -> None:
        entries = [
            (0.0, 3.0, "这条牛仔裤的侧缝往前收，腿会显得更直。"),
            (5.0, 8.0, "搭这件短外套时，整体比例会更利落。"),
        ]
        clips = [
            ("product", entries[1][2], 5.0, 8.0, 50, 3.0),
            ("product", entries[0][2], 0.0, 3.0, 50, 3.0),
        ]

        ai_clipper._begin_analysis_metadata()
        safe, audit = ai_clipper._director_hard_audit(
            clips,
            6,
            8,
            srt_entries=entries,
            main_product="裤子",
        )

        self.assertEqual([clip[1] for clip in safe], [entries[0][2]])
        self.assertEqual(audit["subject_roles"]["removed_count"], 1)

    def test_subject_unknown_can_follow_a_primary_product_as_evidence(self) -> None:
        entries = [
            (0.0, 3.0, "这条牛仔裤的侧缝往前收，腿会显得更直。"),
            (30.0, 33.0, "面料摸起来很细腻，垂感也很自然。"),
        ]
        clips = [
            ("product", entries[0][2], 0.0, 3.0, 50, 3.0),
            ("product", entries[1][2], 30.0, 33.0, 50, 3.0),
        ]

        ai_clipper._begin_analysis_metadata()
        safe, audit = ai_clipper._director_hard_audit(
            clips,
            6,
            8,
            srt_entries=entries,
            main_product="裤子",
        )

        self.assertEqual([clip[1] for clip in safe], [entries[0][2], entries[1][2]])
        self.assertEqual(audit["subject_roles"]["removed_count"], 0)

    def test_unexpanded_two_way_preamble_is_not_a_standalone_product(self) -> None:
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason("它是两种穿法的"),
            "卖点铺垫未展开",
        )
        self.assertEqual(
            ai_clipper._director_standalone_boundary_reason(
                "它是两种穿法的，露腿穿很方便，搭裤子也很利落。"
            ),
            "",
        )

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
