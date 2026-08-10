from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4.business_bundle import build_business_archive
from runtime_v4.core_manifest import build_core_manifest


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "build_v4_full_package.py"

spec = importlib.util.spec_from_file_location("build_v4_full_package", TOOL_PATH)
assert spec is not None and spec.loader is not None
package_builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_builder)


def _write_keypair(root: Path) -> tuple[Path, Path]:
    private = Ed25519PrivateKey.generate()
    private_path = root / "private.pem"
    public_path = root / "public.pem"
    private_path.write_bytes(
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


class BuildV4FullPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key, self.public_key = _write_keypair(self.root)
        self.core = self.root / "core"
        (self.core / "_internal" / "web_client").mkdir(parents=True)
        (self.core / "LiveClipperHost.exe").write_bytes(b"host")
        (self.core / "_internal" / "runtime.bin").write_bytes(b"runtime")
        (self.core / "_internal" / "web_client" / "desktop.py").write_text(
            "# desktop shell\n", encoding="utf-8"
        )
        build_core_manifest(
            self.core,
            core_version="4.0.0",
            private_key_path=self.private_key,
        )
        self.launcher = self.root / "LiveClipperWeb.exe"
        self.launcher.write_bytes(b"launcher")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _business_archive(self, version: str, *, app_version: str | None = None) -> Path:
        source = self.root / f"source-{version}"
        (source / "app").mkdir(parents=True)
        (source / "web_client").mkdir()
        (source / "bundle_entry.py").write_text(
            "def create_application(_context):\n    return {}\n", encoding="utf-8"
        )
        (source / "app" / "version.json").write_text(
            json.dumps({"version": app_version or version}), encoding="utf-8"
        )
        (source / "app" / "logic.py").write_text("VALUE = 1\n", encoding="utf-8")
        (source / "web_client" / "server.py").write_text("VALUE = 1\n", encoding="utf-8")
        policy = {
            "schema_version": 1,
            "policy_id": "test-policy",
            "compatible_core_versions": ["4.0.0", "4.0.1"],
            "entrypoint": {"path": "bundle_entry.py", "callable": "create_application"},
            "import_roots": ["app", "web_client"],
            "include": [
                "bundle_entry.py",
                "app/**/*.py",
                "app/**/*.json",
                "web_client/server.py",
            ],
            "exclude": [],
            "max_file_size": 1024 * 1024,
        }
        policy_path = source / "policy.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        archive = self.root / f"business-{version}.zip"
        build_business_archive(
            source,
            archive,
            application_version=version,
            private_key_path=self.private_key,
            policy_path=policy_path,
        )
        return archive

    def _assemble(self, version: str, backup: str, *, app_version: str | None = None) -> Path:
        with (
            patch.object(package_builder, "PUBLIC_KEY", self.public_key),
            patch.object(package_builder, "DIST_ROOT", self.root / "build-output"),
        ):
            return package_builder.assemble_package(
                self.core,
                self.launcher,
                self._business_archive(version, app_version=app_version),
                version,
                backup_business_version=backup,
                backup_business_archive=self._business_archive(backup),
            )

    def test_full_package_requires_verified_current_and_backup_bundles(self) -> None:
        package = self._assemble("2026.8.9.2", "2026.8.9.1")
        with patch.object(package_builder, "PUBLIC_KEY", self.public_key):
            self.assertTrue(
                package_builder.verify_package(
                    package,
                    version="2026.8.9.2",
                    backup_business_version="2026.8.9.1",
                )
            )
        state = json.loads((package / "current.json").read_text(encoding="utf-8"))
        self.assertTrue(state["verified_cores"]["4.0.0"]["metadata_sha256"])

    def test_package_rejects_current_ui_version_mismatch(self) -> None:
        package = self._assemble(
            "2026.8.9.2",
            "2026.8.9.1",
            app_version="2026.8.9.0",
        )
        with patch.object(package_builder, "PUBLIC_KEY", self.public_key):
            self.assertFalse(
                package_builder.verify_package(
                    package,
                    version="2026.8.9.2",
                    backup_business_version="2026.8.9.1",
                )
            )

    def test_assemble_rejects_incomplete_backup_arguments(self) -> None:
        with (
            patch.object(package_builder, "PUBLIC_KEY", self.public_key),
            patch.object(package_builder, "DIST_ROOT", self.root / "build-output"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Backup version and backup archive"):
                package_builder.assemble_package(
                    self.core,
                    self.launcher,
                    self._business_archive("2026.8.9.2"),
                    "2026.8.9.2",
                    backup_business_version="2026.8.9.1",
                )

    def test_explicit_core_version_prevents_reusing_the_old_core_identity(self) -> None:
        build_core_manifest(
            self.core,
            core_version="4.0.1",
            private_key_path=self.private_key,
        )
        with (
            patch.object(package_builder, "PUBLIC_KEY", self.public_key),
            patch.object(package_builder, "DIST_ROOT", self.root / "build-output"),
        ):
            package = package_builder.assemble_package(
                self.core,
                self.launcher,
                self._business_archive("2026.8.11.1"),
                "2026.8.11.1",
                backup_business_version="2026.8.11.0",
                backup_business_archive=self._business_archive("2026.8.11.0"),
                core_version="4.0.1",
            )
        state = json.loads((package / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(state["current"]["core_version"], "4.0.1")
        self.assertTrue((package / "core" / "4.0.1" / "core_manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
