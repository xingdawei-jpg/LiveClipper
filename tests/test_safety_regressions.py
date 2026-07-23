from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found: {name}")


class SafetyRegressionTests(unittest.TestCase):
    def test_smart_crop_imports_when_opencv_is_unavailable(self) -> None:
        path = ROOT / "app" / "smart_crop.py"
        module_name = "liveclipper_test_smart_crop_without_cv2"
        real_import = builtins.__import__

        def without_cv2(name, *args, **kwargs):
            if name == "cv2":
                raise ImportError("simulated missing OpenCV")
            return real_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location(module_name, path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with mock.patch("builtins.__import__", side_effect=without_cv2):
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)

        self.assertFalse(module._CV2_AVAILABLE)

    def test_idle_gui_queue_does_not_log_a_warning(self) -> None:
        poll_queue = _function_node(ROOT / "app" / "gui.py", "_poll_queue")
        empty_handlers = [
            handler
            for handler in ast.walk(poll_queue)
            if isinstance(handler, ast.ExceptHandler)
            and isinstance(handler.type, ast.Attribute)
            and isinstance(handler.type.value, ast.Name)
            and handler.type.value.id == "queue"
            and handler.type.attr == "Empty"
        ]
        self.assertEqual(len(empty_handlers), 1)
        warning_calls = [
            node
            for node in ast.walk(empty_handlers[0])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_LOG"
            and node.func.attr == "warning"
        ]
        self.assertFalse(warning_calls)

    def test_datetime_format_probes_continue_without_warning(self) -> None:
        parser = _function_node(ROOT / "web_client" / "server.py", "_parse_live_start_datetime")
        value_error_handlers = [
            handler
            for handler in ast.walk(parser)
            if isinstance(handler, ast.ExceptHandler)
            and isinstance(handler.type, ast.Name)
            and handler.type.id == "ValueError"
        ]
        self.assertTrue(any(any(isinstance(node, ast.Continue) for node in ast.walk(handler)) for handler in value_error_handlers))

    def test_activation_does_not_unbind_an_existing_code_before_success(self) -> None:
        activation = _function_node(ROOT / "app" / "license_client.py", "activate_with_code")
        unbind_calls = [
            node
            for node in ast.walk(activation)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_fc_unbind"
        ]
        self.assertFalse(unbind_calls)

    def test_product_scanner_returns_parts_when_concat_fails(self) -> None:
        product_scanner = importlib.import_module("product_scanner")
        scanner = product_scanner.ProductScanner()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / "source.mp4"
            output_dir = root / "output"
            video.write_bytes(b"source")

            def fake_run(command, **_kwargs):
                target = Path(command[-1])
                is_concat = "-f" in command and command[command.index("-f") + 1] == "concat"
                if not is_concat:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"x" * 2048)
                    return SimpleNamespace(returncode=0)
                return SimpleNamespace(returncode=1)

            with mock.patch.object(product_scanner.subprocess, "run", side_effect=fake_run):
                outputs = scanner.extract_clip(
                    str(video),
                    {"name": "product", "segments": [(0, 4), (5, 9)]},
                    str(output_dir),
                )

            self.assertEqual(len(outputs), 2)
            self.assertTrue(all(Path(path).is_file() for path in outputs))

    def test_smart_cut_temp_uses_managed_user_cache(self) -> None:
        source = (ROOT / "app" / "cutter_logic.py").read_text(encoding="utf-8")
        self.assertIn('"cache", "processing"', source)
        self.assertNotIn('mkdtemp(prefix="lc_temp_", dir="C:\\\\")', source)
        self.assertNotIn('os.path.join("C:\\\\", "lc_temp_mix_"', source)

    def test_processing_temp_dir_follows_user_data_location(self) -> None:
        cutter_logic = importlib.import_module("cutter_logic")
        config = importlib.import_module("config")
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(config, "USER_DATA_DIR", temp):
                created = Path(cutter_logic._create_processing_temp_dir("lc_test_"))
            self.assertEqual(created.parent, Path(temp) / "cache" / "processing")
            self.assertTrue(created.is_dir())

    def test_ffmpeg_command_keeps_system_path_fallback(self) -> None:
        get_ffmpeg_cmd = _function_node(ROOT / "app" / "cutter_logic.py", "get_ffmpeg_cmd")
        return_values = [
            node.value.value
            for node in ast.walk(get_ffmpeg_cmd)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
        self.assertIn("ffmpeg", return_values)

    def test_gui_never_deletes_pyinstaller_mei_directories(self) -> None:
        source = (ROOT / "app" / "gui.py").read_text(encoding="utf-8")
        self.assertNotIn("_cleanup_temp_dir", source)
        self.assertNotIn("_cleanup_tempdir", source)
        self.assertNotIn("'_MEI*'", source)

    def test_primary_video_lists_accept_dropped_files(self) -> None:
        markup = (ROOT / "web_client" / "frontend" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "web_client" / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")
        styles = (ROOT / "web_client" / "frontend" / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertIn('data-drop-target="video-paths"', markup)
        self.assertIn('data-drop-target="mix-video-paths"', markup)
        self.assertIn('zone.dataset.dropClickPicker !== "false"', script)
        self.assertIn('await addDroppedVideoFiles(targetId, files)', script)
        self.assertIn('upload("/api/uploads/videos", form)', script)
        self.assertIn(".video-picker-card.is-dragging", styles)


if __name__ == "__main__":
    unittest.main()
