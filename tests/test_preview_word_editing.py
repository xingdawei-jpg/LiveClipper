from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))
server = importlib.import_module("server")
cutter_logic = importlib.import_module("cutter_logic")
ai_clipper = importlib.import_module("ai_clipper")


class PreviewWordEditingTests(unittest.TestCase):
    def test_workbench_selling_point_categories_are_domain_aware(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        category_start = script.rfind("function previewWorkbenchCategoryDomain")
        category_end = script.index("function previewWorkbenchCandidateText", category_start)
        category_source = script[category_start:category_end]
        food_quality_line = next(
            line for line in category_source.splitlines() if 'return "food_quality"' in line
        )

        self.assertIn('["pref_quality", "\\u54c1\\u8d28\\u7ec6\\u8282"]', script)
        self.assertIn('return "apparel"', category_source)
        self.assertIn('return "food"', category_source)
        self.assertIn('previewWorkbenchPreferenceCategory(focus, domain)', category_source)
        self.assertIn('previewWorkbenchPreferenceCategory(spokenText, domain)', category_source)
        self.assertIn('const incompatibleFocus = (', category_source)
        self.assertNotIn("\\u54c1\\u8d28", food_quality_line)
        self.assertNotIn("\\u8d28\\u91cf", food_quality_line)
        self.assertNotIn("|\\u9c9c|", food_quality_line)
        self.assertIn('previewWorkbenchCandidateCategory(clip, scope)', script)

    def test_focus_classifier_change_invalidates_old_preview_cache(self):
        server_source = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        self.assertIn('_AI_PREVIEW_CACHE_SCHEMA = "layered_selection_v2"', server_source)

    def test_preview_focus_block_requires_food_evidence_from_spoken_text(self):
        apparel = {
            "clip_type": "product",
            "text": "这件衣服的拉链做工和品质都很好",
            "focus": "新鲜品质",
        }
        food = {
            "clip_type": "product",
            "text": "果园当天现摘，果形饱满",
            "focus": "新鲜品质",
        }

        self.assertEqual(server._preview_focus_block(apparel), "品质细节")
        self.assertEqual(server._preview_focus_block(food), "新鲜品质")
        self.assertEqual(
            server._preview_focus_block({
                "clip_type": "product",
                "text": "这件新款刚刚上新",
                "focus": "新鲜品质",
            }),
            "流行趋势",
        )

    def test_preview_and_director_share_craft_topic_classifier(self):
        clip = {
            "clip_type": "product",
            "text": "色织从纱线源头定做，每一件拉毛都要手工完成。",
            "focus": "工艺品质-色织亚麻",
            "segments": [{
                "text": "色织从纱线源头定做，每一件拉毛都要手工完成。",
                "selected": True,
            }],
        }
        director_clip = (
            "product", clip["text"], 0.0, 4.0, 50.0, 4.0, clip["focus"],
        )

        self.assertEqual(ai_clipper._clip_primary_topic(director_clip), "工艺细节")
        self.assertEqual(server._preview_focus_block(clip), "工艺细节")

        material_text = "克重上来了，整个成分和含量就是不一样的。"
        material_clip = ("product", material_text, 0.0, 4.0, 50.0, 4.0, "工艺品质-克重补充")
        self.assertEqual(ai_clipper._clip_primary_topic(material_clip), "面料质感")
        self.assertEqual(
            server._preview_focus_block({
                "clip_type": "product",
                "text": material_text,
                "focus": "工艺品质-克重补充",
            }),
            "面料质感",
        )

    def test_preview_refresh_loops_start_before_optional_page_initialization(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        listener = script.split('document.addEventListener("DOMContentLoaded", () => {', 1)[1].split("});", 1)[0]
        loops = script.split("function startBackgroundRefreshLoops()", 1)[1].split("}", 1)[0]

        self.assertLess(listener.index("startBackgroundRefreshLoops();"), listener.index("bindNavigation();"))
        self.assertIn("window.setInterval(refreshTasks, 2500)", loops)
        self.assertIn("window.setInterval(loadLatestSmartPreview, 5000)", loops)
        self.assertIn("window.setInterval(loadLatestMixPreview, 5000)", loops)

    def test_preview_refresh_recovers_missing_workbench_after_long_local_asr(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        latest_refresh = script.split("async function loadLatestSmartPreview()", 1)[1].split(
            "async function pollSmartPreview", 1
        )[0]

        self.assertIn('previewWorkbenchNeedsRender("smart", preview)', latest_refresh)
        self.assertIn('previewWorkbenchNeedsRender("mix", preview)', latest_refresh)
        self.assertIn("statusChanged || workbenchMissing", latest_refresh)
        self.assertIn("const previewPollMaxAttempts = 1800", script)
        self.assertNotIn("attempt > 180) return", script)

    def test_removing_selected_clip_keeps_story_scroll_and_selects_neighbor(self):
        source = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        remove_block = source.split("function removePreviewAssemblyCandidate", 1)[1].split(
            "function autoArrangePreviewAssembly", 1
        )[0]

        self.assertIn("const neighborIndex =", remove_block)
        self.assertIn("state.previewDetailSelection[scope] = neighborIndex", remove_block)
        self.assertIn("renderPreviewStateKeepStoryScroll(scope)", remove_block)
        self.assertNotIn("renderPreviewState(scope)", remove_block)

    forbidden = "\u8fdd\u7981\u8bcd"

    def test_selected_preview_rows_expose_pointer_drag_only_order_controls(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        start = script.rfind("function renderPreviewSelectedRows")
        end = script.find("// [AI_WORKBENCH_LIBRARY_END]", start)
        rendered_rows = script[start:end]

        self.assertIn('class="clip-drag-handle" data-preview-drag-handle', rendered_rows)
        self.assertIn('data-preview-row', rendered_rows)
        self.assertNotIn('draggable="true"', rendered_rows)
        self.assertNotIn('data-action="preview-assembly-move"', rendered_rows)
        self.assertNotIn('data-direction=', rendered_rows)

        drag_start = script.index("function bindPreviewRowDrag")
        drag_end = script.index("function previewInlineVideoKey", drag_start)
        drag_binding = script[drag_start:drag_end]
        self.assertIn("data-preview-drag-handle", drag_binding)
        self.assertIn('addEventListener("pointerdown"', drag_binding)
        self.assertIn("setPointerCapture", drag_binding)
        self.assertIn("document.elementFromPoint", drag_binding)
        self.assertIn("reorderPreviewClip(", drag_binding)
        self.assertNotIn("event.dataTransfer", drag_binding)

    def test_workbench_duration_fit_is_explicit_and_uses_projected_duration(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        fit_start = script.index("function previewDurationFitState")
        fit_end = script.index("function renderPreviewTriageRoleFilters", fit_start)
        fit_source = script[fit_start:fit_end]
        render_start = script.rfind("function renderPreviewWorkbench")
        render_end = script.index("function toast", render_start)
        rendered = script[render_start:render_end]

        self.assertIn("rawTotal / speed", fit_source)
        self.assertIn("target * speed", fit_source)
        self.assertIn("function autoFitPreviewDuration", fit_source)
        self.assertIn("commitPreviewDraft(scope)", fit_source)
        self.assertIn('data-action="preview-duration-fit"', rendered)
        self.assertIn("duration.projected.toFixed(1)", rendered)
        self.assertIn("duration.low.toFixed(0)", rendered)
        self.assertNotIn("autoFitPreviewDuration(", rendered)

    def test_workbench_renders_realtime_current_film_overview(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        overview_start = script.rfind("function buildPreviewFilmOverview")
        overview_end = script.index("function renderPreviewWorkbench", overview_start)
        overview = script[overview_start:overview_end]
        render_start = script.rfind("function renderPreviewWorkbench")
        render_end = script.index("function toast", render_start)
        rendered = script[render_start:render_end]

        self.assertIn("selected.forEach", overview)
        self.assertIn("effectiveClipDuration(clip)", overview)
        self.assertIn('scope !== "mix"', overview)
        self.assertIn("previewOverviewSelectedSourceStats", overview)
        self.assertIn("buildSalesChainSummary(selected)", overview)
        self.assertIn('data-action="preview-overview-locate"', overview)
        self.assertIn('data-action="preview-overview-toggle"', overview)
        self.assertIn("AI\\u521d\\u59cb\\u7ed3\\u679c", overview)
        self.assertNotIn("story_block_ids", overview)
        self.assertIn("renderPreviewFilmOverview(scope, preview, targetId, selected, duration)", rendered)

    def test_preview_output_requires_effective_manual_assembly_duration(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        ready_start = script.index("function previewReady")
        ready_end = script.index("function syncFlowActionState", ready_start)
        ready = script[ready_start:ready_end]
        actions_start = script.index("function syncFlowActionState")
        actions_end = script.index("function bindVideoRowDrag", actions_start)
        actions = script[actions_start:actions_end]

        self.assertIn("previewWorkbenchSelectedClips(scope, preview)", ready)
        self.assertIn("effectiveClipDuration(clip) > 0.05", ready)
        self.assertIn('previewReady(state.smartPreview, "smart")', actions)
        self.assertIn('previewReady(state.mixPreview, "mix")', actions)

    def test_word_editor_uses_lexical_groups_without_changing_ctc_word_indices(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        start = script.rfind("const previewEditorWordLexicon")
        end = script.find("function togglePreviewWordGroupSelection", start)
        grouping = script[start:end]

        self.assertIn('Intl.Segmenter("zh-CN", { granularity: "word" })', grouping)
        self.assertIn('"\\u5bf9\\u649e\\u886b"', grouping)
        self.assertIn('"\\u70c2\\u5927\\u8857"', grouping)
        self.assertIn("previewEditorLexicalRanges", grouping)
        self.assertIn("units[unitIndex].word", grouping)
        self.assertNotIn("current.length + wordLength <= 4", grouping)

    def test_legacy_duplicate_word_indices_are_normalized_after_draft_restore(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        ensure_start = script.index("function ensurePreviewDraft")
        ensure_end = script.index("function commitPreviewDraft", ensure_start)
        ensure = script[ensure_start:ensure_end]
        normalizer_start = script.index("function normalizePreviewWordIndices")
        normalizer_end = script.index("function isPreviewWordLocked", normalizer_start)
        normalizer = script[normalizer_start:normalizer_end]

        self.assertIn("applyPreviewDraftToState(scope, draft)", ensure)
        self.assertIn("normalizePreviewWordIndices(preview)", ensure)
        self.assertIn("seen.has(index)", normalizer)
        self.assertIn("word.index = index", normalizer)

    def test_word_editor_batches_range_selection_and_keeps_existing_word_contract(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        start = script.rfind("function previewWordRangeIndices")
        end = script.find("function renderPreviewEditorSentence", start)
        range_editing = script[start:end]
        binding_start = script.rfind("function bindDirectPreviewWorkbenchActions")
        binding_end = script.find("if (document.readyState", binding_start)
        binding = script[binding_start:binding_end]

        self.assertIn("previewWordRangeIndices", range_editing)
        self.assertIn("applyPreviewWordSelection", range_editing)
        self.assertIn("recordPreviewWordEdit", range_editing)
        self.assertIn("undoPreviewWordEdit", range_editing)
        self.assertIn("renderPreviewStateKeepStoryScroll(scope)", range_editing)
        self.assertIn("pointermove", binding)
        self.assertIn("finishPreviewWordRangeGesture", binding)
        self.assertIn("event.shiftKey && applyPreviewWordRangeFromAnchor", binding)
        self.assertIn("preview-word-audition", binding)

    def test_compact_workbench_uses_the_editor_as_the_only_full_text_surface(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        video_start = script.rfind("function renderPreviewWorkbenchVideoStage")
        video_end = script.find("function renderPreviewEditorSentence", video_start)
        video = script[video_start:video_end]
        editor_start = script.rfind("function renderPreviewSentenceEditor")
        editor_end = script.find("function previewOverviewSourceValue", editor_start)
        editor = script[editor_start:editor_end]
        styles = (ROOT / "web_client" / "frontend" / "assets" / "styles.css").read_text(encoding="utf-8")

        self.assertNotIn("preview-current-text", video)
        self.assertIn("preview-editor-head-actions", editor)
        self.assertLess(editor.index("preview-editor-head-actions"), editor.index("preview-editor-sentence-list"))
        self.assertIn("grid-template-rows: 244px minmax(0, 1fr)", styles)
        self.assertIn("flex-direction: column", styles)
        self.assertIn("-webkit-line-clamp: 1", styles)

    def test_inline_preview_failure_retries_once_and_offers_manual_regeneration(self):
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        video_start = script.rfind("function setInlinePreviewStatus")
        video_end = script.find("// [AI_WORKBENCH_VIDEO_END]", video_start)
        video = script[video_start:video_end]
        binding_start = script.rfind("function bindDirectPreviewWorkbenchActions")
        binding_end = script.find("if (document.readyState", binding_start)
        binding = script[binding_start:binding_end]

        self.assertIn('button.dataset.action = "preview-inline-retry"', video)
        self.assertIn("function isRetryableInlinePreviewError", video)
        self.assertIn("force = false, retryAttempt = 0", video)
        self.assertIn("(existing?.error && !force)", video)
        self.assertIn("retryAttempt < 1", video)
        self.assertIn("retryAttempt: retryAttempt + 1", video)
        self.assertIn("action === 'preview-inline-retry'", binding)

    def test_selected_preview_clips_follow_manual_assembly_order(self):
        ordered = server._ordered_preview_selection_indices([0, 1, 2], [2, 0, 1], 3)
        self.assertEqual(ordered, [2, 0, 1])

        # Incomplete/stale order payloads must retain valid selected clips.
        recovered = server._ordered_preview_selection_indices([0, 1, 2], [2, 99, 2], 3)
        self.assertEqual(recovered, [2, 0, 1])

    def test_preview_draft_prefers_stable_keys_when_candidate_indices_change(self):
        raw_clips = [
            ("hook", "第一段", 0.0, 2.0, 0, 2.0, "", "C:/source-a.mp4"),
            ("product", "第二段", 2.0, 5.0, 0, 3.0, "", "C:/source-b.mp4"),
        ]
        public_clips = server._preview_public_clips(raw_clips)
        preview = {
            "id": "stable-preview",
            "candidate_raw_clips": raw_clips,
            "candidate_clips": public_clips,
        }
        first_key = public_clips[0]["candidate_key"]
        second_key = public_clips[1]["candidate_key"]

        draft = server._normalize_preview_selection_draft(
            preview,
            "smart",
            [0],
            {},
            order=[0, 1],
            selected_keys=[second_key],
            order_keys=[second_key, first_key],
        )

        self.assertEqual(draft["selected_indices"], [1])
        self.assertEqual(draft["selected_keys"], [second_key])
        self.assertEqual(draft["order"], [1, 0])
        self.assertEqual(draft["order_keys"], [second_key, first_key])

    def test_preview_candidate_key_changes_when_same_path_source_is_replaced(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "same-name.mp4"
            source.write_bytes(b"first")
            first = server._clip_public(
                0, ("product", "相同字幕", 1.0, 3.0, 0, 2.0, "", str(source))
            )["candidate_key"]
            source.write_bytes(b"replacement-content")
            second = server._clip_public(
                0, ("product", "相同字幕", 1.0, 3.0, 0, 2.0, "", str(source))
            )["candidate_key"]

        self.assertNotEqual(first, second)

    def test_preview_quality_report_exposes_duration_timing_and_continuity_gates(self):
        clips = server._preview_public_clips([
            ("product", "第一段卖点", 0.0, 10.0, 0, 10.0, "", "C:/source.mp4"),
            ("product", "第二段卖点", 10.5, 20.0, 0, 9.5, "", "C:/source.mp4"),
        ])
        report = server._preview_quality_report(
            clips,
            {"duration_speed_factor": 1.0, "selection_result": {"status": "partial_insufficient"}},
            60,
            10,
        )

        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["gates"]["selection"], "warning")
        self.assertEqual(report["gates"]["duration"], "warning")
        self.assertEqual(report["source_count"], 1)
        self.assertEqual(report["continuity_break_count"], 0)
        self.assertAlmostEqual(report["projected_final_duration"], 19.5)

    def test_both_preview_workers_apply_the_saved_assembly_order(self):
        smart_source = server._run_smart_cut_from_preview.__code__.co_names
        mix_source = server._run_mix_from_preview.__code__.co_names
        self.assertIn("_ordered_preview_selection_indices", smart_source)
        self.assertIn("_ordered_preview_selection_indices", mix_source)

    def test_media_pipeline_rejects_cross_workspace_overlap(self):
        original_tasks = server._TASKS
        try:
            server._TASKS = {
                "mix-running": {
                    "scope": "mix",
                    "title": "混剪 AI 选片预览",
                    "status": "running",
                }
            }
            with self.assertRaises(server.HTTPException) as context:
                server._ensure_scope_idle("smart-cut", "AI选片预览")
            self.assertEqual(context.exception.status_code, 409)
            self.assertIn("共用媒体处理队列", context.exception.detail)
        finally:
            server._TASKS = original_tasks

    def test_preview_workers_use_invocation_local_result_collectors(self):
        server_source = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        mix_start = server_source.index("def _run_mix_preview")
        smart_start = server_source.index("def _run_smart_preview")
        mix_worker = server_source[mix_start:smart_start]
        smart_worker = server_source[smart_start:server_source.index("def _run_smart_cut_from_preview", smart_start)]

        for worker in (mix_worker, smart_worker):
            self.assertIn("result_cache: dict[str, Any] = {}", worker)
            self.assertIn("_result_cache=result_cache", worker)
            self.assertNotIn("cutter_mod._multi_result_cache", worker)

    def test_clip_only_result_collector_does_not_overwrite_legacy_global_cache(self):
        original_cache = cutter_logic._multi_result_cache
        try:
            cutter_logic._multi_result_cache = {"legacy": "keep"}
            with tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                video = root / "smart.mp4"
                subtitle = root / "smart.srt"
                video.write_bytes(b"")
                subtitle.write_text(
                    "1\n00:00:00,000 --> 00:00:04,000\n独立任务的字幕内容\n",
                    encoding="utf-8",
                )
                result_cache: dict[str, object] = {}
                with (
                    mock.patch("ai_clipper.is_enabled", return_value=False),
                    mock.patch.object(cutter_logic, "_remux_ts_for_editing", side_effect=lambda value, *_args: value),
                    mock.patch.object(
                        cutter_logic,
                        "parse_srt_clips",
                        return_value=[("product", "独立任务的字幕内容", 0.0, 4.0, 50, 4.0, "")],
                    ),
                ):
                    result = cutter_logic.process_video(
                        str(video),
                        srt_path=str(subtitle),
                        output_path=str(root / "preview.mp4"),
                        _clips_only=True,
                        _result_cache=result_cache,
                    )

            self.assertTrue(result["ok"])
            self.assertIn("独立任务的字幕内容", str(result_cache.get("srt_text") or ""))
            self.assertTrue(result_cache.get("clips"))
            self.assertEqual(cutter_logic._multi_result_cache, {"legacy": "keep"})
        finally:
            cutter_logic._multi_result_cache = original_cache

    def test_partial_ai_preview_reaches_result_cache_but_final_cut_remains_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            video = root / "short.mp4"
            subtitle = root / "short.srt"
            video.write_bytes(b"")
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:50,000\n完整安全卖点\n",
                encoding="utf-8",
            )
            clips = [("product", "完整安全卖点", 0.0, 50.0, 50, 50.0, "面料")]

            def partial_ai(*_args, **kwargs):
                contract = kwargs["duration_contract"]
                ai_clipper._begin_analysis_metadata()
                ai_clipper._record_partial_insufficient(
                    clips,
                    candidate_count=1,
                    duration_contract=contract,
                )
                return list(clips)

            result_cache: dict[str, object] = {}
            with (
                mock.patch("ai_clipper.is_enabled", return_value=True),
                mock.patch("ai_clipper.ai_analyze_clips", side_effect=partial_ai),
                mock.patch.object(cutter_logic, "_remux_ts_for_editing", side_effect=lambda value, *_args: value),
            ):
                preview = cutter_logic.process_video(
                    str(video),
                    srt_path=str(subtitle),
                    output_path=str(root / "preview.mp4"),
                    _clips_only=True,
                    target_duration=60,
                    duration_tolerance=15,
                    _result_cache=result_cache,
                )
                self.assertTrue(preview["ok"])
                self.assertEqual(
                    result_cache["analysis_metadata"]["selection_result"]["status"],
                    "partial_insufficient",
                )
                with self.assertRaisesRegex(RuntimeError, "AI未满足时长"):
                    cutter_logic.process_video(
                        str(video),
                        srt_path=str(subtitle),
                        output_path=str(root / "final.mp4"),
                        target_duration=60,
                        duration_tolerance=15,
                    )

    def _public_clip(self):
        raw_clip = (
            "product",
            "[V2] \u597d\u770b\u8fdd\u7981\u8bcd\u7a7f\u8d77\u6765\u5f88\u663e\u7626",
            10.0,
            12.0,
            0,
            2.0,
            "",
            "C:/v2.mp4",
        )
        words = [
            {"index": 0, "text": "\u597d\u770b", "start": 10.0, "end": 10.35},
            {"index": 1, "text": "\u8fdd\u7981", "start": 10.35, "end": 10.65},
            {"index": 2, "text": "\u8bcd", "start": 10.65, "end": 10.82},
            {"index": 3, "text": "\u7a7f\u8d77\u6765", "start": 10.82, "end": 11.35},
            {"index": 4, "text": "\u5f88\u663e\u7626", "start": 11.35, "end": 12.0},
        ]
        with mock.patch.object(server, "_preview_forbidden_words", return_value=[self.forbidden]):
            server._preview_lock_forbidden_words(words)
            public = {
                "index": 0,
                "text": raw_clip[1],
                "source": raw_clip[7],
                "source_marker": "V2",
                "selected": True,
                "segments": [{
                    "index": 0,
                    "text": "\u597d\u770b\u8fdd\u7981\u8bcd\u7a7f\u8d77\u6765\u5f88\u663e\u7626",
                    "start": 10.0,
                    "end": 12.0,
                    "duration": 2.0,
                    "selected": True,
                    "word_timed": True,
                    "words": words,
                }],
            }
            server._preview_lock_unsafe_segments([public])
        return raw_clip, public

    def _preview_fixture(self):
        raw_clip, public = self._public_clip()
        return {
            "id": "preview-word-edit",
            "status": "ready",
            "candidate_raw_clips": [raw_clip],
            "candidate_clips": [public],
            "selection_draft": {
                "selected_indices": [0],
                "selected_segments": {"0": [0]},
                "selected_words": {"0": {"0": [0]}},
                "order": [0],
            },
        }

    def test_forbidden_phrase_is_locked_but_safe_words_remain_available(self):
        _raw_clip, public = self._public_clip()
        segment = public["segments"][0]
        self.assertTrue(segment["selected"])
        self.assertFalse(segment.get("selection_locked", False))
        self.assertTrue(segment["word_timed"])
        self.assertEqual([word["index"] for word in segment["words"]], [0, 1, 2, 3, 4])
        self.assertTrue(segment["words"][1]["selection_locked"])
        self.assertTrue(segment["words"][2]["selection_locked"])
        self.assertEqual(segment["words"][1]["blocked_reason"], "\u8fdd\u7981\u8bcd\uff1a" + self.forbidden)

    def test_normalizer_rejects_locked_words_and_keeps_explicit_empty_selection(self):
        preview = self._preview_fixture()
        draft = server._normalize_preview_selection_draft(
            preview,
            "mix",
            [0],
            {"0": [0]},
            {"0": {"0": [0, 1, 2, 4, 99]}},
            order=[0],
        )
        self.assertEqual(draft["selected_words"], {"0": {"0": [0, 4]}})
        empty = server._normalize_preview_selection_draft(
            preview,
            "mix",
            [0],
            {"0": [0]},
            {"0": {"0": []}},
            order=[0],
        )
        self.assertEqual(empty["selected_words"], {"0": {"0": []}})

    def test_word_gap_becomes_exact_safe_video_runs(self):
        raw_clip, public = self._public_clip()
        parts = server._merge_selected_segments(
            public,
            raw_clip,
            [0],
            {"0": [0, 1, 2, 3, 4]},
        )
        self.assertEqual([(round(part[2], 2), round(part[3], 2)) for part in parts], [(10.0, 10.35), (10.82, 12.0)])
        self.assertEqual([part[1] for part in parts], ["[V2] \u597d\u770b", "[V2] \u7a7f\u8d77\u6765\u5f88\u663e\u7626"])
        self.assertTrue(all("\u8fdd\u7981" not in part[1] and "\u8bcd" not in part[1] for part in parts))

    def test_connected_word_timed_piece_stays_with_its_previous_sentence(self):
        raw_clip = (
            "product",
            "\u7b49\u5230\u516b\u4e5d\u6708\u4efd\u7a7f\u6ca1\u95ee\u9898\u56e0\u4e3a\u5b83\u662f\u5355\u8584\u7684\u957f\u8896\u800c\u4e14\u662f\u6709\u70b9\u5c0f\u886c\u886b\u611f\u7684",
            10.0,
            17.0,
            50,
            7.0,
            "\u9762\u6599\u8d28\u611f",
        )
        word_timings = [
            {
                "words": [
                    {"text": "\u7b49\u5230\u516b\u4e5d\u6708\u4efd\u7a7f\u6ca1\u95ee\u9898", "start": 10.0, "end": 12.7},
                    {"text": "\u56e0\u4e3a\u5b83\u662f\u5355\u8584\u7684\u957f\u8896", "start": 12.8, "end": 14.8},
                ],
            },
            {
                "words": [
                    {"text": "\u800c\u4e14", "start": 15.1, "end": 15.35},
                    {"text": "\u662f\u6709\u70b9\u5c0f\u886c\u886b\u611f\u7684", "start": 15.4, "end": 17.0},
                ],
            },
        ]

        public = server._preview_public_clips([raw_clip], word_timings=word_timings)
        segments = public[0]["segments"]

        self.assertEqual(len(segments), 1)
        self.assertIn("\u800c\u4e14", segments[0]["text"])
        self.assertEqual(segments[0]["semantic_piece_count"], 2)
        self.assertEqual(segments[0]["start"], 10.0)
        self.assertEqual(segments[0]["end"], 17.0)
        self.assertEqual(
            [word["index"] for word in segments[0]["words"]],
            list(range(len(segments[0]["words"]))),
        )

        orphan = {
            "index": 0,
            "start": 15.1,
            "end": 17.0,
            "text": "\u800c\u4e14\u662f\u6709\u70b9\u5c0f\u886c\u886b\u611f\u7684",
            "words": [],
        }
        self.assertEqual(server._preview_segment_selection_units(orphan, {}), [])

    def test_legacy_duplicate_word_indices_are_resolved_by_sentence_position(self):
        segment = {
            "index": 0,
            "start": 10.0,
            "end": 12.0,
            "text": "甲乙丙丁",
            # This is the old cached shape produced when two semantic pieces
            # were merged before their indices were made unique.
            "words": [
                {"index": 0, "text": "甲", "start": 10.0, "end": 10.3},
                {"index": 1, "text": "乙", "start": 10.3, "end": 10.6},
                {"index": 0, "text": "丙", "start": 10.6, "end": 10.9},
                {"index": 1, "text": "丁", "start": 10.9, "end": 11.2},
            ],
        }

        draft = server._normalize_preview_selection_draft(
            {"raw_clips": [("product", "甲乙丙丁", 10.0, 12.0)], "clips": [{"index": 0, "segments": [segment]}]},
            "smart",
            [0],
            {"0": [0]},
            {"0": {"0": [0, 2, 3]}},
        )
        self.assertEqual(draft["selected_words"], {"0": {"0": [0, 2, 3]}})

        units = server._preview_segment_selection_units(segment, draft["selected_words"]["0"])

        self.assertEqual(
            [(unit["text"], unit["selected_word_indices"]) for unit in units],
            [("甲", [0]), ("丙丁", [2, 3])],
        )

    def test_empty_preview_cache_recovers_word_timing_from_sidecar(self):
        sidecar_segments = [{
            "start": 10.0,
            "end": 12.0,
            "text": "\u80a9\u7ebf\u81ea\u7136\u7a7f\u8d77\u6765\u5f88\u663e\u7626",
            "words": [
                {"text": "\u80a9\u7ebf", "start": 10.0, "end": 10.5},
                {"text": "\u81ea\u7136", "start": 10.5, "end": 11.0},
                {"text": "\u663e\u7626", "start": 11.0, "end": 12.0},
            ],
        }]
        with mock.patch("volcengine_asr.load_word_timing_sidecar", return_value=sidecar_segments):
            restored, summary = server._preview_word_timings_with_recovery(
                [],
                [("V2", "C:/video.srt")],
                scope="test",
            )
        self.assertEqual(summary["source"], "sidecar_recovery")
        self.assertEqual(summary["semantic_segment_count"], 1)
        self.assertEqual(summary["word_count"], 3)
        self.assertEqual(restored[0]["source_marker"], "V2")
        self.assertEqual(restored[0]["words"][0]["start"], 10.0)

    def test_explicit_empty_words_do_not_fall_back_to_saved_draft(self):
        preview = self._preview_fixture()
        handlers = [
            (server.SmartPreviewCutPayload, server.start_smart_from_preview),
            (server.MixPreviewCutPayload, server.start_mix_from_preview),
        ]
        for payload_type, handler in handlers:
            payload = payload_type(
                preview_id="preview-word-edit",
                selected_indices=[0],
                selected_segments={"0": [0]},
                selected_words={},
                order=[0],
            )
            with (
                mock.patch.object(server, "_ensure_scope_idle"),
                mock.patch.object(server, "_get_preview", return_value=preview),
                mock.patch.object(server, "_raise_preflight_errors"),
                mock.patch.object(
                    server,
                    "_apply_preview_payload_draft",
                    return_value={"selected_indices": [0], "selected_keys": [], "order": [0], "order_keys": []},
                ) as apply_draft,
                mock.patch.object(server, "_record_preview_selection_feedback"),
                mock.patch.object(server, "_new_task", return_value="task-word-edit"),
                mock.patch.object(server.threading, "Thread"),
            ):
                handler(payload)
            self.assertEqual(apply_draft.call_args.args[5], {})
            fields = getattr(payload, "model_fields_set", None)
            if fields is None:
                fields = getattr(payload, "__fields_set__", set())
            self.assertIn("selected_words", fields)


if __name__ == "__main__":
    unittest.main()
