"""Stable launcher for immutable LiveClipper runtime version directories."""

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
from pathlib import Path, PurePosixPath
from typing import Any

APP_IMPORT_DIR = Path(__file__).resolve().parents[1] / "app"
if APP_IMPORT_DIR.is_dir() and str(APP_IMPORT_DIR) not in sys.path:
    sys.path.insert(0, str(APP_IMPORT_DIR))


from release_signing import sha256_file, verify_manifest
from runtime_v3_versions import LAUNCHER_VERSION


LAYOUT_VERSION = 3
STATE_FILE = "current.json"
RUNTIME_MANIFEST = "runtime_manifest.json"
DEFAULT_ENTRYPOINT = "LiveClipperWeb.exe"
VERSION_PATTERN = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,79}$")
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
)


class LaunchError(RuntimeError):
    pass


def _install_root(argument: str = "") -> Path:
    if argument:
        return Path(argument).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _data_root() -> Path:
    base = Path(
        os.environ.get("LOCALAPPDATA")
        or os.environ.get("APPDATA")
        or tempfile.gettempdir()
    )
    return base / "LiveClipper"


def _write_log(message: str) -> None:
    path = _data_root() / "update_logs" / "launcher.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def _safe_version(value: Any) -> str:
    version = str(value or "").strip()
    if not VERSION_PATTERN.fullmatch(version) or version.startswith("."):
        raise LaunchError(f"invalid runtime version: {value!r}")
    return version


def _safe_relative_path(value: Any) -> str:
    text = str(value or "").replace("\\", "/").strip()
    if not text or text.startswith(("/", "./", "../")):
        raise LaunchError(f"unsafe runtime path: {value!r}")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LaunchError(f"unsafe runtime path: {value!r}")
    if ":" in path.parts[0]:
        raise LaunchError(f"unsafe runtime path: {value!r}")
    return path.as_posix()


