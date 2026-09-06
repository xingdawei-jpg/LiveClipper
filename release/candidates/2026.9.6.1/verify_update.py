"""Verify the 2026.9.4.1 to 2026.9.6.1 V4 business update route."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from runtime_v4.business_bundle import (  # noqa: E402
    extract_verified_business_archive,
    verify_business_directory,
)
from runtime_v4.update_channel import (  # noqa: E402
    apply_signed_business_update,
    load_update_channel_bytes,
)


SOURCE_VERSION = "2026.9.4.1"
TARGET_VERSION = "2026.9.6.1"
CORE_VERSION = "4.0.0"


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", type=Path, required=True)
    parser.add_argument("--old-bundle", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()

    work = args.work_root.resolve()
    work.mkdir(parents=True, exist_ok=False)
    public_key = ROOT / "app" / "release_update_public_key.pem"
    install = work / "install"
    (install / "updater").mkdir(parents=True)
    (install / "updater" / public_key.name).write_bytes(public_key.read_bytes())

    previous = extract_verified_business_archive(
        args.old_bundle,
        install / "versions" / SOURCE_VERSION,
        public_key,
        expected_version=SOURCE_VERSION,
        expected_core_version=CORE_VERSION,
    )
    _write_json(
        install / "current.json",
        {
            "schema_version": 1,
            "runtime_layout_version": 4,
            "current": {
                "application_version": SOURCE_VERSION,
                "core_version": CORE_VERSION,
            },
            "previous": None,
            "pending": False,
        },
    )

    appdata = work / "appdata"
    user_data = appdata / "LiveClipper"
    user_data.mkdir(parents=True)
    sentinel = user_data / "update-preservation-sentinel.txt"
    sentinel.write_text("user data must survive the update", encoding="utf-8")
    sentinel_hash = _digest(sentinel)

    channel = load_update_channel_bytes(args.channel.read_bytes(), public_key)
    result = apply_signed_business_update(
        install,
        channel,
        work / "downloads",
        public_key_path=public_key,
    )
    if result.install_result is None or not result.install_result.activated:
        raise RuntimeError("signed update did not activate")

    state = json.loads((install / "current.json").read_text(encoding="utf-8-sig"))
    if state["current"]["application_version"] != TARGET_VERSION:
        raise RuntimeError("target version was not selected")
    if state["previous"]["application_version"] != SOURCE_VERSION:
        raise RuntimeError("rollback source was not retained")
    if state.get("pending") is not True:
        raise RuntimeError("new selection is not pending health confirmation")

    target = install / "versions" / TARGET_VERSION / "business"
    verified = verify_business_directory(
        target,
        public_key,
        expected_version=TARGET_VERSION,
        expected_core_version=CORE_VERSION,
    )
    probe = r"""
import importlib
import json
import os
import sys
from pathlib import Path

bundle = Path(sys.argv[1]).resolve()
source = Path(sys.argv[2]).resolve()
expected_workspace = Path(sys.argv[3]).resolve() / "workspace" / "ui_commerce_director_experiment"
sys.path[:] = [str(bundle / "app"), str(bundle / "web_client"), str(bundle)] + [
    entry for entry in sys.path if Path(entry or ".").resolve() != source
]
server = importlib.import_module("server")
product_contract = importlib.import_module("director_product_contract")
wire_schema = importlib.import_module("director_wire_schema")
runner = importlib.import_module("run_m3_new_golden_plan_fidelity")
if server.CODE_SOURCE != "bundled":
    raise SystemExit("server did not select bundled code")
if server._load_version() != "2026.9.6.1":
    raise SystemExit("server reported the wrong application version")
if server._commerce_director_workspace_root() != expected_workspace:
    raise SystemExit("commercial director workspace is not in user data")
for module in (product_contract, wire_schema, runner):
    if bundle not in Path(module.__file__).resolve().parents:
        raise SystemExit(f"module imported outside bundle: {module.__name__}")
print(json.dumps({"code_source": server.CODE_SOURCE, "version": server._load_version()}))
"""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LIVECLIPPER_") and key != "PYTHONPATH"
    }
    environment.update(
        {
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(work / "localappdata"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "LIVECLIPPER_BUNDLE_DIR": str(target),
            "LIVECLIPPER_V4_BUNDLE_VERIFIED": "1",
            "LIVECLIPPER_RUNTIME_LAYOUT": "4",
            "LIVECLIPPER_CODE_SOURCE": "bundled",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-B", "-c", probe, str(target), str(ROOT), str(user_data)],
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            "bundle-only runtime probe failed\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    verify_business_directory(
        target,
        public_key,
        expected_version=TARGET_VERSION,
        expected_core_version=CORE_VERSION,
    )
    if _digest(sentinel) != sentinel_hash:
        raise RuntimeError("user data changed during update")

    report = {
        "from": SOURCE_VERSION,
        "to": TARGET_VERSION,
        "core_version": CORE_VERSION,
        "old_bundle_manifest_sha256": previous.manifest_sha256,
        "new_bundle_manifest_sha256": verified.manifest_sha256,
        "downloaded_archive_sha256": _digest(result.archive_path),
        "signed_remote_update": "pass",
        "atomic_activation": "pass",
        "rollback_source_retained": "pass",
        "bundle_only_runtime_import": "pass",
        "new_module_imports": "pass",
        "workspace_outside_bundle": "pass",
        "user_data_preserved": "pass",
    }
    _write_json(work / "acceptance_results.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
