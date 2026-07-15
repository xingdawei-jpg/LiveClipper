from __future__ import annotations

import hashlib
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


def _load_tool(name: str):
    path = ROOT / "tools" / f"{name}.py"
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(f"liveclipper_test_{name}", path)
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


class _DownloadResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunk_size: int = 3,
    ) -> None:
        self._body = body
        self._offset = 0
        self.status = status
        self.headers = headers or {}
        self._chunk_size = chunk_size

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        if self._offset >= len(self._body):
            return b""
        if size < 0:
            limit = len(self._body)
        else:
            limit = min(len(self._body), self._offset + min(size, self._chunk_size))
        chunk = self._body[self._offset:limit]
        self._offset = limit
        return chunk


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
        self.assertEqual(manifest["minimum_updater_version"], "1.3.0")
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

    def test_updater_plans_patch_chains_and_prefers_direct_patch(self) -> None:
        updater = _load_updater()
        chain_info = {
            "version": "2026.7.13.14",
            "patches": [
                {
                    "format": updater.DELTA_FORMAT,
                    "from_version": "2026.7.13.11",
                    "to_version": "2026.7.13.12",
                    "url": "https://example.invalid/a.zip",
                    "sha256": "a" * 64,
                    "size": 10,
                },
                {
                    "format": updater.DELTA_FORMAT,
                    "from_version": "2026.7.13.12",
                    "to_version": "2026.7.13.13",
                    "url": "https://example.invalid/b.zip",
                    "sha256": "b" * 64,
                    "size": 11,
                },
                {
                    "format": updater.DELTA_FORMAT,
                    "from_version": "2026.7.13.13",
                    "to_version": "2026.7.13.14",
                    "url": "https://example.invalid/c.zip",
                    "sha256": "c" * 64,
                    "size": 12,
                },
            ],
        }
        chain = updater._select_delta_chain(chain_info, "2026.7.13.11")
        self.assertEqual(
            [(item["from_version"], item["to_version"]) for item in chain],
            [
                ("2026.7.13.11", "2026.7.13.12"),
                ("2026.7.13.12", "2026.7.13.13"),
                ("2026.7.13.13", "2026.7.13.14"),
            ],
        )

        direct_info = dict(chain_info)
        direct_info["patches"] = [
            *chain_info["patches"],
            {
                "format": updater.DELTA_FORMAT,
                "from_version": "2026.7.13.11",
                "to_version": "2026.7.13.14",
                "url": "https://example.invalid/direct.zip",
                "sha256": "d" * 64,
                "size": 13,
            },
        ]
        direct = updater._select_delta_chain(direct_info, "2026.7.13.11")
        self.assertEqual(len(direct), 1)
        self.assertEqual(direct[0]["to_version"], "2026.7.13.14")

    def test_apply_update_headless_hands_multi_patch_plan_to_agent(self) -> None:
        updater = _load_updater()
        patches = [
            {
                "format": updater.DELTA_FORMAT,
                "from_version": "2026.7.13.11",
                "to_version": "2026.7.13.12",
                "url": "https://example.invalid/a.zip",
                "sha256": "a" * 64,
                "size": 10,
            },
            {
                "format": updater.DELTA_FORMAT,
                "from_version": "2026.7.13.12",
                "to_version": "2026.7.13.13",
                "url": "https://example.invalid/b.zip",
                "sha256": "b" * 64,
                "size": 11,
            },
        ]
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            download_root = base / "downloads"
            install_root = base / "install"
            install_root.mkdir()
            agent = install_root / "updater.exe"
            key = install_root / "release_update_public_key.pem"
            agent.write_bytes(b"agent")
            key.write_bytes(b"key")
            manifests = [
                {"target_runtime_manifest": {"files": {"a.bin": {"size": 1}}}},
                {"target_runtime_manifest": {"files": {"b.bin": {"size": 1}}}},
            ]
            with (
                patch.object(updater, "delta_runtime_supported", return_value=True),
                patch.object(updater, "_updater_meets_minimum", return_value=True),
                patch.object(updater, "_download_root", return_value=download_root),
                patch.object(updater, "_install_root", return_value=install_root),
                patch.object(updater, "_update_agent_path", return_value=agent),
                patch.object(updater, "_release_public_key_path", return_value=key),
                patch.object(updater, "_require_free_space"),
                patch.object(updater, "_verified_download") as download,
                patch.object(updater, "_verify_signed_patch_manifest", side_effect=manifests),
                patch.object(updater.subprocess, "Popen") as popen,
            ):
                result = updater.apply_update_headless(
                    {
                        "version": "2026.7.13.13",
                        "selected_patches": patches,
                    }
                )
            self.assertTrue(result["ok"])
            self.assertEqual(result["patch_count"], 2)
            self.assertEqual(download.call_count, 2)
            command = popen.call_args.args[0]
            self.assertIn("--plan", command)
            self.assertNotIn("--patch", command)
            plan_path = Path(command[command.index("--plan") + 1])
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            self.assertEqual(plan["format"], updater.CHAIN_PLAN_FORMAT)
            self.assertEqual(len(plan["patches"]), 2)

    def test_download_space_preflight_fails_before_download(self) -> None:
        updater = _load_updater()

        class Usage:
            free = 10

        with tempfile.TemporaryDirectory() as temp:
            with patch.object(updater.shutil, "disk_usage", return_value=Usage()):
                with self.assertRaisesRegex(RuntimeError, "not enough disk space"):
                    updater._require_free_space(Path(temp), 1024 * 1024, "test")

    def test_stable_component_versions_have_one_source(self) -> None:
        versions = _load_tool("runtime_v3_versions")
        launcher = _load_tool("liveclipper_launcher")
        agent = _load_tool("liveclipper_update_agent")
        package_builder = _load_tool("build_v3_package")
        manifest_builder = _load_tool("build_update_manifest")
        self.assertEqual(versions.LAUNCHER_VERSION, "1.1.0")
        self.assertEqual(versions.UPDATER_VERSION, "1.3.0")
        self.assertEqual(launcher.LAUNCHER_VERSION, versions.LAUNCHER_VERSION)
        self.assertEqual(agent.UPDATER_VERSION, versions.UPDATER_VERSION)
        self.assertEqual(package_builder.LAUNCHER_VERSION, versions.LAUNCHER_VERSION)
        self.assertEqual(package_builder.UPDATER_VERSION, versions.UPDATER_VERSION)
        built = manifest_builder.build_manifest("2099.1.1", "test")
        self.assertEqual(built["minimum_launcher_version"], versions.LAUNCHER_VERSION)
        self.assertEqual(built["minimum_updater_version"], versions.UPDATER_VERSION)

    def test_release_sources_require_https_and_keep_order(self) -> None:
        builder = _load_tool("build_release_channel")
        sources = builder._download_sources(
            [
                "https://github.com/example/patch.zip",
                "https://bucket.oss-cn-hangzhou.aliyuncs.com/patch.zip",
                "https://github.com/example/patch.zip",
            ]
        )
        self.assertEqual(
            [item["name"] for item in sources],
            ["GitHub", "Aliyun OSS"],
        )
        with self.assertRaises(ValueError):
            builder._download_sources(["http://example.invalid/patch.zip"])

    def test_rollup_planner_flags_long_patch_chains(self) -> None:
        planner = _load_tool("plan_delta_rollups")
        channel = {
            "version": "2026.7.13.14",
            "patches": [
                {"from_version": "2026.7.13.11", "to_version": "2026.7.13.12"},
                {"from_version": "2026.7.13.12", "to_version": "2026.7.13.13"},
                {"from_version": "2026.7.13.13", "to_version": "2026.7.13.14"},
            ],
        }
        result = planner.plan_rollups(channel, rollup_after_versions=2)
        self.assertEqual(
            result["rollups_required"],
            [
                {
                    "from_version": "2026.7.13.11",
                    "to_version": "2026.7.13.14",
                    "chain_length": 3,
                    "chain": [
                        "2026.7.13.11",
                        "2026.7.13.12",
                        "2026.7.13.13",
                        "2026.7.13.14",
                    ],
                }
            ],
        )

    def test_update_progress_frontend_backend_contract(self) -> None:
        server = (ROOT / "web_client" / "server.py").read_text(encoding="utf-8")
        frontend = (
            ROOT / "web_client" / "frontend" / "assets" / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/update/status")', server)
        self.assertIn('api("/api/update/status")', frontend)
        self.assertIn("progress_callback=on_download_progress", server)

    def test_hold_channel_does_not_offer_an_update(self) -> None:
        updater = _load_updater()
        local = {
            "version": "2026.7.14.4",
            "runtime_files": {},
            "integrity_files": [],
        }
        remote = {
            "version": "2026.7.14.5",
            "channel_status": "hold",
            "patches": [],
        }
        with (
            patch.object(updater, "_local_manifest", return_value=local),
            patch.object(updater, "_fetch_signed_release", return_value=remote),
        ):
            self.assertIsNone(updater.check_update())

    def test_patch_sources_are_https_ordered_and_deduplicated(self) -> None:
        updater = _load_updater()
        sources = updater._patch_sources(
            {
                "url": "https://github.example/patch.zip",
                "sources": [
                    {"name": "GitHub", "url": "https://github.example/patch.zip"},
                    {"name": "OSS", "url": "https://oss.example/patch.zip"},
                    "http://unsafe.example/patch.zip",
                ],
            }
        )
        self.assertEqual(
            [item["url"] for item in sources],
            [
                "https://github.example/patch.zip",
                "https://oss.example/patch.zip",
            ],
        )

    def test_verified_download_falls_back_to_second_source(self) -> None:
        updater = _load_updater()
        payload = b"verified delta payload"
        patch_info = {
            "url": "https://github.example/patch.zip",
            "sources": [
                {"name": "GitHub", "url": "https://github.example/patch.zip"},
                {"name": "OSS", "url": "https://oss.example/patch.zip"},
            ],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        response = _DownloadResponse(
            payload,
            headers={"Content-Length": str(len(payload))},
        )
        progress: list[tuple[int, int, str]] = []
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "patch.zip"
            with (
                patch.object(
                    updater.urllib.request,
                    "urlopen",
                    side_effect=[
                        OSError("primary unavailable"),
                        OSError("primary unavailable"),
                        response,
                    ],
                ) as open_mock,
                patch.object(updater.time, "sleep", return_value=None),
            ):
                updater._verified_download(
                    patch_info,
                    destination,
                    lambda done, total, message: progress.append(
                        (done, total, message)
                    ),
                )
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(open_mock.call_count, 3)
            self.assertTrue(any("OSS" in item[2] for item in progress))

    def test_verified_download_resumes_a_partial_file(self) -> None:
        updater = _load_updater()
        payload = b"resume this signed patch"
        split = 7
        patch_info = {
            "url": "https://oss.example/patch.zip",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        response = _DownloadResponse(
            payload[split:],
            status=206,
            headers={
                "Content-Length": str(len(payload) - split),
                "Content-Range": f"bytes {split}-{len(payload) - 1}/{len(payload)}",
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "patch.zip"
            partial = destination.with_suffix(".zip.part")
            partial.write_bytes(payload[:split])
            with patch.object(
                updater.urllib.request,
                "urlopen",
                return_value=response,
            ) as open_mock:
                updater._verified_download(patch_info, destination)
            request = open_mock.call_args.args[0]
            self.assertEqual(request.headers.get("Range"), f"bytes={split}-")
            self.assertEqual(destination.read_bytes(), payload)
            self.assertFalse(partial.exists())

    def test_verified_download_uses_cache_without_network(self) -> None:
        updater = _load_updater()
        payload = b"already verified"
        patch_info = {
            "url": "https://oss.example/patch.zip",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "patch.zip"
            destination.write_bytes(payload)
            with patch.object(updater.urllib.request, "urlopen") as open_mock:
                updater._verified_download(patch_info, destination)
            open_mock.assert_not_called()

    def test_verified_download_preserves_partial_after_network_failure(self) -> None:
        updater = _load_updater()
        payload = b"partial bytes must survive"
        patch_info = {
            "url": "https://github.example/patch.zip",
            "sources": [
                {"name": "OSS", "url": "https://oss.example/patch.zip"},
            ],
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "patch.zip"
            partial = destination.with_suffix(".zip.part")
            partial.write_bytes(payload[:5])
            with (
                patch.object(
                    updater.urllib.request,
                    "urlopen",
                    side_effect=OSError("offline"),
                ),
                patch.object(updater.time, "sleep", return_value=None),
            ):
                with self.assertRaisesRegex(RuntimeError, "已保留断点"):
                    updater._verified_download(patch_info, destination)
            self.assertEqual(partial.read_bytes(), payload[:5])

    def test_old_updater_version_forces_full_package_route(self) -> None:
        updater = _load_updater()
        release = {
            "version": "2026.7.13.14",
            "minimum_updater_version": "1.2.0",
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
                '{"updater_version":"1.1.0"}',
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
                    '{"updater_version":"1.2.0"}',
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
