import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from micro_beat_inventory import (
    apply_micro_beat_quality,
    apply_micro_beat_boundary_adjudication,
    apply_micro_beat_publishable_adjudication,
    build_micro_beat_source_batches,
    build_micro_beat_boundary_adjudication_prompt,
    build_micro_beat_publishable_adjudication_prompt,
    build_micro_beat_source_rows,
    build_micro_beat_boundary_quality_prompt,
    prepare_narrative_mode_beat_execution,
    parse_micro_beat_inventory,
    parse_micro_beat_boundary_quality,
)
from clip_selector import audit_materialization_fidelity, materialize_narrative_plan
from story_planner import NarrativeBeat, NarrativePlan, OpeningPackage


class _Span:
    def __init__(self, *, bound=True, index=0, start=0.0, end=1.0):
        self.status = "bound" if bound else "unmatched"
        self.word_start_index = index if bound else None
        self.word_end_index = index if bound else None
        self.word_start_time = start if bound else None
        self.word_end_time = end if bound else None


class _Timeline:
    def __init__(self, rows):
        self.by_subtitle_id = {
            int(item["id"]): _Span(index=index, start=item["start"], end=item["end"])
            for index, item in enumerate(rows)
        }


class MicroBeatInventoryTests(unittest.TestCase):
    def test_ai_declared_short_beats_preserve_source_lineage_and_new_sub_outcomes(self):
        rows = [
            {"id": 1, "start": 0.0, "end": 2.4, "text": "穿上以后正面整个人就看起来很窄。"},
            {"id": 2, "start": 2.6, "end": 5.2, "text": "黑色花边线会把人的肩往里压。"},
            {"id": 3, "start": 5.4, "end": 8.6, "text": "不光瘦肩还瘦腰还瘦胯。"},
            {"id": 4, "start": 9.0, "end": 12.0, "text": "点击购物车下单。"},
        ]
        source_rows = build_micro_beat_source_rows(
            source_units=rows,
            hard_safe_subtitle_ids=(1, 2, 3),
            word_timeline=_Timeline(rows),
        )
        result = parse_micro_beat_inventory(data={
            "beats": [
                {
                    "subtitle_ids": [1], "commercial_theme": "身形效果",
                    "purchase_value": "正面显窄", "sub_outcome": "整体视觉收窄",
                    "evidence_function": "result", "standalone_quality": "完整结论",
                    "source_context_required": False, "why_this_is_a_new_beat": "第一眼结果",
                },
                {
                    "subtitle_ids": [2], "commercial_theme": "肩线机制",
                    "purchase_value": "肩部收窄", "sub_outcome": "花边线压肩",
                    "evidence_function": "mechanism", "standalone_quality": "完整机制说明",
                    "source_context_required": False, "why_this_is_a_new_beat": "解释显窄原因",
                },
                {
                    "subtitle_ids": [4], "commercial_theme": "互动", "purchase_value": "无",
                    "sub_outcome": "无", "evidence_function": "other", "standalone_quality": "完整",
                    "source_context_required": False, "why_this_is_a_new_beat": "不应通过安全门",
                },
            ],
            "rejected_important_segments": [],
            "inventory_quality": {"complete_source_scanned": True},
        }, source_rows=source_rows)
        self.assertEqual([item["beat_id"] for item in result["beat_inventory"]], ["B001", "B002"])
        self.assertEqual(result["beat_inventory"][0]["subtitle_ids"], [1])
        self.assertEqual(result["beat_inventory"][1]["sub_outcome"], "花边线压肩")
        self.assertEqual(result["contract_rejected_declared_beats"][0]["reason"], "source_not_hard_safe")
        self.assertFalse(result["contract"]["arc_assembly_performed"])
        self.assertFalse(result["contract"]["m3_invoked"])

    def test_rejects_noncontiguous_and_overlong_spans_without_selecting_replacement(self):
        rows = [
            {"id": 1, "start": 0.0, "end": 3.0, "text": "夏天穿起来很透薄。"},
            {"id": 2, "start": 3.2, "end": 6.2, "text": "三伏天也能随便穿。"},
            {"id": 3, "start": 6.4, "end": 10.2, "text": "出汗也不会粘在身上。"},
        ]
        source_rows = build_micro_beat_source_rows(
            source_units=rows, hard_safe_subtitle_ids=(1, 2, 3), word_timeline=_Timeline(rows),
        )
        result = parse_micro_beat_inventory(data={
            "beats": [
                {
                    "subtitle_ids": [1, 3], "commercial_theme": "舒适", "purchase_value": "透薄",
                    "sub_outcome": "透薄", "evidence_function": "experience", "standalone_quality": "完整",
                    "source_context_required": False, "why_this_is_a_new_beat": "新认知",
                },
                {
                    "subtitle_ids": [1, 2, 3], "commercial_theme": "舒适", "purchase_value": "夏季",
                    "sub_outcome": "高温", "evidence_function": "experience", "standalone_quality": "完整",
                    "source_context_required": False, "why_this_is_a_new_beat": "新认知",
                },
            ],
        }, source_rows=source_rows)
        self.assertEqual(result["total_micro_beats"], 0)
        self.assertEqual(
            [item["reason"] for item in result["contract_rejected_declared_beats"]],
            ["subtitle_ids_not_contiguous_source_span", "duration_exceeds_micro_beat_max"],
        )

    def test_p02_quality_can_only_remove_mined_beats_never_add_or_replace_them(self):
        inventory = {
            "beat_inventory": [
                {"beat_id": "B001", "duration_seconds": 2.4, "subtitle_ids": [1], "text": "完整结果。"},
                {"beat_id": "B002", "duration_seconds": 3.1, "subtitle_ids": [2], "text": "明显残句。"},
            ],
            "contract": {"arc_assembly_performed": False, "m3_invoked": False},
        }
        result = apply_micro_beat_quality(inventory=inventory, data={
            "beat_quality": [
                {"beat_id": "B001", "beat_start_clean": True, "beat_end_clean": True,
                 "spoken_completeness": True, "sentence_cleanliness": True, "asr_quality": True,
                 "final_utterance_eligible": True, "reason": "完整自然"},
                {"beat_id": "B002", "beat_start_clean": False, "beat_end_clean": True,
                 "spoken_completeness": False, "sentence_cleanliness": True, "asr_quality": True,
                 "final_utterance_eligible": False, "reason": "承接残句"},
                {"beat_id": "B999", "beat_start_clean": True, "beat_end_clean": True,
                 "spoken_completeness": True, "sentence_cleanliness": True, "asr_quality": True,
                 "final_utterance_eligible": True, "reason": "不得新增"},
            ],
        })
        self.assertEqual([item["beat_id"] for item in result["beat_inventory"]], ["B001"])
        self.assertEqual(result["p0_2_quality_rejected_beats"][0]["beat_id"], "B002")
        self.assertTrue(result["contract"]["p0_2_micro_beat_quality_applied"])
        self.assertFalse(result["contract"]["arc_assembly_performed"])

    def test_source_batching_is_complete_deterministic_coverage_not_candidate_truncation(self):
        rows = [
            {"subtitle_id": 1, "start": 0.0, "end": 3.0},
            {"subtitle_id": 2, "start": 3.2, "end": 6.0},
            {"subtitle_id": 3, "start": 10.0, "end": 13.0},
            {"subtitle_id": 4, "start": 13.2, "end": 16.0},
        ]
        batches = build_micro_beat_source_batches(rows, max_seconds=8.0)
        self.assertEqual([item["batch_id"] for item in batches], ["S01", "S02"])
        self.assertEqual(
            [subtitle_id for batch in batches for subtitle_id in batch["source_subtitle_ids"]],
            [1, 2, 3, 4],
        )

    def test_direct_source_safety_does_not_inherit_ledger_boundary_but_keeps_policy_blocks(self):
        rows = [
            {"id": 1, "start": 0.0, "end": 3.0, "text": "78910月份当内搭搭西装搭风衣。"},
            {"id": 2, "start": 3.2, "end": 6.2, "text": "一分钟卖200单首批全走光。"},
        ]
        source_rows = build_micro_beat_source_rows(
            source_units=rows,
            # Neither row is in the historical Candidate Ledger.  That fact
            # cannot make the current-product styling sentence unavailable.
            hard_safe_subtitle_ids=(),
            word_timeline=_Timeline(rows),
        )
        self.assertTrue(source_rows[0]["hard_safe"])
        self.assertFalse(source_rows[0]["candidate_ledger_safe"])
        self.assertFalse(source_rows[1]["hard_safe"])
        self.assertEqual(source_rows[1]["safety_block_reason"], "default_policy_social_proof_or_cta")

    def test_ai_can_declare_a_clean_word_exact_subrange_with_resolved_lineage(self):
        source_rows = [{
            "subtitle_id": 1, "start": 0.0, "end": 3.0,
            "text": "嗯，三伏天随便穿。", "hard_safe": True, "materializable": True,
            "word_lineage": {
                "word_start_index": 20, "word_end_index": 27,
                "word_start_time": 0.0, "word_end_time": 3.0,
            },
            "word_tokens": [
                {"offset": 0, "text": "嗯", "start": 0.0, "end": 0.2},
                {"offset": 1, "text": "三", "start": 0.3, "end": 0.6},
                {"offset": 2, "text": "伏", "start": 0.7, "end": 1.0},
                {"offset": 3, "text": "天", "start": 1.1, "end": 1.4},
                {"offset": 4, "text": "随", "start": 1.5, "end": 1.8},
                {"offset": 5, "text": "便", "start": 1.9, "end": 2.2},
                {"offset": 6, "text": "穿", "start": 2.3, "end": 2.8},
            ],
        }]
        result = parse_micro_beat_inventory(data={
            "beats": [{
                "subtitle_ids": [1], "start_word_offset": 1, "end_word_offset": 6,
                "commercial_theme": "夏季体验", "purchase_value": "三伏天可穿",
                "sub_outcome": "高温适穿", "evidence_function": "experience",
                "standalone_quality": "完整短句", "source_context_required": False,
                "why_this_is_a_new_beat": "明确高温场景",
            }],
        }, source_rows=source_rows)
        beat = result["beat_inventory"][0]
        self.assertEqual(beat["text"], "三伏天随便穿。")
        self.assertEqual((beat["start"], beat["end"]), (0.3, 2.8))
        self.assertEqual(beat["word_lineage"][0]["word_start_index"], 21)
        self.assertEqual(beat["word_lineage"][0]["word_end_index"], 26)

    def test_boundary_quality_ai_can_trim_leading_live_residue_without_semantic_rewrite(self):
        source_rows = [{
            "subtitle_id": 1, "start": 0.0, "end": 4.5,
            "text": "还好你聪明。整个袖子是盖手的。", "hard_safe": True, "materializable": True,
            "word_lineage": {
                "word_start_index": 100, "word_end_index": 109,
                "word_start_time": 0.0, "word_end_time": 4.5,
            },
            "word_tokens": [
                {"offset": 0, "text": "还", "start": 0.0, "end": 0.3},
                {"offset": 1, "text": "好", "start": 0.35, "end": 0.65},
                {"offset": 2, "text": "你", "start": 0.7, "end": 1.0},
                {"offset": 3, "text": "聪", "start": 1.05, "end": 1.35},
                {"offset": 4, "text": "明", "start": 1.4, "end": 1.7},
                {"offset": 5, "text": "整", "start": 1.8, "end": 2.1},
                {"offset": 6, "text": "个", "start": 2.15, "end": 2.45},
                {"offset": 7, "text": "袖", "start": 2.5, "end": 2.8},
                {"offset": 8, "text": "子", "start": 2.85, "end": 3.15},
                {"offset": 9, "text": "是", "start": 3.2, "end": 3.45},
                {"offset": 10, "text": "盖", "start": 3.5, "end": 3.75},
                {"offset": 11, "text": "手", "start": 3.8, "end": 4.2},
                {"offset": 12, "text": "的", "start": 4.25, "end": 4.5},
            ],
        }]
        frozen = [{
            "beat_id": "B001", "start": 0.0, "end": 4.5, "duration_seconds": 4.5,
            "subtitle_ids": [1], "text": "还好你聪明。整个袖子是盖手的。",
            "commercial_theme": "袖型", "purchase_value": "遮手臂", "sub_outcome": "盖手袖",
            "evidence_function": "experience", "standalone_quality": "完整", "why_this_is_a_new_beat": "袖型细节",
            "word_lineage": [{"subtitle_id": 1, "start_word_offset": 0, "end_word_offset": 12}],
        }]
        decision = {
            "boundary_decisions": [{
                "beat_id": "B001", "decision": "TRIM_AND_KEEP",
                "current_start_is_earliest_natural": False,
                "current_end_is_latest_necessary": True,
                "trim_reason": "移除前置直播互动", "reject_reason": "",
                "segments": [{
                    "segment_key": "A", "start_word_id": 105, "end_word_id": 112,
                    "final_spoken_text": "整个袖子是盖手的",
                    "short_complete_exception_reason": "",
                    "boundary_quality": {
                        "start_clean": True, "end_clean": True, "local_completeness": True,
                        "context_dependency_resolved": True, "asr_publishable": True,
                        "minimal_sufficient_expression": True,
                    },
                }],
            }],
        }
        result = parse_micro_beat_boundary_quality(
            frozen_inventory=frozen, source_rows=source_rows, data=decision,
        )
        beat = result["publishable_beat_inventory"][0]
        self.assertEqual(beat["final_start_word_id"], 105)
        self.assertGreater(beat["start"], 0.0)
        self.assertIn("整个袖子", beat["text"])
        self.assertEqual(result["boundary_statistics"]["word_trimmed_count"], 1)
        self.assertGreater(result["boundary_statistics"]["word_trimmed_seconds_saved"], 0.0)

    def test_boundary_quality_ai_can_split_two_independent_real_word_ranges(self):
        source_rows = [{
            "subtitle_id": 1, "start": 0.0, "end": 5.2,
            "text": "它不会粘身，而且汗干得快。", "hard_safe": True, "materializable": True,
            "word_lineage": {"word_start_index": 20, "word_end_index": 30},
            "word_tokens": [
                {"offset": 0, "text": "它", "start": 0.0, "end": 0.25},
                {"offset": 1, "text": "不", "start": 0.3, "end": 0.6},
                {"offset": 2, "text": "会", "start": 0.65, "end": 0.95},
                {"offset": 3, "text": "粘", "start": 1.0, "end": 1.3},
                {"offset": 4, "text": "身", "start": 1.35, "end": 2.1},
                {"offset": 5, "text": "而", "start": 2.3, "end": 2.45},
                {"offset": 6, "text": "且", "start": 2.5, "end": 2.65},
                {"offset": 7, "text": "汗", "start": 2.7, "end": 3.0},
                {"offset": 8, "text": "干", "start": 3.05, "end": 3.4},
                {"offset": 9, "text": "得", "start": 3.45, "end": 3.75},
                {"offset": 10, "text": "快", "start": 3.8, "end": 5.0},
            ],
        }]
        frozen = [{
            "beat_id": "B010", "start": 0.0, "end": 5.2, "duration_seconds": 5.2,
            "subtitle_ids": [1], "text": "它不会粘身，而且汗干得快。",
            "commercial_theme": "夏日舒适", "purchase_value": "不粘身快干", "sub_outcome": "汗后干爽",
            "evidence_function": "experience", "standalone_quality": "完整", "why_this_is_a_new_beat": "舒适体验",
            "word_lineage": [{"subtitle_id": 1, "start_word_offset": 0, "end_word_offset": 10}],
        }]
        good_quality = {
            "start_clean": True, "end_clean": True, "local_completeness": True,
            "context_dependency_resolved": True, "asr_publishable": True,
            "minimal_sufficient_expression": True,
        }
        result = parse_micro_beat_boundary_quality(frozen_inventory=frozen, source_rows=source_rows, data={
            "boundary_decisions": [{
                "beat_id": "B010", "decision": "SPLIT",
                "current_start_is_earliest_natural": True,
                "current_end_is_latest_necessary": True,
                "trim_reason": "拆分两个独立认知", "reject_reason": "",
                "segments": [
                    {"segment_key": "A", "start_word_id": 20, "end_word_id": 24,
                     "final_spoken_text": "它不会粘身",
                     "short_complete_exception_reason": "", "boundary_quality": good_quality},
                    {"segment_key": "B", "start_word_id": 27, "end_word_id": 30,
                     "final_spoken_text": "汗干得快",
                     "short_complete_exception_reason": "", "boundary_quality": good_quality},
                ],
            }],
        })
        self.assertEqual([item["beat_id"] for item in result["publishable_beat_inventory"]], ["B010.A", "B010.B"])
        self.assertEqual(result["boundary_statistics"]["split_count"], 1)
        self.assertEqual(result["publishable_beat_inventory"][0]["sub_outcome"], "汗后干爽")

    def test_boundary_quality_rejects_false_publishability_and_prompt_never_bans_discourse_words(self):
        source_rows = [{
            "subtitle_id": 1, "start": 0.0, "end": 2.5,
            "text": "你看连后背都显瘦。", "hard_safe": True, "materializable": True,
            "word_lineage": {"word_start_index": 1, "word_end_index": 7},
            "word_tokens": [
                {"offset": 0, "text": "你", "start": 0.0, "end": 0.3},
                {"offset": 1, "text": "看", "start": 0.35, "end": 0.6},
                {"offset": 2, "text": "连", "start": 0.65, "end": 0.9},
                {"offset": 3, "text": "后", "start": 0.95, "end": 1.2},
                {"offset": 4, "text": "背", "start": 1.25, "end": 1.5},
                {"offset": 5, "text": "都", "start": 1.55, "end": 1.8},
                {"offset": 6, "text": "显", "start": 1.85, "end": 2.1},
                {"offset": 7, "text": "瘦", "start": 2.15, "end": 2.5},
            ],
        }]
        frozen = [{
            "beat_id": "B020", "start": 0.0, "end": 2.5, "duration_seconds": 2.5,
            "subtitle_ids": [1], "text": "你看连后背都显瘦。",
            "commercial_theme": "后背修饰", "purchase_value": "后背显瘦", "sub_outcome": "后背收窄",
            "evidence_function": "result", "standalone_quality": "完整", "why_this_is_a_new_beat": "后背结果",
            "word_lineage": [{"subtitle_id": 1, "start_word_offset": 0, "end_word_offset": 7}],
        }]
        prompt = build_micro_beat_boundary_quality_prompt(
            beat_inventory=frozen, source_rows_by_id={1: source_rows[0]},
        )
        self.assertIn("不要把“你看/它其实/然后/所以/这个/真的/我觉得”当禁词", prompt)
        result = parse_micro_beat_boundary_quality(frozen_inventory=frozen, source_rows=source_rows, data={
            "boundary_decisions": [{
                "beat_id": "B020", "decision": "REJECT", "segments": [],
                "reject_reason": "现场指代悬空", "trim_reason": "",
                "current_start_is_earliest_natural": True,
                "current_end_is_latest_necessary": True,
            }],
        })
        self.assertEqual(result["boundary_statistics"]["final_beats"], 0)
        self.assertEqual(result["boundary_rejected_beats"][0]["reject_reason"], "现场指代悬空")

    def test_publishable_adjudication_uses_semantics_not_final_punctuation(self):
        boundary_result = {
            "publishable_beat_inventory": [
                {"beat_id": "B008", "source_beat_id": "B008", "boundary_decision": "TRIM_AND_KEEP",
                 "duration_seconds": 2.64, "text": "它的肩是全部做这种花边工艺的，把你的肩往里挖，",
                 "lineage_status": "resolved"},
                {"beat_id": "B085", "source_beat_id": "B085", "boundary_decision": "TRIM_AND_KEEP",
                 "duration_seconds": 2.52, "text": "整个把你的肉全部藏在了这个马甲一样的形状里，",
                 "lineage_status": "resolved"},
                {"beat_id": "B062", "source_beat_id": "B062", "boundary_decision": "TRIM_AND_KEEP",
                 "duration_seconds": 2.7, "text": "这个衣服把我的肩从这视觉重心延到了这儿，",
                 "lineage_status": "resolved"},
                {"beat_id": "B073", "source_beat_id": "B073", "boundary_decision": "TRIM_AND_KEEP",
                 "duration_seconds": 2.52, "text": "用的是再生纤维素纤维，这个面料是",
                 "lineage_status": "resolved"},
            ],
            "boundary_statistics": {},
            "contract": {"arc_assembly_performed": False, "m3_invoked": False},
        }
        prompt = build_micro_beat_publishable_adjudication_prompt(
            boundary_beat_inventory=boundary_result["publishable_beat_inventory"],
        )
        self.assertIn("句末是逗号、SRT 在此断行", prompt)
        self.assertIn("publishable_visual", prompt)
        data = {
            "publishable_beat_adjudications": [
                {"beat_id": "B008", "publishability_status": "publishable_clean",
                 "semantic_subject_resolved": True, "semantic_predicate_resolved": True,
                 "commercial_result_resolved": True, "no_dangling_dependency": True,
                 "visual_dependency": "none", "role_permissions": ["core", "proof", "support"],
                 "context_requirement": "journey_context_ok", "narrative_priority": "high", "reason": "机制结果完整"},
                {"beat_id": "B085", "publishability_status": "publishable_clean",
                 "semantic_subject_resolved": True, "semantic_predicate_resolved": True,
                 "commercial_result_resolved": True, "no_dangling_dependency": True,
                 "visual_dependency": "none", "role_permissions": ["hook", "core", "proof", "support"],
                 "context_requirement": "standalone", "narrative_priority": "high", "reason": "藏肉结果完整"},
                {"beat_id": "B062", "publishability_status": "publishable_visual",
                 "semantic_subject_resolved": True, "semantic_predicate_resolved": True,
                 "commercial_result_resolved": True, "no_dangling_dependency": True,
                 "visual_dependency": "required", "role_permissions": ["proof", "support"],
                 "context_requirement": "visual_required", "narrative_priority": "medium", "reason": "位置展示依赖画面"},
                {"beat_id": "B073", "publishability_status": "reject",
                 "semantic_subject_resolved": True, "semantic_predicate_resolved": False,
                 "commercial_result_resolved": False, "no_dangling_dependency": False,
                 "visual_dependency": "none", "role_permissions": [],
                 "context_requirement": "", "narrative_priority": "low", "reason": "未完成谓语"},
            ],
        }
        result = apply_micro_beat_publishable_adjudication(boundary_result=boundary_result, data=data)
        self.assertEqual(
            [item["beat_id"] for item in result["publishable_clean_beat_inventory"]], ["B008", "B085"],
        )
        self.assertEqual(
            [item["beat_id"] for item in result["publishable_visual_beat_inventory"]], ["B062"],
        )
        self.assertFalse(result["publishable_visual_beat_inventory"][0]["audio_only_eligible"])
        self.assertTrue(result["publishable_visual_beat_inventory"][0]["arc_eligible"])
        self.assertEqual(result["p0_5a2_publishable_adjudication_rejected_beats"][0]["beat_id"], "B073")
        self.assertEqual(result["boundary_statistics"]["final_usable_seconds"], 7.86)
        self.assertEqual(
            result["contract"]["principal_ai_publishability_adjudication_count"], 1,
        )

    def test_narrative_mode_inventory_can_bypass_legacy_static_p02_before_calibration(self):
        source_rows = [{
            "subtitle_id": 1, "start": 0.0, "end": 2.5, "text": "这件衣服肩干嘛，",
            "hard_safe": True, "materializable": True,
            "word_lineage": {"word_start_index": 0, "word_end_index": 4},
            "word_tokens": [
                {"offset": 0, "text": "这", "start": 0.0, "end": 0.3},
                {"offset": 1, "text": "件", "start": 0.35, "end": 0.6},
                {"offset": 2, "text": "衣", "start": 0.65, "end": 0.9},
                {"offset": 3, "text": "服", "start": 0.95, "end": 1.2},
                {"offset": 4, "text": "肩干嘛", "start": 1.25, "end": 2.5},
            ],
        }]
        data = {"beats": [{
            "subtitle_ids": [1], "start_word_offset": None, "end_word_offset": None,
            "commercial_theme": "肩部", "purchase_value": "肩部机制", "sub_outcome": "肩线",
            "evidence_function": "mechanism", "standalone_quality": "待边界主判",
            "source_context_required": False, "why_this_is_a_new_beat": "测试三态主判",
            "short_complete_exception_reason": "",
        }]}
        legacy = parse_micro_beat_inventory(data=data, source_rows=source_rows)
        narrative_mode = parse_micro_beat_inventory(
            data=data, source_rows=source_rows, apply_static_p02_quality=False,
        )
        self.assertEqual(legacy["total_micro_beats"], 0)
        self.assertEqual(narrative_mode["total_micro_beats"], 1)

    def test_short_complete_proof_is_not_rejected_for_missing_exception_reason(self):
        source_rows = [{
            "subtitle_id": 1, "start": 0.0, "end": 1.2, "text": "后背也显薄。",
            "hard_safe": True, "materializable": True,
            "word_lineage": {"word_start_index": 0, "word_end_index": 3},
            "word_tokens": [
                {"offset": 0, "text": "后", "start": 0.0, "end": 0.2},
                {"offset": 1, "text": "背", "start": 0.25, "end": 0.45},
                {"offset": 2, "text": "也", "start": 0.5, "end": 0.65},
                {"offset": 3, "text": "显薄", "start": 0.7, "end": 1.2},
            ],
        }]
        result = parse_micro_beat_inventory(data={"beats": [{
            "subtitle_ids": [1], "start_word_offset": None, "end_word_offset": None,
            "commercial_theme": "显瘦", "purchase_value": "后背修饰", "sub_outcome": "后背显薄",
            "evidence_function": "proof", "standalone_quality": "局部结果清楚",
            "source_context_required": True, "why_this_is_a_new_beat": "补充后背效果",
        }]}, source_rows=source_rows, apply_static_p02_quality=False)
        self.assertEqual(result["total_micro_beats"], 1)
        self.assertTrue(result["beat_inventory"][0]["short_beat"])
        self.assertEqual(result["contract_rejected_declared_beats"], [])

    def test_boundary_can_micro_expand_only_into_adjacent_source_words(self):
        source_rows = [
            {"subtitle_id": 1, "start": 0.0, "end": 0.8, "text": "穿上以后，",
             "hard_safe": True, "materializable": True, "word_lineage": {"word_start_index": 0, "word_end_index": 3},
             "word_tokens": [
                 {"offset": 0, "text": "穿", "start": 0.0, "end": 0.15},
                 {"offset": 1, "text": "上", "start": 0.2, "end": 0.35},
                 {"offset": 2, "text": "以", "start": 0.4, "end": 0.55},
                 {"offset": 3, "text": "后", "start": 0.6, "end": 0.8},
             ]},
            {"subtitle_id": 2, "start": 0.85, "end": 2.0, "text": "正面整个人就看",
             "hard_safe": True, "materializable": True, "word_lineage": {"word_start_index": 4, "word_end_index": 8},
             "word_tokens": [
                 {"offset": 0, "text": "正", "start": 0.85, "end": 1.0},
                 {"offset": 1, "text": "面", "start": 1.02, "end": 1.15},
                 {"offset": 2, "text": "整", "start": 1.2, "end": 1.35},
                 {"offset": 3, "text": "个人", "start": 1.4, "end": 1.55},
                 {"offset": 4, "text": "就看", "start": 1.6, "end": 2.0},
             ]},
            {"subtitle_id": 3, "start": 2.05, "end": 2.8, "text": "起来很窄。",
             "hard_safe": True, "materializable": True, "word_lineage": {"word_start_index": 9, "word_end_index": 11},
             "word_tokens": [
                 {"offset": 0, "text": "起来", "start": 2.05, "end": 2.3},
                 {"offset": 1, "text": "很", "start": 2.35, "end": 2.5},
                 {"offset": 2, "text": "窄", "start": 2.55, "end": 2.8},
             ]},
        ]
        frozen = [{
            "beat_id": "B001", "subtitle_ids": [1, 2], "start": 0.0, "end": 2.0,
            "duration_seconds": 2.0, "text": "穿上以后，正面整个人就看",
            "word_lineage": [
                {"subtitle_id": 1, "start_word_offset": 0, "end_word_offset": 3, "word_start_index": 0},
                {"subtitle_id": 2, "start_word_offset": 0, "end_word_offset": 4, "word_start_index": 4},
            ],
            "commercial_theme": "显瘦", "purchase_value": "正面显窄", "sub_outcome": "整体显窄",
            "evidence_function": "result", "standalone_quality": "待补齐", "why_this_is_a_new_beat": "正面结果",
        }]
        quality = {key: True for key in (
            "start_clean", "end_clean", "local_completeness", "context_dependency_resolved",
            "asr_publishable", "minimal_sufficient_expression",
        )}
        result = parse_micro_beat_boundary_quality(frozen_inventory=frozen, source_rows=source_rows, data={
            "boundary_decisions": [{
                "beat_id": "B001", "decision": "MICRO_EXPAND_AND_KEEP",
                "current_start_is_earliest_natural": True, "current_end_is_latest_necessary": False,
                "trim_reason": "补齐字幕断句", "reject_reason": "",
                "segments": [{
                    "segment_key": "A", "start_word_id": 0, "end_word_id": 11,
                    "final_spoken_text": "穿上以后，正面整个人就看起来很窄。",
                    "short_beat_reason": "", "expansion_reason": "补齐结果收束",
                    "neighbor_subtitle_ids_used": [3], "same_purchase_value": True,
                    "no_new_purchase_value": True, "boundary_quality": quality,
                }],
            }],
        })
        beat = result["publishable_beat_inventory"][0]
        self.assertTrue(beat["micro_expanded"])
        self.assertEqual(beat["neighbor_subtitle_ids_used"], [3])
        self.assertEqual(beat["text"], "穿上以后，正面整个人就看起来很窄。")

    def test_boundary_normalizes_multi_segment_trim_receipt_to_ai_declared_split_children(self):
        source_rows = [{
            "subtitle_id": 1, "start": 0.0, "end": 3.0, "text": "老透老透了。这就是一个很透薄，三伏天随便穿的料子。",
            "hard_safe": True, "materializable": True,
            "word_lineage": {"word_start_index": 0, "word_end_index": 5},
            "word_tokens": [
                {"offset": 0, "text": "老透", "start": 0.0, "end": 0.4},
                {"offset": 1, "text": "老透", "start": 0.45, "end": 0.8},
                {"offset": 2, "text": "这就是", "start": 0.9, "end": 1.25},
                {"offset": 3, "text": "一个很透薄", "start": 1.3, "end": 1.8},
                {"offset": 4, "text": "三伏天", "start": 1.85, "end": 2.25},
                {"offset": 5, "text": "随便穿的料子", "start": 2.3, "end": 3.0},
            ],
        }]
        frozen = [{
            "beat_id": "B001", "subtitle_ids": [1], "start": 0.0, "end": 3.0,
            "duration_seconds": 3.0, "text": source_rows[0]["text"],
            "word_lineage": [{"subtitle_id": 1, "start_word_offset": 0, "end_word_offset": 5, "word_start_index": 0}],
            "commercial_theme": "夏季舒适", "purchase_value": "三伏天可穿", "sub_outcome": "透薄",
            "evidence_function": "experience", "standalone_quality": "完整体验", "why_this_is_a_new_beat": "夏季体验",
        }]
        quality = {key: True for key in (
            "start_clean", "end_clean", "local_completeness", "context_dependency_resolved",
            "asr_publishable", "minimal_sufficient_expression",
        )}
        result = parse_micro_beat_boundary_quality(frozen_inventory=frozen, source_rows=source_rows, data={
            "boundary_decisions": [{
                "beat_id": "B001", "decision": "TRIM_AND_KEEP",
                "current_start_is_earliest_natural": True, "current_end_is_latest_necessary": True,
                "trim_reason": "分成两个完整短句", "reject_reason": "",
                "segments": [
                    {"segment_key": "A", "start_word_id": 0, "end_word_id": 1,
                     "final_spoken_text": "老透老透", "short_beat_reason": "", "boundary_quality": quality},
                    {"segment_key": "B", "start_word_id": 2, "end_word_id": 5,
                     "final_spoken_text": "这就是一个很透薄三伏天随便穿的料子", "short_beat_reason": "", "boundary_quality": quality},
                ],
            }],
        })
        self.assertEqual([item["beat_id"] for item in result["publishable_beat_inventory"]], ["B001.A", "B001.B"])
        self.assertEqual(result["boundary_decision_audit"][0]["decision"], "SPLIT")
        self.assertTrue(result["boundary_decision_audit"][0]["decision_normalized_from_multi_segment_trim"])

    def test_short_beat_role_permission_can_be_proof_but_never_hook(self):
        boundary_result = {"publishable_beat_inventory": [{
            "beat_id": "B001", "source_beat_id": "B001", "boundary_decision": "KEEP",
            "duration_seconds": 1.2, "text": "后背也显薄。", "lineage_status": "resolved",
        }], "boundary_statistics": {}, "contract": {}}
        result = apply_micro_beat_publishable_adjudication(boundary_result=boundary_result, data={
            "publishable_beat_adjudications": [{
                "beat_id": "B001", "publishability_status": "publishable_clean",
                "semantic_subject_resolved": True, "semantic_predicate_resolved": True,
                "commercial_result_resolved": True, "no_dangling_dependency": True,
                "visual_dependency": "none", "role_permissions": ["proof", "support"],
                "context_requirement": "journey_context_ok", "narrative_priority": "high", "reason": "后背结果明确",
            }],
        })
        beat = result["publishable_beat_inventory"][0]
        self.assertFalse(beat["hook_eligible"])
        self.assertEqual(beat["role_permissions"], ["proof", "support"])

    def test_narrative_mode_p05_beat_execution_keeps_m3_word_materialization_exact(self):
        timeline = type("Timeline", (), {"words": (
            {"text": "显", "start": 0.0, "end": 0.3},
            {"text": "瘦", "start": 0.35, "end": 0.7},
            {"text": "肩", "start": 0.8, "end": 1.1},
            {"text": "收", "start": 1.15, "end": 1.5},
        )})()
        inventory = {"publishable_beat_inventory": [
            {"beat_id": "B001", "publishability_status": "publishable_clean", "audio_only_eligible": True,
             "hook_eligible": False, "role_permissions": ["proof", "support"], "context_requirement": "journey_context_ok",
             "visual_dependency": "none", "final_start_word_id": 0, "final_end_word_id": 1,
             "subtitle_ids": [1], "text": "显瘦。", "duration_seconds": 0.7,
             "narrative_priority": "high", "purchase_value": "显瘦", "sub_outcome": "显窄", "evidence_function": "result"},
            {"beat_id": "B002", "publishability_status": "publishable_clean", "audio_only_eligible": True,
             "hook_eligible": False, "role_permissions": ["proof", "support"], "context_requirement": "journey_context_ok",
             "visual_dependency": "none", "final_start_word_id": 2, "final_end_word_id": 3,
             "subtitle_ids": [2], "text": "肩收。", "duration_seconds": 0.7,
             "narrative_priority": "high", "purchase_value": "机制", "sub_outcome": "肩收", "evidence_function": "mechanism"},
        ]}
        execution = prepare_narrative_mode_beat_execution(
            publishable_inventory=inventory, word_timeline=timeline,
        )
        candidates = execution["candidates"]
        plan = NarrativePlan(
            strategy_id="S1", thesis="显瘦", target_duration=10.0,
            beats=(
                NarrativeBeat("p05", "hook", "结果", (candidates[0].candidate_id,), True, 0.7, "", "C1"),
                NarrativeBeat("p05", "payoff", "机制", (candidates[1].candidate_id,), True, 0.7, "", "C2"),
            ),
            status="journey_complete", recommended_duration=1.4, issues=(), removed_beats=(), plan_valid=True,
            opening_package=OpeningPackage(
                promise="显瘦", payoff_relation="肩收解释", hook_candidate_ids=(candidates[0].candidate_id,),
                payoff_candidate_ids=(candidates[1].candidate_id,), hook_promise="显瘦",
                payoff_delivery="肩收解释", connection_reason="结果到机制",
            ),
            selected_candidates=candidates,
        )
        result = materialize_narrative_plan(plan, execution["candidate_words"])
        fidelity = audit_materialization_fidelity(
            plan, result, execution["execution_ledger"], require_word_boundaries=True,
        )
        self.assertEqual(result.status, "ok")
        self.assertTrue(fidelity["passed"])

    def test_publishable_adjudication_preserves_publishability_separately_from_priority(self):
        boundary_result = {
            "publishable_beat_inventory": [{
                "beat_id": "B007", "source_beat_id": "B007", "boundary_decision": "KEEP",
                "duration_seconds": 2.58,
                "text": "如果真的是你这个时间等不住的，我宁可劝你别买",
                "lineage_status": "resolved",
            }],
            "boundary_statistics": {},
            "contract": {},
        }
        result = apply_micro_beat_publishable_adjudication(boundary_result=boundary_result, data={
            "publishable_beat_adjudications": [{
                "beat_id": "B007", "publishability_status": "publishable_clean",
                "semantic_subject_resolved": True, "semantic_predicate_resolved": True,
                "commercial_result_resolved": True, "no_dangling_dependency": True,
                "visual_dependency": "none", "role_permissions": ["core", "proof", "support"],
                "context_requirement": "journey_context_ok", "narrative_priority": "low", "reason": "完整但叙事优先低",
            }],
        })
        beat = result["publishable_beat_inventory"][0]
        self.assertEqual(beat["publishability_status"], "publishable_clean")
        self.assertEqual(beat["narrative_priority"], "low")
        self.assertFalse(result["contract"]["narrative_priority_is_selection"])

    def test_final_utterance_adjudication_can_only_veto_a_boundary_beat(self):
        boundary_result = {
            "publishable_beat_inventory": [
                {"beat_id": "B001", "source_beat_id": "B001", "boundary_decision": "KEEP",
                 "duration_seconds": 2.6, "text": "三伏天随便穿。", "lineage_status": "resolved"},
                {"beat_id": "B002", "source_beat_id": "B002", "boundary_decision": "TRIM_AND_KEEP",
                 "duration_seconds": 2.1, "text": "这个面料是", "lineage_status": "resolved"},
            ],
            "boundary_rejected_beats": [],
            "boundary_statistics": {},
            "contract": {"arc_assembly_performed": False, "m3_invoked": False},
        }
        prompt = build_micro_beat_boundary_adjudication_prompt(
            publishable_beat_inventory=boundary_result["publishable_beat_inventory"],
        )
        self.assertIn("不能增加、删除词、裁剪、替换、重写、重排", prompt)
        result = apply_micro_beat_boundary_adjudication(boundary_result=boundary_result, data={
            "final_utterance_adjudications": [
                {"beat_id": "B001", "publishable": True, "reason": "独立完整自然"},
                {"beat_id": "B002", "publishable": False, "reason": "未完成谓语"},
                {"beat_id": "B999", "publishable": True, "reason": "不得新增"},
            ],
        })
        self.assertEqual([item["beat_id"] for item in result["publishable_beat_inventory"]], ["B001"])
        self.assertEqual(result["p0_2_final_utterance_adjudication_rejected_beats"][0]["beat_id"], "B002")
        self.assertTrue(result["contract"]["p0_2_final_utterance_adjudication_applied"])
        self.assertFalse(result["contract"]["arc_assembly_performed"])


if __name__ == "__main__":
    unittest.main()
