from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_updater():
    path = ROOT / "app" / "updater.py"
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("liveclipper_test_updater", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_desktop():
    path = ROOT / "web_client" / "desktop.py"
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("liveclipper_test_desktop", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _JsonResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class ReleaseArchitectureTests(unittest.TestCase):
    @staticmethod
    def _healthy_runtime(version: str) -> dict[str, object]:
        return {
            "version": version,
            "runtime_layout_version": 3,
            "code_source": "bundled",
            "runtime_integrity": {"ok": True, "checked": 9, "mismatched": []},
        }

    def test_manifest_declares_runtime_v3(self) -> None:
        manifest = json.loads((ROOT / "app" / "version.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["schema_version"], 3)
        self.assertEqual(manifest["runtime_layout_version"], 3)
        self.assertEqual(
            manifest["update_strategy"],
            "verified-version-delta-with-full-fallback",
        )
        self.assertTrue(manifest["supports_incremental_updates"])
        self.assertFalse(manifest["requires_full_package"])
        self.assertEqual(manifest["minimum_updater_version"], "1.1.0")
        self.assertEqual(manifest["files"], {})
        self.assertIn("签名补丁", manifest["requires_full_package_note"])
        self.assertNotRegex(manifest["release_notes"], r"[Ãæ]")
        self.assertIn("app/release_update_public_key.pem", manifest["runtime_files"])

    def test_updater_requires_an_exact_signed_patch_route(self) -> None:
        updater = _load_updater()
        self.assertFalse(hasattr(updater, "_write_update_file"))
        info = {
            "version": "2026.7.13.12",
            "patches": [
                {
                    "format": updater.DELTA_FORMAT,
                    "from_version": "2026.7.13.11",
                    "to_version": "2026.7.13.12",
                    "url": "https://example.invalid/patch.zip",
                    "sha256": "a" * 64,
                    "size": 123,
                }
            ],
        }
        self.assertIsNotNone(updater._select_delta_patch(info, "2026.7.13.11"))
        self.assertIsNone(updater._select_delta_patch(info, "2026.7.13.10"))
        self.assertFalse(updater.delta_runtime_supported())

    def test_old_updater_version_forces_full_package_route(self) -> None:
        updater = _load_updater()
        release = {
            "version": "2026.7.13.14",
            "minimum_updater_version": "1.1.0",
            "patches": [
                {
                    "format": updater.DELTA_FORMAT,
                    "from_version": "2026.7.13.13",
                    "to_version": "2026.7.13.14",
                    "url": "https://example.invalid/patch.zip",
                    "sha256": "a" * 64,
                    "size": 123,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            install_root = Path(temp)
            install_manifest = install_root / updater.INSTALL_MANIFEST
            install_manifest.write_text(
                '{"updater_version":"1.0.0"}',
                encoding="utf-8",
            )
            with (
                patch.object(updater, "_install_root", return_value=install_root),
                patch.object(updater, "delta_runtime_supported", return_value=True),
            ):
                old_route = updater._with_update_route(dict(release), "2026.7.13.13")
                self.assertTrue(old_route["requires_full_package"])
                self.assertFalse(old_route["supports_incremental_updates"])
                install_manifest.write_text(
                    '{"updater_version":"1.1.0"}',
                    encoding="utf-8",
                )
                current_route = updater._with_update_route(
                    dict(release),
                    "2026.7.13.13",
                )
                self.assertFalse(current_route["requires_full_package"])
                self.assertTrue(current_route["supports_incremental_updates"])

    def test_unmatched_update_falls_back_to_full_package(self) -> None:
        updater = _load_updater()
        result = updater.apply_update_headless(
            {
                "version": "9999.1.1",
                "release_page_url": "https://example.invalid/release",
                "patches": [],
            }
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["full_package_required"])
        self.assertEqual(result["updated"], [])

    def test_installed_version_is_the_running_manifest(self) -> None:
        updater = _load_updater()
        manifest = json.loads((ROOT / "app" / "version.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(updater._get_installed_version(), manifest["version"])
        self.assertFalse(updater._set_installed_version("9999.1.1"))
        self.assertEqual(updater._get_installed_version(), manifest["version"])

    def test_frozen_updater_ignores_an_inherited_old_bundle_dir(self) -> None:
        updater = _load_updater()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old_bundle = base / "versions" / "2026.7.13.13" / "_internal"
            new_bundle = base / "versions" / "2026.7.14.4" / "_internal"
            old_bundle.mkdir(parents=True)
            new_bundle.mkdir(parents=True)
            with (
                patch.dict(
                    os.environ,
                    {"LIVECLIPPER_BUNDLE_DIR": str(old_bundle)},
                    clear=False,
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(new_bundle), create=True),
            ):
                self.assertEqual(updater._runtime_root(), new_bundle.resolve())

    def test_updater_uses_configured_bundle_manifest(self) -> None:
        updater = _load_updater()
        previous = os.environ.get("LIVECLIPPER_BUNDLE_DIR")
        try:
            with tempfile.TemporaryDirectory() as temp:
                bundle = Path(temp)
                (bundle / "app").mkdir()
                (bundle / "app" / "version.json").write_text(
                    '{"version":"2030.1.2.3"}',
                    encoding="utf-8",
                )
                os.environ["LIVECLIPPER_BUNDLE_DIR"] = str(bundle)
                self.assertEqual(updater._get_installed_version(), "2030.1.2.3")
        finally:
            if previous is None:
                os.environ.pop("LIVECLIPPER_BUNDLE_DIR", None)
            else:
                os.environ["LIVECLIPPER_BUNDLE_DIR"] = previous

    def test_source_runtime_ignores_polluted_appdata(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            appdata = Path(temp)
            legacy_app = appdata / "LiveClipper" / "app"
            legacy_web = appdata / "LiveClipper" / "web_client" / "frontend"
            legacy_app.mkdir(parents=True)
            legacy_web.mkdir(parents=True)
            (legacy_app / "version.json").write_text('{"version":"9999.1.1"}', encoding="utf-8")
            (legacy_app / "cutter_logic.py").write_text(
                "raise RuntimeError('legacy code loaded')",
                encoding="utf-8",
            )
            (legacy_web / "index.html").write_text("legacy", encoding="utf-8")

            env = os.environ.copy()
            env["APPDATA"] = str(appdata)
            env.pop("LIVECLIPPER_BUNDLE_DIR", None)
            env.pop("LIVECLIPPER_FROZEN", None)
            command = [
                sys.executable,
                "-c",
                (
                    "import json,sys;"
                    f"sys.path.insert(0,{str(ROOT / 'web_client')!r});"
                    "import server;"
                    "print(json.dumps({'app':str(server.APP_DIR),'web':str(server.WEB_DIR),"
                    "'source':server.CODE_SOURCE,'legacy':server._legacy_runtime_overlays_present(),"
                    "'delta':server._safe_web_incremental_supported()}))"
                ),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            runtime = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(Path(runtime["app"]).resolve(), (ROOT / "app").resolve())
            self.assertEqual(Path(runtime["web"]).resolve(), (ROOT / "web_client").resolve())
            self.assertEqual(runtime["source"], "source")
            self.assertTrue(runtime["legacy"])
            self.assertFalse(runtime["delta"])

    def test_bootstrap_has_no_appdata_code_loader(self) -> None:
        desktop = (ROOT / "web_client" / "desktop.py").read_text(encoding="utf-8")
        server = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        launcher = (ROOT / "app" / "launcher.py").read_text(encoding="utf-8")
        self.assertNotIn("_updated_server_path", desktop)
        self.assertNotIn("USER_UPDATE_ROOT", server)
        self.assertNotIn("LiveClipper', 'app", launcher)
        launcher_tool = (ROOT / "tools" / "liveclipper_launcher.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("LIVECLIPPER_INSTALL_ROOT", launcher_tool)

    def test_frozen_desktop_replaces_an_inherited_old_bundle_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old_bundle = base / "versions" / "2026.7.13.13" / "_internal"
            new_bundle = base / "versions" / "2026.7.14.4" / "_internal"
            old_bundle.mkdir(parents=True)
            new_bundle.mkdir(parents=True)
            with (
                patch.dict(
                    os.environ,
                    {"LIVECLIPPER_BUNDLE_DIR": str(old_bundle)},
                    clear=False,
                ),
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(new_bundle), create=True),
            ):
                desktop = _load_desktop()
                self.assertEqual(desktop.BUNDLE_DIR.resolve(), new_bundle.resolve())
                self.assertEqual(
                    Path(os.environ["LIVECLIPPER_BUNDLE_DIR"]).resolve(),
                    new_bundle.resolve(),
                )
                self.assertEqual(os.environ["LIVECLIPPER_CODE_SOURCE"], "bundled")

    def test_frozen_server_ignores_an_inherited_old_bundle_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            old_bundle = Path(temp) / "versions" / "2026.7.13.13" / "_internal"
            old_bundle.mkdir(parents=True)
            env = os.environ.copy()
            env["LIVECLIPPER_BUNDLE_DIR"] = str(old_bundle)
            command = [
                sys.executable,
                "-c",
                (
                    "import json,sys;"
                    f"sys.frozen=True;sys._MEIPASS={str(ROOT)!r};"
                    f"sys.path.insert(0,{str(ROOT / 'web_client')!r});"
                    "import server;"
                    "print(json.dumps({'bundle':str(server.BUNDLE_DIR),"
                    "'app':str(server.APP_DIR),'version':server._load_version()}))"
                ),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
                check=True,
            )
            runtime = json.loads(completed.stdout.strip().splitlines()[-1])
            self.assertEqual(Path(runtime["bundle"]).resolve(), ROOT.resolve())
            self.assertEqual(Path(runtime["app"]).resolve(), (ROOT / "app").resolve())
            self.assertNotEqual(runtime["version"], "2026.7.13.13")

    def test_launcher_health_retries_after_transient_runtime_timeout(self) -> None:
        desktop = _load_desktop()
        version = "2026.7.14.3"
        token = "a" * 32
        with tempfile.TemporaryDirectory() as temp:
            local = Path(temp) / "local"
            destination = local / "LiveClipper" / "launcher_health" / f"{token}.json"
            calls = 0

            def delayed_runtime(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise TimeoutError("simulated first health timeout")
                return _JsonResponse(self._healthy_runtime(version))

            with (
                patch.dict(
                    os.environ,
                    {
                        "LOCALAPPDATA": str(local),
                        "LIVECLIPPER_HEALTH_TOKEN": token,
                        "LIVECLIPPER_HEALTH_FILE": str(destination),
                        "LIVECLIPPER_ACTIVE_VERSION": version,
                    },
                    clear=False,
                ),
                patch.object(desktop.urllib.request, "urlopen", side_effect=delayed_runtime),
                patch.object(desktop.time, "sleep", return_value=None),
            ):
                self.assertTrue(desktop._report_launcher_health(8765, report_timeout=2.0))

            self.assertEqual(calls, 2)
            receipt = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(receipt["version"], version)
            self.assertEqual(receipt["attempt"], 2)
            diagnostic = destination.parent / "runtime-health.log"
            self.assertIn("attempt 1 failed", diagnostic.read_text(encoding="utf-8"))
            self.assertIn("confirmed", diagnostic.read_text(encoding="utf-8"))

    def test_launcher_health_accepts_launcher_owned_path_from_another_data_root(self) -> None:
        desktop = _load_desktop()
        version = "2026.7.14.3"
        token = "b" * 32
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            child_local = base / "child-local"
            launcher_data = base / "launcher-local" / "LiveClipper"
            destination = launcher_data / "launcher_health" / f"{token}.json"
            with (
                patch.dict(
                    os.environ,
                    {
                        "LOCALAPPDATA": str(child_local),
                        "LIVECLIPPER_HEALTH_TOKEN": token,
                        "LIVECLIPPER_HEALTH_FILE": str(destination),
                        "LIVECLIPPER_ACTIVE_VERSION": version,
                    },
                    clear=False,
                ),
                patch.object(
                    desktop.urllib.request,
                    "urlopen",
                    return_value=_JsonResponse(self._healthy_runtime(version)),
                ),
            ):
                self.assertTrue(desktop._report_launcher_health(8765, report_timeout=1.0))
            self.assertTrue(destination.is_file())

    def test_launcher_health_rejects_a_non_token_receipt_filename(self) -> None:
        desktop = _load_desktop()
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "LiveClipper" / "launcher_health" / "wrong.json"
            with self.assertRaisesRegex(ValueError, "does not match"):
                desktop._launcher_health_destination(str(destination), "c" * 32)


if __name__ == "__main__":
    unittest.main()
