from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4.business_bundle import build_business_archive, extract_verified_business_archive
from runtime_v4.update_agent import UpdateError
from runtime_v4.update_channel import (
    UpdateChannelError,
    apply_signed_business_update,
    build_signed_update_channel,
    download_business_bundle,
    fetch_signed_update_channel,
    plan_business_update,
    verify_update_channel,
)
from runtime_v4.update_service import (
    RuntimeV4UpdateService,
    UpdateServiceError,
    load_update_source_config,
)
import runtime_v4.update_service as update_service_module


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
SAMPLE = ROOT / "tests" / "fixtures" / "runtime_v4_sample"


def _write_keypair(root: Path, name: str = "release") -> tuple[Path, Path]:
    private = Ed25519PrivateKey.generate()
    private_path = root / f"{name}-private.pem"
    public_path = root / f"{name}-public.pem"
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


class _Response:
    def __init__(
        self,
        payload: bytes,
        *,
        url: str,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._stream = io.BytesIO(payload)
        self._url = url
        self.status = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def getcode(self) -> int:
        return self.status


class RuntimeV4UpdateChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.private_key, self.public_key = _write_keypair(self.root)
        self.current_version = "2026.7.30.1"
        self.target_version = "2026.7.30.2"
        self.current_archive = self._build_bundle(self.current_version)
        self.target_archive = self._build_bundle(self.target_version)
        self.channel_path = self.root / "v4-stable.json"
        self.channel = self._build_channel()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _build_bundle(self, version: str) -> Path:
        archive = self.root / f"LiveClipperBusiness_{version}.zip"
        build_business_archive(
            SAMPLE,
            archive,
            application_version=version,
            private_key_path=self.private_key,
            policy_path=POLICY,
        )
        return archive

    def _build_channel(self, **overrides):
        options = {
            "application_version": self.target_version,
            "allowed_source_versions": [self.current_version],
            "compatible_core_versions": ["4.0.0"],
            "sources": [
                {"name": "Primary", "url": "https://updates.example.test/business.zip"},
                {"name": "Mirror", "url": "https://mirror.example.test/business.zip"},
            ],
            "private_key_path": self.private_key,
            "published_at": "2026-07-31 12:00:00",
        }
        options.update(overrides)
        return build_signed_update_channel(
            self.channel_path,
            self.target_archive,
            **options,
        )

    def _create_install(self) -> Path:
        install = self.root / "install"
        (install / "updater").mkdir(parents=True)
        (install / "updater" / "release_update_public_key.pem").write_bytes(
            self.public_key.read_bytes()
        )
        extract_verified_business_archive(
            self.current_archive,
            install / "versions" / self.current_version,
            self.public_key,
            expected_version=self.current_version,
            expected_core_version="4.0.0",
        )
        (install / "current.json").write_text(
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
        return install

    def _archive_opener(self, request, **_kwargs):
        range_value = request.get_header("Range") or ""
        offset = int(range_value.removeprefix("bytes=").removesuffix("-")) if range_value else 0
        payload = self.target_archive.read_bytes()[offset:]
        headers = {}
        status = 200
        if offset:
            status = 206
            headers["Content-Range"] = (
                f"bytes {offset}-{self.target_archive.stat().st_size - 1}/"
                f"{self.target_archive.stat().st_size}"
            )
        return _Response(payload, url=request.full_url, status=status, headers=headers)

    def test_signed_channel_round_trip_and_exact_update_plan(self) -> None:
        document = json.loads(self.channel_path.read_text(encoding="utf-8"))
        verified = verify_update_channel(document, self.public_key)
        self.assertEqual(verified, self.channel)
        decision = plan_business_update(
            verified,
            current_version=self.current_version,
            current_core_version="4.0.0",
        )
        self.assertTrue(decision.available)
        self.assertEqual(decision.reason, "update_available")
        self.assertEqual(verified.sha256, self._sha256(self.target_archive))

    def test_update_channel_stays_in_host_and_out_of_launcher_top_level(self) -> None:
        package_init = (ROOT / "runtime_v4" / "__init__.py").read_text(encoding="utf-8")
        host_spec = (ROOT / "runtime_v4" / "liveclipper_host_v4.spec").read_text(
            encoding="utf-8"
        )
        launcher_spec = (
            ROOT / "runtime_v4" / "liveclipper_launcher_v4.spec"
        ).read_text(encoding="utf-8")
        self.assertNotIn("update_channel", package_init)
        self.assertIn('"runtime_v4.update_channel"', host_spec)
        self.assertNotIn("update_channel", launcher_spec)

    def test_tamper_wrong_key_and_insecure_source_fail_closed(self) -> None:
        document = json.loads(self.channel_path.read_text(encoding="utf-8"))
        document["target"]["application_version"] = "2026.7.30.9"
        with self.assertRaisesRegex(UpdateChannelError, "signature verification failed"):
            verify_update_channel(document, self.public_key)

        _, wrong_public = _write_keypair(self.root, "wrong")
        original = json.loads(self.channel_path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(UpdateChannelError, "key id mismatch"):
            verify_update_channel(original, wrong_public)

        with self.assertRaisesRegex(UpdateChannelError, "HTTPS"):
            self._build_channel(
                sources=[{"name": "Bad", "url": "http://updates.example.test/business.zip"}]
            )

    def test_hold_route_core_and_version_decisions_do_not_offer_update(self) -> None:
        hold = self._build_channel(channel_status="hold")
        self.assertEqual(
            plan_business_update(
                hold,
                current_version=self.current_version,
                current_core_version="4.0.0",
            ).reason,
            "channel_hold",
        )
        self.assertEqual(
            plan_business_update(
                self.channel,
                current_version="2026.7.29.9",
                current_core_version="4.0.0",
            ).reason,
            "source_version_not_allowed",
        )
        self.assertEqual(
            plan_business_update(
                self.channel,
                current_version=self.current_version,
                current_core_version="4.1.0",
            ).reason,
            "core_incompatible",
        )
        self.assertEqual(
            plan_business_update(
                self.channel,
                current_version=self.target_version,
                current_core_version="4.0.0",
            ).reason,
            "up_to_date",
        )

    def test_download_uses_verified_cache_without_network(self) -> None:
        destination = self.root / "downloads" / self.channel.filename
        destination.parent.mkdir()
        shutil.copy2(self.target_archive, destination)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("network must not be used for a verified cache hit")

        result = download_business_bundle(self.channel, destination, opener=forbidden)
        self.assertEqual(result, destination.resolve())

    def test_download_resumes_partial_file_and_falls_back_to_mirror(self) -> None:
        destination = self.root / "downloads" / self.channel.filename
        destination.parent.mkdir()
        partial = destination.with_suffix(destination.suffix + ".part")
        archive_bytes = self.target_archive.read_bytes()
        partial.write_bytes(archive_bytes[:97])
        calls = []

        def opener(request, **_kwargs):
            calls.append((request.full_url, request.get_header("Range")))
            if "updates.example.test" in request.full_url:
                raise OSError("primary unavailable")
            return self._archive_opener(request)

        result = download_business_bundle(self.channel, destination, opener=opener)
        self.assertEqual(result.read_bytes(), archive_bytes)
        self.assertTrue(any(url.startswith("https://mirror") for url, _ in calls))
        self.assertTrue(any(value == "bytes=97-" for _, value in calls))

    def test_fetch_falls_back_and_rejects_insecure_redirects(self) -> None:
        channel_bytes = self.channel_path.read_bytes()
        calls = []

        def opener(request, **_kwargs):
            calls.append(request.full_url)
            if request.full_url.endswith("first.json"):
                return _Response(b"{}", url="http://redirect.example.test/v4.json")
            return _Response(channel_bytes, url=request.full_url)

        fetched = fetch_signed_update_channel(
            [
                "https://updates.example.test/first.json",
                "https://mirror.example.test/stable.json",
            ],
            self.public_key,
            opener=opener,
        )
        self.assertEqual(fetched.document_sha256, self.channel.document_sha256)
        self.assertEqual(len(calls), 2)

    def test_apply_downloads_verifies_and_atomically_activates(self) -> None:
        install = self._create_install()
        result = apply_signed_business_update(
            install,
            self.channel,
            self.root / "downloads",
            public_key_path=self.public_key,
            opener=self._archive_opener,
        )
        state = json.loads((install / "current.json").read_text(encoding="utf-8"))
        self.assertTrue(result.decision.available)
        self.assertTrue(result.install_result.activated)
        self.assertEqual(state["current"]["application_version"], self.target_version)
        self.assertEqual(state["previous"]["application_version"], self.current_version)
        self.assertTrue(state["pending"])

    def test_hash_manifest_and_state_race_fail_before_target_activation(self) -> None:
        install = self._create_install()
        original_state = (install / "current.json").read_bytes()
        corrupt = bytearray(self.target_archive.read_bytes())
        corrupt[len(corrupt) // 2] ^= 1

        def corrupt_opener(request, **_kwargs):
            return _Response(bytes(corrupt), url=request.full_url)

        with self.assertRaisesRegex(UpdateChannelError, "SHA256 mismatch"):
            apply_signed_business_update(
                install,
                self.channel,
                self.root / "bad-downloads",
                public_key_path=self.public_key,
                opener=corrupt_opener,
            )
        self.assertEqual((install / "current.json").read_bytes(), original_state)

        wrong_manifest = self.channel.__class__(
            **{
                **self.channel.__dict__,
                "bundle_manifest_sha256": "0" * 64,
            }
        )
        with self.assertRaisesRegex(UpdateError, "manifest does not match"):
            apply_signed_business_update(
                install,
                wrong_manifest,
                self.root / "downloads",
                public_key_path=self.public_key,
                opener=self._archive_opener,
            )
        self.assertEqual((install / "current.json").read_bytes(), original_state)

        def change_state_before_install(*args, **kwargs):
            destination = Path(args[1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.target_archive, destination)
            state = json.loads((install / "current.json").read_text(encoding="utf-8"))
            state["current"]["application_version"] = "2026.7.30.0"
            (install / "current.json").write_text(json.dumps(state), encoding="utf-8")
            return destination

        with patch(
            "runtime_v4.update_channel.download_business_bundle",
            side_effect=change_state_before_install,
        ):
            with self.assertRaisesRegex(UpdateError, "current application changed"):
                apply_signed_business_update(
                    install,
                    self.channel,
                    self.root / "race-downloads",
                    public_key_path=self.public_key,
                )
        state = json.loads((install / "current.json").read_text(encoding="utf-8"))
        self.assertNotEqual(state["current"]["application_version"], self.target_version)

    def test_host_update_service_checks_applies_cleans_and_requests_restart(self) -> None:
        install = self._create_install()
        restarted = []

        def opener(request, **kwargs):
            if request.full_url.endswith("stable.json"):
                return _Response(self.channel_path.read_bytes(), url=request.full_url)
            return self._archive_opener(request, **kwargs)

        service = RuntimeV4UpdateService(
            install,
            self.public_key,
            ["https://updates.example.test/stable.json"],
            download_root=self.root / "service-downloads",
            restart_callback=lambda: restarted.append(True) is None,
            opener=opener,
        )
        checked = service.check_update()
        self.assertTrue(checked["update_available"])
        self.assertEqual(checked["update"]["update_strategy"], "v4-signed-business-bundle")
        self.assertFalse(checked["update"]["requires_full_package"])

        progress = []
        applied = service.apply_update(lambda done, total, message: progress.append(message))
        state = json.loads((install / "current.json").read_text(encoding="utf-8"))
        self.assertTrue(applied["ok"])
        self.assertTrue(applied["restart_required"])
        self.assertEqual(state["current"]["application_version"], self.target_version)
        self.assertTrue(any("下载" in message for message in progress))
        self.assertFalse(
            (self.root / "service-downloads" / self.target_version / self.channel.filename).exists()
        )
        self.assertTrue(service.schedule_restart())
        self.assertEqual(restarted, [True])

    def test_host_service_fails_closed_when_channel_is_unconfigured_or_busy(self) -> None:
        install = self._create_install()
        service = RuntimeV4UpdateService(
            install,
            self.public_key,
            [],
            download_root=self.root / "downloads",
        )
        checked = service.check_update()
        self.assertFalse(checked["update_available"])
        self.assertEqual(checked["reason"], "channel_not_configured")
        with self.assertRaisesRegex(UpdateServiceError, "尚未配置"):
            service.apply_update()

        configured = RuntimeV4UpdateService(
            install,
            self.public_key,
            ["https://updates.example.test/stable.json"],
            download_root=self.root / "downloads",
        )
        configured._apply_lock.acquire()
        try:
            with self.assertRaisesRegex(UpdateServiceError, "正在运行"):
                configured.apply_update()
        finally:
            configured._apply_lock.release()

        process_lock = update_service_module._acquire_service_update_lock(install)
        try:
            with self.assertRaisesRegex(UpdateServiceError, "另一个 V4 更新进程"):
                configured.apply_update()
        finally:
            update_service_module._release_service_update_lock(process_lock)

    def test_update_source_config_is_core_owned_and_https_only(self) -> None:
        config = self.root / "sources.json"
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime_layout_version": 4,
                    "channel": "stable",
                    "urls": ["https://updates.example.test/stable.json"],
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(
            load_update_source_config(config),
            ("https://updates.example.test/stable.json",),
        )
        document = json.loads(config.read_text(encoding="utf-8"))
        document["urls"] = ["http://updates.example.test/stable.json"]
        config.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(UpdateServiceError, "unsafe URL"):
            load_update_source_config(config)

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
