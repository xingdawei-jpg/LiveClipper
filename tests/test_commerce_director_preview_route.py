from __future__ import annotations

import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web_client"))
sys.path.insert(0, str(ROOT / "app"))


server = importlib.import_module("server")


class CommerceDirectorPreviewRouteTests(unittest.TestCase):
    def test_internal_direct_render_plan_does_not_replace_latest_editable_preview(self) -> None:
        visible = {
            "id": "visible",
            "scope": "smart-cut",
            "created_at": 10.0,
            "status": "ready",
        }
        hidden = {
            "id": "hidden",
            "scope": "smart-cut",
            "created_at": 20.0,
            "status": "ready",
            "hidden_from_latest": True,
        }
        with (
            mock.patch.object(server, "_PREVIEW_CLEARED_AT", 0.0),
            mock.patch.dict(server._CLIP_PREVIEWS, {"visible": visible, "hidden": hidden}, clear=True),
        ):
            latest = server._latest_preview("smart-cut")

        self.assertEqual(latest["id"], "visible")

    def test_direct_smart_cut_endpoint_uses_the_director_render_worker(self) -> None:
        payload = server.SmartCutPayload(video_paths=["C:/source.mp4"])
        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_raise_preflight_errors"),
            mock.patch.object(server, "_new_task", return_value="director-render-task"),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            result = server.start_smart_cut(payload)

        self.assertTrue(result["ok"])
        self.assertIs(thread.call_args.kwargs["args"][1], server._run_commerce_director_smart_cut)

    def test_direct_mix_endpoints_use_the_director_render_workers(self) -> None:
        payload = server.MixPayload(video_paths=["C:/v1.mp4", "C:/v2.mp4"])
        batch = server.MixBatchPayload(groups=[{
            "name": "第一组",
            "video_paths": ["C:/v1.mp4", "C:/v2.mp4"],
        }])
        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_raise_preflight_errors"),
            mock.patch.object(server, "_new_task", return_value="director-mix-task"),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            self.assertTrue(server.start_mix(payload)["ok"])
            self.assertIs(thread.call_args.kwargs["args"][1], server._run_commerce_director_mix)
            self.assertTrue(server.start_mix_batch(batch)["ok"])
            self.assertIs(thread.call_args.kwargs["args"][1], server._run_commerce_director_mix_batch)

    def test_direct_smart_cut_renders_only_the_recommended_director_story(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "source.mp4"
            video.write_bytes(b"video")
            payload = server.SmartCutPayload(video_paths=[str(video)], versions=2)
            preview = {
                "status": "ready",
                "raw_clips": [
                    ("hook", "第一句", 1.0, 3.0, 0.0, 2.0, "", str(video)),
                    ("product", "第二句", 4.0, 6.0, 0.0, 2.0, "", str(video)),
                ],
                "candidate_raw_clips": [
                    ("hook", "第一句", 1.0, 3.0, 0.0, 2.0, "", str(video)),
                    ("product", "第二句", 4.0, 6.0, 0.0, 2.0, "", str(video)),
                    ("product", "备用句", 7.0, 9.0, 0.0, 2.0, "", str(video)),
                ],
            }
            with (
                mock.patch.object(server, "_ensure_feature_access"),
                mock.patch.object(
                    server,
                    "_build_director_preview_for_direct_output",
                    return_value=("preview-1", preview),
                ),
                mock.patch.object(
                    server,
                    "_run_smart_cut_from_preview",
                    return_value=["out_v1.mp4", "out_v2.mp4"],
                ) as render,
                mock.patch.object(server, "_set_task"),
                mock.patch.object(server, "_set_task_progress"),
                mock.patch.object(server, "emit_log"),
            ):
                server._run_commerce_director_smart_cut("task-1", payload)

        render_payload = render.call_args.args[1]
        self.assertEqual(render_payload.preview_id, "preview-1")
        self.assertEqual(render_payload.selected_indices, [0, 1])
        self.assertEqual(render_payload.order, [0, 1])
        self.assertEqual(render_payload.versions, 2)
        self.assertFalse(render.call_args.kwargs["finalize_task"])
        self.assertTrue(render.call_args.kwargs["raise_errors"])

    def test_normal_smart_preview_endpoint_uses_the_director_worker(self) -> None:
        payload = server.SmartCutPayload(video_paths=["C:/source.mp4"])
        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_raise_preflight_errors"),
            mock.patch.object(server, "_new_task", return_value="director-task"),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            result = server.start_smart_preview(payload)

        self.assertTrue(result["ok"])
        self.assertIs(thread.call_args.kwargs["args"][1], server._run_commerce_director_preview_auto_batch)

    def test_mix_preview_endpoint_uses_the_multisource_director_worker(self) -> None:
        payload = server.MixPayload(video_paths=["C:/v1.mp4", "C:/v2.mp4"])
        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_raise_preflight_errors"),
            mock.patch.object(server, "_new_task", return_value="mix-director-task"),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            result = server.start_mix_preview(payload)

        self.assertTrue(result["ok"])
        self.assertIs(thread.call_args.kwargs["args"][1], server._run_commerce_director_mix_preview)
        self.assertIn("2 条素材", result["message"])

    def test_mix_alternative_reuses_all_sources_after_cost_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            experiment_dir = root / "workspace" / "ui_commerce_director_experiment" / "source-task"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "m1_story_brief.json").write_text(json.dumps({
                "m1_result": {"strategies": [
                    {"strategy_id": "S1", "director_plan_role": "primary"},
                    {"strategy_id": "S2", "director_plan_role": "alternative", "core_desire": "夏日场景"},
                ]},
            }, ensure_ascii=False), encoding="utf-8")
            (experiment_dir / "source_info.json").write_text(json.dumps({
                "task_content_policy": {"price": "block"},
                "director_controls": {"director_direction": "场景种草", "preferred_terms": ["三伏天"],
                                      "preference_weights": {"场景搭配": 3}},
            }, ensure_ascii=False), encoding="utf-8")
            previous = {
                "scope": "mix",
                "commercial_director_experiment": True,
                "sources": ["C:/v1.mp4", "C:/v2.mp4"],
                "target_duration": 60,
                "duration_tolerance": 8,
                "dedup_summary": {"experiment_dir": str(experiment_dir)},
                "director_review": {"director_strategy_library": {"proposals": [{
                    "director_strategy_id": "D2",
                    "primary_story_id": "S2",
                    "name": "夏日松弛感",
                    "available": True,
                    "requires_additional_ai_call": True,
                }]}},
            }
            payload = server.CommerceDirectorStrategySelectionPayload(
                preview_id="old-mix",
                director_strategy_id="D2",
                confirm_additional_ai_call=True,
            )
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_ensure_scope_idle"),
                mock.patch.object(server, "_get_preview", return_value=previous),
                mock.patch.object(server, "_new_task", return_value="mix-alt-task"),
                mock.patch.object(server.threading, "Thread") as thread,
            ):
                result = server.select_mix_commerce_director_strategy(payload)

            self.assertTrue(result["ok"])
            self.assertEqual(result["additional_ai_calls"], 2)
            args = thread.call_args.kwargs["args"]
            self.assertIs(args[1], server._run_commerce_director_mix_preview)
            self.assertEqual(args[4].video_paths, ["C:/v1.mp4", "C:/v2.mp4"])
            self.assertEqual(args[4].ai_controls["director_direction"], "场景种草")
            self.assertEqual(args[4].ai_controls["preferred_terms"], ["三伏天"])
            self.assertEqual(args[4].ai_controls["preference_weights"], {"场景搭配": 3})
            self.assertEqual(args[5]["strategy_id"], "S2")

    def test_smart_alternative_keeps_original_director_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            experiment_dir = root / "workspace" / "ui_commerce_director_experiment" / "source-task"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "m1_story_brief.json").write_text(json.dumps({
                "m1_result": {"strategies": [{"strategy_id": "S2", "director_plan_role": "alternative"}]},
            }), encoding="utf-8")
            controls = {"director_direction": "显瘦转化", "main_product": "焦糖朗姆",
                        "preferred_terms": ["三伏天"], "avoid": ["闲聊"], "preference_weights": {"场景搭配": 3}}
            (experiment_dir / "source_info.json").write_text(json.dumps({
                "director_controls": controls, "task_content_policy": {"price": "block"},
            }), encoding="utf-8")
            previous = {
                "commercial_director_experiment": True, "video": "C:/source.mp4", "srt_path": "C:/source.srt",
                "target_duration": 60, "dedup_summary": {"experiment_dir": str(experiment_dir)},
                "director_review": {"director_strategy_library": {"proposals": [{
                    "director_strategy_id": "D2", "primary_story_id": "S2", "available": True,
                    "requires_additional_ai_call": True,
                }]}},
            }
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_ensure_scope_idle"),
                mock.patch.object(server, "_get_preview", return_value=previous),
                mock.patch.object(server, "_new_task", return_value="smart-alt-task"),
                mock.patch.object(server.threading, "Thread") as thread,
            ):
                result = server.select_commerce_director_strategy(server.CommerceDirectorStrategySelectionPayload(
                    preview_id="previous", director_strategy_id="D2", confirm_additional_ai_call=True,
                ))
            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[4].ai_controls, {**controls, "content_policy": {"price": "block"}})
            self.assertEqual(args[-1]["strategy_id"], "S2")
            self.assertEqual(result["additional_ai_calls"], 2)

    def test_mix_director_virtual_ranges_map_back_to_each_real_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            videos = [root / "v1.mp4", root / "v2.mp4"]
            srts = [root / "v1.srt", root / "v2.srt"]
            for video in videos:
                video.write_bytes(b"video")
            srts[0].write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n第一条真实原话\n", encoding="utf-8",
            )
            srts[1].write_text(
                "1\n00:00:02,000 --> 00:00:04,000\n第二条真实原话\n", encoding="utf-8",
            )

            def ensure(video: Path, _scope: str) -> Path:
                return srts[videos.index(video)]

            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_ensure_srt", side_effect=ensure),
                mock.patch.object(server, "_verify_local_asr_temp_identity_for_experiment"),
            ):
                bundle = server._prepare_mix_director_source(videos, task_id="mix-map", scope="mix")

            from semantic_word_binder import build_semantic_srt_word_timeline
            timeline = build_semantic_srt_word_timeline(
                bundle["srt_path"], source_video_path=videos[0],
            )
            self.assertTrue(timeline.words)
            self.assertFalse(timeline.validation_issues)

            second = bundle["intervals"][1]
            offset = float(second["offset"])
            mapped = server._remap_mix_director_raw_clips(
                [("product", "第二条真实原话", offset + 2.0, offset + 4.0, 0.0, 2.0, "", str(videos[0]))],
                bundle,
            )
            public = server._clip_public(0, mapped[0])
            self.assertEqual(public["source"], str(videos[1]))
            self.assertEqual((public["start"], public["end"]), (2.0, 4.0))
            self.assertTrue(public["text"].startswith("[V2]"))
            self.assertIn("[V1]", bundle["preview_srt_text"])
            self.assertIn("[V2]", bundle["preview_srt_text"])

    def test_director_route_no_longer_requires_an_experimental_planner_setting(self) -> None:
        payload = server.SmartCutPayload(video_paths=["C:/source.mp4"])
        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_raise_preflight_errors"),
            mock.patch.object(server, "_new_task", return_value="director-task"),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            result = server.start_commerce_director_preview(payload)

        self.assertTrue(result["ok"])
        self.assertIs(thread.call_args.kwargs["args"][1], server._run_commerce_director_preview_auto_batch)

    def test_experimental_preview_cannot_enter_formal_preview_rendering(self) -> None:
        preview = {
            "id": "experimental-preview",
            "status": "ready",
            "scope": "smart-cut",
            "commercial_director_experiment": True,
            "raw_clips": [("product", "可审阅片段", 1.0, 3.0, 0.0, 2.0, "", "C:/source.mp4")],
        }
        payload = server.SmartPreviewCutPayload(preview_id="experimental-preview")
        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_get_preview", return_value=preview),
            self.assertRaises(server.HTTPException) as raised,
        ):
            server.start_smart_from_preview(payload)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("仅供人工审核", raised.exception.detail)

    def test_public_preview_keeps_experiment_identity_for_the_frontend(self) -> None:
        public = server._preview_public({
            "id": "preview-1",
            "status": "ready",
            "commercial_director_experiment": True,
            "commercial_director_sentence_preview": True,
            "planner_mode": "lite_director_experiment",
            "director_review": {"kind": "m2_draft_review_only"},
        })
        self.assertTrue(public["commercial_director_experiment"])
        self.assertTrue(public["commercial_director_sentence_preview"])
        self.assertEqual(public["planner_mode"], "lite_director_experiment")
        self.assertEqual(public["director_review"]["kind"], "m2_draft_review_only")

    def test_sentence_preview_can_use_the_legacy_word_editing_render_path(self) -> None:
        preview = {
            "id": "sentence-preview",
            "status": "ready",
            "scope": "smart-cut",
            "commercial_director_experiment": True,
            "commercial_director_sentence_preview": True,
            "raw_clips": [
                ("product", "肩线会往里收", 1.0, 3.0, 50.0, 2.0, "版型", "C:/source.mp4"),
            ],
            "clips": [{
                "index": 0,
                "selected": True,
                "segments": [{
                    "index": 0, "selected": True, "start": 1.0, "end": 3.0,
                    "text": "肩线会往里收", "words": [],
                }],
            }],
        }
        payload = server.SmartPreviewCutPayload(
            preview_id="sentence-preview",
            selected_indices=[0],
            selected_segments={"0": [0]},
        )

        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_get_preview", return_value=preview),
            mock.patch.object(server, "_raise_preflight_errors"),
            mock.patch.object(server, "_record_preview_selection_feedback"),
            mock.patch.object(server, "_new_task", return_value="render-task"),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            result = server.start_smart_from_preview(payload)

        self.assertTrue(result["ok"])
        self.assertEqual(result["task_id"], "render-task")
        self.assertIs(thread.call_args.kwargs["args"][1], server._run_smart_cut_from_preview)

    def test_selecting_a_story_reuses_frozen_m1_without_starting_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            experiment_dir = root / "workspace" / "ui_commerce_director_experiment" / "source-task"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "m1_story_brief.json").write_text(json.dumps({
                "m1_result": {"strategies": [{
                    "strategy_id": "S2", "type": "commercial_story", "thesis": "材质体验",
                    "target_user": "在意舒适的人", "evidence": [{
                        "role": "hook", "claim": "穿一天也舒服", "subtitle_ids": [7],
                    }],
                }]},
            }, ensure_ascii=False), encoding="utf-8")
            preview = {
                "commercial_director_experiment": True,
                "video": "C:/source.mp4", "srt_path": "C:/source.srt", "target_duration": 45,
                "dedup_summary": {"experiment_dir": str(experiment_dir)},
                "director_review": {"m1_story_library": {"stories": [{"story_id": "S2"}]}},
            }
            payload = server.CommerceDirectorStorySelectionPayload(preview_id="old", story_id="S2")
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_ensure_scope_idle"),
                mock.patch.object(server, "_get_preview", return_value=preview),
                mock.patch.object(server, "_new_task", return_value="new-task"),
                mock.patch.object(server.threading, "Thread") as thread,
            ):
                result = server.select_commerce_director_story(payload)

            self.assertTrue(result["ok"])
            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[5].strategy_id, "S2")
            self.assertEqual(args[9], "S2")

    def test_selecting_director_strategy_compiles_a_story_mix_for_m2(self) -> None:
        from commercial_analyzer import Strategy
        from director_strategy import build_director_strategy_library

        raw_strategies = [
            {"strategy_id": "S1", "type": "problem_transformation", "strategy_family": "body_confidence",
             "thesis": "宽肩显瘦", "target_user": "宽肩女生", "story_priority": "high",
             "evidence": [{"role": "hook", "claim": "肩宽显壮", "subtitle_ids": [1]}]},
            {"strategy_id": "S2", "type": "trust_authority", "strategy_family": "quality_assurance",
             "thesis": "面料品质", "target_user": "在意品质的人", "evidence": [
                 {"role": "proof", "claim": "面料可信", "subtitle_ids": [2]}]},
        ]
        strategies = tuple(Strategy.from_dict(item, index=index) for index, item in enumerate(raw_strategies, 1))
        strategy_library = build_director_strategy_library(strategies)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            experiment_dir = root / "workspace" / "ui_commerce_director_experiment" / "source-task"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "m1_story_brief.json").write_text(json.dumps({
                "m1_result": {"strategies": raw_strategies},
            }, ensure_ascii=False), encoding="utf-8")
            preview = {
                "commercial_director_experiment": True,
                "video": "C:/source.mp4", "srt_path": "C:/source.srt", "target_duration": 45,
                "dedup_summary": {"experiment_dir": str(experiment_dir)},
                "director_review": {
                    "m1_story_library": {"stories": [{"story_id": "S1"}, {"story_id": "S2"}]},
                    "director_strategy_library": strategy_library,
                },
            }
            payload = server.CommerceDirectorStrategySelectionPayload(preview_id="old", director_strategy_id="D_CONVERSION_S2")
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_ensure_scope_idle"),
                mock.patch.object(server, "_get_preview", return_value=preview),
                mock.patch.object(server, "_new_task", return_value="new-task"),
                mock.patch.object(server.threading, "Thread") as thread,
            ):
                result = server.select_commerce_director_strategy(payload)

            self.assertTrue(result["ok"])
            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[5].strategy_id, "D_CONVERSION_S2")
            self.assertEqual(args[8]["source_story_ids"], ["S2", "S1"])

    def test_batch_generation_reuses_one_m1_map_for_multiple_strategies(self) -> None:
        from commercial_analyzer import Strategy
        from director_strategy import build_director_strategy_library

        raw_strategies = [
            {"strategy_id": "S1", "type": "problem_transformation", "strategy_family": "body_confidence",
             "thesis": "宽肩显瘦", "target_user": "宽肩女生", "story_priority": "high",
             "evidence": [{"role": "hook", "claim": "肩宽显壮", "subtitle_ids": [1]}]},
            {"strategy_id": "S2", "type": "trust_authority", "strategy_family": "quality_assurance",
             "thesis": "面料品质", "target_user": "在意品质的人",
             "evidence": [{"role": "proof", "claim": "面料可信", "subtitle_ids": [2]}]},
        ]
        strategies = tuple(Strategy.from_dict(item, index=index) for index, item in enumerate(raw_strategies, 1))
        strategy_library = build_director_strategy_library(strategies)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            experiment_dir = root / "workspace" / "ui_commerce_director_experiment" / "source-task"
            experiment_dir.mkdir(parents=True)
            (experiment_dir / "m1_story_brief.json").write_text(json.dumps({
                "m1_result": {"strategies": raw_strategies},
            }, ensure_ascii=False), encoding="utf-8")
            preview = {
                "commercial_director_experiment": True,
                "video": "C:/source.mp4", "srt_path": "C:/source.srt", "target_duration": 45,
                "dedup_summary": {"experiment_dir": str(experiment_dir)},
                "director_review": {
                    "kind": "m1_story_map_discovery",
                    "m1_story_library": {"stories": [{"story_id": "S1"}, {"story_id": "S2"}]},
                    "director_strategy_library": strategy_library,
                },
            }
            payload = server.CommerceDirectorStrategyBatchPayload(preview_id="old")
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_ensure_scope_idle"),
                mock.patch.object(server, "_get_preview", return_value=preview),
                mock.patch.object(server, "_new_task", side_effect=["batch", "child-1", "child-2"]),
                mock.patch.object(server.threading, "Thread") as thread,
            ):
                result = server.generate_commerce_director_strategies(payload)

            self.assertTrue(result["ok"])
            self.assertEqual(result["strategy_count"], 2)
            args = thread.call_args.kwargs["args"]
            self.assertEqual(args[0], "batch")
            entries = args[5]
            self.assertEqual([item["proposal"]["director_strategy_id"] for item in entries], [
                "D_TRAFFIC_S1", "D_CONVERSION_S2",
            ])
            self.assertEqual([item["contract"]["source_story_ids"] for item in entries], [["S1"], ["S2", "S1"]])

    def test_initial_preview_runs_two_stage_primary_director_packet_only(self) -> None:
        payload = server.SmartCutPayload(video_paths=["C:/source.mp4"], srt_path="C:/source.srt", target_duration=45)
        with (
            mock.patch.object(server, "_run_commerce_director_preview") as run_m1,
        ):
            server._run_commerce_director_preview_auto_batch("parent", "public-preview", payload)

        self.assertEqual(run_m1.call_args.args[:3], ("parent", "public-preview", payload))
        self.assertEqual(
            run_m1.call_args.kwargs["director_strategy_contract"]["single_ai_director_packet"],
            True,
        )
        self.assertTrue(
            run_m1.call_args.kwargs["director_strategy_contract"]["two_pass_director_packet"]
        )
        self.assertTrue(
            run_m1.call_args.kwargs["director_strategy_contract"]["sentence_preview_without_m3"]
        )
        self.assertEqual(
            run_m1.call_args.kwargs["director_strategy_contract"]["semantic_call_count"], 2
        )
        self.assertEqual(run_m1.call_count, 1)

    def test_initial_preview_endpoint_starts_the_one_click_auto_batch_worker(self) -> None:
        payload = server.SmartCutPayload(video_paths=["C:/source.mp4"])
        settings = {"m2_planner_mode": "lite_director_experiment"}
        with (
            mock.patch.object(server, "_ensure_scope_idle"),
            mock.patch.object(server, "_raise_preflight_errors"),
            mock.patch.object(server, "_load_settings", return_value=settings),
            mock.patch("ai_director_experiment.controlled_planner_mode", return_value="lite_director_experiment"),
            mock.patch.object(server, "_new_task", return_value="public-task"),
            mock.patch.object(server.threading, "Thread") as thread,
        ):
            result = server.start_commerce_director_preview(payload)

        self.assertTrue(result["ok"])
        self.assertIn("本次目标 60 秒", result["message"])
        self.assertIn("从完整字幕选择真实短句", result["message"])
        self.assertIs(thread.call_args.kwargs["args"][1], server._run_commerce_director_preview_auto_batch)

    def test_review_concat_reencodes_instead_of_copying_cached_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parts = [root / "part_00.mp4", root / "part_01.mp4"]
            for part in parts:
                part.write_bytes(b"part")
            target = root / "review.mp4"
            observed: dict[str, list[str]] = {}

            def fake_run(cmd, **_kwargs):
                observed["cmd"] = list(cmd)
                target.write_bytes(b"x" * 1500)
                return mock.Mock(returncode=0, stderr="")

            with (
                mock.patch.object(server, "_ffmpeg_cmd", return_value="ffmpeg"),
                mock.patch.object(server.subprocess, "run", side_effect=fake_run),
                mock.patch.object(server, "_preview_media_decode_error", return_value=""),
            ):
                server._concat_preview_clip_parts(parts, target)

            cmd = observed["cmd"]
            self.assertIn("libx264", cmd)
            self.assertIn("aac", cmd)
            self.assertFalse(any(cmd[i:i + 2] == ["-c", "copy"] for i in range(len(cmd) - 1)))

    def test_review_video_discards_corrupt_cached_target_and_part(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_dir = Path(tmp)
            source = review_dir / "source.mp4"
            source.write_bytes(b"source")
            target = review_dir / "m2_draft_review.mp4"
            target.write_bytes(b"broken-target" * 200)
            stale_part = review_dir / "part_00.mp4"
            stale_part.write_bytes(b"broken-part" * 200)
            preview = {
                "commercial_director_experiment": True,
                "director_review": {"kind": "m2_draft_review_only"},
                "raw_clips": [("C1", "可审阅片段", 1.0, 2.0, 0.0, 1.0, "", str(source))],
            }
            rendered: list[Path] = []

            def fake_render(_source, _start, _duration, path):
                rendered.append(path)
                path.write_bytes(b"valid-part" * 200)

            def fake_concat(_parts, path):
                path.write_bytes(b"valid-target" * 200)

            with (
                mock.patch.object(server, "_get_preview", return_value=preview),
                mock.patch.object(server, "_safe_user_child", return_value=review_dir),
                mock.patch.object(server, "_preview_clip_source", return_value=source),
                mock.patch.object(server, "_preview_media_decode_error", return_value="invalid_nal"),
                mock.patch.object(server, "_render_preview_clip_range", side_effect=fake_render),
                mock.patch.object(server, "_concat_preview_clip_parts", side_effect=fake_concat),
            ):
                result = server._commerce_director_review_video("preview-1")

            self.assertEqual(result, target)
            self.assertEqual(rendered, [stale_part])
            self.assertEqual(target.read_bytes(), b"valid-target" * 200)

    def test_invalid_m2_plan_can_be_exposed_only_as_exact_lineage_draft_review(self) -> None:
        case = {
            "m2_plan": {
                "plan_valid": False,
                "target_duration": 60,
                "issues": ["bridge_not_consumed"],
                "story_brief": {"thesis": "宽肩显窄", "core_commercial_idea": "版型解决肩宽"},
                "selected_candidates": [
                    {"candidate_id": 1, "start": 1.0, "end": 3.0, "text": "宽肩穿这个会显窄"},
                    {"candidate_id": 2, "start": 4.0, "end": 6.0, "text": "肩线往里收"},
                ],
                "beats": [
                    {"chapter_id": "C1", "narrative_role": "hook", "goal": "提出痛点", "candidate_ids": [1]},
                    {"chapter_id": "C2", "narrative_role": "mechanism", "goal": "解释机制", "candidate_ids": [2]},
                ],
            },
            "m3_selection_result": {"status": "selector_blocked"},
            "chapter_lineage": [
                {"candidate_id": 1, "semantic_srt_word_spans": [{"status": "bound", "alignment_kind": "exact", "alignment_confidence": 1.0}]},
                {"candidate_id": 2, "semantic_srt_word_spans": [{"status": "bound", "alignment_kind": "exact", "alignment_confidence": 1.0}]},
            ],
        }
        result = server._commerce_director_draft_review(case, video=Path("C:/source.mp4"))
        self.assertIsNotNone(result)
        raw_clips, review = result
        self.assertEqual([clip[0] for clip in raw_clips], ["C1", "C2"])
        self.assertEqual(review["kind"], "m2_draft_review_only")
        self.assertFalse(review["review_contract"]["m3_materialized"])

    def test_invalid_m2_plan_without_exact_word_lineage_has_no_review_video(self) -> None:
        case = {
            "m2_plan": {
                "plan_valid": False,
                "selected_candidates": [{"candidate_id": 1, "start": 1.0, "end": 2.0, "text": "候选"}],
                "beats": [{"chapter_id": "C1", "candidate_ids": [1]}],
            },
            "chapter_lineage": [{"candidate_id": 1, "semantic_srt_word_spans": [{"status": "unbound"}]}],
        }
        self.assertIsNone(server._commerce_director_draft_review(case, video=Path("C:/source.mp4")))

    def test_m2_sentence_preview_keeps_each_ai_selected_candidate_separate_and_ordered(self) -> None:
        case = {
            "m2_plan": {
                "selected_candidates": [
                    {"candidate_id": 2, "source_id": "S", "start": 4.0, "end": 6.2, "duration": 2.2, "text": "第二句", "origin_subtitle_ids": [2]},
                    {"candidate_id": 1, "source_id": "S", "start": 1.0, "end": 3.0, "duration": 2.0, "text": "第一句", "origin_subtitle_ids": [1]},
                ],
                "beats": [
                    {"chapter_id": "C1", "candidate_ids": [1]},
                    {"chapter_id": "C2", "candidate_ids": [2]},
                ],
            },
            "chapter_lineage": [],
        }

        clips = server._commerce_director_sentence_raw_clips(case, video=Path("C:/source.mp4"))

        self.assertEqual([server._clip_public(0, item)["text"] for item in clips], ["第一句", "第二句"])
        self.assertEqual([server._clip_public(0, item)["clip_type"] for item in clips], ["C1", "C2"])
        self.assertEqual([server._clip_public(0, item)["duration"] for item in clips], [2.0, 2.2])

    def test_same_casting_call_alternatives_are_manual_candidates_only(self) -> None:
        case = {
            "m2_plan": {
                "selection_contract": {
                    "director_chapter_packets": [{
                        "chapter_id": "C1",
                        "title": "大身材显瘦结果",
                        "chapter_kind": "result",
                        "buyer_advance": "先让观众看到整体显窄",
                        "beats": [{"beat_id": "C1B1", "subtitle_ids": [1]}],
                        "alternative_beats": [{
                            "beat_id": "C1A1",
                            "beat_function": "result",
                            "subtitle_ids": [2],
                            "replaces_beat_id": "C1B1",
                        }],
                    }],
                },
            },
        }
        srt = (
            "1\n00:00:00,000 --> 00:00:02,000\n正面看起来很窄\n\n"
            "2\n00:00:03,000 --> 00:00:05,500\n一百六十斤也能穿得显瘦\n"
        )

        raw, metadata = server._commerce_director_alternative_raw_clips(
            case, video=Path("C:/source.mp4"), source_srt_text=srt,
        )

        self.assertEqual(len(raw), 1)
        self.assertEqual(server._clip_public(0, raw[0])["text"], "一百六十斤也能穿得显瘦")
        self.assertEqual(metadata[0]["director_chapter_title"], "大身材显瘦结果")
        self.assertEqual(metadata[0]["director_beat_function"], "result")
        public = server._preview_public_clips(raw, srt)
        server._annotate_commerce_director_public_clips(public, metadata, recommended=False)
        self.assertFalse(public[0]["selected"])
        self.assertEqual(public[0]["candidate_origin"], "director_alternative")

    def test_complete_executable_inventory_is_exposed_as_manual_candidates(self) -> None:
        case = {
            "commerce_lite": {
                "tags": [
                    {
                        "candidate_id": 1,
                        "text": "正面看起来很窄",
                        "materializable": True,
                        "asset_role": "result",
                        "purchase_value_hints": ["slimming"],
                    },
                    {
                        "candidate_id": 2,
                        "text": "里面做了安全里衬",
                        "materializable": True,
                        "asset_role": "risk_remove",
                        "purchase_value_hints": ["lining"],
                    },
                ],
            },
        }
        srt = (
            "1\n00:00:00,000 --> 00:00:02,000\n正面看起来很窄\n\n"
            "2\n00:00:03,000 --> 00:00:05,500\n里面做了安全里衬\n"
        )

        raw, metadata = server._commerce_director_inventory_raw_clips(
            case, video=Path("C:/source.mp4"), source_srt_text=srt,
        )

        self.assertEqual(len(raw), 2)
        self.assertEqual(server._clip_public(0, raw[1])["text"], "里面做了安全里衬")
        self.assertTrue(metadata[1]["manual_candidate_only"])
        public = server._preview_public_clips(raw, srt)
        server._annotate_commerce_director_public_clips(
            public, metadata, recommended=False, origin="source_inventory",
        )
        self.assertFalse(public[1]["selected"])
        self.assertEqual(public[1]["candidate_origin"], "source_inventory")

    def test_batch_result_exposes_readable_review_timeline_without_export_access(self) -> None:
        entry = {
            "proposal": {
                "director_strategy_id": "D1",
                "name": "成交转化版",
                "opening_promise": "大骨架也能显瘦",
            },
            "preview_id": "child-preview",
            "task_id": "child-task",
        }
        child = {
            "status": "ready",
            "clips": [{
                "clip_type": "C1",
                "text": "肩宽女生穿它，肩会看起来更窄。",
                "duration": 3.2,
            }],
            "director_review": {
                "kind": "m3_materialized_review",
                "m1_story": {"thesis": "肩宽显瘦"},
            },
        }
        with mock.patch.object(server, "_get_preview", return_value=child):
            result = server._director_batch_result_entry(entry)

        self.assertEqual(result["state"], "m3_materialized")
        self.assertTrue(result["review_video_available"])
        self.assertEqual(result["timeline"], [{
            "position": 1, "chapter_id": "C1",
            "text": "肩宽女生穿它，肩会看起来更窄。", "duration": 3.2,
        }])
        self.assertFalse(result["formal_export_allowed"])

    def test_batch_result_keeps_read_only_candidate_lineage_for_manual_review(self) -> None:
        entry = {"proposal": {"director_strategy_id": "D1"}, "preview_id": "child-preview"}
        timeline = [{
            "position": 1, "chapter_id": "C1", "candidate_id": 7,
            "text": "真实冻结候选", "duration": 2.4,
            "word_materialization_status": "exact_bound",
            "source_lineage": {"candidate_id": 7, "start": 1.0, "end": 3.4},
        }]
        child = {
            "status": "ready", "clips": [],
            "director_review": {
                "kind": "m2_draft_review_only",
                "m2_candidate_timeline": timeline,
                "m2_draft": {"issues": ["total_12s_below_60s"]},
                "m3_status": "selector_blocked",
            },
        }
        with mock.patch.object(server, "_get_preview", return_value=child):
            result = server._director_batch_result_entry(entry)

        self.assertEqual(result["timeline"], timeline)
        self.assertEqual(result["issues"], ["total_12s_below_60s"])
        self.assertEqual(result["m3_status"], "selector_blocked")
        self.assertFalse(result["formal_export_allowed"])

    def test_m2_outline_is_read_only_summary_of_beats(self) -> None:
        outline = server._commerce_director_m2_outline({"beats": [{
            "chapter_id": "C2", "narrative_role": "proof", "goal": "兑现机制",
            "purchase_value_outcomes": ["肩看起来变窄"], "target_seconds": 4.2,
        }]})
        self.assertEqual(outline, [{
            "position": 1, "chapter_id": "C2", "narrative_role": "proof",
            "goal": "兑现机制", "purchase_value": "肩看起来变窄", "seconds": 4.2,
        }])

    def test_worker_creates_the_artifact_directory_before_m1_can_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"test")
            srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n完整字幕\n", encoding="utf-8")
            payload = server.SmartCutPayload(
                video_paths=[str(video)], srt_path=str(srt), target_duration=45,
                ai_controls={"goal": "场景种草", "main_product": "焦糖朗姆", "selling_points": ["穿着体验"],
                             "priority_terms": ["三伏天"], "avoid": ["价格", "闲聊"]},
            )
            observed: dict[str, Path] = {}

            def fake_run_case(_case_id, **kwargs):
                observed["output_dir"] = kwargs["output_dir"]
                observed["director_controls"] = kwargs["director_controls"]
                self.assertEqual(kwargs["source_definition"]["product"], "焦糖朗姆")
                self.assertTrue(kwargs["output_dir"].is_dir())
                return {
                    "passed": True,
                    "selected_m1_hero": {"strategy_id": "S1"},
                    "m3_selection_result": {"ranges": [{
                        "chapter_id": "C1", "start": 0.0, "end": 2.0, "text": "完整字幕",
                    }]},
                }

            settings = {
                "ai_director_mode": "experimental",
                "m2_planner_mode": "lite_director_experiment",
                "preference_weights": {"场景搭配": 3.0},
            }
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_load_settings", return_value=settings),
                mock.patch("run_m3_new_golden_plan_fidelity._run_case", side_effect=fake_run_case),
                mock.patch("ai_cost_ledger.generate_ai_cost_reports", return_value=({"records": {}}, {})),
                mock.patch.object(server, "_preview_word_timings_with_recovery", return_value=([], {})),
                mock.patch.object(server, "_set_task"),
                mock.patch.object(server, "_set_task_progress"),
                mock.patch.object(server, "emit_log"),
            ):
                server._run_commerce_director_preview("task-1", "preview-1", payload)

            self.assertEqual(observed["output_dir"], root / "workspace" / "ui_commerce_director_experiment" / "task-1")
            self.assertTrue((observed["output_dir"] / "run_manifest.json").exists())
            controls = observed["director_controls"]
            self.assertEqual(controls["director_direction"], "场景种草")
            self.assertEqual(controls["preferred_topics"], ["穿着体验"])
            self.assertEqual(controls["preferred_terms"], ["三伏天"])
            self.assertEqual(controls["preference_weights"], {"场景搭配": 3.0})
            self.assertEqual(controls["source_product_hints"], ["source"])
            source_info = json.loads((observed["output_dir"] / "source_info.json").read_text(encoding="utf-8"))
            self.assertEqual(source_info["director_controls"], controls)
            self.assertEqual(source_info["task_content_policy"]["price"], "block")

    def test_initial_director_run_stops_after_m1_story_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"test")
            srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n完整字幕\n", encoding="utf-8")
            payload = server.SmartCutPayload(video_paths=[str(video)], srt_path=str(srt), target_duration=45)
            observed: dict[str, object] = {}

            def fake_run_case(_case_id, **kwargs):
                observed.update(kwargs)
                return {
                    "director_discovery_only": True,
                    "passed": False,
                    "selected_m1_hero": {"strategy_id": "S1", "thesis": "显瘦"},
                    "m1_result": {"strategies": [{"strategy_id": "S1"}]},
                    "m1_story_library": {"stories": [{"story_id": "S1"}]},
                    "director_strategy_library": {"proposals": [{"director_strategy_id": "D_TRAFFIC"}]},
                }

            settings = {"ai_director_mode": "experimental", "m2_planner_mode": "lite_director_experiment"}
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_load_settings", return_value=settings),
                mock.patch("run_m3_new_golden_plan_fidelity._run_case", side_effect=fake_run_case),
                mock.patch("ai_cost_ledger.generate_ai_cost_reports", return_value=({"records": {}}, {})),
                mock.patch.object(server, "_set_task"),
                mock.patch.object(server, "_set_task_progress"),
                mock.patch.object(server, "emit_log"),
            ):
                server._run_commerce_director_preview(
                    "discover-task", "discover-preview", payload, director_strategy_discovery_only=True,
                )

            self.assertTrue(observed["director_strategy_discovery_only"])
            preview = server._get_preview("discover-preview")
            self.assertEqual(preview["director_review"]["kind"], "m1_story_map_discovery")
            self.assertEqual(preview["clips"], [])

    def test_worker_forwards_chapter_compression_only_to_experimental_runner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"test")
            srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n完整字幕\n", encoding="utf-8")
            payload = server.SmartCutPayload(video_paths=[str(video)], srt_path=str(srt), target_duration=45)
            observed: dict[str, object] = {}

            def fake_run_case(_case_id, **kwargs):
                observed.update(kwargs)
                return {
                    "passed": True,
                    "selected_m1_hero": {"strategy_id": "S1"},
                    "m3_selection_result": {"ranges": [{
                        "chapter_id": "C1", "start": 0.0, "end": 2.0, "text": "完整字幕",
                    }]},
                }

            settings = {
                "ai_director_mode": "experimental",
                "m2_planner_mode": "lite_chapter_compression_experiment",
            }
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_load_settings", return_value=settings),
                mock.patch("run_m3_new_golden_plan_fidelity._run_case", side_effect=fake_run_case),
                mock.patch("ai_cost_ledger.generate_ai_cost_reports", return_value=({"records": {}}, {})),
                mock.patch.object(server, "_preview_word_timings_with_recovery", return_value=([], {})),
                mock.patch.object(server, "_set_task"),
                mock.patch.object(server, "_set_task_progress"),
                mock.patch.object(server, "emit_log"),
            ):
                server._run_commerce_director_preview("task-compress", "preview-compress", payload)

            self.assertTrue(observed["commerce_lite_chapter_compression"])
            self.assertFalse(observed["commerce_lite"])
            self.assertFalse(observed["commerce_director"])

    def test_worker_failure_keeps_source_identity_and_failure_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            srt = root / "source.srt"
            video.write_bytes(b"test")
            srt.write_text("1\n00:00:00,000 --> 00:00:02,000\n完整字幕\n", encoding="utf-8")
            payload = server.SmartCutPayload(video_paths=[str(video)], srt_path=str(srt), target_duration=45)
            settings = {
                "ai_director_mode": "experimental",
                "m2_planner_mode": "lite_director_experiment",
            }
            with (
                mock.patch.object(server, "REPO_ROOT", root),
                mock.patch.object(server, "_load_settings", return_value=settings),
                mock.patch(
                    "run_m3_new_golden_plan_fidelity._run_case",
                    side_effect=ValueError("no materializable hard-safe candidates"),
                ),
                mock.patch.object(server, "_set_task"),
                mock.patch.object(server, "_set_task_progress"),
                mock.patch.object(server, "emit_log"),
            ):
                server._run_commerce_director_preview("failed-task", "failed-preview", payload)

            artifact_dir = root / "workspace" / "ui_commerce_director_experiment" / "failed-task"
            self.assertTrue((artifact_dir / "source_info.json").exists())
            self.assertTrue((artifact_dir / "failure.json").exists())
            preview = server._get_preview("failed-preview")
            self.assertEqual(preview["video"], str(video))
            self.assertEqual(preview["srt_path"], str(srt))
            self.assertTrue(preview["dedup_summary"]["failure_artifact"].endswith("failure.json"))

    def test_local_asr_temp_sidecar_is_verified_only_for_its_exact_video_hash(self) -> None:
        from asr_cache import inspect_cache

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "source.mp4"
            video.write_bytes(b"source")
            temp_root = root / "live_cutter_stt"
            temp_root.mkdir()
            token = hashlib.md5(str(video).encode("utf-8")).hexdigest()[:8]
            srt = temp_root / f"sub_{token}.srt"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n完整表达\n", encoding="utf-8")
            srt.with_suffix(".words.json").write_text(json.dumps({
                "schema": "liveclipper.word-timings.v1",
                "provider": "sensevoice",
                "word_count": 4,
                "segments": [{
                    "start": 0.0,
                    "end": 1.0,
                    "text": "完整表达",
                    "words": [{"text": char, "start": index * 0.2, "end": index * 0.2 + 0.1}
                              for index, char in enumerate("完整表达")],
                }],
            }, ensure_ascii=False), encoding="utf-8")

            with (
                mock.patch.object(server.tempfile, "gettempdir", return_value=str(root)),
                mock.patch.object(server, "emit_log"),
            ):
                self.assertTrue(server._verify_local_asr_temp_identity_for_experiment(video, srt, "smart-cut"))

            identity = inspect_cache(video, srt)
            self.assertTrue(identity["valid"])
            self.assertTrue(identity["managed"])
            self.assertEqual(identity["timing_precision"], "word")

    def test_ensure_srt_migrates_verified_legacy_cache_beside_source_video(self) -> None:
        from asr_cache import write_metadata

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "中文素材.mp4"
            video.write_bytes(b"source-video")
            legacy_root = root / "live_cutter_stt"
            legacy_root.mkdir()
            token = hashlib.md5(str(video).encode("utf-8")).hexdigest()[:8]
            legacy_srt = legacy_root / f"sub_{token}.srt"
            legacy_srt.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n完整表达\n", encoding="utf-8",
            )
            legacy_srt.with_suffix(".words.json").write_text(json.dumps({
                "schema": "liveclipper.word-timings.v1",
                "provider": "sensevoice",
                "word_count": 4,
                "segments": [],
            }, ensure_ascii=False), encoding="utf-8")
            write_metadata(
                video, legacy_srt, provider="sensevoice",
                model="iic/SenseVoiceSmall", timing_precision="word",
            )

            with (
                mock.patch.object(server.tempfile, "gettempdir", return_value=str(root)),
                mock.patch.object(server, "emit_log"),
            ):
                resolved = server._ensure_srt(video, "smart-cut")

            source_srt = video.with_suffix(".srt")
            self.assertEqual(resolved, source_srt)
            self.assertTrue(source_srt.is_file())
            self.assertTrue(source_srt.with_suffix(".words.json").is_file())
            self.assertTrue(source_srt.with_suffix(".asr-cache.json").is_file())


if __name__ == "__main__":
    unittest.main()
