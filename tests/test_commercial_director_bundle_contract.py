"""Regression coverage for the Commercial Director preview's V4 bundle boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from runtime_v4.business_bundle import (
    _collect_source_files,
    _load_policy,
    build_business_archive,
    extract_verified_business_archive,
    verify_business_directory,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "release" / "runtime_v4_business_policy.json"
TEST_VERSION = "2026.9.4.1"
COMMERCIAL_DIRECTOR_RUNNERS = (
    "run_commercial_asset_ledger_audit.py",
    "run_m1_asset_aware_goldens.py",
    "run_m1_story_goldens.py",
    "run_m2_story_consumption_validation.py",
    "run_m2_story_goldens.py",
    "run_m3_golden_source_identity.py",
    "run_m3_new_golden_plan_fidelity.py",
)


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


class CommercialDirectorBundleContractTests(unittest.TestCase):
    def test_policy_contains_the_complete_runtime_runner_closure(self) -> None:
        selected = _collect_source_files(ROOT, _load_policy(POLICY))
        self.assertTrue(set(COMMERCIAL_DIRECTOR_RUNNERS).issubset(selected))

    def test_extracted_business_bundle_imports_preview_runner_without_source_tree(self) -> None:
        with tempfile.TemporaryDirectory(prefix="liveclipper-director-bundle-") as temporary:
            temporary_root = Path(temporary)
            private_key, public_key = _write_keypair(temporary_root)
            archive = temporary_root / "business.zip"
            build_business_archive(
                ROOT,
                archive,
                application_version=TEST_VERSION,
                private_key_path=private_key,
                policy_path=POLICY,
            )
            verified = extract_verified_business_archive(
                archive,
                temporary_root / "extracted",
                public_key,
                expected_version=TEST_VERSION,
                expected_core_version="4.0.0",
            )
            script = """
import importlib
import sys
from pathlib import Path

bundle_root = Path(sys.argv[1]).resolve()
source_root = Path(sys.argv[2]).resolve()
sys.path[:] = [
    str(bundle_root / 'app'),
    str(bundle_root / 'web_client'),
    str(bundle_root),
] + [
    entry for entry in sys.path
    if Path(entry or '.').resolve() != source_root
]
module = importlib.import_module('run_m3_new_golden_plan_fidelity')
module_path = Path(module.__file__).resolve()
if bundle_root not in module_path.parents:
    raise SystemExit(f'runner imported outside the verified bundle: {module_path}')
if not callable(module._run_case):
    raise SystemExit('preview runner does not expose _run_case')
"""
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [sys.executable, "-c", script, str(verified.root), str(ROOT)],
                cwd=temporary_root,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"bundle-only runner import failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )

    def test_bundled_commercial_director_workspace_is_outside_signed_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="liveclipper-director-workspace-") as temporary:
            temporary_root = Path(temporary)
            private_key, public_key = _write_keypair(temporary_root)
            archive = temporary_root / "business.zip"
            build_business_archive(
                ROOT,
                archive,
                application_version=TEST_VERSION,
                private_key_path=private_key,
                policy_path=POLICY,
            )
            verified = extract_verified_business_archive(
                archive,
                temporary_root / "extracted",
                public_key,
                expected_version=TEST_VERSION,
                expected_core_version="4.0.0",
            )
            appdata = temporary_root / "appdata"
            script = """
import importlib
import os
import sys
from pathlib import Path

sys.dont_write_bytecode = True
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
bundle_root = Path(sys.argv[1]).resolve()
source_root = Path(sys.argv[2]).resolve()
appdata = Path(sys.argv[3]).resolve()
os.environ['APPDATA'] = str(appdata)
os.environ['LIVECLIPPER_BUNDLE_DIR'] = str(bundle_root)
os.environ['LIVECLIPPER_V4_BUNDLE_VERIFIED'] = '1'
os.environ['LIVECLIPPER_RUNTIME_LAYOUT'] = '4'
sys.path[:] = [
    str(bundle_root / 'app'),
    str(bundle_root / 'web_client'),
    str(bundle_root),
] + [
    entry for entry in sys.path
    if Path(entry or '.').resolve() != source_root
]
server = importlib.import_module('server')
workspace = server._commerce_director_workspace_root()
expected = (appdata / 'LiveClipper' / 'workspace' / 'ui_commerce_director_experiment').resolve()
if workspace != expected:
    raise SystemExit(f'wrong bundled workspace: {workspace}')
if bundle_root in workspace.parents:
    raise SystemExit(f'workspace is inside signed bundle: {workspace}')
(workspace / 'runtime-artifact.json').write_text('{}', encoding='utf-8')
"""
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(verified.root),
                    str(ROOT),
                    str(appdata),
                ],
                cwd=temporary_root,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                check=False,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=f"bundled workspace placement failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            verify_business_directory(
                verified.root,
                public_key,
                expected_version=TEST_VERSION,
                expected_core_version="4.0.0",
            )


if __name__ == "__main__":
    unittest.main()
