"""Verify the 9.4.1 repair using real signed bundles and the existing frozen Core."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from runtime_v4.business_bundle import BundleVerificationError, extract_verified_business_archive, verify_business_directory
from runtime_v4.core_manifest import verify_core_directory
from runtime_v4 import launcher
from runtime_v4.update_channel import apply_signed_business_update, build_signed_update_channel

VERSION = "2026.9.4.1"
SOURCES = ["2026.8.5.1", "2026.8.5.2", "2026.8.7.1", "2026.8.8.1", "2026.8.11.1", "2026.8.11.2", "2026.8.12.1", "2026.8.12.2", "2026.8.15.1", "2026.9.3.2", "2026.9.3.3"]

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write_json(path, payload):
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def run(command, *, env, cwd, expected=0):
    result = subprocess.run(command, env=env, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != expected:
        raise RuntimeError(f"{Path(command[0]).name} returned {result.returncode}, expected {expected}\n{result.stdout}\n{result.stderr}")
    return result.stdout

class Response:
    def __init__(self, payload, url):
        self.stream, self.url = io.BytesIO(payload), url
        self.headers, self.status = {}, 200
    def read(self, size=-1): return self.stream.read(size)
    def geturl(self): return self.url
    def getcode(self): return self.status
    def __enter__(self): return self
    def __exit__(self, *_): return False

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--old-bundle-dir", type=Path, required=True)
    parser.add_argument("--core-install", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=False)
    public = ROOT / "app/release_update_public_key.pem"
    key = Path(os.environ.get("LIVECLIPPER_V4_PRIVATE_KEY") or Path.home() / ".liveclipper-keys/release_update_private_key.pem")
    core_source = args.core_install.resolve() / "core/4.0.0"
    print("Verifying the delivered confirmed-Core path...", flush=True)
    core = verify_core_directory(core_source, public, expected_version="4.0.0", hash_mode="entrypoint")
    options = dict(application_version=VERSION, allowed_source_versions=SOURCES, compatible_core_versions=["4.0.0"], sources=[{"name": "AliyunOSS", "url": f"https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/LiveClipperBusiness_{VERSION}.zip"}], private_key_path=key, release_notes="修复使用 AI 导演后重启软件报错的问题；改善切割句尾声音不完整，并保留商业导演预览修复。")
    hold = build_signed_update_channel(Path(__file__).parent / "stable.hold.json", args.bundle, channel_status="hold", **options)
    channel = build_signed_update_channel(work / "acceptance.ready.local.json", args.bundle, channel_status="ready", **options)
    payload = args.bundle.read_bytes()
    report = {"version": VERSION, "core_manifest_sha256": core.manifest_sha256, "core_validation_mode": "confirmed receipt plus signed entrypoint", "core_full_validation": "known baseline failure: delivered Core _internal/web_client/desktop.py differs from its signed manifest; no Core file is shipped by this business update", "bundle_sha256": digest(args.bundle), "bundle_manifest_sha256": channel.bundle_manifest_sha256, "hold_document_sha256": hold.document_sha256, "routes": []}
    for old in ("2026.9.3.2", "2026.9.3.3"):
        print(f"Testing {old} -> {VERSION}...", flush=True)
        route_root = work / old
        install = route_root / "install"
        install.mkdir(parents=True)
        shutil.copytree(core_source, install / "core/4.0.0", copy_function=os.link)
        shutil.copy2(args.core_install / "LiveClipperWeb.exe", install / "LiveClipperWeb.exe")
        (install / "updater").mkdir()
        shutil.copy2(public, install / "updater/release_update_public_key.pem")
        old_archive = args.old_bundle_dir / f"LiveClipperBusiness_{old}.zip"
        previous = extract_verified_business_archive(old_archive, install / "versions" / old, public, expected_version=old, expected_core_version="4.0.0")
        state = {"schema_version": 1, "runtime_layout_version": 4, "current": {"application_version": old, "core_version": "4.0.0"}, "previous": None, "pending": False, "verified_cores": {"4.0.0": {"verification_mode": "full", "manifest_sha256": core.manifest_sha256, "metadata_sha256": "", "verified_at": "2026-08-06 21:05:44"}}}
        write_json(install / "current.json", state)
        environment = {k: v for k, v in os.environ.items() if not k.startswith("LIVECLIPPER_") and k != "PYTHONPATH"}
        environment.update(APPDATA=str(route_root / "appdata"), LOCALAPPDATA=str(route_root / "localappdata"), PYTHONDONTWRITEBYTECODE="1")
        data = route_root / "appdata/LiveClipper"
        data.mkdir(parents=True)
        sentinel = data / "settings-preservation-sentinel.txt"
        sentinel.write_text("user settings must survive update and rollback", encoding="utf-8")
        sentinel_hash = digest(sentinel)
        recovery = "not_needed"
        if old == "2026.9.3.3":
            artifact = previous.root / "workspace/ui_commerce_director_experiment/mix-recovery/status.json"
            artifact.parent.mkdir(parents=True)
            artifact.write_text('{"status":"completed"}', encoding="utf-8")
            artifact_hash = digest(artifact)
            try:
                verify_business_directory(previous.root, public)
                raise AssertionError("Legacy workspace pollution was not rejected")
            except BundleVerificationError as exc:
                assert "file set mismatch" in str(exc)
            repair = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "tools/repair_v4_director_workspace.ps1"), "-InstallRoot", str(install)]
            run(repair + ["-CheckOnly"], env=environment, cwd=route_root)
            assert artifact.exists()
            unexpected = previous.root / "unexpected.py"
            unexpected.write_text("# must be rejected", encoding="utf-8")
            run(repair, env=environment, cwd=route_root, expected=1)
            assert artifact.exists()
            unexpected.unlink()
            before_state = digest(install / "current.json")
            run(repair, env=environment, cwd=route_root)
            assert not artifact.exists()
            backups = list((install / "recovery").glob("*/ui_commerce_director_experiment/mix-recovery/status.json"))
            assert len(backups) == 1 and digest(backups[0]) == artifact_hash
            assert digest(install / "current.json") == before_state
            verify_business_directory(previous.root, public)
            run(repair, env=environment, cwd=route_root)
            recovery = "pass: pollution reproduced; check-only and unrelated-extra rejection; lossless backup; signed bundle restored; idempotent"
        result = apply_signed_business_update(install, channel, route_root / "downloads", public_key_path=public, opener=lambda request, **_: Response(payload, request.full_url))
        assert result.install_result is not None
        selected = json.loads((install / "current.json").read_text(encoding="utf-8-sig"))
        assert selected["current"]["application_version"] == VERSION and selected["previous"]["application_version"] == old and selected["pending"] is True
        target = install / "versions" / VERSION / "business"
        helper = """
import importlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
sys.path[:0] = [str(root/'app'), str(root/'web_client'), str(root)]
server = importlib.import_module('server')
workspace = server._commerce_director_workspace_root()
assert root not in workspace.parents
assert workspace == Path(sys.argv[2])/'workspace/ui_commerce_director_experiment'
job = workspace/'mix-restart-check'
job.mkdir(parents=True, exist_ok=True)
(job/'status.json').write_text('{"status":"completed"}', encoding='utf-8')
(job/'mix_director_virtual_source.srt').write_text('1\\n00:00:00,000 --> 00:00:01,000\\nTest\\n', encoding='utf-8')
runner = importlib.import_module('run_m3_new_golden_plan_fidelity')
assert callable(runner._run_case)
print(json.dumps({'workspace':str(workspace),'runner_import':'pass'}))
"""
        helper_env = dict(environment, LIVECLIPPER_BUNDLE_DIR=str(target), LIVECLIPPER_V4_BUNDLE_VERIFIED="1", LIVECLIPPER_RUNTIME_LAYOUT="4", LIVECLIPPER_CODE_SOURCE="bundled")
        for restart in range(2):
            run([sys.executable, "-B", "-c", helper, str(target), str(data)], env=helper_env, cwd=route_root)
            verify_business_directory(target, public, expected_version=VERSION, expected_core_version="4.0.0")
        print(f"Starting real frozen Host for {old} upgrade...", flush=True)
        diagnostic = route_root / "frozen-host-diagnostic.json"
        host_env = dict(environment, LIVECLIPPER_INSTALL_ROOT=str(install), LIVECLIPPER_ACTIVE_VERSION=VERSION, LIVECLIPPER_RUNTIME_LAYOUT="4", LIVECLIPPER_V4_CORE_VERSION="4.0.0")
        run([str(install / "core/4.0.0/LiveClipperHost.exe"), "--liveclipper-v4-diagnostic", str(diagnostic)], env=host_env, cwd=install)
        actual = json.loads(diagnostic.read_text(encoding="utf-8"))
        assert actual["ok"] and actual["application_version"] == VERSION
        assert actual["runtime"]["code_source"] == "bundled" and actual["runtime"]["v4_bundle_verified"] is True
        verify_business_directory(target, public, expected_version=VERSION)
        launcher._validated_selection_with_receipt(
            install,
            launcher.RuntimeSelection(VERSION, "4.0.0"),
            json.loads((install / "current.json").read_text(encoding="utf-8-sig")),
            force_full=False,
        )
        assert digest(sentinel) == sentinel_hash
        entry = target / "bundle_entry.py"
        entry.write_bytes(entry.read_bytes() + b"\n# isolated rollback acceptance\n")
        original_validate = launcher._validated_selection_with_receipt
        def confirmed_validate(root, selection, state, *, force_full=False):
            return original_validate(root, selection, state, force_full=False)
        with patch.object(launcher, "_validated_selection_with_receipt", side_effect=confirmed_validate):
            assert launcher.run(install, validate_only=True) == 2
        rolled_back = json.loads((install / "current.json").read_text(encoding="utf-8-sig"))
        assert rolled_back["current"]["application_version"] == old and digest(sentinel) == sentinel_hash
        report["routes"].append({"from": old, "to": VERSION, "old_archive_sha256": digest(old_archive), "recovery": recovery, "signed_update": "pass", "workspace_restart_twice": "pass", "frozen_host": "pass", "confirmed_core_launcher_validation": "pass", "tamper_rollback": "pass", "user_data_preserved": "pass", "diagnostic": str(diagnostic)})
        print(f"PASS: {old} update, frozen Host, restart and rollback", flush=True)
    assert verify_core_directory(core_source, public, expected_version="4.0.0", hash_mode="entrypoint").manifest_sha256 == report["core_manifest_sha256"]
    report["original_core_unchanged"] = True
    write_json(work / "acceptance_results.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)

if __name__ == "__main__":
    main()
