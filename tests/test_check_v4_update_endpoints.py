from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4.business_bundle import build_business_archive
from runtime_v4.update_channel import build_signed_update_channel


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
SAMPLE = ROOT / "tests" / "fixtures" / "runtime_v4_sample"
TOOL_PATH = ROOT / "tools" / "check_v4_update_endpoints.py"

spec = importlib.util.spec_from_file_location("check_v4_update_endpoints", TOOL_PATH)
assert spec is not None and spec.loader is not None
endpoint_checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(endpoint_checker)


class _Response:
    def __init__(self, payload: bytes, url: str) -> None:
        self._payload = payload
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._payload

    def geturl(self) -> str:
        return self._url


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


class RuntimeV4EndpointCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key, self.public_key = _write_keypair(self.root)
        self.archive = self.root / "bundle.zip"
        build_business_archive(
            SAMPLE,
            self.archive,
            application_version="2026.8.9.1",
            private_key_path=self.private_key,
            policy_path=POLICY,
        )
        self.channel_path = self.root / "stable.json"
        build_signed_update_channel(
            self.channel_path,
            self.archive,
            application_version="2026.8.9.1",
            allowed_source_versions=["2026.8.7.1"],
            compatible_core_versions=["4.0.0"],
            sources=[
                {"name": "primary", "url": "https://updates.example/bundle.zip"},
                {"name": "backup", "url": "https://mirror.example/bundle.zip"},
            ],
            private_key_path=self.private_key,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _opener(self, payloads: dict[str, bytes]):
        def open_url(request, **_kwargs):
            return _Response(payloads[request.full_url], request.full_url)

        return open_url

    def test_all_channel_and_business_mirrors_must_match(self) -> None:
        channel = self.channel_path.read_bytes()
        archive = self.archive.read_bytes()
        report = endpoint_checker.check_endpoints(
            ["https://updates.example/stable.json", "https://mirror.example/stable.json"],
            self.public_key,
            opener=self._opener(
                {
                    "https://updates.example/stable.json": channel,
                    "https://mirror.example/stable.json": channel,
                    "https://updates.example/bundle.zip": archive,
                    "https://mirror.example/bundle.zip": archive,
                }
            ),
        )
        self.assertTrue(report["ok"])
        self.assertEqual(len(report["endpoints"]), 2)
        self.assertEqual(len(report["archives"]), 2)

    def test_mismatched_mirror_fails_the_release_check(self) -> None:
        channel = self.channel_path.read_bytes()
        archive = self.archive.read_bytes()
        report = endpoint_checker.check_endpoints(
            ["https://updates.example/stable.json", "https://mirror.example/stable.json"],
            self.public_key,
            opener=self._opener(
                {
                    "https://updates.example/stable.json": channel,
                    "https://mirror.example/stable.json": channel[:-1] + b"0",
                    "https://updates.example/bundle.zip": archive,
                    "https://mirror.example/bundle.zip": archive,
                }
            ),
        )
        self.assertFalse(report["ok"])
        self.assertTrue(any("channel endpoint" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()
