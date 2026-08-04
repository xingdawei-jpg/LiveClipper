from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.release_signing import generate_keypair, sha256_file, sign_manifest
from runtime_v4.core_manifest import build_core_manifest
from runtime_v4.migration import (
    MigrationError,
    assemble_core_from_v3,
    build_core_bridge,
    inspect_v3_install,
)


class RuntimeV4MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key = self.root / "private.pem"
        self.public_key = self.root / "public.pem"
        generate_keypair(self.private_key, self.public_key)
        self.legacy_root = self.root / "legacy"
        self.legacy_runtime = self.legacy_root / "versions" / "2026.7.27.5"
        (self.legacy_runtime / "_internal").mkdir(parents=True)
        (self.legacy_runtime / "LiveClipperWeb.exe").write_bytes(b"old-runtime")
        (self.legacy_runtime / "_internal" / "shared.bin").write_bytes(b"shared")
        (self.legacy_runtime / "_internal" / "changed.bin").write_bytes(b"old")
        self._write_v3_manifest()
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

        self.target_core = self.root / "target-core"
        (self.target_core / "_internal").mkdir(parents=True)
        (self.target_core / "LiveClipperHost.exe").write_bytes(b"new-host")
        (self.target_core / "_internal" / "shared.bin").write_bytes(b"shared")
        (self.target_core / "_internal" / "changed.bin").write_bytes(b"new")
        build_core_manifest(
            self.target_core,
            core_version="4.0.0",
            private_key_path=self.private_key,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_v3_manifest(self) -> None:
        files = {}
        for path in sorted(self.legacy_runtime.rglob("*")):
            if path.is_file() and path.name != "runtime_manifest.json":
                relative = path.relative_to(self.legacy_runtime).as_posix()
                files[relative] = {"sha256": sha256_file(path), "size": path.stat().st_size}
        manifest = sign_manifest(
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
        (self.legacy_runtime / "runtime_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def _bridge(self) -> tuple[Path, object]:
        legacy = inspect_v3_install(self.legacy_root, self.public_key)
        bridge = self.root / "bridge"
        plan = build_core_bridge(legacy, self.target_core, self.public_key, bridge)
        return bridge, plan

    def test_bridge_reuses_matching_signed_v3_files_and_copies_only_changes(self) -> None:
        bridge, plan = self._bridge()
        self.assertEqual(plan.target_files, 3)
        self.assertEqual(plan.reusable_files, 1)
        self.assertEqual(
            set(plan.payload_files),
            {"LiveClipperHost.exe", "_internal/changed.bin"},
        )
        self.assertFalse((bridge / "payload" / "_internal" / "shared.bin").exists())

        legacy = inspect_v3_install(self.legacy_root, self.public_key)
        destination = self.root / "installed-core"
        result = assemble_core_from_v3(
            legacy,
            bridge,
            destination,
            self.public_key,
            expected_core_version="4.0.0",
        )
        self.assertEqual(result.hardlinked_files, 1)
        self.assertEqual(result.payload_files, 2)
        self.assertTrue(
            os.path.samefile(
                self.legacy_runtime / "_internal" / "shared.bin",
                destination / "_internal" / "shared.bin",
            )
        )
        self.assertEqual((destination / "_internal" / "changed.bin").read_bytes(), b"new")

    def test_tampered_reused_file_fails_final_verification_and_removes_destination(self) -> None:
        bridge, _ = self._bridge()
        shared = self.legacy_runtime / "_internal" / "shared.bin"
        shared.write_bytes(b"xxxxxx")
        destination = self.root / "bad-core"
        with self.assertRaises(MigrationError):
            assemble_core_from_v3(
                inspect_v3_install(self.legacy_root, self.public_key),
                bridge,
                destination,
                self.public_key,
            )
        self.assertFalse(destination.exists())

    def test_tampered_bridge_payload_is_rejected_before_activation(self) -> None:
        bridge, _ = self._bridge()
        payload = bridge / "payload" / "_internal" / "changed.bin"
        payload.write_bytes(b"bad")
        destination = self.root / "bad-payload-core"
        with self.assertRaisesRegex(MigrationError, "payload digest mismatch"):
            assemble_core_from_v3(
                inspect_v3_install(self.legacy_root, self.public_key),
                bridge,
                destination,
                self.public_key,
            )
        self.assertFalse(destination.exists())

    def test_wrong_key_and_wrong_source_version_fail_closed(self) -> None:
        wrong_private = self.root / "wrong-private.pem"
        wrong_public = self.root / "wrong-public.pem"
        generate_keypair(wrong_private, wrong_public)
        with self.assertRaisesRegex(MigrationError, "signature verification failed"):
            inspect_v3_install(self.legacy_root, wrong_public)
        with self.assertRaisesRegex(MigrationError, "version mismatch"):
            inspect_v3_install(
                self.legacy_root,
                self.public_key,
                expected_version="2026.7.30.1",
            )

    def test_existing_destination_is_never_modified(self) -> None:
        bridge, _ = self._bridge()
        destination = self.root / "existing-core"
        destination.mkdir()
        marker = destination / "keep.txt"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "already exists"):
            assemble_core_from_v3(
                inspect_v3_install(self.legacy_root, self.public_key),
                bridge,
                destination,
                self.public_key,
            )
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
