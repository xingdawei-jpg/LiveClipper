"""Stable Runtime V4 desktop host prototype.

The packaged host derives its business directory and release public key from
the installation layout. Source-only overrides are intentionally disabled in
frozen builds.
"""

from __future__ import annotations

import json
import importlib
import importlib.util
import os
import runpy
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from runtime_v4.business_bundle import (
    BundleVerificationError,
    VerifiedBusinessBundle,
    activate_verified_import_roots,
    load_verified_application,
    verify_business_directory,
)
from runtime_v4.update_service import RuntimeV4UpdateService, load_update_source_config


TOOL_RUN_FLAG = "--liveclipper-run-tool"
DIAGNOSTIC_FLAG = "--liveclipper-v4-diagnostic"
CORE_VERSION = "4.0.1"


@dataclass(frozen=True)
class HostLayout:
    install_root: Path
    business_root: Path
    public_key: Path
    application_version: str


def _safe_version(value: object) -> str:
    version = str(value or "").strip()
    if not version or any(char not in "0123456789." for char in version):
        raise BundleVerificationError(f"invalid V4 application version: {version!r}")
    return version


def _production_layout() -> HostLayout:
    install_text = str(os.environ.get("LIVECLIPPER_INSTALL_ROOT") or "").strip()
    version = _safe_version(os.environ.get("LIVECLIPPER_ACTIVE_VERSION"))
    if not install_text:
        raise BundleVerificationError("V4 packaged host requires LIVECLIPPER_INSTALL_ROOT")
    install_root = Path(install_text).resolve()
    business_root = (install_root / "versions" / version / "business").resolve()
    public_key = _core_resource("core_keys/release_update_public_key.pem")
    if public_key is None:
        raise BundleVerificationError("V4 packaged host is missing its embedded release key")
    return HostLayout(install_root, business_root, public_key, version)


def _development_layout() -> HostLayout:
    business_text = str(os.environ.get("LIVECLIPPER_V4_BUSINESS_DIR") or "").strip()
    public_key_text = str(os.environ.get("LIVECLIPPER_V4_PUBLIC_KEY") or "").strip()
    version = _safe_version(os.environ.get("LIVECLIPPER_ACTIVE_VERSION"))
    if not business_text or not public_key_text:
        raise BundleVerificationError(
            "source V4 host requires LIVECLIPPER_V4_BUSINESS_DIR and LIVECLIPPER_V4_PUBLIC_KEY"
        )
    business_root = Path(business_text).resolve()
    public_key = Path(public_key_text).resolve()
    install_text = str(os.environ.get("LIVECLIPPER_INSTALL_ROOT") or "").strip()
    install_root = Path(install_text).resolve() if install_text else business_root.parent.parent
    return HostLayout(install_root, business_root, public_key, version)


def resolve_host_layout() -> HostLayout:
    return _production_layout() if getattr(sys, "frozen", False) else _development_layout()


def validate_launcher_core_identity() -> None:
    declared = str(os.environ.get("LIVECLIPPER_V4_CORE_VERSION") or "").strip()
    if getattr(sys, "frozen", False) and not declared:
        raise BundleVerificationError("V4 packaged host requires launcher core identity")
    if declared and declared != CORE_VERSION:
        raise BundleVerificationError(
            f"V4 launcher/core version mismatch: expected {CORE_VERSION}, got {declared}"
        )


def _core_resource(relative: str) -> Path | None:
    if getattr(sys, "frozen", False):
        root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent / "_internal"))
    else:
        root = Path(__file__).resolve().parents[1]
    candidate = (root / Path(*PurePosixPath(relative).parts)).resolve()
    return candidate if candidate.is_file() else None


def _update_channel_urls() -> tuple[str, ...]:
    config = _core_resource("core_config/runtime_v4_update_sources.json")
    if config is None and not getattr(sys, "frozen", False):
        config = _core_resource("release/runtime_v4_update_sources.json")
    if config is None:
        raise BundleVerificationError("V4 core is missing its update source config")
    try:
        return load_update_source_config(config)
    except Exception as exc:
        raise BundleVerificationError(str(exc)) from exc


