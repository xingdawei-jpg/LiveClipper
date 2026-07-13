from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_updater():
    path = ROOT / "app" / "updater.py"
    spec = importlib.util.spec_from_file_location("liveclipper_test_updater", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseArchitectureTests(unittest.TestCase):
    def test_manifest_disables_legacy_file_updates(self) -> None:
        manifest = json.loads((ROOT / "app" / "version.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["runtime_layout_version"], 2)
        self.assertEqual(manifest["update_strategy"], "full-package")
        self.assertFalse(manifest["supports_incremental_updates"])
        self.assertTrue(manifest["requires_full_package"])
        self.assertEqual(manifest["files"], {})
        self.assertTrue(manifest["runtime_files"])

    def test_updater_never_applies_program_files(self) -> None:
        updater = _load_updater()
        self.assertFalse(hasattr(updater, "_write_update_file"))
        result = updater.apply_update_headless(
            {"version": "9999.1.1", "release_page_url": "https://example.invalid/release"}
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

    def test_updater_uses_configured_bundle_manifest(self) -> None:
        updater = _load_updater()
        previous = os.environ.get("LIVECLIPPER_BUNDLE_DIR")
        try:
            with tempfile.TemporaryDirectory() as temp:
                bundle = Path(temp)
                (bundle / "app").mkdir()
                (bundle / "app" / "version.json").write_text(
                    '{"version":"2030.1.2.3"}', encoding="utf-8"
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
            (legacy_app / "cutter_logic.py").write_text("raise RuntimeError('legacy code loaded')", encoding="utf-8")
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
                    "'source':server.CODE_SOURCE,'legacy':server._legacy_runtime_overlays_present()}))"
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

    def test_bootstrap_has_no_appdata_code_loader(self) -> None:
        desktop = (ROOT / "web_client" / "desktop.py").read_text(encoding="utf-8")
        server = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        launcher = (ROOT / "app" / "launcher.py").read_text(encoding="utf-8")
        self.assertNotIn("_updated_server_path", desktop)
        self.assertNotIn("USER_UPDATE_ROOT", server)
        self.assertNotIn("LiveClipper', 'app", launcher)


if __name__ == "__main__":
    unittest.main()
