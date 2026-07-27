from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "web_client"))

desktop = importlib.import_module("desktop")
server = importlib.import_module("server")


class DesktopMediaImportTests(unittest.TestCase):
    def test_folder_resolver_is_zero_copy_recursive_and_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "中文素材"
            nested = root / "nested"
            nested.mkdir(parents=True)
            first = root / "01.mp4"
            second = nested / "02.MOV"
            ignored = root / "notes.txt"
            no_suffix = root / "readme"
            for path in (first, second, ignored, no_suffix):
                path.write_bytes(b"fixture")

            values = [str(root), str(first), str(root / "missing.mp4")]
            first_result = server._resolve_video_import_paths(values)
            second_result = server._resolve_video_import_paths(values)

            self.assertEqual(first_result["storage"], "original_local_paths")
            self.assertEqual(first_result["paths"], second_result["paths"])
            self.assertEqual(first_result["paths"], [str(first), str(second)])
            self.assertTrue(all(Path(path).is_absolute() for path in first_result["paths"]))
            self.assertTrue(all("web_uploads" not in path.lower() for path in first_result["paths"]))
            self.assertEqual(first_result["summary"]["added"], 2)
            self.assertEqual(first_result["summary"]["duplicates"], 1)
            self.assertEqual(first_result["summary"]["missing"], 1)
            self.assertEqual(first_result["summary"]["unsupported"], 1)
            self.assertEqual(first_result["summary"]["no_extension"], 1)

    def test_resolver_skips_reparse_points_without_following_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clip.mp4"
            path.write_bytes(b"fixture")
            with mock.patch.object(server, "_video_import_is_reparse_point", return_value=True):
                result = server._resolve_video_import_paths([str(path)])
            self.assertEqual(result["paths"], [])
            self.assertEqual(result["summary"]["reparse_points"], 1)

    def test_video_input_endpoint_uses_the_same_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "clip.mp4"
            path.write_bytes(b"fixture")
            result = asyncio.run(server.resolve_video_inputs(server.PathsPayload(paths=[str(path)])))
            self.assertEqual(result["paths"], [str(path)])
            self.assertEqual(result["storage"], "original_local_paths")

    def test_native_ole_file_drop_bridge_dispatches_absolute_paths_and_coordinates(self) -> None:
        class LoadedSignal:
            @staticmethod
            def wait(_timeout: int) -> bool:
                return True

        class FakePoint:
            def __init__(self, x: int, y: int) -> None:
                self.X = x
                self.Y = y

        class FakeControl:
            def __init__(self) -> None:
                import clr

                clr.AddReference("System")
                clr.AddReference("System.Drawing")
                from System import IntPtr

                self.Handle = IntPtr(100)
                self.InvokeRequired = False
                self.DeviceDpi = 144
                self.point_calls = []

            def PointToClient(self, point):
                self.point_calls.append((point.X, point.Y))
                return FakePoint(80, 160)

        class FakeWindow:
            def __init__(self) -> None:
                self.events = type("Events", (), {"loaded": LoadedSignal()})()
                self.control = FakeControl()
                self.native = type("BrowserForm", (), {"webview": self.control})()
                self.scripts = []

            def evaluate_js(self, script: str) -> None:
                self.scripts.append(script)

        class FakeBridge:
            def __init__(self) -> None:
                self.render_handle = None
                self.drop_callback = None
                self.diagnostic_callback = None

            @staticmethod
            def FindRenderWidgetHost(_control_handle):
                import clr

                clr.AddReference("System")
                from System import IntPtr

                return IntPtr(200)

            def Attach(self, render_handle, drop_callback, diagnostic_callback):
                self.render_handle = render_handle
                self.drop_callback = drop_callback
                self.diagnostic_callback = diagnostic_callback
                return object()

        window = FakeWindow()
        bridge = FakeBridge()
        logs = []
        with mock.patch.dict(desktop.os.environ, {desktop.ZERO_COPY_TEST_ENV: "1"}, clear=False), mock.patch.object(
            desktop, "_load_native_file_drop_bridge", return_value=bridge
        ):
            desktop._enable_desktop_native_file_drop_support(window, lambda *args: logs.append(args))
            self.assertEqual(int(bridge.render_handle.ToInt64()), 200)
            self.assertIsNotNone(bridge.drop_callback)
            self.assertIsNotNone(bridge.diagnostic_callback)
            bridge.diagnostic_callback("native OLE DragEnter: CF_HDROP=yes")
            bridge.drop_callback(
                [r"D:\素材\one.mp4", r"D:\素材\one.mp4", r"D:\素材\folder"],
                120,
                240,
            )
        deadline = time.monotonic() + 1.0
        while (
            (not window.scripts or not any("CustomEvent 已派发" in item[1] for item in logs))
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertEqual(len(window.scripts), 1)
        self.assertIn("liveclipper:native-video-drop", window.scripts[0])
        self.assertIn(r"D:\\素材\\one.mp4", window.scripts[0])
        self.assertIn(r"D:\\素材\\folder", window.scripts[0])
        self.assertIn('"x": 80', window.scripts[0])
        self.assertIn('"y": 160', window.scripts[0])
        self.assertIn('"dpi": 144', window.scripts[0])
        self.assertTrue(any(item[0] == "info" for item in logs))
        self.assertTrue(any("native OLE hook ready" in item[1] for item in logs))
        self.assertTrue(any("native OLE DragEnter" in item[1] for item in logs))
        self.assertTrue(any("native OLE callback" in item[1] for item in logs))

        desktop_source = (ROOT / "web_client" / "desktop.py").read_text(encoding="utf-8")
        bridge_source = (ROOT / "web_client" / "native_file_drop_bridge.cs").read_text(encoding="utf-8")
        self.assertIn("NativeFileDropBridge", desktop_source)
        self.assertIn("FindRenderWidgetHost", desktop_source)
        self.assertIn("native_file_drop_bridge.dll", desktop_source)
        self.assertIn("RegisterDragDrop", bridge_source)
        self.assertIn("CF_HDROP", bridge_source)
        self.assertNotIn("DOMEventHandler", desktop_source)
        self.assertNotIn("pywebviewFullPath", desktop_source)
        self.assertNotIn("js_api=", desktop_source)

    def test_frontend_keeps_upload_cache_out_of_desktop_drop_branch(self) -> None:
        source = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        start = source.index("async function addDroppedVideoFiles")
        end = source.index("function setButtonBusy", start)
        handler = source[start:end]
        self.assertIn("isDesktopWebViewHost()", handler)
        self.assertIn("等待 native CF_HDROP bridge", handler)
        self.assertNotIn("file.path", handler)
        self.assertNotIn("webkitRelativePath", handler)
        self.assertLess(handler.index("isDesktopWebViewHost()"), handler.index('upload("/api/uploads/videos"'))
        self.assertIn("const seen = new Set(next.map(normalizeVideoPath));", source)
        self.assertIn("bindDesktopNativeVideoDropBridge", source)
        self.assertIn("nativeVideoDropTargetFromCoordinates", source)
        self.assertIn("activePageDesktopVideoDropTarget", source)
        self.assertIn("active-page=${state.page}", source)
        self.assertIn('window.addEventListener("liveclipper:native-video-drop"', source)
        self.assertIn("rememberDesktopVideoDropTarget(targetId);", source)
        self.assertIn("last-active", source)
        self.assertIn("reportZeroCopyDropDiagnostic", source)
        self.assertIn("desktop-drop/diagnostic", source)
        self.assertIn("desktopVideoDropTargetIds", source)
        for target in (
            "video-paths",
            "mix-video-paths",
            "scan-video-paths",
            "ps-video-paths",
            "vs-video-paths",
            "dedup-video-paths",
        ):
            self.assertIn(target, source)
        self.assertNotIn("receive_video_drop", source)
        self.assertNotIn("getAsFileSystemHandle", source)


if __name__ == "__main__":
    unittest.main()