def _stable_launcher_path(install_root: Path) -> Path | None:
    root = install_root.resolve()
    for name in ("LiveClipperWeb.exe", "LiveClipperLauncherV4.exe"):
        candidate = root / name
        if not candidate.is_file() or candidate.is_symlink():
            continue
        resolved = candidate.resolve()
        if resolved.parent == root:
            return resolved
    return None


def _schedule_launcher_restart(layout: HostLayout, delay: float = 1.2) -> bool:
    if not getattr(sys, "frozen", False):
        return False
    launcher = _stable_launcher_path(layout.install_root)
    if launcher is None:
        return False

    launcher_ps = str(launcher).replace("'", "''")
    workdir_ps = str(layout.install_root).replace("'", "''")
    command = (
        f"$old = Get-Process -Id {os.getpid()} -ErrorAction SilentlyContinue; "
        "if ($old) { $old.WaitForExit() }; "
        "Start-Sleep -Milliseconds 350; "
        f"Start-Process -FilePath '{launcher_ps}' "
        f"-WorkingDirectory '{workdir_ps}' -WindowStyle Hidden"
    )
    flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )
    try:
        subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-Command",
                command,
            ],
            cwd=str(layout.install_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
        )
    except OSError:
        return False

    def _exit() -> None:
        time.sleep(max(0.1, delay))
        os._exit(0)

    threading.Thread(
        target=_exit,
        daemon=True,
        name="liveclipper-v4-launcher-restart",
    ).start()
    return True


def configure_verified_environment(layout: HostLayout, verified: VerifiedBusinessBundle) -> None:
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["LIVECLIPPER_INSTALL_ROOT"] = str(layout.install_root)
    os.environ["LIVECLIPPER_ACTIVE_VERSION"] = verified.application_version
    os.environ["LIVECLIPPER_BUNDLE_DIR"] = str(verified.root)
    os.environ["LIVECLIPPER_RUNTIME_LAYOUT"] = "4"
    os.environ["LIVECLIPPER_CODE_SOURCE"] = "bundled" if getattr(sys, "frozen", False) else "v4-prototype"
    os.environ["LIVECLIPPER_FROZEN"] = "1" if getattr(sys, "frozen", False) else "0"
    os.environ["LIVECLIPPER_V4_CORE_VERSION"] = CORE_VERSION
    os.environ["LIVECLIPPER_V4_BUNDLE_VERIFIED"] = "1"
    os.environ["LIVECLIPPER_V4_BUNDLE_MANIFEST_SHA256"] = verified.manifest_sha256
    os.environ["LIVECLIPPER_V4_BUNDLE_KEY_ID"] = verified.signature_key_id
    license_key = _core_resource("core_keys/license_public_key.txt")
    if license_key is None and not getattr(sys, "frozen", False):
        license_key = _core_resource("app/license_public_key.txt")
    if license_key is not None:
        os.environ["LIVECLIPPER_LICENSE_PUBLIC_KEY_FILE"] = str(license_key)


def load_core_compatibility_modules() -> None:
    """Expose trust helpers owned by the core, never by the business bundle."""

    try:
        importlib.import_module("release_signing")
        return
    except ModuleNotFoundError:
        if getattr(sys, "frozen", False):
            raise BundleVerificationError("V4 core is missing release_signing")

    source = _core_resource("app/release_signing.py")
    if source is None:
        raise BundleVerificationError("source V4 core is missing app/release_signing.py")
    spec = importlib.util.spec_from_file_location("release_signing", source)
    if spec is None or spec.loader is None:
        raise BundleVerificationError("cannot load source V4 release_signing module")
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_signing"] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop("release_signing", None)
        raise


def _repair_stdio() -> None:
    for name, fd, mode in (("stdin", 0, "r"), ("stdout", 1, "w"), ("stderr", 2, "w")):
        existing = getattr(sys, name, None)
        if existing is not None:
            try:
                existing.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
            continue
        try:
            stream = os.fdopen(os.dup(fd), mode, encoding="utf-8", errors="replace", buffering=1)
        except Exception:
            stream = open(os.devnull, mode, encoding="utf-8", errors="replace")
        setattr(sys, name, stream)


