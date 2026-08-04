from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.release_signing import generate_keypair, sha256_file, sign_manifest
from runtime_v4.business_bundle import build_business_archive
from runtime_v4.core_manifest import build_core_manifest
from runtime_v4.migration import build_core_bridge, inspect_v3_install
from runtime_v4.migration_package import (
    MigrationPackageError,
    build_migration_package,
    verify_migration_package,
)
from runtime_v4.migration_transaction import (
    MigrationTransactionError,
    migrate_v3_install,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
SAMPLE = ROOT / "tests" / "fixtures" / "runtime_v4_sample"


class RuntimeV4MigrationPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
        generate_keypair(self.private_key, self.public_key)
        self.legacy_root = self.root / "legacy"
        runtime = self.legacy_root / "versions" / "2026.7.27.5"
        (runtime / "_internal").mkdir(parents=True)
        (runtime / "LiveClipperWeb.exe").write_bytes(b"old")
        (runtime / "_internal" / "shared.bin").write_bytes(b"shared")
        files = {}
        for path in sorted(runtime.rglob("*")):
            if path.is_file():
                files[path.relative_to(runtime).as_posix()] = {
                    "sha256": sha256_file(path),
                    "size": path.stat().st_size,
                }
        signed = sign_manifest(
            {
                "schema_version": 3,
                "format": "liveclipper-runtime-manifest-v1",
                "runtime_layout_version": 3,
                "version": "2026.7.27.5",
                "entrypoint": "LiveClipperWeb.exe",
                "files": files,
            },
            self.private_key,
        )
        (runtime / "runtime_manifest.json").write_text(
            json.dumps(signed), encoding="utf-8"
        )
        (self.legacy_root / "current.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime_layout_version": 3,
                    "current_version": "2026.7.27.5",
                    "previous_version": "",
                    "pending": False,
                    "generation": 1,
                }
            ),
            encoding="utf-8",
        )
        (self.legacy_root / "LiveClipperWeb.exe").write_bytes(b"stable-v3-launcher")

        target_core = self.root / "target-core"
        (target_core / "_internal").mkdir(parents=True)
        (target_core / "LiveClipperHost.exe").write_bytes(b"host")
        (target_core / "_internal" / "shared.bin").write_bytes(b"shared")
        build_core_manifest(
            target_core,
            core_version="4.0.0",
            private_key_path=self.private_key,
        )
        self.bridge = self.root / "bridge"
        build_core_bridge(
            inspect_v3_install(self.legacy_root, self.public_key),
            target_core,
            self.public_key,
            self.bridge,
        )
        self.business_archive = self.root / "business.zip"
        build_business_archive(
            SAMPLE,
            self.business_archive,
            application_version="2026.8.1.1",
            private_key_path=self.private_key,
            policy_path=POLICY,
        )
        self.launcher = self.root / "LiveClipperWeb.exe"
        self.launcher.write_bytes(b"stable-v4-launcher")
        self.package = self.root / "migration-package"
        build_migration_package(
            source_version="2026.7.27.5",
            application_version="2026.8.1.1",
            core_version="4.0.0",
            launcher_path=self.launcher,
            core_bridge_path=self.bridge,
            business_archive_path=self.business_archive,
            private_key_path=self.private_key,
            public_key_path=self.public_key,
            output_root=self.package,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_signed_package_binds_source_launcher_core_and_business(self) -> None:
        verified = verify_migration_package(self.package, self.public_key)
        self.assertEqual(verified.source_version, "2026.7.27.5")
        self.assertEqual(verified.application_version, "2026.8.1.1")
        self.assertEqual(verified.core_version, "4.0.0")
        self.assertEqual(verified.launcher.name, "LiveClipperWeb.exe")

    def test_tampered_launcher_and_business_are_rejected(self) -> None:
        launcher = self.package / "launcher" / "LiveClipperWeb.exe"
        launcher.write_bytes(b"x" * launcher.stat().st_size)
        with self.assertRaisesRegex(MigrationPackageError, "launcher.*mismatch"):
            verify_migration_package(self.package, self.public_key)

    def test_extra_top_level_file_and_wrong_key_are_rejected(self) -> None:
        extra = self.package / "unexpected.txt"
        extra.write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(MigrationPackageError, "file set mismatch"):
            verify_migration_package(self.package, self.public_key)
        extra.unlink()
        wrong_private = self.root / "wrong-private.pem"
        wrong_public = self.root / "wrong-public.pem"
        generate_keypair(wrong_private, wrong_public)
        with self.assertRaisesRegex(MigrationPackageError, "signature verification failed"):
            verify_migration_package(self.package, wrong_public)

    def test_resigned_version_collision_is_rejected(self) -> None:
        manifest_path = self.package / "migration_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["target"]["application_version"] = "2026.7.27.5"
        manifest_path.write_text(
            json.dumps(sign_manifest(manifest, self.private_key)),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MigrationPackageError, "collides"):
            verify_migration_package(self.package, self.public_key)

    @staticmethod
    def _confirm_health(install_root: Path, _timeout: float) -> bool:
        state_path = install_root / "current.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["pending"] = False
        state_path.write_text(json.dumps(state), encoding="utf-8")
        return True

    def test_transaction_switches_only_after_preparation_and_retains_v3_by_default(self) -> None:
        result = migrate_v3_install(
            self.legacy_root,
            self.package,
            self.public_key,
            health_runner=self._confirm_health,
            process_guard=lambda _root: None,
        )
        state = json.loads((self.legacy_root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(state["runtime_layout_version"], 4)
        self.assertFalse(state["pending"])
        self.assertEqual(
            (self.legacy_root / "LiveClipperWeb.exe").read_bytes(),
            b"stable-v4-launcher",
        )
        self.assertTrue((
            self.legacy_root
            / "versions"
            / "2026.8.1.1"
            / "business"
            / "bundle_manifest.json"
        ).is_file())
        self.assertTrue((self.legacy_root / "versions" / "2026.7.27.5").is_dir())
        self.assertIsNotNone(result.backup_root)
        self.assertEqual(result.core_reused_files, 1)

    def test_failed_health_restores_v3_launcher_state_and_removes_v4(self) -> None:
        original_state = (self.legacy_root / "current.json").read_bytes()
        with self.assertRaisesRegex(
            MigrationTransactionError,
            "health confirmation failed",
        ):
            migrate_v3_install(
                self.legacy_root,
                self.package,
                self.public_key,
                health_runner=lambda _root, _timeout: False,
                process_guard=lambda _root: None,
            )
        self.assertEqual((self.legacy_root / "current.json").read_bytes(), original_state)
        self.assertEqual(
            (self.legacy_root / "LiveClipperWeb.exe").read_bytes(),
            b"stable-v3-launcher",
        )
        self.assertFalse((self.legacy_root / "core" / "4.0.0").exists())
        self.assertFalse((self.legacy_root / "versions" / "2026.8.1.1").exists())
        self.assertTrue((self.legacy_root / "versions" / "2026.7.27.5").is_dir())

    def test_fault_after_state_switch_rolls_back_without_touching_v3(self) -> None:
        def fail(stage: str) -> None:
            if stage == "after_state":
                raise RuntimeError("injected state failure")

        with self.assertRaisesRegex(MigrationTransactionError, "V3 was restored"):
            migrate_v3_install(
                self.legacy_root,
                self.package,
                self.public_key,
                health_runner=self._confirm_health,
                process_guard=lambda _root: None,
                fault_injector=fail,
            )
        state = json.loads((self.legacy_root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(state["runtime_layout_version"], 3)
        self.assertEqual(
            (self.legacy_root / "LiveClipperWeb.exe").read_bytes(),
            b"stable-v3-launcher",
        )

    def test_confirmed_cleanup_removes_only_exact_signed_v3_runtime(self) -> None:
        result = migrate_v3_install(
            self.legacy_root,
            self.package,
            self.public_key,
            cleanup_legacy=True,
            health_runner=self._confirm_health,
            process_guard=lambda _root: None,
        )
        self.assertEqual(result.legacy_cleanup.removed_versions, ("2026.7.27.5",))
        self.assertEqual(result.legacy_cleanup.preserved_versions, ())
        self.assertFalse((self.legacy_root / "versions" / "2026.7.27.5").exists())
        self.assertTrue(
            (self.legacy_root / "core" / "4.0.0" / "_internal" / "shared.bin").is_file()
        )
        self.assertIsNone(result.backup_root)

    def test_interrupted_state_switch_is_restored_before_a_clean_retry(self) -> None:
        def interrupt(stage: str) -> None:
            if stage == "after_state":
                raise KeyboardInterrupt("simulated process termination")

        with self.assertRaises(KeyboardInterrupt):
            migrate_v3_install(
                self.legacy_root,
                self.package,
                self.public_key,
                health_runner=self._confirm_health,
                process_guard=lambda _root: None,
                fault_injector=interrupt,
            )
        interrupted = json.loads(
            (self.legacy_root / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(interrupted["runtime_layout_version"], 4)
        self.assertTrue(interrupted["pending"])

        result = migrate_v3_install(
            self.legacy_root,
            self.package,
            self.public_key,
            health_runner=self._confirm_health,
            process_guard=lambda _root: None,
        )
        recovered = json.loads(
            (self.legacy_root / "current.json").read_text(encoding="utf-8")
        )
        self.assertEqual(recovered["runtime_layout_version"], 4)
        self.assertFalse(recovered["pending"])
        self.assertEqual(result.source_version, "2026.7.27.5")

    def test_interrupted_after_health_is_adopted_as_confirmed(self) -> None:
        def confirm_then_interrupt(install_root: Path, _timeout: float) -> bool:
            self._confirm_health(install_root, _timeout)
            raise KeyboardInterrupt("simulated termination after health")

        with self.assertRaises(KeyboardInterrupt):
            migrate_v3_install(
                self.legacy_root,
                self.package,
                self.public_key,
                health_runner=confirm_then_interrupt,
                process_guard=lambda _root: None,
            )
        result = migrate_v3_install(
            self.legacy_root,
            self.package,
            self.public_key,
            health_runner=lambda _root, _timeout: self.fail(
                "confirmed recovery must not launch again"
            ),
            process_guard=lambda _root: None,
        )
        state = json.loads((self.legacy_root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(state["runtime_layout_version"], 4)
        self.assertFalse(state["pending"])
        self.assertEqual(result.core_reused_files, 0)
        self.assertIsNotNone(result.backup_root)

    def test_frozen_migrator_owns_the_release_key_and_has_no_key_override(self) -> None:
        source = (ROOT / "runtime_v4" / "migrator.py").read_text(encoding="utf-8")
        spec = (ROOT / "runtime_v4" / "liveclipper_migrator_v4.spec").read_text(
            encoding="utf-8"
        )
        self.assertIn('"core_keys"', spec)
        self.assertIn('name="LiveClipperMigratorV4"', spec)
        self.assertIn("a.binaries", spec)
        self.assertIn("a.datas", spec)
        self.assertNotIn("COLLECT(", spec)
        self.assertIn("release_update_public_key.pem", source)
        self.assertNotIn('add_argument("--public-key"', source)


if __name__ == "__main__":
    unittest.main()
