from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4.core_manifest import (
    CoreVerificationError,
    build_core_manifest,
    verify_core_directory,
)


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


def _resign(core: Path, private_path: Path) -> None:
    private = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    manifest_bytes = (core / "core_manifest.json").read_bytes()
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
    (core / "core_manifest.sig").write_text(
        json.dumps(signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


class RuntimeV4CoreManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key, self.public_key = _write_keypair(self.root)
        self.core = self.root / "core"
        (self.core / "_internal").mkdir(parents=True)
        (self.core / "LiveClipperHost.exe").write_bytes(b"stable-host")
        (self.core / "_internal" / "runtime.bin").write_bytes(b"stable-runtime")
        self.result = build_core_manifest(
            self.core,
            core_version="4.0.0",
            private_key_path=self.private_key,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_complete_core_verifies_and_rebuild_is_deterministic(self) -> None:
        verified = verify_core_directory(
            self.core,
            self.public_key,
            expected_version="4.0.0",
        )
        first_manifest = (self.core / "core_manifest.json").read_bytes()
        first_signature = (self.core / "core_manifest.sig").read_bytes()
        second = build_core_manifest(
            self.core,
            core_version="4.0.0",
            private_key_path=self.private_key,
        )
        self.assertEqual(verified.entrypoint_path, "LiveClipperHost.exe")
        self.assertEqual(first_manifest, (self.core / "core_manifest.json").read_bytes())
        self.assertEqual(first_signature, (self.core / "core_manifest.sig").read_bytes())
        self.assertEqual(self.result["manifest_sha256"], second["manifest_sha256"])

    def test_tampered_missing_and_extra_core_files_are_rejected(self) -> None:
        (self.core / "LiveClipperHost.exe").write_bytes(b"tampered-host")
        with self.assertRaisesRegex(CoreVerificationError, "size mismatch|digest mismatch"):
            verify_core_directory(self.core, self.public_key)

        build_core_manifest(
            self.core,
            core_version="4.0.0",
            private_key_path=self.private_key,
        )
        (self.core / "_internal" / "runtime.bin").unlink()
        with self.assertRaisesRegex(CoreVerificationError, "missing or unsafe"):
            verify_core_directory(self.core, self.public_key)

        (self.core / "_internal" / "runtime.bin").write_bytes(b"stable-runtime")
        build_core_manifest(
            self.core,
            core_version="4.0.0",
            private_key_path=self.private_key,
        )
        (self.core / "unlisted.dll").write_bytes(b"extra")
        with self.assertRaisesRegex(CoreVerificationError, "file set mismatch"):
            verify_core_directory(self.core, self.public_key)

    def test_wrong_key_stale_version_and_resigned_traversal_are_rejected(self) -> None:
        _, wrong_public = _write_keypair(self.root, "wrong")
        with self.assertRaisesRegex(CoreVerificationError, "key id mismatch"):
            verify_core_directory(self.core, wrong_public)
        with self.assertRaisesRegex(CoreVerificationError, "version mismatch"):
            verify_core_directory(self.core, self.public_key, expected_version="4.0.1")

        manifest_path = self.core / "core_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"]["../outside.dll"] = manifest["files"].pop(
            "_internal/runtime.bin"
        )
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        _resign(self.core, self.private_key)
        with self.assertRaisesRegex(CoreVerificationError, "unsafe core manifest file path"):
            verify_core_directory(self.core, self.public_key)

    def test_symlink_is_rejected(self) -> None:
        outside = self.root / "outside.bin"
        outside.write_bytes(b"outside")
        link = self.core / "_internal" / "link.bin"
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")
        with self.assertRaisesRegex(CoreVerificationError, "cannot contain symlinks"):
            verify_core_directory(self.core, self.public_key)


if __name__ == "__main__":
    unittest.main()

