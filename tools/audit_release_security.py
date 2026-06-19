"""Audit release folders for licensing/admin tooling leaks.

Run before publishing a desktop package:

    python tools/audit_release_security.py release_dist

Pass one or more folders. If no folder is passed, common release folders are
checked when they exist.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

BANNED_FILENAMES = {
    "generated_codes.json",
    "internal_license_generator.py",
    "license_generator.py",
    "license_server.py",
    "license_feishu_backend.py",
    "license_stats_store.py",
    "licenses.json",
    "feishu_scheduler.py",
}

SENSITIVE_TEXT_MARKERS = {
    "SECRET_KEY",
    "_FS_APP_SECRET_HEX",
    "_FS_APP_ID_HEX",
    "_BITABLE_APP_TOKEN",
    "_BITABLE_TABLE_ID",
    "app_access_token/internal",
    "license_generator",
    "internal_license_generator",
    "license_server",
    "license_feishu_backend",
    "LIVECLIPPER_LICENSE_TOKEN_PRIVATE_KEY",
    "license token signing keys",
}

TEXT_SUFFIXES = {
    ".py",
    ".json",
    ".txt",
    ".md",
    ".spec",
    ".js",
    ".html",
    ".css",
}

MAX_TEXT_BYTES = 5 * 1024 * 1024


def _default_targets() -> list[Path]:
    names = [
        "release_dist",
        "release_artifacts",
        "app/dist",
        "packaging_smoke/dist",
    ]
    return [ROOT / name for name in names if (ROOT / name).exists()]


def _iter_files(target: Path):
    if target.is_file():
        yield target
        return
    for path in target.rglob("*"):
        if path.is_file():
            yield path


def _is_text_candidate(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES and path.stat().st_size <= MAX_TEXT_BYTES


def audit_target(target: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    target = target.resolve()
    if not target.exists():
        errors.append(f"missing target: {target}")
        return errors, warnings

    for path in _iter_files(target):
        rel = path.relative_to(target)
        if path.name in BANNED_FILENAMES:
            errors.append(f"banned file: {rel}")
            continue

        try:
            if not _is_text_candidate(path):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception as exc:
            errors.append(f"read failed: {rel} ({exc})")
            continue

        for marker in sorted(SENSITIVE_TEXT_MARKERS):
            if marker in text:
                warnings.append(f"sensitive marker {marker!r}: {rel}")
                break

    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit release folders for sensitive licensing files.")
    parser.add_argument("targets", nargs="*", help="Release folders or files to audit.")
    parser.add_argument("--strict", action="store_true", help="Fail on sensitive text markers too.")
    args = parser.parse_args(argv)

    targets = [Path(item) for item in args.targets] if args.targets else _default_targets()
    if not targets:
        print("No release targets found. Pass a folder explicitly.")
        return 0

    all_errors: list[tuple[Path, str]] = []
    all_warnings: list[tuple[Path, str]] = []
    for target in targets:
        path = target if target.is_absolute() else ROOT / target
        errors, warnings = audit_target(path)
        all_errors.extend((path, item) for item in errors)
        all_warnings.extend((path, item) for item in warnings)

    if all_errors or (args.strict and all_warnings):
        print("Release security audit FAILED:")
        for target, finding in all_errors:
            print(f"- {target}: {finding}")
        if args.strict:
            for target, finding in all_warnings:
                print(f"- {target}: {finding}")
        return 1

    if all_warnings:
        print("Release security audit passed with warnings:")
        for target, finding in all_warnings:
            print(f"- {target}: {finding}")
        return 0

    print("Release security audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
