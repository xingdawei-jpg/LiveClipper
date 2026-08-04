"""Verify a Runtime V4 business bundle without importing it."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.business_bundle import (
    MANIFEST_NAME,
    verify_business_archive,
    verify_business_directory,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--expected-version")
    parser.add_argument("--expected-core-version")
    args = parser.parse_args()
    if args.bundle.is_dir():
        root = args.bundle
        if not (root / MANIFEST_NAME).is_file() and (root / "business").is_dir():
            root = root / "business"
        verified = verify_business_directory(
            root,
            args.public_key,
            expected_version=args.expected_version,
            expected_core_version=args.expected_core_version,
        )
    else:
        verified = verify_business_archive(
            args.bundle,
            args.public_key,
            expected_version=args.expected_version,
            expected_core_version=args.expected_core_version,
        )
    print(
        json.dumps(
            {
                "ok": True,
                "application_version": verified.application_version,
                "compatible_core_versions": list(verified.compatible_core_versions),
                "manifest_sha256": verified.manifest_sha256,
                "signature_key_id": verified.signature_key_id,
                "file_count": len(verified.files),
                "entrypoint": f"{verified.entrypoint_path}:{verified.entrypoint_callable}",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
