from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4 import launcher
from runtime_v4.business_bundle import build_business_archive
from runtime_v4.core_manifest import build_core_manifest


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
SAMPLE = ROOT / "tests" / "fixtures" / "runtime_v4_sample"


class _FakeProcess:
    def __init__(self, code: int | None = None) -> None:
        self.code = code
        self.terminated = False

    def poll(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        self.code = 0

    def wait(self, timeout: float | None = None) -> int:
        return int(self.code or 0)

    def kill(self) -> None:
        self.terminated = True
        self.code = -9


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


class RuntimeV4LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.install = Path(self.temporary.name)
        self.private_key, public_key = _write_keypair(self.install)
        updater = self.install / "updater"
        updater.mkdir()
        (updater / "release_update_public_key.pem").write_bytes(public_key.read_bytes())
        self.previous = launcher.RuntimeSelection("2026.7.29.4", "4.0.0")
        self.current = launcher.RuntimeSelection("2026.7.30.1", "4.0.1")
        self._install_selection(self.previous)
        self._install_selection(self.current)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _install_selection(self, selection: launcher.RuntimeSelection) -> None:
        core = self.install / "core" / selection.core_version
        (core / "_internal").mkdir(parents=True, exist_ok=True)
        (core / "LiveClipperHost.exe").write_bytes(
            f"host-{selection.core_version}".encode("ascii")
        )
        (core / "_internal" / "runtime.bin").write_bytes(
            f"runtime-{selection.core_version}".encode("ascii")
        )
        build_core_manifest(
            core,
            core_version=selection.core_version,
            private_key_path=self.private_key,
        )

        archive = self.install / f"business-{selection.application_version}.zip"
        policy = json.loads(POLICY.read_text(encoding="utf-8"))
        policy["compatible_core_versions"] = [selection.core_version]
        policy_path = self.install / f"policy-{selection.application_version}.json"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        build_business_archive(
            SAMPLE,
            archive,
            application_version=selection.application_version,
            private_key_path=self.private_key,
            policy_path=policy_path,
        )
        destination = self.install / "versions" / selection.application_version
        destination.mkdir(parents=True)
        with zipfile.ZipFile(archive, "r") as bundle:
            bundle.extractall(destination)
        archive.unlink()

    def _write_state(self, *, pending: bool = True) -> Path:
        state_path = self.install / "current.json"
        state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "runtime_layout_version": 4,
                    "current": self.current.as_dict(),
                    "previous": self.previous.as_dict(),
                    "pending": pending,
                }
            ),
            encoding="utf-8",
        )
        return state_path

    def test_selection_validates_both_signed_layers(self) -> None:
        validated = launcher._validated_selection(self.install, self.current)
        self.assertEqual(validated.core.core_version, "4.0.1")
        self.assertEqual(validated.business.application_version, "2026.7.30.1")

    def test_selection_repairs_unlisted_legacy_run_log_before_validation(self) -> None:
        log_dir = (
            self.install
            / "versions"
            / self.current.application_version
            / "business"
            / "app"
            / "logs"
        )
        log_dir.mkdir(parents=True)
        legacy_log = log_dir / "20260801_173445_sample_\u6210\u529f.json"
        legacy_log.write_text("{}", encoding="utf-8")

        appdata = self.install / "appdata"
        with patch.dict(os.environ, {"APPDATA": str(appdata)}):
            validated = launcher._validated_selection(self.install, self.current)

        self.assertEqual(validated.business.application_version, "2026.7.30.1")
        self.assertFalse(legacy_log.exists())
        recovered = (
            appdata
            / "LiveClipper"
            / "recovered_logs"
            / "v4"
            / self.current.application_version
            / "app"
            / "logs"
            / legacy_log.name
        )
        self.assertTrue(recovered.is_file())

    def test_frozen_launcher_uses_only_its_embedded_release_key(self) -> None:
        embedded_root = self.install / "embedded"
        embedded_key = embedded_root / "core_keys" / "release_update_public_key.pem"
        embedded_key.parent.mkdir(parents=True)
        embedded_key.write_bytes(
            (self.install / "updater" / "release_update_public_key.pem").read_bytes()
        )
        external_key = self.install / "updater" / "release_update_public_key.pem"
        external_key.write_bytes(b"replaced external key")
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "_MEIPASS", str(embedded_root), create=True),
        ):
            selected = launcher._public_key_path(self.install)
        self.assertEqual(selected, embedded_key.resolve())

    def test_launch_scrubs_inherited_runtime_environment(self) -> None:
        validated = launcher._validated_selection(self.install, self.current)
        inherited = {name: "stale" for name in launcher.RUNTIME_OWNED_ENV}
        with (
            patch.dict(os.environ, inherited, clear=False),
            patch.object(launcher.subprocess, "Popen", return_value=_FakeProcess()) as spawn,
        ):
            launcher._launch(self.install, validated)
        child_env = spawn.call_args.kwargs["env"]
        self.assertEqual(child_env["LIVECLIPPER_RUNTIME_LAYOUT"], "4")
        self.assertEqual(child_env["LIVECLIPPER_V4_CORE_VERSION"], "4.0.1")
        self.assertEqual(
            child_env["LIVECLIPPER_V4_CORE_MANIFEST_SHA256"],
            validated.core.manifest_sha256,
        )
        self.assertEqual(
            child_env["LIVECLIPPER_V4_BUNDLE_MANIFEST_SHA256"],
            validated.business.manifest_sha256,
        )
        self.assertNotIn("LIVECLIPPER_BUNDLE_DIR", child_env)
        self.assertNotIn("LIVECLIPPER_V4_BUNDLE_VERIFIED", child_env)

    def test_exact_health_receipt_confirms_pending_selection(self) -> None:
        state_path = self._write_state()
        process = _FakeProcess()
        with (
            patch.object(launcher, "_launch", return_value=process),
            patch.object(launcher, "_wait_for_health", return_value=(True, "")),
        ):
            result = launcher.run(self.install, health_timeout=1.0)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertFalse(state["pending"])
        self.assertEqual(state["current"], self.current.as_dict())

    def test_confirmed_selection_reuses_matching_full_core_receipt(self) -> None:
        state_path = self._write_state(pending=False)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        fully_verified = launcher._validated_selection(self.install, self.current)
        launcher._remember_verified_core(state, fully_verified.core)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        with (
            patch.object(
                launcher,
                "verify_core_directory",
                wraps=launcher.verify_core_directory,
            ) as verify_core,
            patch.object(launcher, "_launch", return_value=_FakeProcess()),
            patch.object(launcher, "_wait_for_health", return_value=(True, "")),
        ):
            result = launcher.run(self.install)
        self.assertEqual(result, 0)
        self.assertEqual(
            [call.kwargs["hash_mode"] for call in verify_core.call_args_list],
            ["entrypoint"],
        )

    def test_changed_internal_core_requires_runtime_health_and_rolls_back(self) -> None:
        state_path = self._write_state(pending=False)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        fully_verified = launcher._validated_selection(self.install, self.current)
        launcher._remember_verified_core(state, fully_verified.core)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        runtime_file = (
            self.install
            / "core"
            / self.current.core_version
            / "_internal"
            / "runtime.bin"
        )
        runtime_file.write_bytes(b"x" * runtime_file.stat().st_size)
        with (
            patch.object(launcher, "_launch", return_value=_FakeProcess()) as launch_process,
            patch.object(
                launcher,
                "_wait_for_health",
                return_value=(False, "simulated runtime health failure"),
            ),
        ):
            result = launcher.run(self.install)
        rolled_back = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertEqual(rolled_back["current"], self.previous.as_dict())
        self.assertEqual(launch_process.call_count, 2)
        self.assertIn("simulated runtime health failure", rolled_back["rollback_reason"])

    def test_unhealthy_first_launch_rolls_back_the_selection_pair(self) -> None:
        state_path = self._write_state()
        failed_process = _FakeProcess()
        restored_process = _FakeProcess()
        with (
            patch.object(
                launcher,
                "_launch",
                side_effect=[failed_process, restored_process],
            ) as launch_process,
            patch.object(
                launcher,
                "_wait_for_health",
                return_value=(False, "simulated health failure"),
            ),
        ):
            result = launcher.run(self.install, health_timeout=1.0)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertTrue(failed_process.terminated)
        self.assertEqual(state["current"], self.previous.as_dict())
        self.assertEqual(state["previous"], self.current.as_dict())
        self.assertEqual(state["failed"], self.current.as_dict())
        self.assertFalse(state["pending"])
        self.assertEqual(launch_process.call_count, 2)

    def test_tampered_current_core_rolls_back_before_execution(self) -> None:
        state_path = self._write_state()
        current_host = (
            self.install / "core" / self.current.core_version / "LiveClipperHost.exe"
        )
        current_host.write_bytes(b"tampered")
        with patch.object(launcher, "_launch", return_value=_FakeProcess()) as launch_process:
            result = launcher.run(self.install, validate_only=True)
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(result, 2)
        self.assertEqual(state["current"], self.previous.as_dict())
        self.assertEqual(launch_process.call_count, 0)

    def test_health_receipt_rejects_wrong_core_or_bundle_identity(self) -> None:
        validated = launcher._validated_selection(self.install, self.current)
        health_file = self.install / "health.json"
        health_file.write_text(
            json.dumps(
                {
                    "token": "token",
                    "version": self.current.application_version,
                    "runtime_layout_version": 4,
                    "core_version": "4.0.0",
                    "core_manifest_sha256": validated.core.manifest_sha256,
                    "bundle_manifest_sha256": validated.business.manifest_sha256,
                    "runtime_integrity_ok": True,
                }
            ),
            encoding="utf-8",
        )
        healthy, reason = launcher._wait_for_health(
            _FakeProcess(code=7),
            health_file,
            "token",
            validated,
            0.5,
        )
        self.assertFalse(healthy)
        self.assertIn("code 7", reason)


if __name__ == "__main__":
    unittest.main()
