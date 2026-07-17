"""Promote one accepted signed hold candidate to the live ready channel."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from build_release_channel import _release_policy, _validate_distribution_policy

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
import sys

if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from release_signing import sha256_file, sign_manifest, verify_manifest

STABLE_FILE = ROOT / "release" / "stable.json"
PUBLIC_KEY_FILE = ROOT / "app" / "release_update_public_key.pem"
VERSION_RE = re.compile(r"^\d{4}\.\d{1,2}\.\d{1,2}\.\d+$")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _version_key(value: str) -> tuple[int, ...]:
    if not VERSION_RE.fullmatch(str(value or "")):
        raise ValueError(f"invalid version: {value!r}")
    return tuple(int(part) for part in value.split("."))


def validate_acceptance(
    candidate_path: Path,
    candidate: dict[str, Any],
    acceptance: dict[str, Any],
    policy: dict[str, Any],
) -> None:
    version = str(candidate.get("version") or "")
    if acceptance.get("version") != version:
        raise ValueError("acceptance version differs from candidate")
    expected_candidate_hash = str(acceptance.get("candidate_sha256") or "").upper()
    actual_candidate_hash = sha256_file(candidate_path).upper()
    if expected_candidate_hash != actual_candidate_hash:
        raise ValueError("acceptance is not bound to this exact candidate manifest")
    release_type = str(acceptance.get("release_type") or "")
    if release_type not in {"business_runtime", "full_baseline"}:
        raise ValueError("invalid acceptance release_type")
    candidate_release_type = str(candidate.get("release_type") or release_type)
    if candidate_release_type != release_type:
        raise ValueError("acceptance release_type differs from candidate")
    gate_policy = policy.get("acceptance_gates")
    gates = acceptance.get("gates")
    evidence = acceptance.get("evidence")
    if not isinstance(gate_policy, dict) or not isinstance(gates, dict):
        raise ValueError("invalid acceptance gate structure")
    if not isinstance(evidence, dict):
        raise ValueError("acceptance evidence must be an object")
    required = [
        *(gate_policy.get("always") or []),
        *(gate_policy.get(release_type) or []),
    ]
    for gate in required:
        if gates.get(gate) != "pass":
            raise ValueError(f"acceptance gate is not pass: {gate}")
        if not evidence.get(gate):
            raise ValueError(f"acceptance evidence is missing: {gate}")


def validate_candidate_distribution(
    candidate: dict[str, Any],
    release_type: str,
    policy: dict[str, Any],
) -> None:
    package = candidate.get("package")
    patches = candidate.get("patches")
    if not isinstance(package, dict) or not isinstance(patches, list):
        raise ValueError("candidate package/patch structure is invalid")
    package_url = str(package.get("url") or "")
    if release_type == "business_runtime":
        if any(
            (
                package_url,
                str(package.get("sha256") or ""),
                str(package.get("filename") or ""),
                package.get("size"),
            )
        ):
            raise ValueError("business_runtime must not include a full package")
        if not patches:
            raise ValueError("business_runtime requires at least one signed patch")
    elif release_type == "full_baseline":
        if not package_url:
            raise ValueError("full_baseline requires the Baidu full-package URL")
        if patches:
            raise ValueError("full_baseline cannot contain ordinary runtime patches")
    else:
        raise ValueError("invalid acceptance release_type")
    _validate_distribution_policy(package_url, patches, policy)


def promote(
    candidate_path: Path,
    acceptance_path: Path,
    private_key_path: Path,
) -> dict[str, Any]:
    candidate = _load_json(candidate_path)
    acceptance = _load_json(acceptance_path)
    policy = _release_policy()
    verify_manifest(candidate, PUBLIC_KEY_FILE)
    if candidate.get("channel_status") != "hold":
        raise ValueError("only a signed hold candidate can be promoted")
    candidate_root = (ROOT / "release" / "candidates").resolve()
    candidate_path.resolve().relative_to(candidate_root)

    version = str(candidate.get("version") or "")
    if str(candidate.get("latest_version") or "") != version:
        raise ValueError("candidate version fields do not match")
    _version_key(version)
    current = _load_json(STABLE_FILE)
    verify_manifest(current, PUBLIC_KEY_FILE)
    current_version = str(current.get("version") or current.get("latest_version") or "")
    if _version_key(version) <= _version_key(current_version):
        raise ValueError("candidate must be newer than the live stable channel")

    release_type = str(acceptance.get("release_type") or "")
    validate_candidate_distribution(candidate, release_type, policy)
    validate_acceptance(candidate_path, candidate, acceptance, policy)

    ready = dict(candidate)
    ready["release_type"] = release_type
    ready["channel_status"] = "ready"
    has_patches = bool(candidate.get("patches"))
    ready["supports_incremental_updates"] = has_patches
    ready["requires_full_package"] = not has_patches
    ready["update_strategy"] = (
        "verified-version-delta-with-full-fallback"
        if has_patches
        else "full-package"
    )
    ready["published_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    signed = sign_manifest(ready, private_key_path)
    verify_manifest(signed, PUBLIC_KEY_FILE)
    return signed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--confirm-publish-ready", action="store_true")
    args = parser.parse_args()
    if not args.confirm_publish_ready:
        parser.error("promotion requires --confirm-publish-ready")

    signed = promote(
        args.candidate.resolve(),
        args.acceptance.resolve(),
        args.private_key.resolve(),
    )
    temp = STABLE_FILE.with_suffix(".json.promoting")
    temp.write_text(
        json.dumps(signed, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, STABLE_FILE)
    print(f"promoted candidate to {STABLE_FILE}")
    print(
        f"version={signed['version']} patches={len(signed.get('patches') or [])} "
        f"status={signed['channel_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
