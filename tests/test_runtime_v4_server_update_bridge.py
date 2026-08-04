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
                "https://updates.liveclipper.top/stable.json",
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


if __name__ == "__main__":
    unittest.main()
