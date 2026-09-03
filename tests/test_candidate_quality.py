from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

candidate_quality = importlib.import_module("candidate_quality")


def _tokens(text: str, step: float = 0.1) -> list[dict[str, object]]:
    return [
        {"text": char, "norm": char, "start": index * step, "end": (index + 1) * step}
        for index, char in enumerate(text)
    ]


class CandidateQualityTests(unittest.TestCase):

    @staticmethod
    def _timed_segment(text: str, timings: list[tuple[float, float]]) -> dict[str, object]:
        spoken = [char for char in text if char not in "，。！？!?；;：:、 "]
        assert len(spoken) == len(timings)
        return {
            "start": timings[0][0],
            "end": timings[-1][1],
            "text": text,
            "semantic_unit": True,
            "words": [
                {"text": char, "start": start, "end": end}
                for char, (start, end) in zip(spoken, timings)
            ],
        }

    def test_short_form_refinement_splits_only_at_exact_source_boundaries(self) -> None:
        text = "所以你的肩围看起来很瘦。去草原我觉得也很可以，它自带一点民族风。"
        spoken = [char for char in text if char not in "，。！？!?；;：:、 "]
        timings = []
        cursor = 0.0
        for index, _char in enumerate(spoken):
            step = 0.10 if index < len("所以你的肩围看起来很瘦") else 0.30
            timings.append((cursor, cursor + step))
            cursor += step
            if index == len("所以你的肩围看起来很瘦") - 1:
                cursor += 0.25
            if index == len("所以你的肩围看起来很瘦去草原我觉得也很可以") - 1:
                cursor += 0.35
        segment = self._timed_segment(text, timings)

        refined, metrics = candidate_quality.refine_short_form_semantic_segments([segment])

        self.assertEqual(metrics["split_segments"], 1)
        self.assertEqual(
            [item["text"] for item in refined],
            ["所以你的肩围看起来很瘦。", "去草原我觉得也很可以，", "它自带一点民族风。"],
        )
        self.assertEqual(
            [(item["start"], item["end"]) for item in refined],
            [
                (round(timings[0][0], 3), round(timings[len("所以你的肩围看起来很瘦") - 1][1], 3)),
                (
                    round(timings[len("所以你的肩围看起来很瘦")][0], 3),
                    round(timings[len("所以你的肩围看起来很瘦去草原我觉得也很可以") - 1][1], 3),
                ),
                (round(timings[len("所以你的肩围看起来很瘦去草原我觉得也很可以")][0], 3), round(cursor, 3)),
            ],
        )
        rebuilt_words = [
            word["text"] for item in refined for word in item["words"]
        ]
        self.assertEqual(rebuilt_words, spoken)

    def test_short_form_refinement_keeps_long_text_without_a_reliable_boundary(self) -> None:
        text = "这个面料穿起来轻轻薄薄夏天出门不会闷而且垂感也很自然"
        timings = [(index * 0.25, (index + 1) * 0.25) for index, _ in enumerate(text)]
        segment = self._timed_segment(text, timings)

        refined, metrics = candidate_quality.refine_short_form_semantic_segments([segment])

        self.assertEqual(len(refined), 1)
        self.assertEqual(refined[0]["text"], text)
        self.assertEqual(metrics["long_unsplit_segments"], 1)

    def test_short_form_scene_clause_can_end_before_a_new_subject(self) -> None:
        self.assertTrue(candidate_quality.short_form_independent_clause("去草原我觉得也很可以，"))
        self.assertTrue(candidate_quality.short_form_independent_clause("上班穿也很合适。"))
        self.assertFalse(candidate_quality.short_form_independent_clause("这个料子很轻，而且"))
    def test_word_exact_trim_removes_dangling_clause_before_complete_question(self) -> None:
        text = "而且亚麻的哎你们有没有发现今年大衣都有亚麻"

        repair = candidate_quality.leading_fragment_trim(_tokens(text))

        self.assertIsNotNone(repair)
        self.assertEqual(repair["prefix"], "而且亚麻的哎")
        self.assertAlmostEqual(repair["boundary"], len("而且亚麻的哎") * 0.1)

    def test_normal_material_sentence_is_not_trimmed(self) -> None:
        self.assertIsNone(candidate_quality.leading_fragment_trim(_tokens("而且亚麻面料穿着很透气")))

    def test_trim_boundary_uses_next_kept_word_start_across_a_timing_gap(self) -> None:
        tokens = _tokens("而且亚麻的哎你们有没有发现今年大衣都有亚麻")
        kept_index = len("而且亚麻的哎")
        tokens[kept_index]["start"] = float(tokens[kept_index - 1]["end"]) + 0.03

        repair = candidate_quality.leading_fragment_trim(tokens)

        self.assertIsNotNone(repair)
        self.assertAlmostEqual(repair["boundary"], tokens[kept_index]["start"])

    def test_only_high_confidence_garble_is_filtered(self) -> None:
        clips = [
            ("product", "这个面料穿起来很柔软", 0.0, 3.0, 0, 3.0),
            ("product", "这件感兴趣的是夏天的吗", 3.0, 6.0, 0, 3.0),
            ("product", "白搭白搭绿还蛮干净的", 6.0, 9.0, 0, 3.0),
            ("product", "支数越高织法越密", 9.0, 12.0, 0, 3.0),
            ("product", "高支亚麻手感更细腻", 12.0, 15.0, 0, 3.0),
            ("product", "这件外套夏到秋都能穿", 15.0, 18.0, 0, 3.0),
        ]

        filtered = candidate_quality.filter_candidate_clips(clips)

        self.assertEqual(
            [clip[1] for clip in filtered],
            [clips[0][1], clips[3][1], clips[4][1], clips[5][1]],
        )
        self.assertEqual(filtered[0], clips[0])

    def test_observed_caramel_asr_residue_cannot_enter_safe_candidate_inventory(self) -> None:
        for text in (
            "下0天40度了，你以为杭州不热呀。",
            "人间一定是直角的。",
            "木浆纤维可降解的A类母婴店，就是你小宝宝。",
            "好的，来一拉开160斤，一收上看起来像100斤葡萄。",
        ):
            with self.subTest(text=text):
                self.assertIn("明显ASR错词", candidate_quality.candidate_quality_flags(text))

    def test_caramel_unclosed_delivery_and_obfuscated_price_are_rejected(self) -> None:
        unusable = (
            "直到3到5厘米的一个挖尖挖进来会显得我们的肩干嘛？",
            "是但是你的视觉重心会落在这个线上。",
            "它是很很凉爽的一个面料，而且上身完全不。",
            "它是大是显瘦。",
        )
        for text in unusable:
            with self.subTest(text=text):
                self.assertTrue(candidate_quality.candidate_quality_flags(text))

        policy = {"price": "block"}
        for text in (
            "V4五0百啊，你们要400多买都是贵的。",
            "来388388对吧？",
            "对，3881整套。",
        ):
            with self.subTest(text=text):
                self.assertIn("价格/成本报价", candidate_quality.candidate_quality_flags(text, content_policy=policy))

        # A comma in SRT is transport punctuation, not a reason to kill a
        # complete spoken buyer benefit.
        self.assertFalse(candidate_quality.candidate_quality_flags("整个把你的肉全部藏在了这个马甲一样的形状里，"))
        self.assertFalse(candidate_quality.candidate_quality_flags("你的两边拜拜肉全部藏在了这个大网纱里面，"))

    def test_unusable_transcript_residue_and_cost_quotes_are_filtered(self) -> None:
        clips = [
            ("product", "\u8fd9\u4e2a\u9762\u6599\u7a7f\u8d77\u6765\u5f88\u67d4\u8f6f", 0.0, 3.0, 0, 3.0),
            ("product", "\u4ed6\u9ebb\u5dfe\u8ddf\u80a0\u6e29\u67d4\u8f6f\u4e86\u7136\u540e\u8fd8\u6709\u4e00\u70b9", 3.0, 6.0, 0, 3.0),
            ("product", "\u5728\u8fd9\u91cc\u3002\u6574\u4e2a\u95e8\u895f\u505a\u5230\u4f60\u770b\u5b83\u8fd9\u6837\u7684", 6.0, 9.0, 0, 3.0),
            ("product", "\u8fd9\u4ef6\u4e0d\u884c\u54e6\uff0c\u5bf9\uff0c\u98ce\u683c\u4e0d\u5927", 9.0, 12.0, 0, 3.0),
            ("product", "\u9762\u6599\u6210\u672c210\uff0c\u6240\u4ee5\u4e0d\u4fbf\u5b9c", 12.0, 15.0, 0, 3.0),
            ("product", "\u61c2\u8d27\u7684\u4eba\u76f4\u63a5\u79d2\u5e26\uff0c\u4e0d\u7528\u6307\u671b\u4fbf\u5b9c", 15.0, 18.0, 0, 3.0),
            ("product", "\u5b83\u4e0b\u610f\u8bc6\u4e2d\u8fd8\u86ee\u597d\u770b\u7684", 18.0, 21.0, 0, 3.0),
            ("product", "\u90a3\u62dc\u62dc\u4e9a\u9ebb\u8fd8\u662f\u5f88\u7ec6\u817b", 21.0, 24.0, 0, 3.0),
            ("product", "200\u65a4\u5185\u6211\u5e94\u8be5\u6ca1\u6bdb\u75c5", 24.0, 27.0, 0, 3.0),
            ("product", "\u9ad8\u652f\u4e9a\u9ebb\u624b\u611f\u66f4\u7ec6\u817b", 27.0, 30.0, 0, 3.0),
            ("product", "\u80a9\u7ebf\u5411\u5185\u6536\uff0c\u89c6\u89c9\u66f4\u5229\u843d", 30.0, 33.0, 0, 3.0),
            ("product", "\u8272\u7ec7\u7eb1\u7ebf\u8ba9\u989c\u8272\u66f4\u5747\u5300", 33.0, 36.0, 0, 3.0),
        ]

        filtered = candidate_quality.filter_candidate_clips(clips)

        self.assertEqual([clip[1] for clip in filtered], [clips[0][1], clips[9][1], clips[10][1], clips[11][1]])

    def test_joined_transcript_residue_and_personal_try_on_claims_are_rejected(self) -> None:
        unusable = [
            "它其实偏深绿，在这里。整个门襟做了拼接色系。",
            "你们也能发现整件衣服唉好多毛边。",
            "你看毛边。袖口的毛边。",
            "麻它是植物的根茎它要多道层层筛选。",
            "这种亚麻纱很难。",
            "这衣服将近3米，真的假的，我计算机按一下。",
            "200斤内我应该没毛病。",
        ]

        for text in unusable:
            with self.subTest(text=text):
                self.assertTrue(candidate_quality.candidate_quality_flags(text))

        self.assertFalse(candidate_quality.candidate_quality_flags("高克重粗织亚麻，纹理更立体。"))

    def test_embedded_material_selling_point_cannot_launder_clear_asr_miswords(self) -> None:
        broken = (
            "非常的高会热吗？你看料子都是那种薄薄透透的，"
            "会不被面儿热，我真的想把它防风。"
        )

        self.assertIn("疑似ASR错词", candidate_quality.candidate_quality_flags(broken))
        self.assertFalse(
            candidate_quality.candidate_quality_flags(
                "料子薄薄透透的，夏天穿不会闷，风一吹会更凉快。"
            )
        )

    def test_orphaned_adjective_tail_is_not_a_candidate(self) -> None:
        self.assertTrue(candidate_quality.candidate_quality_flags("吸引的。但这件我特别喜欢。"))
        self.assertFalse(candidate_quality.candidate_quality_flags("显瘦的版型能把肩线视觉往里收。"))

    def test_unfinished_wind_scene_tail_is_not_a_candidate(self) -> None:
        self.assertTrue(candidate_quality.candidate_quality_flags("站远看就像在海边风吹过来整。"))
        self.assertFalse(candidate_quality.candidate_quality_flags("风吹起来以后，罩衫的垂感会更明显。"))

    def test_live_follow_up_and_bare_body_stats_tail_are_not_candidates(self) -> None:
        self.assertTrue(
            candidate_quality.candidate_quality_flags(
                "下面穿另外一种就好了可以搭防走光防走光还有吗"
            )
        )
        self.assertTrue(
            candidate_quality.candidate_quality_flags("你就可以这样去穿177142斤。")
        )
        self.assertFalse(
            candidate_quality.candidate_quality_flags("内搭防走光短裤，抬手也更安心。")
        )

    def test_incomplete_request_and_broken_condition_are_not_candidates(self) -> None:
        self.assertTrue(
            candidate_quality.candidate_quality_flags(
                "搭这双也好看，头发一半放后面，会不会把头发给"
            )
        )
        self.assertTrue(
            candidate_quality.candidate_quality_flags(
                "我觉得大家如果喜欢。这个卫衣的女生，反正买回就懒人必备好穿。"
            )
        )
        self.assertFalse(
            candidate_quality.candidate_quality_flags("会不会把头发给撅起来？")
        )
        self.assertFalse(
            candidate_quality.candidate_quality_flags("如果喜欢这个卫衣，露腿穿也很好看。")
        )

    def test_live_overlay_orphaned_material_and_model_switch_are_not_candidates(self) -> None:
        unusable = [
            "好，家人这字大的有点看不见了，这个太大了，小一点啊，它里面是有棉加人。",
            "的一个混纺，内里的话是这样的。",
            "换个风格，换个人给你们看看短头发的效果。",
        ]

        for text in unusable:
            with self.subTest(text=text):
                self.assertTrue(candidate_quality.candidate_quality_flags(text))

        self.assertFalse(
            candidate_quality.candidate_quality_flags("棉和人丝混纺，贴身穿会更柔软。")
        )

    def test_observed_orphaned_effect_and_styling_tails_are_not_candidates(self) -> None:
        unusable = [
            "呢还遮肚子前短后长前衣长58。",
            "会稍微亮一点点。",
            "单穿早秋秋高气爽的季节。",
            "再加上它立体的。",
            "出去玩的时候，棒球帽一戴。",
        ]
        for text in unusable:
            with self.subTest(text=text):
                self.assertTrue(candidate_quality.candidate_quality_flags(text))

        for text in (
            "它会稍微显白一点，黄黑皮自然光下穿也更提气色。",
            "早秋秋高气爽的季节单穿很舒服，不会觉得厚重。",
            "再加上它立体的肩线，整个人会更显精神。",
            "棒球帽一戴，整套立刻更休闲年轻。",
        ):
            with self.subTest(text=text):
                self.assertFalse(candidate_quality.candidate_quality_flags(text))

    def test_orphaned_material_and_structure_word_tails_are_not_candidates(self) -> None:
        self.assertTrue(candidate_quality.candidate_quality_flags("酯纤维的面料"))
        self.assertTrue(candidate_quality.candidate_quality_flags("侧面是做了一个三角的立"))
        self.assertFalse(
            candidate_quality.candidate_quality_flags("聚酯纤维的面料不显廉价")
        )
        self.assertFalse(
            candidate_quality.candidate_quality_flags(
                "侧面做了一个三角的立体捏褶，腰线更利落。"
            )
        )

    def test_malformed_audience_opening_is_hook_only_defect(self) -> None:
        malformed = "你们机洗水洗久穿久如新的整件衣服能够做到遮肉显瘦"

        self.assertFalse(candidate_quality.candidate_quality_flags(malformed))
        self.assertEqual(candidate_quality.hook_candidate_quality_flags(malformed), ["Hook人称残句"])
        self.assertFalse(
            candidate_quality.hook_candidate_quality_flags(
                "你们穿这件衬衫通勤一整天，肩线还是很利落。"
            )
        )


if __name__ == "__main__":
    unittest.main()
