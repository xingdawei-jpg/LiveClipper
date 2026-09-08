from __future__ import annotations

import importlib
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))
server = importlib.import_module("server")


class DirectorConcurrencyTests(unittest.TestCase):
    def test_parallel_analysis_keeps_fee_and_log_identity(self):
        ledger = importlib.import_module("ai_cost_ledger")
        gate = threading.Barrier(2)
        observed = {}
        def analyze(name):
            with ledger.ai_cost_ledger_scope(task_id=name, session_id=name + "-preview"):
                gate.wait(timeout=3)
                observed[name] = (dict(ledger._SCOPE.get()), server._TASK_LOG_CONTEXT.task_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(server._run_task_worker, name, analyze, name) for name in ("smart", "mix")]
            for future in futures:
                future.result(timeout=5)
        for name, (cost, log_id) in observed.items():
            self.assertEqual(cost["task_id"], name)
            self.assertEqual(cost["session_id"], name + "-preview")
            self.assertEqual(log_id, name)

    def test_admission_is_atomic_per_workspace(self):
        gate = threading.Barrier(3)
        def start():
            gate.wait(timeout=3)
            try:
                return server._new_task("smart-cut", "test")
            except server.HTTPException as exc:
                return exc.status_code
        with mock.patch.object(server, "_TASKS", {}), mock.patch.object(server, "_TASK_CANCEL_EVENTS", {}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.submit(start), pool.submit(start)
                gate.wait(timeout=3)
                results = [first.result(timeout=3), second.result(timeout=3)]
            self.assertEqual(results.count(409), 1)
            self.assertTrue(server._new_task("mix", "test").startswith("mix-"))
            with self.assertRaises(server.HTTPException):
                server._new_task("mix-batch", "test")

    def test_waiting_render_can_stop_without_stopping_active_render(self):
        active, release, waiting = threading.Event(), threading.Event(), threading.Event()
        events = {key: threading.Event() for key in ("smart", "mix")}
        called = []
        def render():
            active.set()
            self.assertTrue(release.wait(timeout=5))
            called.append("smart")
            return {"ok": True}
        def status(task_id, **updates):
            if task_id == "mix" and "等待" in updates.get("message", ""):
                waiting.set()
        with mock.patch.object(server, "_DIRECTOR_RENDER_LOCK", threading.RLock()), mock.patch.object(server, "_task_cancel_event", side_effect=events.__getitem__), mock.patch.object(server, "_set_task", side_effect=status):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(server._director_render_queued, "smart", render)
                try:
                    self.assertTrue(active.wait(timeout=3))
                    second = pool.submit(server._director_render_queued, "mix", lambda: called.append("mix"))
                    self.assertTrue(waiting.wait(timeout=3))
                    events["mix"].set()
                    self.assertEqual(second.result(timeout=3), {"ok": False, "error": "cancelled"})
                    self.assertFalse(events["smart"].is_set())
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=3), {"ok": True})
            self.assertEqual(called, ["smart"])

    def test_render_waits_then_continues_with_own_context(self):
        active, release, waiting = threading.Event(), threading.Event(), threading.Event()
        order = []
        def render(name):
            if name == "smart":
                active.set()
                self.assertTrue(release.wait(timeout=5))
            order.append((name, getattr(server._TASK_LOG_CONTEXT, "task_id", None)))
        def status(task_id, **updates):
            if "等待" in updates.get("message", ""):
                waiting.set()
        with mock.patch.object(server, "_DIRECTOR_RENDER_LOCK", threading.RLock()), mock.patch.object(server, "_task_cancel_event", return_value=threading.Event()), mock.patch.object(server, "_set_task", side_effect=status):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(server._run_task_worker, "smart", server._director_render_queued, "smart", render, "smart")
                try:
                    self.assertTrue(active.wait(timeout=3))
                    second = pool.submit(server._run_task_worker, "mix", server._director_render_queued, "mix", render, "mix")
                    self.assertTrue(waiting.wait(timeout=3))
                    self.assertEqual(order, [])
                finally:
                    release.set()
                first.result(timeout=3)
                second.result(timeout=3)
            self.assertEqual(order, [("smart", "smart"), ("mix", "mix")])

    def test_render_failure_releases_slot(self):
        with mock.patch.object(server, "_DIRECTOR_RENDER_LOCK", threading.RLock()), mock.patch.object(server, "_task_cancel_event", return_value=threading.Event()), mock.patch.object(server, "_set_task"):
            with self.assertRaises(ValueError):
                server._director_render_queued("smart", mock.Mock(side_effect=ValueError("failed")))
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertEqual(pool.submit(server._director_render_queued, "mix", lambda: "ok").result(timeout=3), "ok")

    def test_frozen_clips_are_local_to_render_invocation(self):
        cutter = importlib.import_module("cutter_logic")
        ai = importlib.import_module("ai_clipper")
        original_analyze, original_enabled = ai.ai_analyze_clips, ai.is_enabled
        clips = [("product", "test", 0, 2)]
        def render(*args, **kwargs):
            self.assertIs(ai.ai_analyze_clips, original_analyze)
            self.assertIs(ai.is_enabled, original_enabled)
            self.assertEqual(kwargs["_selected_clips"], clips)
            return {"ok": True}
        with mock.patch.object(cutter, "process_video", side_effect=render):
            result = cutter._process_version_with_clips("source.mp4", "source.srt", "out.mp4", clips)
        self.assertEqual(result, {"ok": True})

    def test_filter_request_does_not_change_active_render_preset(self):
        cutter = importlib.import_module("cutter_logic")
        with mock.patch.object(cutter, "DEDUP_PRESET", "heavy"), mock.patch.object(server, "_probe_video_info", return_value={"width": 720, "height": 1280}), mock.patch.object(cutter, "build_dedup_filters", return_value={}) as build:
            server._core_dedup_filters(Path("source.mp4"), 0, "light", False)
            self.assertEqual(cutter.DEDUP_PRESET, "heavy")
            self.assertEqual(build.call_args.kwargs["preset"], "light")

    def test_cancelled_subtitle_preparation_does_not_write_cache(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(server, "_ensure_srt_unlocked") as prepare:
            self.assertIsNone(server._ensure_srt(Path("same.mp4"), "mix", cancel_event=event))
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
