from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
TOOLS_DIR = ROOT / "tools"
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(TOOLS_DIR))

import build_delta_package
import build_v3_package
import liveclipper_launcher
import liveclipper_update_agent
from release_signing import generate_keypair, sha256_file, sign_manifest, verify_manifest


class RuntimeV3UpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.key_temp = tempfile.TemporaryDirectory()
        key_root = Path(cls.key_temp.name)
        cls.private_key = key_root / "release_private.pem"
        cls.public_key = key_root / "release_public.pem"
        generate_keypair(cls.private_key, cls.public_key)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.key_temp.cleanup()

    def _runtime(self, root: Path, version: str, marker: str) -> Path:
        runtime = root / f"runtime-{version}"
        (runtime / "_internal" / "app").mkdir(parents=True)
        (runtime / "LiveClipperWeb.exe").write_bytes(b"fake-runtime-executable")
        (runtime / "_internal" / "base.txt").write_text("shared-runtime-data", encoding="utf-8")
        (runtime / "_internal" / "changed.txt").write_text(marker, encoding="utf-8")
        (runtime / "_internal" / "py.typed").write_bytes(b"")
        deep_file = runtime / "_internal" / "numpy-2.4.4.dist-info" / "licenses" / "numpy" / "_core" / "include" / "numpy" / "libdivide"
        deep_file.parent.mkdir(parents=True)
        deep_file.write_bytes(b"")
        (runtime / "_internal" / "app" / "version.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "latest_version": version,
                    "runtime_layout_version": 3,
                }
            ),
            encoding="utf-8",
        )
        shutil.copy2(
            self.public_key,
            runtime / "_internal" / "app" / "release_update_public_key.pem",
        )
        return runtime

    def _stable_files(self, root: Path) -> tuple[Path, Path]:
        launcher = root / "LiveClipperLauncher.exe"
        updater = root / "LiveClipperUpdater.exe"
        launcher.write_bytes(b"stable-launcher")
        updater.write_bytes(b"stable-updater")
        return launcher, updater

    def _v2_zip(self, runtime: Path, output: Path) -> None:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(item for item in runtime.rglob("*") if item.is_file()):
                archive.write(path, (Path("LiveClipperWeb") / path.relative_to(runtime)).as_posix())

    def _v3_zip(
        self,
        base: Path,
        version: str,
        marker: str,
        filename: str,
        stable: tuple[Path, Path],
    ) -> Path:
        runtime = self._runtime(base, version, marker)
        package_root = base / f"package-{version}"
        build_v3_package.assemble(
            runtime,
            package_root,
            stable[0],
            stable[1],
            self.public_key,
            self.private_key,
            base,
        )
        output = base / filename
        build_v3_package.build_zip(package_root, output)
        return output

    def _extract(self, archive_path: Path, destination: Path) -> Path:
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(destination)
        return destination / "LiveClipperWeb"

    def test_v2_bridge_builds_current_and_previous_versions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / ("long-install-root-" + "x" * 55)
            base.mkdir()
            local_data = base / "local-data"
            old_runtime = self._runtime(base, "2026.7.13.10", "old")
            source_zip = base / "source.zip"
            self._v2_zip(old_runtime, source_zip)
            stable = self._stable_files(base)
            target_zip = self._v3_zip(
                base,
                "2026.7.13.11",
                "new",
                "target.zip",
                stable,
            )
            patch_zip = base / "patch.zip"
            summary = build_delta_package.build_patch(
                source_zip,
                target_zip,
                patch_zip,
                self.private_key,
                self.public_key,
            )
            install_root = self._extract(source_zip, base / "install")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(local_data)}, clear=False):
                result = liveclipper_update_agent.apply_patch(
                    patch_zip,
                    install_root,
                    self.public_key,
                    launch_after=False,
                )

            self.assertEqual(summary["source_layout_version"], 2)
            self.assertEqual(result["source_layout_version"], 2)
            state = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_version"], "2026.7.13.11")
            self.assertEqual(state["previous_version"], "2026.7.13.10")
            self.assertTrue(state["pending"])
            install_manifest = json.loads((install_root / "install_manifest.json").read_text(encoding="utf-8"))
            verify_manifest(install_manifest, self.public_key)
            self.assertEqual(install_manifest["initial_version"], "2026.7.13.11")
            self.assertEqual(
                (install_root / "versions" / "2026.7.13.11" / "_internal" / "changed.txt").read_text(
                    encoding="utf-8"
                ),
                "new",
            )
            self.assertEqual(
                (install_root / "versions" / "2026.7.13.10" / "_internal" / "changed.txt").read_text(
                    encoding="utf-8"
                ),
                "old",
            )
            self.assertEqual(liveclipper_launcher.run(install_root, validate_only=True), 0)

    def test_v3_to_v3_patch_reuses_stable_components(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            stable = self._stable_files(base)
            source_zip = self._v3_zip(
                base,
                "2026.7.13.11",
                "before",
                "source-v3.zip",
                stable,
            )
            target_zip = self._v3_zip(
                base,
                "2026.7.13.12",
                "after",
                "target-v3.zip",
                stable,
            )
            patch_zip = base / "v3-to-v3.zip"
            summary = build_delta_package.build_patch(
                source_zip,
                target_zip,
                patch_zip,
                self.private_key,
                self.public_key,
            )
            self.assertEqual(summary["source_layout_version"], 3)
            self.assertEqual(summary["stable_payload_files"], 0)
            install_root = self._extract(source_zip, base / "install")
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(base / "local-data")},
                clear=False,
            ):
                liveclipper_update_agent.apply_patch(
                    patch_zip,
                    install_root,
                    self.public_key,
                    launch_after=False,
                )
            state = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_version"], "2026.7.13.12")
            self.assertEqual(state["previous_version"], "2026.7.13.11")
            install_manifest = json.loads((install_root / "install_manifest.json").read_text(encoding="utf-8"))
            verify_manifest(install_manifest, self.public_key)
            self.assertEqual(install_manifest["initial_version"], "2026.7.13.12")
            self.assertEqual(
                (install_root / "versions" / "2026.7.13.12" / "_internal" / "changed.txt").read_text(
                    encoding="utf-8"
                ),
                "after",
            )

    def test_v3_to_v3_patch_rejects_stable_component_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source_stable_root = base / "source-stable"
            target_stable_root = base / "target-stable"
            source_stable_root.mkdir()
            target_stable_root.mkdir()
            source_stable = self._stable_files(source_stable_root)
            target_stable = self._stable_files(target_stable_root)
            target_stable[1].write_bytes(b"rebuilt-updater")
            source_zip = self._v3_zip(
                base,
                "2026.7.13.11",
                "before",
                "source-v3.zip",
                source_stable,
            )
            target_zip = self._v3_zip(
                base,
                "2026.7.13.12",
                "after",
                "target-v3.zip",
                target_stable,
            )

            with self.assertRaisesRegex(
                build_delta_package.PackageError,
                "cannot replace stable components",
            ):
                build_delta_package.build_patch(
                    source_zip,
                    target_zip,
                    base / "v3-to-v3.zip",
                    self.private_key,
                    self.public_key,
                )

    def test_wrong_source_version_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old_runtime = self._runtime(base, "2026.7.13.10", "old")
            source_zip = base / "source.zip"
            self._v2_zip(old_runtime, source_zip)
            stable = self._stable_files(base)
            target_zip = self._v3_zip(base, "2026.7.13.11", "new", "target.zip", stable)
            patch_zip = base / "patch.zip"
            build_delta_package.build_patch(
                source_zip,
                target_zip,
                patch_zip,
                self.private_key,
                self.public_key,
            )
            install_root = self._extract(source_zip, base / "install")
            version_path = install_root / "_internal" / "app" / "version.json"
            version_path.write_text('{"version":"2026.7.13.9"}', encoding="utf-8")
            with self.assertRaises(liveclipper_update_agent.UpdateError):
                liveclipper_update_agent.apply_patch(
                    patch_zip,
                    install_root,
                    self.public_key,
                    launch_after=False,
                )
            self.assertFalse((install_root / "current.json").exists())
            self.assertEqual(
                (install_root / "LiveClipperWeb.exe").read_bytes(),
                b"fake-runtime-executable",
            )

    def test_outer_hash_rejects_corrupt_patch_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            old_runtime = self._runtime(base, "2026.7.13.10", "old")
            source_zip = base / "source.zip"
            self._v2_zip(old_runtime, source_zip)
            stable = self._stable_files(base)
            target_zip = self._v3_zip(base, "2026.7.13.11", "new", "target.zip", stable)
            patch_zip = base / "patch.zip"
            build_delta_package.build_patch(
                source_zip,
                target_zip,
                patch_zip,
                self.private_key,
                self.public_key,
            )
            install_root = self._extract(source_zip, base / "install")
            with self.assertRaises(liveclipper_update_agent.UpdateError):
                liveclipper_update_agent.apply_patch(
                    patch_zip,
                    install_root,
                    self.public_key,
                    expected_patch_sha256="0" * 64,
                    launch_after=False,
                )
            self.assertFalse((install_root / "current.json").exists())

    def test_signed_manifest_tampering_is_rejected(self) -> None:
        manifest = sign_manifest(
            {"schema_version": 3, "version": "2026.7.13.11"},
            self.private_key,
        )
        verify_manifest(manifest, self.public_key)
        manifest["version"] = "9999.1.1"
        with self.assertRaises(Exception):
            verify_manifest(manifest, self.public_key)

    def test_patch_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            patch_path = Path(temp) / "bad.zip"
            manifest = sign_manifest(
                {
                    "schema_version": 3,
                    "format": liveclipper_update_agent.PATCH_FORMAT,
                    "from_version": "2026.7.13.10",
                    "to_version": "2026.7.13.11",
                    "source_layout_version": 2,
                    "target_layout_version": 3,
                    "runtime_payload": {
                        "../outside.txt": {
                            "archive": "payload/outside.txt",
                            "sha256": "0" * 64,
                            "size": 1,
                        }
                    },
                    "stable_payload": {},
                    "stable_result_files": {},
                },
                self.private_key,
            )
            with zipfile.ZipFile(patch_path, "w") as archive:
                archive.writestr("patch_manifest.json", json.dumps(manifest))
            with self.assertRaises(liveclipper_update_agent.UpdateError):
                liveclipper_update_agent._load_patch(patch_path, self.public_key)

    @unittest.skipUnless(os.name == "nt", "launcher rollback test uses a Windows executable")
    def test_pending_runtime_without_health_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            install_root = Path(temp)
            updater_dir = install_root / "updater"
            updater_dir.mkdir()
            shutil.copy2(self.public_key, updater_dir / "release_update_public_key.pem")
            quick_exit = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "where.exe"
            self.assertTrue(quick_exit.is_file())

            for version in ("2026.7.13.10", "2026.7.13.11"):
                runtime = install_root / "versions" / version
                runtime.mkdir(parents=True)
                executable = runtime / "LiveClipperWeb.exe"
                shutil.copy2(quick_exit, executable)
                manifest = sign_manifest(
                    {
                        "schema_version": 3,
                        "format": "liveclipper-runtime-manifest-v1",
                        "runtime_layout_version": 3,
                        "version": version,
                        "entrypoint": "LiveClipperWeb.exe",
                        "files": {
                            "LiveClipperWeb.exe": {
                                "sha256": sha256_file(executable),
                                "size": executable.stat().st_size,
                            }
                        },
                    },
                    self.private_key,
                )
                (runtime / "runtime_manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False),
                    encoding="utf-8",
                )

            (install_root / "current.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runtime_layout_version": 3,
                        "current_version": "2026.7.13.11",
                        "previous_version": "2026.7.13.10",
                        "pending": True,
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(install_root / "local-data")},
                clear=False,
            ):
                result = liveclipper_launcher.run(install_root, health_timeout=1.0)
            state = json.loads((install_root / "current.json").read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertEqual(state["current_version"], "2026.7.13.10")
            self.assertEqual(state["failed_version"], "2026.7.13.11")
            self.assertFalse(state["pending"])
            time.sleep(0.5)


    def test_embedded_patch_takes_priority_over_adjacent_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            executable_dir = base / "bridge"
            bundle_dir = base / "bundle"
            executable_dir.mkdir()
            bundle_dir.mkdir()
            (executable_dir / "LiveClipperPatch_external-one.zip").write_bytes(b"one")
            (executable_dir / "LiveClipperPatch_external-two.zip").write_bytes(b"two")
            embedded = bundle_dir / "LiveClipperPatch_embedded.zip"
            embedded.write_bytes(b"embedded")
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(sys, "_MEIPASS", str(bundle_dir), create=True),
                patch.object(sys, "executable", str(executable_dir / "bridge.exe")),
            ):
                candidates = liveclipper_update_agent._candidate_patch_paths()
            self.assertEqual(candidates, [embedded.resolve()])


if __name__ == "__main__":
    unittest.main()
