from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4.business_bundle import (
    build_business_archive,
    extract_verified_business_archive,
    verify_business_directory,
)
from runtime_v4.update_agent import (
    UpdateError,
    _update_lock,
    cleanup_download_cache,
    initialize_download_cache,
    install_business_archive,
    prune_business_versions,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
SAMPLE = ROOT / "tests" / "fixtures" / "runtime_v4_sample"


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


class RuntimeV4BusinessUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.install = Path(self.temporary.name)
        self.private_key, self.public_key = _write_keypair(self.install)
        updater = self.install / "updater"
        updater.mkdir()
        (updater / "release_update_public_key.pem").write_bytes(self.public_key.read_bytes())
        self.current_version = "2026.7.30.1"
        self.target_version = "2026.7.30.2"
        self.current_archive = self._build_archive(self.current_version)
        self.target_archive = self._build_archive(self.target_version)
        current_root = self.install / "versions" / self.current_version
        extract_verified_business_archive(
            self.current_archive,
            current_root,
            self.public_key,
            expected_version=self.current_version,
            expected_core_version="4.0.0",
        )
        self.state_path = self.install / "current.json"
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime_layout_version": 4,
                    "current": {
                        "application_version": self.current_version,
                        "core_version": "4.0.0",
                    },
                    "previous": None,
                    "pending": False,
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_archive(
        self,
        version: str,
        *,
        compatible_core: str = "4.0.0",
    ) -> Path:
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        policy["compatible_core_versions"] = [compatible_core]
        policy_path = self.install / f"policy-{version}-{compatible_core}.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        archive = self.install / f"business-{version}-{compatible_core}.zip"
        build_business_archive(
            SAMPLE,
            archive,
            application_version=version,
            private_key_path=self.private_key,
            policy_path=policy_path,
        )
        return archive

    def _state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_business_update_installs_and_atomically_switches_selection(self) -> None:
        result = install_business_archive(
            self.install,
            self.target_archive,
            application_version=self.target_version,
            public_key_path=self.public_key,
        )
        state = self._state()
        self.assertTrue(result.activated)
        self.assertFalse(result.already_installed)
        self.assertEqual(state["current"]["application_version"], self.target_version)
        self.assertEqual(state["current"]["core_version"], "4.0.0")
        self.assertEqual(state["previous"]["application_version"], self.current_version)
        self.assertTrue(state["pending"])
        verify_business_directory(
            result.business_root,
            self.public_key,
            expected_version=self.target_version,
            expected_core_version="4.0.0",
        )

    def test_failure_after_move_keeps_pointer_and_retry_is_idempotent(self) -> None:
        def fail(stage: str) -> None:
            if stage == "after_move":
                raise RuntimeError("simulated crash after move")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            install_business_archive(
                self.install,
                self.target_archive,
                application_version=self.target_version,
                public_key_path=self.public_key,
                fault_injector=fail,
            )
        self.assertEqual(
            self._state()["current"]["application_version"],
            self.current_version,
        )
        self.assertTrue(
            (self.install / "versions" / self.target_version / "business").is_dir()
        )

        retried = install_business_archive(
            self.install,
            self.target_archive,
            application_version=self.target_version,
            public_key_path=self.public_key,
        )
        self.assertTrue(retried.activated)
        self.assertTrue(retried.already_installed)
        self.assertEqual(
            self._state()["current"]["application_version"],
            self.target_version,
        )

    def test_failure_after_state_is_committed_and_safe_to_retry(self) -> None:
        def fail(stage: str) -> None:
            if stage == "after_state":
                raise RuntimeError("simulated caller loss after commit")

        with self.assertRaisesRegex(RuntimeError, "after commit"):
            install_business_archive(
                self.install,
                self.target_archive,
                application_version=self.target_version,
                public_key_path=self.public_key,
                fault_injector=fail,
            )
        self.assertEqual(
            self._state()["current"]["application_version"],
            self.target_version,
        )
        retried = install_business_archive(
            self.install,
            self.target_archive,
            application_version=self.target_version,
            public_key_path=self.public_key,
        )
        self.assertFalse(retried.activated)
        self.assertTrue(retried.already_installed)

    def test_tampered_or_incompatible_archive_changes_nothing(self) -> None:
        tampered = self.install / "tampered.zip"
        payload = bytearray(self.target_archive.read_bytes())
        payload[len(payload) // 2] ^= 0x01
        tampered.write_bytes(payload)
        with self.assertRaises(UpdateError):
            install_business_archive(
                self.install,
                tampered,
                application_version=self.target_version,
                public_key_path=self.public_key,
            )
        self.assertEqual(
            self._state()["current"]["application_version"],
            self.current_version,
        )

        incompatible_version = "2026.7.30.3"
        incompatible = self._build_archive(
            incompatible_version,
            compatible_core="4.1.0",
        )
        with self.assertRaisesRegex(UpdateError, "incompatible with core"):
            install_business_archive(
                self.install,
                incompatible,
                application_version=incompatible_version,
                public_key_path=self.public_key,
            )
        self.assertEqual(
            self._state()["current"]["application_version"],
            self.current_version,
        )

    def test_invalid_existing_target_is_never_activated(self) -> None:
        installed = install_business_archive(
            self.install,
            self.target_archive,
            application_version=self.target_version,
            public_key_path=self.public_key,
            activate=False,
        )
        (installed.business_root / "bundle_entry.py").write_text(
            "def create_application(context): return 'tampered'\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(UpdateError, "existing .* is invalid"):
            install_business_archive(
                self.install,
                self.target_archive,
                application_version=self.target_version,
                public_key_path=self.public_key,
            )
        self.assertEqual(
            self._state()["current"]["application_version"],
            self.current_version,
        )

    def test_existing_version_must_match_the_requested_archive_digest(self) -> None:
        install_business_archive(
            self.install,
            self.target_archive,
            application_version=self.target_version,
            public_key_path=self.public_key,
            activate=False,
        )
        alternate_source = self.install / "alternate-source"
        shutil.copytree(SAMPLE, alternate_source)
        entrypoint = alternate_source / "bundle_entry.py"
        entrypoint.write_text(
            entrypoint.read_text(encoding="utf-8") + "\nALTERNATE_BUILD = True\n",
            encoding="utf-8",
        )
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        alternate_policy = self.install / "alternate-policy.json"
        alternate_policy.write_text(json.dumps(policy), encoding="utf-8")
        alternate_archive = self.install / "alternate.zip"
        build_business_archive(
            alternate_source,
            alternate_archive,
            application_version=self.target_version,
            private_key_path=self.private_key,
            policy_path=alternate_policy,
        )
        with self.assertRaisesRegex(UpdateError, "does not match the requested archive"):
            install_business_archive(
                self.install,
                alternate_archive,
                application_version=self.target_version,
                public_key_path=self.public_key,
            )
        self.assertEqual(
            self._state()["current"]["application_version"],
            self.current_version,
        )

    def test_disk_preflight_and_update_lock_fail_closed(self) -> None:
        with patch(
            "runtime_v4.update_agent.shutil.disk_usage",
            return_value=SimpleNamespace(free=0),
        ):
            with self.assertRaisesRegex(UpdateError, "insufficient disk space"):
                install_business_archive(
                    self.install,
                    self.target_archive,
                    application_version=self.target_version,
                    public_key_path=self.public_key,
                )
        with _update_lock(self.install):
            with self.assertRaisesRegex(UpdateError, "already running"):
                install_business_archive(
                    self.install,
                    self.target_archive,
                    application_version=self.target_version,
                    public_key_path=self.public_key,
                )

    def test_retention_keeps_current_previous_and_one_recent_business_version(self) -> None:
        install_business_archive(
            self.install,
            self.target_archive,
            application_version=self.target_version,
            public_key_path=self.public_key,
        )
        for version in ("2026.7.29.8", "2026.7.29.9"):
            archive = self._build_archive(version)
            install_business_archive(
                self.install,
                archive,
                application_version=version,
                public_key_path=self.public_key,
                activate=False,
                expected_current_version=self.target_version,
                expected_core_version="4.0.0",
            )
            time.sleep(0.01)
        foreign = self.install / "versions" / "foreign"
        foreign.mkdir()
        (foreign / "do-not-delete.txt").write_text("foreign", encoding="utf-8")

        result = prune_business_versions(
            self.install,
            keep_recent_unreferenced=1,
        )
        self.assertEqual(result.removed_versions, ("2026.7.29.8",))
        self.assertTrue((self.install / "versions" / self.current_version).is_dir())
        self.assertTrue((self.install / "versions" / self.target_version).is_dir())
        self.assertTrue((self.install / "versions" / "2026.7.29.9").is_dir())
        self.assertTrue(foreign.is_dir())
        self.assertIn("foreign", result.skipped)

    def test_download_cache_retention_bounds_versions_and_stale_partials(self) -> None:
        root = self.install / "downloads"
        initialize_download_cache(root)
        versions = ["2026.7.29.1", "2026.7.29.2", "2026.7.29.3"]
        now = time.time()
        for index, version in enumerate(versions):
            directory = root / version
            directory.mkdir(parents=True)
            (directory / "bundle.zip").write_bytes(version.encode("ascii"))
            os.utime(directory, (now + index, now + index))
        stale = root / versions[-1] / "bundle.zip.part"
        stale.write_bytes(b"partial")
        old = now - 20 * 24 * 60 * 60
        os.utime(stale, (old, old))
        (root / "notes.txt").write_text("foreign", encoding="utf-8")

        result = cleanup_download_cache(
            root,
            keep_recent_directories=2,
            stale_partial_days=14,
        )
        self.assertFalse((root / versions[0]).exists())
        self.assertTrue((root / versions[1]).is_dir())
        self.assertTrue((root / versions[2]).is_dir())
        self.assertFalse(stale.exists())
        self.assertIn(versions[0], result.removed)
        self.assertIn(f"{versions[-1]}/bundle.zip.part", result.removed)
        self.assertIn("notes.txt", result.skipped)


if __name__ == "__main__":
    unittest.main()