def _target_path(root: Path, relative: str) -> Path:
    safe = _safe_relative_path(relative)
    root = root.resolve()
    target = (root / Path(*PurePosixPath(safe).parts)).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise LaunchError(f"runtime path escapes version directory: {relative}") from exc
    return target


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise LaunchError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise LaunchError(f"{path.name} is not an object")
    return data


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _public_key_path(root: Path) -> Path:
    candidates = [
        root / "updater" / "release_update_public_key.pem",
        Path(getattr(sys, "_MEIPASS", "")) / "app" / "release_update_public_key.pem",
        Path(__file__).resolve().parent.parent / "app" / "release_update_public_key.pem",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise LaunchError("release update public key is missing")


def _validated_runtime(root: Path, version_value: Any) -> tuple[Path, dict[str, Any]]:
    version = _safe_version(version_value)
    runtime_root = (root / "versions" / version).resolve()
    versions_root = (root / "versions").resolve()
    try:
        runtime_root.relative_to(versions_root)
    except ValueError as exc:
        raise LaunchError("runtime directory escapes versions root") from exc
    manifest_path = runtime_root / RUNTIME_MANIFEST
    manifest = _load_json(manifest_path)
    verify_manifest(manifest, _public_key_path(root))
    if int(manifest.get("runtime_layout_version") or 0) not in {2, LAYOUT_VERSION}:
        raise LaunchError("unsupported runtime layout")
    if str(manifest.get("version") or "") != version:
        raise LaunchError("runtime manifest version mismatch")
    entrypoint_rel = _safe_relative_path(manifest.get("entrypoint") or DEFAULT_ENTRYPOINT)
    entrypoint = _target_path(runtime_root, entrypoint_rel)
    files = manifest.get("files")
    if not isinstance(files, dict) or not isinstance(files.get(entrypoint_rel), dict):
        raise LaunchError("runtime manifest does not cover its entrypoint")
    expected = files[entrypoint_rel]
    if not entrypoint.is_file():
        raise LaunchError(f"runtime entrypoint is missing: {entrypoint_rel}")
    expected_size = expected.get("size")
    if expected_size is None or entrypoint.stat().st_size != int(expected_size):
        raise LaunchError("runtime entrypoint size mismatch")
    if sha256_file(entrypoint) != str(expected.get("sha256") or "").lower():
        raise LaunchError("runtime entrypoint hash mismatch")
    return entrypoint, manifest


def _launch(
    root: Path,
    version: str,
    entrypoint: Path,
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
            "LIVECLIPPER_ACTIVE_VERSION": version,
            "LIVECLIPPER_RUNTIME_LAYOUT": str(LAYOUT_VERSION),
            "LIVECLIPPER_LAUNCHER_VERSION": LAUNCHER_VERSION,
        }
    )
    if health_file:
        env["LIVECLIPPER_HEALTH_FILE"] = str(health_file)
        env["LIVECLIPPER_HEALTH_TOKEN"] = health_token
    if rollback_reason:
        env["LIVECLIPPER_ROLLBACK_REASON"] = rollback_reason
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    return subprocess.Popen(
        [str(entrypoint)],
        cwd=str(entrypoint.parent),
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
    version: str,
    timeout: float,
) -> tuple[bool, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_file.is_file():
            try:
                receipt = _load_json(health_file)
                if (
                    receipt.get("token") == token
                    and receipt.get("version") == version
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
        messagebox.showerror("LiveClipper 启动失败", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def run(
    install_root: Path,
    *,
    health_timeout: float = 45.0,
    validate_only: bool = False,
) -> int:
    state_path = install_root / STATE_FILE
    try:
        state = _load_json(state_path)
        if int(state.get("runtime_layout_version") or 0) != LAYOUT_VERSION:
            raise LaunchError("current.json is not a Runtime V3 state file")
        current = _safe_version(state.get("current_version"))
        previous_raw = state.get("previous_version")
        previous = _safe_version(previous_raw) if previous_raw else ""
        try:
            entrypoint, _manifest = _validated_runtime(install_root, current)
        except Exception as current_error:
            if not previous:
                raise
            entrypoint, _manifest = _validated_runtime(install_root, previous)
            state.update(
                {
                    "current_version": previous,
                    "previous_version": current,
                    "pending": False,
                    "failed_version": current,
                    "rollback_reason": str(current_error),
                    "rolled_back_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
            _atomic_write_json(state_path, state)
            current, previous = previous, current
        if validate_only:
            return 0

        if not bool(state.get("pending")):
            _launch(install_root, current, entrypoint)
            _write_log(f"launched confirmed runtime {current}")
            return 0

        token = uuid.uuid4().hex
        health_file = _data_root() / "launcher_health" / f"{token}.json"
        health_file.parent.mkdir(parents=True, exist_ok=True)
        health_file.unlink(missing_ok=True)
        process = _launch(
            install_root,
            current,
            entrypoint,
            health_file=health_file,
            health_token=token,
        )
        healthy, reason = _wait_for_health(
            process,
            health_file,
            token,
            current,
            health_timeout,
        )
        health_file.unlink(missing_ok=True)
        if healthy:
            state.update(
                {
                    "pending": False,
                    "confirmed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "rollback_reason": "",
                }
            )
            _atomic_write_json(state_path, state)
            _write_log(f"confirmed runtime {current}")
            return 0

        _terminate(process)
        if not previous:
            raise LaunchError(f"{current} failed health confirmation and no rollback exists: {reason}")
        previous_entrypoint, _previous_manifest = _validated_runtime(install_root, previous)
        state.update(
            {
                "current_version": previous,
                "previous_version": current,
                "pending": False,
                "failed_version": current,
                "rollback_reason": reason,
                "rolled_back_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        _atomic_write_json(state_path, state)
        _launch(
            install_root,
            previous,
            previous_entrypoint,
            rollback_reason=reason,
        )
        _write_log(f"rolled back {current} -> {previous}: {reason}")
        return 2
    except Exception as exc:
        _show_error(str(exc))
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the active LiveClipper runtime.")
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
