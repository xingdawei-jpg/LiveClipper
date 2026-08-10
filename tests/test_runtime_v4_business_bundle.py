from __future__ import annotations

import base64
import hashlib
import json
import stat
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4.business_bundle import (
    BundleVerificationError,
    _collect_source_files,
    _load_policy,
    build_business_archive,
    load_verified_application,
    verify_business_archive,
    verify_business_directory,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
SAMPLE = ROOT / "tests" / "fixtures" / "runtime_v4_sample"


def _write_keypair(root: Path, name: str = "release") -> tuple[Path, Path]:
    private = Ed25519PrivateKey.generate()
    private_path = root / f"{name}_private.pem"
    public_path = root / f"{name}_public.pem"
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


def _resign_manifest(bundle: Path, private_path: Path) -> None:
    private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    manifest_bytes = (bundle / "bundle_manifest.json").read_bytes()
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    signature = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "key_id": hashlib.sha256(public_raw).hexdigest()[:16],
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "value": base64.b64encode(private.sign(manifest_bytes)).decode("ascii"),
    }
    (bundle / "bundle_manifest.sig").write_text(
        json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


class RuntimeV4BusinessBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key, self.public_key = _write_keypair(self.root)
        self.archive = self.root / "business.zip"
        self.result = build_business_archive(
            SAMPLE,
            self.archive,
            application_version="2026.8.1",
            private_key_path=self.private_key,
            policy_path=POLICY,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _extract(self, name: str = "extracted") -> Path:
        destination = self.root / name
        with zipfile.ZipFile(self.archive, "r") as archive:
            archive.extractall(destination)
        return destination / "business"

    def test_build_is_byte_for_byte_deterministic(self) -> None:
        second = self.root / "business-second.zip"
        second_result = build_business_archive(
            SAMPLE,
            second,
            application_version="2026.8.1",
            private_key_path=self.private_key,
            policy_path=POLICY,
        )
        self.assertEqual(self.archive.read_bytes(), second.read_bytes())
        self.assertEqual(self.result["archive_sha256"], second_result["archive_sha256"])

    def test_build_expands_allowlisted_roots_without_scanning_the_repository(self) -> None:
        output = self.root / "business-no-repo-scan.zip"
        with patch.object(
            Path,
            "rglob",
            side_effect=AssertionError("business build must not scan the whole source root"),
        ):
            result = build_business_archive(
                SAMPLE,
                output,
                application_version="2026.8.1",
                private_key_path=self.private_key,
                policy_path=POLICY,
            )
        self.assertTrue(output.is_file())
        self.assertGreater(result["file_count"], 0)

    def test_repository_policy_excludes_build_copies_and_maintenance_tools(self) -> None:
        selected = _collect_source_files(ROOT, _load_policy(POLICY))
        forbidden = {
            "app/clip_tuple_check.py",
            "app/feishu_scheduler.py",
            "app/gui_clean.py",
            "app/gui_fresh.py",
            "app/gui_tmp.py",
            "app/license_feishu_backend.py",
            "app/license_generator.py",
            "app/license_server.py",
            "app/license_stats_store.py",
            "app/live_recorder_page_BACKUP.py",
            "app/verify.py",
        }

        self.assertIn("app/live_recorder_page.py", selected)
        self.assertIn("web_client/server.py", selected)
        self.assertTrue(forbidden.isdisjoint(selected))
        self.assertFalse(any(path.startswith("app/dist/") for path in selected))
        self.assertFalse(any(path.startswith("app/build/") for path in selected))
        self.assertFalse(any(path.startswith("app/logs/") for path in selected))
        self.assertFalse(any(path.startswith("app/feedback/") for path in selected))
        self.assertFalse(any(Path(path).name.startswith("_") for path in selected))

    def test_archive_and_directory_verify(self) -> None:
        archive_result = verify_business_archive(
            self.archive,
            self.public_key,
            expected_version="2026.8.1",
        )
        directory_result = verify_business_directory(
            self._extract(),
            self.public_key,
            expected_version="2026.8.1",
        )
        self.assertEqual(archive_result.manifest_sha256, directory_result.manifest_sha256)
        self.assertEqual(archive_result.entrypoint_path, "bundle_entry.py")
        self.assertEqual(archive_result.import_roots, ("app",))
        self.assertEqual(archive_result.compatible_core_versions, ("4.0.0", "4.0.1"))

    def test_loader_executes_only_after_verification(self) -> None:
        bundle = self._extract()
        before = list(sys.path)
        previous_bytecode_setting = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        self.addCleanup(setattr, sys, "dont_write_bytecode", previous_bytecode_setting)
        application, verified = load_verified_application(
            bundle,
            self.public_key,
            {"mode": "prototype"},
            expected_version="2026.8.1",
        )
        self.assertEqual(application["sample"], "runtime-v4")
        self.assertEqual(application["context"], {"mode": "prototype"})
        self.assertEqual(verified.application_version, "2026.8.1")
        self.assertEqual(before, sys.path)
        self.assertFalse(any(path.name == "__pycache__" for path in bundle.rglob("__pycache__")))
        verify_business_directory(bundle, self.public_key, expected_version="2026.8.1")

    def test_tampered_entrypoint_is_rejected_before_execution(self) -> None:
        bundle = self._extract()
        marker = self.root / "executed.txt"
        (bundle / "bundle_entry.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
            "def create_application(context): return context\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(BundleVerificationError, "size mismatch|digest mismatch"):
            load_verified_application(bundle, self.public_key, {})
        self.assertFalse(marker.exists())

    def test_missing_and_extra_files_are_rejected(self) -> None:
        missing = self._extract("missing")
        (missing / "bundle_entry.py").unlink()
        with self.assertRaisesRegex(BundleVerificationError, "missing or unsafe"):
            verify_business_directory(missing, self.public_key)

        extra = self._extract("extra")
        (extra / "unlisted.py").write_text("value = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(BundleVerificationError, "file set mismatch"):
            verify_business_directory(extra, self.public_key)

    def test_legacy_run_log_repair_is_narrow_and_opt_in(self) -> None:
        bundle = self._extract("legacy-run-log")
        log_dir = bundle / "app" / "logs"
        log_dir.mkdir(parents=True)
        legacy_log = log_dir / "20260801_173149_sample_\u6210\u529f.json"
        legacy_log.write_text("{}", encoding="utf-8")

        with self.assertRaisesRegex(BundleVerificationError, "file set mismatch"):
            verify_business_directory(bundle, self.public_key)

        verified = verify_business_directory(
            bundle,
            self.public_key,
            repair_legacy_runtime_artifacts=True,
            legacy_artifact_quarantine=self.root / "recovered",
        )
        self.assertEqual(verified.application_version, "2026.8.1")
        self.assertFalse(legacy_log.exists())
        recovered = self.root / "recovered" / "app" / "logs" / legacy_log.name
        self.assertTrue(recovered.is_file())
        self.assertEqual(recovered.read_text(encoding="utf-8"), "{}")

        unrelated = log_dir / "unexpected.json"
        unrelated.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(BundleVerificationError, "file set mismatch"):
            verify_business_directory(
                bundle,
                self.public_key,
                repair_legacy_runtime_artifacts=True,
                legacy_artifact_quarantine=self.root / "recovered",
            )
        self.assertTrue(unrelated.exists())

    def test_wrong_key_and_stale_version_are_rejected(self) -> None:
        _, wrong_public = _write_keypair(self.root, "wrong")
        with self.assertRaisesRegex(BundleVerificationError, "key id mismatch"):
            verify_business_archive(self.archive, wrong_public)
        with self.assertRaisesRegex(BundleVerificationError, "version mismatch"):
            verify_business_archive(
                self.archive,
                self.public_key,
                expected_version="2026.8.2",
            )
        with self.assertRaisesRegex(BundleVerificationError, "incompatible with core"):
            verify_business_archive(
                self.archive,
                self.public_key,
                expected_core_version="4.1.0",
            )

    def test_symlink_is_rejected_before_import(self) -> None:
        bundle = self._extract()
        target = self.root / "outside.py"
        target.write_text("value = 'outside'\n", encoding="utf-8")
        link = bundle / "unlisted_link.py"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(BundleVerificationError, "cannot contain symlinks"):
            verify_business_directory(bundle, self.public_key)

    def test_manifest_path_traversal_is_rejected_even_when_resigned(self) -> None:
        bundle = self._extract()
        manifest_path = bundle / "bundle_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["../outside.py"] = manifest["files"].pop("bundle_entry.py")
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        _resign_manifest(bundle, self.private_key)
        with self.assertRaisesRegex(BundleVerificationError, "unsafe manifest file path"):
            verify_business_directory(bundle, self.public_key)

    def test_archive_path_traversal_and_duplicates_are_rejected(self) -> None:
        traversal = self.root / "traversal.zip"
        with zipfile.ZipFile(traversal, "w") as archive:
            archive.writestr("business/../outside.py", "bad")
        with self.assertRaisesRegex(BundleVerificationError, "unsafe archive member"):
            verify_business_archive(traversal, self.public_key)

        duplicate = self.root / "duplicate.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "w") as archive:
                archive.writestr("business/bundle_entry.py", "one")
                archive.writestr("business/bundle_entry.py", "two")
        with self.assertRaisesRegex(BundleVerificationError, "duplicate archive member"):
            verify_business_archive(duplicate, self.public_key)

        symlink = self.root / "symlink.zip"
        link_info = zipfile.ZipInfo("business/link.py")
        link_info.create_system = 3
        link_info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(symlink, "w") as archive:
            archive.writestr(link_info, "../outside.py")
        with self.assertRaisesRegex(BundleVerificationError, "archive member is a symlink"):
            verify_business_archive(symlink, self.public_key)

    def test_zip_bomb_ratio_is_rejected_and_staging_is_removed(self) -> None:
        bomb = self.root / "bomb.zip"
        with zipfile.ZipFile(bomb, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("business/padding.bin", b"0" * (2 * 1024 * 1024))
        destination = self.root / "bomb-staging"
        from runtime_v4.business_bundle import extract_verified_business_archive

        with self.assertRaisesRegex(BundleVerificationError, "compression ratio is unsafe"):
            extract_verified_business_archive(bomb, destination, self.public_key)
        self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
