from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime_v4 import desktop_host, update_service


ROOT = Path(__file__).resolve().parents[1]


def _load_v4_server():
    path = ROOT / "web_client" / "server.py"
    environment = {
        "LIVECLIPPER_RUNTIME_LAYOUT": "4",
        "LIVECLIPPER_BUNDLE_DIR": str(ROOT),
        "LIVECLIPPER_V4_BUNDLE_VERIFIED": "1",
        "LIVECLIPPER_V4_BUNDLE_MANIFEST_SHA256": "a" * 64,
        "LIVECLIPPER_ACTIVE_VERSION": "2026.7.30.1",
        "LIVECLIPPER_V4_CORE_VERSION": "4.0.0",
    }
    with patch.dict(os.environ, environment, clear=False):
        spec = importlib.util.spec_from_file_location("liveclipper_v4_server_bridge_test", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _HostUpdateService:
    available = True

    def __init__(self) -> None:
        self.checked = 0
        self.applied = 0
        self.restarted = 0

    def check_update(self):
        self.checked += 1
        return {
            "ok": True,
            "update_available": True,
            "update": {
                "version": "2026.7.30.2",
                "release_notes": "V4 bridge",
                "requires_full_package": False,
                "supports_web_incremental_updates": True,
                "update_strategy": "v4-signed-business-bundle",
                "patch_size": 123,
            },
        }

    def apply_update(self, progress_callback):
        self.applied += 1
        progress_callback(123, 123, "更新包下载并校验完成")
        return {
            "ok": True,
            "updated": ["2026.7.30.2"],
            "restart_required": True,
            "msg": "installed",
        }

    def schedule_restart(self):
        self.restarted += 1
        return True


class _DeferredThread:
    created = []

    def __init__(self, *, target, daemon, name):
        self.target = target
        self.daemon = daemon
        self.name = name
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True


class RuntimeV4ServerUpdateBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = _load_v4_server()

    def setUp(self) -> None:
        self.service = _HostUpdateService()
        self.server._HOST_UPDATE_SERVICE = None
        self.server._UPDATE_STATE.update(
            {
                "running": False,
                "stage": "idle",
                "percent": 0,
                "downloaded": 0,
                "total": 0,
                "message": "",
                "error": "",
                "last_result": None,
            }
        )

    def test_bundle_entry_binds_host_service_before_returning_app(self) -> None:
        source = (ROOT / "bundle_entry.py").read_text(encoding="utf-8")
        self.assertIn("configure_host_services(context)", source)
        self.assertLess(
            source.index("configure_host_services(context)"),
            source.index('"asgi_app": app'),
        )

    def test_v4_update_routes_use_only_the_injected_host_service(self) -> None:
        self.assertTrue(self.server.configure_host_services({"update_service": self.service}))
        with patch.object(
            self.server,
            "_runtime_updater_module",
            side_effect=AssertionError("V3 updater must not load in Runtime V4"),
        ):
            checked = self.server.check_update_api()
            applied = self.server.apply_update_api()
        self.assertTrue(checked["update_available"])
        self.assertTrue(applied["ok"])
        self.assertTrue(applied["auto_restart"])
        self.assertEqual(self.service.checked, 1)
        self.assertEqual(self.service.applied, 1)
        self.assertEqual(self.service.restarted, 1)
        status = self.server.update_status_api()
        self.assertFalse(status["running"])
        self.assertEqual(status["stage"], "complete")
        self.assertEqual(status["percent"], 100)

    def test_v4_runtime_reports_host_update_capability_and_fails_closed_without_it(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "update service is unavailable"):
            self.server.configure_host_services({})
        self.assertFalse(self.server._safe_web_incremental_supported())
        self.server.configure_host_services({"update_service": self.service})
        runtime = self.server.runtime()
        self.assertTrue(runtime["supports_web_incremental_updates"])
        self.assertEqual(runtime["update_strategy"], "v4-signed-business-bundle")

    def test_host_restart_returns_to_launcher_and_never_restarts_host_directly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / "LiveClipperWeb.exe"
            launcher.write_bytes(b"launcher")
            prototype_launcher = root / "LiveClipperLauncherV4.exe"
            prototype_launcher.write_bytes(b"prototype launcher")
            layout = desktop_host.HostLayout(
                install_root=root,
                business_root=root / "business",
                public_key=root / "public.pem",
                application_version="2026.7.30.1",
            )
            _DeferredThread.created.clear()
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(desktop_host.subprocess, "Popen") as spawn,
                patch.object(desktop_host.threading, "Thread", _DeferredThread),
            ):
                scheduled = desktop_host._schedule_launcher_restart(layout)
            self.assertTrue(scheduled)
            command = spawn.call_args.args[0]
            self.assertEqual(command[0], "powershell")
            self.assertIn("LiveClipperWeb.exe", command[-1])
            self.assertNotIn("LiveClipperLauncherV4.exe", command[-1])
            self.assertIn("-WindowStyle Hidden", command[-1])
            self.assertEqual(spawn.call_args.kwargs["cwd"], str(root))
            self.assertEqual(len(_DeferredThread.created), 1)
            self.assertTrue(_DeferredThread.created[0].started)

    def test_host_spec_owns_update_service_and_source_config(self) -> None:
        spec = (ROOT / "runtime_v4" / "liveclipper_host_v4.spec").read_text(
            encoding="utf-8"
        )
        launcher_spec = (
            ROOT / "runtime_v4" / "liveclipper_launcher_v4.spec"
        ).read_text(encoding="utf-8")
        self.assertIn('"runtime_v4.update_service"', spec)
        self.assertIn("runtime_v4_update_sources.json", spec)
        for module_name in (
            "tkinter.colorchooser",
            "tkinter.filedialog",
            "tkinter.font",
            "tkinter.messagebox",
            "tkinter.simpledialog",
            "tkinter.ttk",
        ):
            self.assertIn(f'"{module_name}"', spec)
        self.assertIn('or "LiveClipperWeb"', launcher_spec)
        self.assertNotIn("update_service", launcher_spec)
        self.assertIn("a.binaries", launcher_spec)
        self.assertIn("a.datas", launcher_spec)
        self.assertNotIn("COLLECT(", launcher_spec)

    def test_frozen_update_sources_use_verified_pages_endpoints(self) -> None:
        sources = update_service.load_update_source_config(
            ROOT / "release" / "runtime_v4_update_sources.json"
        )
        self.assertEqual(
            sources,
            (
                "https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/stable.json",
                "https://cdn.jsdelivr.net/gh/xingdawei-jpg/LiveClipper@main/release/channel/v4/stable.json",
                "https://liveclipper-updates.pages.dev/stable.json",
            ),
        )

    def test_update_card_uses_v4_messages_progress_and_install_state(self) -> None:
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        index = (ROOT / "web_client" / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn('const noUpdateMessage = result.msg || "当前已是最新版本"', frontend)
        self.assertIn("channel_not_configured", frontend)
        self.assertIn("refreshUpdateProgress", frontend)
        self.assertIn('[data-action="apply-update"]', frontend)
        self.assertIn('data-action="apply-update" disabled>安装更新</button>', index)

    def test_product_scan_results_expose_file_relative_ranges(self) -> None:
        groups = [
            {
                "name": "针织衫",
                "segments": [(12.5, 36.0), (90, 128.25), ("bad", None)],
                "total_duration": 61.75,
            }
        ]
        with self.server._SCAN_LOCK:
            previous = list(self.server._SCAN_RESULTS.get("schedule_groups") or [])
            self.server._SCAN_RESULTS["schedule_groups"] = groups
        try:
            result = self.server.scan_results()
        finally:
            with self.server._SCAN_LOCK:
                self.server._SCAN_RESULTS["schedule_groups"] = previous

        self.assertEqual(
            result["schedule_groups"],
            [
                {
                    "name": "针织衫",
                    "segments": 3,
                    "total_duration": 61.75,
                    "ranges": [
                        {"start": 12.5, "end": 36.0},
                        {"start": 90.0, "end": 128.25},
                    ],
                }
            ],
        )

    def test_product_scan_results_do_not_hide_later_ranges_from_selection(self) -> None:
        ranges = [
            {
                "schedule_start": float(index * 10),
                "schedule_end": float(index * 10 + 8),
                "expected_duration": 8.0,
                "covered_duration": 8.0,
                "missing_duration": 0.0,
                "status": "covered",
                "parts": [{"video": "live.mp4", "file_start": 0.0, "file_end": 8.0}],
            }
            for index in range(14)
        ]
        with self.server._SCAN_LOCK:
            previous = list(self.server._SCAN_RESULTS.get("schedule_groups") or [])
            self.server._SCAN_RESULTS["schedule_groups"] = [{"name": "针织衫", "ranges": ranges}]
        try:
            result = self.server.scan_results()
        finally:
            with self.server._SCAN_LOCK:
                self.server._SCAN_RESULTS["schedule_groups"] = previous

        self.assertEqual(len(result["schedule_groups"][0]["ranges"]), 14)

    def test_product_scan_results_expose_per_product_cut_feedback(self) -> None:
        feedback = [
            {
                "name": "针织衫",
                "status": "success",
                "label": "已切割",
                "message": "已生成 1 段，共 00:25:11",
                "output_count": 1,
                "duration": 1511.0,
                "output_paths": [r"C:\exports\针织衫.mp4"],
            }
        ]
        with self.server._SCAN_LOCK:
            previous = list(self.server._SCAN_RESULTS.get("schedule_feedback") or [])
            self.server._SCAN_RESULTS["schedule_feedback"] = feedback
        try:
            result = self.server.scan_results()
        finally:
            with self.server._SCAN_LOCK:
                self.server._SCAN_RESULTS["schedule_feedback"] = previous

        self.assertEqual(
            result["schedule_feedback"],
            [
                {
                    "name": "针织衫",
                    "status": "success",
                    "label": "已切割",
                    "message": "已生成 1 段，共 00:25:11",
                    "output_count": 1,
                    "duration": 1511.0,
                    "output_paths": [r"C:\exports\针织衫.mp4"],
                }
            ],
        )

    def test_product_scan_cut_feedback_sums_real_export_duration(self) -> None:
        feedback = self.server._product_scan_cut_feedback(
            [
                {
                    "name": "针织衫",
                    "status": "covered",
                    "covered_duration": 1511.0,
                }
            ],
            [
                {
                    "name": "针织衫",
                    "output_path": r"C:\exports\针织衫.mp4",
                    "duration_seconds": 1511.0,
                }
            ],
        )
        self.assertEqual(feedback[0]["status"], "success")
        self.assertEqual(feedback[0]["output_count"], 1)
        self.assertEqual(feedback[0]["duration"], 1511.0)
        self.assertIn("25:11", feedback[0]["message"])

    def test_product_scan_fast_cut_feedback_marks_duration_as_an_estimate(self) -> None:
        feedback = self.server._product_scan_cut_feedback(
            [{"name": "针织衫", "status": "covered", "covered_duration": 60.0}],
            [
                {
                    "name": "针织衫",
                    "output_path": r"C:\\exports\\针织衫.mp4",
                    "duration_seconds": 60.0,
                    "cut_mode": "fast-copy",
                }
            ],
        )
        self.assertIn("极速预计", feedback[0]["message"])
        self.assertIn("允许关键帧偏差", feedback[0]["message"])

    def test_product_scan_keeps_only_user_checked_ranges_and_marks_cancelled_products(self) -> None:
        groups = [
            {
                "name": "针织衫",
                "ranges": [
                    {
                        "schedule_start": 0.0,
                        "schedule_end": 30.0,
                        "expected_duration": 30.0,
                        "covered_duration": 30.0,
                        "missing_duration": 0.0,
                        "status": "covered",
                        "parts": [{"video": "one.mp4"}],
                    },
                    {
                        "schedule_start": 60.0,
                        "schedule_end": 100.0,
                        "expected_duration": 40.0,
                        "covered_duration": 20.0,
                        "missing_duration": 20.0,
                        "status": "partial",
                        "parts": [{"video": "two.mp4"}],
                    },
                ],
            },
            {
                "name": "已取消商品",
                "ranges": [
                    {
                        "schedule_start": 0.0,
                        "schedule_end": 20.0,
                        "expected_duration": 20.0,
                        "covered_duration": 20.0,
                        "missing_duration": 0.0,
                        "status": "covered",
                        "parts": [{"video": "three.mp4"}],
                    }
                ],
            },
        ]
        selected = ["0:1"]
        filtered = self.server._filter_product_scan_coverage_groups(groups, selected)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["segments"], 1)
        self.assertEqual(filtered[0]["ranges"][0]["schedule_start"], 60.0)
        self.assertEqual(filtered[0]["status"], "partial")
        feedback = self.server._product_scan_cut_feedback(groups, [], selected)
        self.assertEqual(feedback[1]["label"], "已取消")
        self.assertIn("手动取消", feedback[1]["message"])

    def test_product_scan_rejects_clock_time_schedule(self) -> None:
        with self.assertRaises(ValueError):
            self.server.ProductScanPayload(schedule_time_basis="clock")
        warnings: list[str] = []
        errors: list[str] = []
        self.server._preflight_product_schedule(
            {
                "excel_path": "table.xlsx",
                "video_paths": ["live_202608051219.mp4"],
                "schedule_time_basis": "clock",
                "live_start_time": "",
            },
            warnings,
            errors,
        )
        self.assertEqual(warnings, [])
        self.assertEqual(errors, ["单品扫描仅支持“开播后时段”，请使用例如 01:04–26:15 的讲解时段。"])

    def test_relative_schedule_locates_explain_columns_at_e_or_f(self) -> None:
        import openpyxl

        app_dir = str(ROOT / "app")
        sys.path.insert(0, app_dir)
        try:
            from schedule_splitter import read_excel

            for column, header in (
                (5, ["商品标题", "商品ID", "价格", "封面", "讲解时段1"]),
                (6, ["商品标题", "商品ID", "价格", "封面", "备注", "讲解时段1"]),
            ):
                workbook = openpyxl.Workbook()
                sheet = workbook.active
                sheet.append(header)
                row = ["测试商品", "SKU-01", "88", ""] + [""] * (column - 4)
                row[column - 1] = "35:59-1:14:16"
                sheet.append(row)
                with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as handle:
                    path = handle.name
                try:
                    workbook.save(path)
                    schedule, _ = read_excel(path, time_basis="relative")
                finally:
                    workbook.close()
                    os.unlink(path)
                self.assertEqual(len(schedule), 1)
                self.assertEqual(schedule[0]["start_offset"], 2159.0)
                self.assertEqual(schedule[0]["end_offset"], 4456.0)
        finally:
            sys.path.remove(app_dir)

    def test_product_scan_frontend_requires_validation_before_split(self) -> None:
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        index = (ROOT / "web_client" / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("function productScanIsReady()", frontend)
        self.assertIn('data-action="product-scan-read"', index)
        self.assertIn('data-action="product-scan-start"', index)
        self.assertIn('id="ps-start-scan" disabled', index)
        self.assertIn(
            'if (!productScanIsReady()) throw new Error("请先读取并校验时间表，再开始分割。")',
            frontend,
        )

    def test_product_scan_defaults_to_fast_keyframe_cutting(self) -> None:
        source = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        index = (ROOT / "web_client" / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("fast_cut: bool = True", source)
        self.assertIn("fast_copy=bool(payload.fast_cut)", source)
        self.assertIn('fast_cut: $("ps-fast-cut")?.checked !== false', frontend)
        self.assertIn('id="ps-fast-cut" type="checkbox" checked', index)

    def test_product_scan_allows_product_and_range_level_export_selection(self) -> None:
        source = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("selected_ranges: list[str]", source)
        self.assertIn("_filter_product_scan_coverage_groups", source)
        self.assertIn('data-product-scan-select-group=', frontend)
        self.assertIn('data-product-scan-select-range=', frontend)
        self.assertIn('selected_ranges: feature === "product-scan"', frontend)
        self.assertNotIn("state.productScan.groups.slice(0, 60)", frontend)

    def test_product_scan_uses_relative_schedule_and_independent_video_starts(self) -> None:
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        index = (ROOT / "web_client" / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('id="ps-video-start-offset"', index)
        self.assertEqual(index.count('id="ps-live-start-time"'), 1)
        self.assertIn("表格“讲解时段”按开播后进度读取", index)
        self.assertNotIn("ps-time-basis-clock", index)
        self.assertNotIn("ps-align-auto", index)
        self.assertNotIn("productScanNeedsLiveStart", frontend)
        self.assertIn('video_offsets: productScanVideoOverrides()', frontend)
        self.assertIn('data-ps-video-offset=', frontend)

    def test_product_scan_video_anchors_preserve_gaps_and_manual_overrides(self) -> None:
        videos = [r"C:\live\clip202608051349.mp4", r"C:\live\clip202608051249.mp4"]
        data = {"video_paths": videos, "live_start_time": "202608051219"}
        self.assertEqual(self.server._resolve_product_scan_video_offsets(data), {videos[0]: 5400, videos[1]: 1800})
        data["video_offsets"] = {videos[0]: "01:31:05"}
        self.assertEqual(self.server._resolve_product_scan_video_offsets(data)[videos[0]], 5465)
        # Reordering or removing files does not rebase the remaining sources.
        data["video_paths"] = [videos[0]]
        self.assertEqual(self.server._resolve_product_scan_video_offsets(data), {videos[0]: 5465})

    def test_product_scan_requires_individual_missing_or_duplicate_timestamps(self) -> None:
        cases = [[r"C:\live\plain.mp4"], [r"C:\live\a202608051219.mp4", r"C:\live\b202608051219.mp4"]]
        for videos in cases:
            data = {"video_paths": videos, "live_start_time": "202608051219"}
            with self.assertRaises(ValueError):
                self.server._resolve_product_scan_video_offsets(data)
            data["video_offsets"] = {video: str(index * 1800) for index, video in enumerate(videos)}
            self.assertEqual(len(self.server._resolve_product_scan_video_offsets(data)), len(videos))
        with self.assertRaises(ValueError):
            self.server._resolve_product_scan_video_offsets({"video_paths": cases[0], "video_start_offset": "0"})

    def test_product_scan_anchors_support_midnight_and_manual_only_sources(self) -> None:
        video = r"C:\live\clip20260806001015.mp4"
        self.assertEqual(self.server._resolve_product_scan_video_offsets({
            "video_paths": [video], "live_start_time": "20260805235015"
        })[video], 1200)
        self.assertEqual(self.server._resolve_product_scan_video_offsets({
            "video_paths": [video], "video_offsets": {video: "01:02:03"}
        })[video], 3723)

    def test_product_scan_result_list_has_its_own_bounded_scroll_area(self) -> None:
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        styles = (ROOT / "web_client" / "frontend" / "assets" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("match[3] ?", frontend)
        self.assertIn("`${match[1]}:${match[2]}`", frontend)
        self.assertIn('const route = `排表 ${target} → ${actual}`;', frontend)
        self.assertIn('align-items: start;', styles)
        self.assertIn('height: min(720px, calc(100vh - 180px));', styles)
        self.assertIn('overflow: hidden;', styles)
        self.assertIn('overscroll-behavior: contain;', styles)
        self.assertNotIn('contain: size;', styles)
        self.assertIn('.product-scan-range-route', styles)

    def test_product_scan_materials_are_grouped_into_a_compact_import_flow(self) -> None:
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        index = (ROOT / "web_client" / "frontend" / "index.html").read_text(
            encoding="utf-8"
        )
        styles = (ROOT / "web_client" / "frontend" / "assets" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn('class="product-scan-material-grid"', index)
        self.assertIn('data-video-count-for="ps-video-paths"', index)
        self.assertIn("product-scan-source-details", frontend)
        self.assertIn('"ps-video-paths"', frontend)
        self.assertIn('.product-scan-material-grid', styles)
        self.assertIn('.product-scan-source-details', styles)


if __name__ == "__main__":
    unittest.main()