def _run_verified_tool(verified: VerifiedBusinessBundle) -> bool:
    if len(sys.argv) < 3 or sys.argv[1] != TOOL_RUN_FLAG:
        return False
    script = Path(sys.argv[2]).resolve()
    try:
        relative = script.relative_to(verified.root).as_posix()
    except ValueError as exc:
        raise BundleVerificationError("V4 tool script is outside the verified business bundle") from exc
    if relative not in verified.files or not relative.startswith("web_client/tools/") or not relative.endswith(".py"):
        raise BundleVerificationError(f"V4 tool script is not authorized by the bundle manifest: {relative}")
    activate_verified_import_roots(verified)
    _repair_stdio()
    sys.argv = [str(script), *sys.argv[3:]]
    runpy.run_path(str(script), run_name="__main__")
    return True


def _application_descriptor(value: Any) -> tuple[Any, Any]:
    if not isinstance(value, dict):
        raise BundleVerificationError("V4 bundle entrypoint returned an invalid descriptor")
    asgi_app = value.get("asgi_app")
    emit_log = value.get("emit_log")
    if asgi_app is None or not callable(emit_log):
        raise BundleVerificationError("V4 bundle descriptor is missing the ASGI app or logger")
    return asgi_app, emit_log


def _write_diagnostic(verified: VerifiedBusinessBundle) -> bool:
    if len(sys.argv) < 3 or sys.argv[1] != DIAGNOSTIC_FLAG:
        return False
    destination = Path(sys.argv[2]).resolve()
    server_module = sys.modules.get("server")
    runtime_fn = getattr(server_module, "runtime", None)
    runtime_payload = runtime_fn() if callable(runtime_fn) else {}
    payload = {
        "ok": True,
        "core_version": CORE_VERSION,
        "application_version": verified.application_version,
        "business_root": str(verified.root),
        "manifest_sha256": verified.manifest_sha256,
        "signature_key_id": verified.signature_key_id,
        "server_module_file": str(getattr(server_module, "__file__", "")),
        "runtime": runtime_payload,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return True


def main() -> int:
    validate_launcher_core_identity()
    layout = resolve_host_layout()
    verified = verify_business_directory(
        layout.business_root,
        layout.public_key,
        expected_version=layout.application_version,
        expected_core_version=CORE_VERSION,
        repair_legacy_runtime_artifacts=True,
    )
    configure_verified_environment(layout, verified)
    load_core_compatibility_modules()
    if _run_verified_tool(verified):
        return 0

    update_service = RuntimeV4UpdateService(
        layout.install_root,
        layout.public_key,
        _update_channel_urls(),
        restart_callback=lambda: _schedule_launcher_restart(layout),
    )

    descriptor, verified = load_verified_application(
        layout.business_root,
        layout.public_key,
        {
            "runtime_layout_version": 4,
            "core_version": CORE_VERSION,
            "application_version": layout.application_version,
            "install_root": str(layout.install_root),
            "business_root": str(layout.business_root),
            "manifest_sha256": verified.manifest_sha256,
            "update_service": update_service,
        },
        expected_version=layout.application_version,
        expected_core_version=CORE_VERSION,
        retain_import_roots=True,
    )
    asgi_app, _ = _application_descriptor(descriptor)
    if _write_diagnostic(verified):
        return 0

    from web_client import desktop as desktop_shell

    # The legacy shell initializes its own frozen bundle directory at import
    # time. Restore the already verified V4 environment before it starts any
    # worker or server process.
    configure_verified_environment(layout, verified)
    desktop_shell.BUNDLE_DIR = verified.root
    desktop_shell.RUNTIME_LAYOUT_VERSION = 4
    desktop_shell._load_server_app = lambda: asgi_app
    os.environ["LIVECLIPPER_RUNTIME_LAYOUT"] = "4"
    desktop_shell.main()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        _repair_stdio()
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
            flush=True,
        )
        raise
