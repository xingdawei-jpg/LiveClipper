"""Stable launcher for signed Runtime V4 core/business selections."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runtime_v4.business_bundle import VerifiedBusinessBundle, verify_business_directory
from runtime_v4.core_manifest import VerifiedCore, verify_core_directory


RUNTIME_LAYOUT_VERSION = 4
STATE_SCHEMA_VERSION = 1
LAUNCHER_VERSION = "4.0.0"
STATE_FILE = "current.json"
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")
RUNTIME_OWNED_ENV = (
    "LIVECLIPPER_BUNDLE_DIR",
    "LIVECLIPPER_FROZEN",
    "LIVECLIPPER_CODE_SOURCE",
    "LIVECLIPPER_LEGACY_OVERLAYS_PRESENT",
    "LIVECLIPPER_INSTALL_ROOT",
    "LIVECLIPPER_ACTIVE_VERSION",
    "LIVECLIPPER_RUNTIME_LAYOUT",
    "LIVECLIPPER_LAUNCHER_VERSION",
    "LIVECLIPPER_HEALTH_FILE",
    "LIVECLIPPER_HEALTH_TOKEN",
    "LIVECLIPPER_ROLLBACK_REASON",
    "LIVECLIPPER_V4_CORE_VERSION",
    "LIVECLIPPER_V4_CORE_MANIFEST_SHA256",
    "LIVECLIPPER_V4_BUNDLE_VERIFIED",
    "LIVECLIPPER_V4_BUNDLE_MANIFEST_SHA256",
    "LIVECLIPPER_V4_BUNDLE_KEY_ID",
)


class LaunchError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeSelection:
    application_version: str
    core_version: str

    def as_dict(self) -> dict[str, str]:
        return {
            "application_version": self.application_version,
            "core_version": self.core_version,
        }


@dataclass(frozen=True)
class ValidatedSelection:
    selection: RuntimeSelection
    core: VerifiedCore
    business: VerifiedBusinessBundle


def _install_root(argument: str = "") -> Path:
    if argument:
        return Path(argument).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _data_root() -> Path:
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or tempfile.gettempdir()
    )
    return base / "LiveClipper"


def _write_log(message: str) -> None:
    path = _data_root() / "update_logs" / "launcher-v4.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def _safe_version(value: object, *, label: str) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise LaunchError(f"invalid {label}: {version!r}")
    return version


def _selection(value: object, *, label: str) -> RuntimeSelection:
    if not isinstance(value, dict):
        raise LaunchError(f"{label} runtime selection is invalid")
    return RuntimeSelection(
        application_version=_safe_version(
            value.get("application_version"),
            label=f"{label} application version",
        ),
        core_version=_safe_version(
            value.get("core_version"),
            label=f"{label} core version",
        ),
    )


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise LaunchError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise LaunchError(f"{path.name} is not an object")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _public_key_path(root: Path) -> Path:
    if getattr(sys, "frozen", False):
        candidates = [
            Path(getattr(sys, "_MEIPASS", ""))
            / "core_keys"
            / "release_update_public_key.pem"
        ]
    else:
        candidates = [
            root / "updater" / "release_update_public_key.pem",
            Path(__file__).resolve().parents[1] / "app" / "release_update_public_key.pem",
        ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise LaunchError("Runtime V4 release public key is missing")


def _validated_selection(root: Path, selection: RuntimeSelection) -> ValidatedSelection:
    return _validated_selection_with_mode(root, selection, core_hash_mode="full")


def _validated_selection_with_mode(
    root: Path,
    selection: RuntimeSelection,
    *,
    core_hash_mode: str,
) -> ValidatedSelection:
    public_key = _public_key_path(root)
    core_root = (root / "core" / selection.core_version).resolve()
    business_root = (
        root / "versions" / selection.application_version / "business"
    ).resolve()
    for target, parent, label in (
        (core_root, (root / "core").resolve(), "core"),
        (business_root, (root / "versions").resolve(), "business"),
    ):
        try:
            target.relative_to(parent)
        except ValueError as exc:
            raise LaunchError(f"Runtime V4 {label} directory escapes its root") from exc
    try:
        core = verify_core_directory(
            core_root,
            public_key,
            expected_version=selection.core_version,
            hash_mode=core_hash_mode,
        )
        business = verify_business_directory(
            business_root,
            public_key,
            expected_version=selection.application_version,
            expected_core_version=selection.core_version,
            repair_legacy_runtime_artifacts=True,
        )
    except Exception as exc:
        raise LaunchError(str(exc)) from exc
    return ValidatedSelection(selection, core, business)


def _core_receipt_matches(state: dict[str, Any], core: VerifiedCore) -> bool:
    receipts = state.get("verified_cores")
    if not isinstance(receipts, dict):
        return False
    receipt = receipts.get(core.core_version)
    return bool(
        isinstance(receipt, dict)
        and receipt.get("verification_mode") == "full"
        and receipt.get("manifest_sha256") == core.manifest_sha256
    )


def _remember_verified_core(state: dict[str, Any], core: VerifiedCore) -> None:
    raw_receipts = state.get("verified_cores")
    receipts = dict(raw_receipts) if isinstance(raw_receipts, dict) else {}
    receipts[core.core_version] = {
        "verification_mode": "full",
        "manifest_sha256": core.manifest_sha256,
        "metadata_sha256": core.metadata_sha256,
        "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    state["verified_cores"] = receipts


def _validated_selection_with_receipt(
    root: Path,
    selection: RuntimeSelection,
    state: dict[str, Any],
    *,
    force_full: bool = False,
) -> tuple[ValidatedSelection, bool]:
    if force_full:
        full = _validated_selection_with_mode(root, selection, core_hash_mode="full")
        _remember_verified_core(state, full.core)
        return full, True
    quick = _validated_selection_with_mode(root, selection, core_hash_mode="entrypoint")
    if _core_receipt_matches(state, quick.core):
        return quick, False
    full = _validated_selection_with_mode(root, selection, core_hash_mode="full")
    _remember_verified_core(state, full.core)
    return full, True


def _launch(
    root: Path,
    validated: ValidatedSelection,
    *,
    health_file: Path | None = None,
    health_token: str = "",
    rollback_reason: str = "",
) -> subprocess.Popen:
    env = os.environ.copy()
    for name in RUNTIME_OWNED_ENV:
        env.pop(name, None)
    env.update(
        {
            "LIVECLIPPER_INSTALL_ROOT": str(root),
            "LIVECLIPPER_ACTIVE_VERSION": validated.selection.application_version,
            "LIVECLIPPER_RUNTIME_LAYOUT": str(RUNTIME_LAYOUT_VERSION),
            "LIVECLIPPER_LAUNCHER_VERSION": LAUNCHER_VERSION,
            "LIVECLIPPER_V4_CORE_VERSION": validated.selection.core_version,
            "LIVECLIPPER_V4_CORE_MANIFEST_SHA256": validated.core.manifest_sha256,
            "LIVECLIPPER_V4_BUNDLE_MANIFEST_SHA256": validated.business.manifest_sha256,
        }
    )
    if health_file is not None:
        env["LIVECLIPPER_HEALTH_FILE"] = str(health_file)
        env["LIVECLIPPER_HEALTH_TOKEN"] = health_token
    if rollback_reason:
        env["LIVECLIPPER_ROLLBACK_REASON"] = rollback_reason
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        [str(validated.core.entrypoint)],
        cwd=str(validated.core.root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def _wait_for_health(
    process: subprocess.Popen,
    health_file: Path,
    token: str,
    validated: ValidatedSelection,
    timeout: float,
) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if health_file.is_file():
            try:
                receipt = _load_json(health_file)
                if (
                    receipt.get("token") == token
                    and receipt.get("version") == validated.selection.application_version
                    and receipt.get("runtime_layout_version") == RUNTIME_LAYOUT_VERSION
                    and receipt.get("core_version") == validated.selection.core_version
                    and receipt.get("core_manifest_sha256") == validated.core.manifest_sha256
                    and receipt.get("bundle_manifest_sha256") == validated.business.manifest_sha256
                    and receipt.get("runtime_integrity_ok") is True
                ):
                    return True, ""
            except Exception:
                pass
        code = process.poll()
        if code is not None:
            return False, f"runtime exited before health confirmation (code {code})"
        time.sleep(0.25)
    return False, "runtime health confirmation timed out"


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=8)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _show_error(message: str) -> None:
    _write_log(message)
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("LiveClipper V4 启动失败", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def _rollback_state(
    state: dict[str, Any],
    *,
    failed: RuntimeSelection,
    restored: RuntimeSelection,
    reason: str,
) -> None:
    state.update(
        {
            "current": restored.as_dict(),
            "previous": failed.as_dict(),
            "pending": False,
            "failed": failed.as_dict(),
            "rollback_reason": reason,
            "rolled_back_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


def run(
    install_root: Path,
    *,
    health_timeout: float = 45.0,
    validate_only: bool = False,
) -> int:
    state_path = install_root / STATE_FILE
    try:
        state = _load_json(state_path)
        if state.get("schema_version") != STATE_SCHEMA_VERSION:
            raise LaunchError("current.json has an unsupported Runtime V4 schema")
        if state.get("runtime_layout_version") != RUNTIME_LAYOUT_VERSION:
            raise LaunchError("current.json is not a Runtime V4 state file")
        current = _selection(state.get("current"), label="current")
        previous_raw = state.get("previous")
        previous = _selection(previous_raw, label="previous") if previous_raw else None
        if previous == current:
            raise LaunchError("Runtime V4 current and previous selections must differ")

        try:
            active, receipt_changed = _validated_selection_with_receipt(
                install_root,
                current,
                state,
                force_full=validate_only,
            )
            if receipt_changed:
                _atomic_write_json(state_path, state)
        except Exception as current_error:
            if previous is None:
                raise
            restored, _ = _validated_selection_with_receipt(
                install_root,
                previous,
                state,
                force_full=validate_only,
            )
            reason = f"current selection failed verification: {current_error}"
            _rollback_state(
                state,
                failed=current,
                restored=previous,
                reason=reason,
            )
            _atomic_write_json(state_path, state)
            active = restored
            current, previous = previous, current
            if validate_only:
                return 2
            _launch(install_root, active, rollback_reason=reason)
            _write_log(
                "rolled back unverified selection "
                f"{previous.application_version}/{previous.core_version} -> "
                f"{current.application_version}/{current.core_version}: {reason}"
            )
            return 2

        if validate_only:
            return 0
        token = uuid.uuid4().hex
        health_file = _data_root() / "launcher_health" / f"{token}.json"
        health_file.parent.mkdir(parents=True, exist_ok=True)
        health_file.unlink(missing_ok=True)
        process = _launch(
            install_root,
            active,
            health_file=health_file,
            health_token=token,
        )
        healthy, reason = _wait_for_health(
            process,
            health_file,
            token,
            active,
            health_timeout,
        )
        health_file.unlink(missing_ok=True)
        if healthy:
            if bool(state.get("pending")):
                state.update(
                    {
                        "pending": False,
                        "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "rollback_reason": "",
                    }
                )
                _atomic_write_json(state_path, state)
            _write_log(
                "healthy V4 selection "
                f"{current.application_version}/{current.core_version}"
            )
            return 0

        _terminate(process)
        if previous is None:
            raise LaunchError(
                f"{current.application_version}/{current.core_version} failed health "
                f"confirmation and no rollback exists: {reason}"
            )
        restored, _ = _validated_selection_with_receipt(
            install_root,
            previous,
            state,
        )
        _rollback_state(
            state,
            failed=current,
            restored=previous,
            reason=reason,
        )
        _atomic_write_json(state_path, state)
        _launch(install_root, restored, rollback_reason=reason)
        _write_log(
            "rolled back unhealthy selection "
            f"{current.application_version}/{current.core_version} -> "
            f"{previous.application_version}/{previous.core_version}: {reason}"
        )
        return 2
    except Exception as exc:
        _show_error(str(exc))
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the active Runtime V4 selection.")
    parser.add_argument("--install-root", default="")
    parser.add_argument("--health-timeout", type=float, default=45.0)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args(argv)
    return run(
        _install_root(args.install_root),
        health_timeout=max(1.0, args.health_timeout),
        validate_only=args.validate_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())
