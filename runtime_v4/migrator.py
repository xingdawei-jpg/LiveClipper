"""Frozen external entrypoint for the one-time Runtime V3-to-V4 migration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from runtime_v4.migration_transaction import migrate_v3_install


def _public_key_path() -> Path:
    if getattr(sys, "frozen", False):
        candidate = (
            Path(getattr(sys, "_MEIPASS", ""))
            / "core_keys"
            / "release_update_public_key.pem"
        )
    else:
        candidate = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "release_update_public_key.pem"
        )
    if not candidate.is_file():
        raise RuntimeError("Runtime V4 migrator release public key is missing")
    return candidate.resolve()


def _show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("LiveClipper V4 迁移失败", message)
        root.destroy()
    except Exception:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--cleanup-legacy", action="store_true")
    parser.add_argument("--health-timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    try:
        result = migrate_v3_install(
            args.install_root,
            args.package,
            _public_key_path(),
            cleanup_legacy=args.cleanup_legacy,
            health_timeout=max(10.0, args.health_timeout),
        )
    except Exception as exc:
        _show_error(str(exc))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "source_version": result.source_version,
                "application_version": result.application_version,
                "core_version": result.core_version,
                "removed_legacy_versions": list(
                    result.legacy_cleanup.removed_versions
                ),
                "preserved_legacy_versions": list(
                    result.legacy_cleanup.preserved_versions
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
