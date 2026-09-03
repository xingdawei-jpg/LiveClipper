import json
import os
import sys
import unittest
from unittest import mock


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

import ai_clipper
from selection_context import build_narrative_chapters, build_selection_context
from selection_contracts import DurationContract, SelectionCandidate


class SelectionContextTests(unittest.TestCase):
    def setUp(self) -> None:
        ai_clipper._begin_analysis_metadata()

    def test_story_and_continuity_groups_are_stable_without_reordering(self) -> None:
        rows = [
            {"candidate_id": 1, "source_id": "[V1]", "start": 0.0, "end": 2.0},
            {"candidate_id": 2, "source_id": "[V1]", "start": 2.8, "end": 5.0},
            {"candidate_id": 3, "source_id": "[V1]", "start": 9.0, "end": 11.0},
            {"candidate_id": 4, "source_id": "[V1]", "start": 30.0, "end": 33.0},
            {"candidate_id": 5, "source_id": "[V2]", "start": 0.0, "end": 3.0},
        ]

        context = build_selection_context(rows)
        again = build_selection_context(rows)

        self.assertEqual(
            [item.candidate_id for item in context.candidates],
            [1, 2, 3, 4, 5],
        )
        self.assertEqual(context, again)
        self.assertEqual(
            context.candidates[0].continuity_group_id,
            context.candidates[1].continuity_group_id,
        )
        self.assertNotEqual(
            context.candidates[1].continuity_group_id,
            context.candidates[2].continuity_group_id,
        )
        self.assertEqual(
            context.candidates[0].story_block_id,
            context.candidates[2].story_block_id,
        )
        self.assertNotEqual(
            context.candidates[2].story_block_id,
            context.candidates[3].story_block_id,
        )
        self.assertNotEqual(
            context.candidates[3].story_block_id,
            context.candidates[4].story_block_id,
        )
        self.assertEqual(context.summary()["story_block_count"], 3)

    def test_candidate_payload_keeps_legacy_fields_and_adds_context(self) -> None:
        candidate = SelectionCandidate(
            candidate_id=7,
            source_id="[V1]",
            start=1.0,
            end=4.0,
            text="这件衬衫版型很显瘦",
            hook_eligible=True,
            story_block_id="SB-V1-001",
            continuity_group_id="CG-V1-001",
        )
        payload = candidate.payload()
        self.assertEqual(payload["candidate_id"], 7)
        self.assertEqual(payload["story_block_id"], "SB-V1-001")
        self.assertEqual(payload["continuity_group_id"], "CG-V1-001")

    def test_narrative_chapters_keep_every_safe_candidate_and_do_not_split_on_topic(self) -> None:
        inventory = [
            {"srt_index": 7, "source": "[V1]", "start": 0.0, "end": 2.5, "text": "肩线往里收"},
            {"srt_index": 9, "source": "[V1]", "start": 2.8, "end": 5.5, "text": "所以显肩窄"},
            {"srt_index": 11, "source": "[V1]", "start": 7.2, "end": 10.0, "text": "通勤穿也利落"},
            {"srt_index": 15, "source": "[V1]", "start": 20.0, "end": 23.0, "text": "面料不扎"},
            {"srt_index": 21, "source": "[V2]", "start": 0.0, "end": 3.0, "text": "上身很有型"},
        ]
        cards = [
            {"candidate_id": 7, "topic": "版型显瘦", "subtopic": "肩线", "roles": ["effect"], "tier": "main"},
            {"candidate_id": 9, "topic": "工艺细节", "subtopic": "肩线结构", "roles": ["evidence"], "tier": "main"},
            {"candidate_id": 11, "topic": "场景搭配", "subtopic": "通勤", "roles": ["scene"], "tier": "reserve"},
        ]
        original = json.loads(json.dumps(inventory, ensure_ascii=False))

        chapters = build_narrative_chapters(inventory, cards)

        self.assertEqual(inventory, original)
        self.assertEqual(chapters[0].candidate_ids, (7, 9, 11))
        self.assertEqual(chapters[0].topics, ("版型显瘦", "工艺细节", "场景搭配"))
        self.assertEqual(chapters[0].main_count, 2)
        self.assertEqual(chapters[1].candidate_ids, (15,))
        self.assertEqual(chapters[2].candidate_ids, (21,))
        self.assertEqual({candidate_id for chapter in chapters for candidate_id in chapter.candidate_ids}, {7, 9, 11, 15, 21})

    def test_layered_contract_exposes_narrative_chapters_without_filtering_candidates(self) -> None:
        context = build_selection_context([
            {"candidate_id": 1, "source_id": "[V1]", "start": 0.0, "end": 3.0},
            {"candidate_id": 2, "source_id": "[V1]", "start": 3.2, "end": 6.0},
        ])
        raw = ai_clipper._layered_selection_prompt_contract(
            selection_context=context,
            narrative_chapters=[{
                "chapter_id": "NC-001",
                "source_id": "[V1]",
                "candidate_ids": [1, 2],
                "topics": ["版型显瘦"],
                "roles": ["effect", "evidence"],
                "start": 0.0,
                "end": 6.0,
                "reviewed_count": 2,
                "main_count": 1,
            }],
        )
        contract = json.loads(raw)

        self.assertEqual(contract["narrative_chapter_summary"]["role"], "advisory_not_candidate_filter")
        self.assertEqual(contract["narrative_chapters"][0]["candidate_ids"], [1, 2])
        self.assertIn("selected_narrative_chapter_ids", contract["output_schema"]["plan_report"])

    def test_layered_contract_separates_hard_constraints_and_preferences(self) -> None:
        entries = [
            (0.0, 3.0, "[V1] 这件衬衫肩线向内收"),
            (3.2, 7.0, "[V1] 所以上身会更显瘦"),
        ]
        context = ai_clipper._selection_context_from_srt_entries(entries)
        raw = ai_clipper._layered_selection_prompt_contract(
            selection_context=context,
            ai_controls={
                "main_product": "白色防晒衬衫",
                "selling_points": ["版型显瘦"],
                "hook_style": "上身效果开头",
                "strictness": "严格",
            },
            rules={
                "category_filter": True,
                "time_coherence": True,
                "narrative": "先效果后证据",
            },
            main_category="上衣",
            duration_contract=DurationContract.create(60, 1.15, tolerance=15),
            required_sources={"[V1]": 1},
        )
        contract = json.loads(raw)

        self.assertEqual(contract["contract_version"], "layered-selection-v1")
        self.assertIn("白色防晒衬衫", contract["hard_constraints"]["product_lock"])
        self.assertEqual(contract["soft_preferences"]["selling_points"], ["版型显瘦"])
        self.assertEqual(contract["soft_preferences"]["hook_style"], "上身效果开头")
        self.assertEqual(contract["story_blocks"][0][0], "SB-V1-001")
        self.assertEqual(contract["story_block_summary"]["strategy"], "all")
        self.assertEqual(contract["hard_constraints"]["required_sources"], {"[V1]": 1})

    def test_style_profile_never_becomes_a_local_candidate_filter(self) -> None:
        light = ai_clipper._feedback_runtime_policy("light")
        strong = ai_clipper._feedback_runtime_policy("strong")

        self.assertTrue(light["prompt_enabled"])
        self.assertFalse(light["local_filter_enabled"])
        self.assertFalse(strong["local_filter_enabled"])
        self.assertTrue(strong["duration_rank_enabled"])

    def test_ai_plan_report_accepts_only_real_story_blocks(self) -> None:
        entries = [
            (0.0, 3.0, "这件衬衫肩线向内收"),
            (3.2, 7.0, "所以上身会更显瘦"),
        ]
        content = json.dumps({
            "clips": [
                {
                    "clip_type": "product",
                    "srt_indices": [1],
                    "focus": "版型显瘦",
                    "reason": "具体效果",
                    "trim_priority": 0,
                }
            ],
            "expansion_plan": [],
            "plan_report": {
                "selected_product": "白色防晒衬衫",
                "selected_story_block_ids": ["SB-SINGLE-001", "SB-FAKE-999"],
                "selected_narrative_chapter_ids": ["NC-001", "NC-FAKE"],
                "missing_roles": ["scene"],
                "warnings": ["缺少场景片段"],
            },
        }, ensure_ascii=False)

        clips = ai_clipper._parse_ai_response(
            content,
            None,
            entries,
            require_srt_indices=True,
            valid_narrative_chapter_ids={"NC-001"},
        )
        report = ai_clipper.get_last_analysis_metadata()["ai_plan_report"]

        self.assertEqual(len(clips), 1)
        self.assertEqual(report["selected_product"], "白色防晒衬衫")
        self.assertEqual(report["selected_story_block_ids"], ["SB-SINGLE-001"])
        self.assertEqual(report["selected_narrative_chapter_ids"], ["NC-001"])
        self.assertEqual(report["missing_roles"], ["scene"])

    def test_director_request_contains_layered_contract_and_candidate_context(self) -> None:
        entries = [
            (0.0, 3.0, "[V1] 这件衬衫肩线向内收"),
            (3.2, 7.0, "[V1] 所以上身会更显瘦"),
        ]
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                content = json.dumps({
                    "clips": [{
                        "clip_type": "product",
                        "srt_indices": [1],
                        "focus": "版型显瘦",
                        "reason": "具体效果",
                        "trim_priority": 0,
                    }],
                    "expansion_plan": [],
                    "plan_report": {
                        "selected_product": "白色防晒衬衫",
                        "selected_story_block_ids": ["SB-V1-001"],
                        "missing_roles": [],
                        "warnings": [],
                    },
                }, ensure_ascii=False)
                return json.dumps({
                    "choices": [{"message": {"content": content}}]
                }, ensure_ascii=False).encode("utf-8")

        def fake_urlopen(request, **_kwargs):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        with mock.patch.object(ai_clipper.urllib.request, "urlopen", side_effect=fake_urlopen):
            clips = ai_clipper._call_ai(
                "test-key",
                "https://example.invalid/v1",
                "deepseek-v4-flash",
                "",
                None,
                srt_entries=entries,
                ai_controls={
                    "main_product": "白色防晒衬衫",
                    "selling_points": ["版型显瘦"],
                },
                main_category="上衣",
                duration_contract=DurationContract.create(60, 1.0, tolerance=15),
                required_sources={"[V1]": 1},
                review_story_chapters=[{
                    "chapter_id": "NC-001",
                    "source_id": "[V1]",
                    "candidate_ids": [1, 2],
                    "topics": ["版型显瘦"],
                    "roles": ["effect", "evidence"],
                    "start": 0.0,
                    "end": 7.0,
                    "reviewed_count": 2,
                    "main_count": 1,
                }],
            )

        self.assertEqual(len(clips), 1)
        messages = captured["body"]["messages"]
        user_prompt = messages[1]["content"]
        self.assertIn("layered-selection-v1", user_prompt)
        self.assertIn("story=SB-V1-001", user_prompt)
        self.assertIn("continuity=CG-V1-001", user_prompt)
        self.assertIn("NC-001", user_prompt)
        self.assertIn("advisory_not_candidate_filter", user_prompt)
        self.assertIn("白色防晒衬衫", user_prompt)
        self.assertNotIn("test-key", json.dumps(captured["body"], ensure_ascii=False))

    def test_local_plan_quality_report_does_not_apply_soft_preferences(self) -> None:
        entries = [
            (0.0, 4.0, "[V1] 这件衬衫肩线向内收"),
            (4.2, 8.0, "[V1] 所以上身会更显瘦"),
            (20.0, 25.0, "[V1] 通勤穿也很利落"),
        ]
        clips = [
            ("product", entries[0][2], 0.0, 4.0, 0.0, 4.0, "版型显瘦"),
            ("product", entries[1][2], 4.2, 8.0, 0.0, 3.8, "版型显瘦"),
            ("close", entries[2][2], 20.0, 25.0, 0.0, 5.0, "场景搭配"),
        ]
        report = ai_clipper._build_plan_quality_report(
            clips,
            entries,
            duration_contract=DurationContract.create(13, 1.0, tolerance=2),
            required_sources={"[V1]": 1},
            main_product="白色防晒衬衫",
            ai_plan_report={
                "selected_product": "白色防晒衬衫",
                "selected_story_block_ids": ["SB-V1-001"],
                "missing_roles": ["evidence"],
            },
        )

        self.assertEqual(report["status"], "pass")
        self.assertEqual(report["product"]["locked"], "白色防晒衬衫")
        self.assertEqual(report["missing_roles"], ["evidence"])
        self.assertEqual(report["duplicate_candidate_count"], 0)
        self.assertGreaterEqual(report["continuity_break_count"], 1)

    def test_plan_quality_marks_dense_unhooked_selling_point_pile_for_review(self) -> None:
        entries = [
            (0.0, 9.0, "[V1] 肩线向内收，视觉更利落。"),
            (22.0, 31.0, "[V1] 面料薄透，夏天穿不会闷。"),
            (45.0, 54.0, "[V1] 腰线位置更高，比例更好看。"),
            (68.0, 77.0, "[V1] 裙摆垂下来不会贴腿。"),
            (90.0, 99.0, "[V1] 领口做得更显脸小。"),
            (112.0, 121.0, "[V1] 通勤穿也会很利落。"),
        ]
        clips = [
            ("product", text, start, end, 0.0, end - start, "卖点")
            for start, end, text in entries
        ]

        report = ai_clipper._build_plan_quality_report(
            clips,
            entries,
            ai_plan_report={"missing_roles": ["hook"]},
        )

        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["quality_state"], "needs_review")
        self.assertTrue(any("未找到已验证Hook" in item for item in report["soft_quality_issues"]))
        self.assertTrue(any("叙事切换过密" in item for item in report["soft_quality_issues"]))

    def test_plan_time_reverse_is_metadata_not_a_quality_warning(self) -> None:
        entries = [
            (0.0, 4.0, "[V1] 肩线向内收，视觉更利落。"),
            (12.0, 16.0, "[V1] 你看这个版型不会横向撑开。"),
        ]
        clips = [
            ("product", entries[1][2], 12.0, 16.0, 0.0, 4.0, "版型显瘦"),
            ("product", entries[0][2], 0.0, 4.0, 0.0, 4.0, "版型显瘦"),
        ]

        report = ai_clipper._build_plan_quality_report(clips, entries)

        self.assertEqual(report["same_source_time_reverse_count"], 1)
        self.assertFalse(any("时间倒序" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
