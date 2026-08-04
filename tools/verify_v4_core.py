from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.core_manifest import verify_core_directory


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a signed Runtime V4 stable core.")
    parser.add_argument("core_root")
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--expected-version", default="")
    parser.add_argument(
        "--hash-mode",
        choices=("full", "metadata", "entrypoint"),
        default="full",
    )
    args = parser.parse_args()
    verified = verify_core_directory(
        args.core_root,
        args.public_key,
        expected_version=args.expected_version or None,
        hash_mode=args.hash_mode,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "core_root": str(verified.root),
                "core_version": verified.core_version,
                "entrypoint": verified.entrypoint_path,
                "file_count": len(verified.files),
                "manifest_sha256": verified.manifest_sha256,
                "metadata_sha256": verified.metadata_sha256,
                "signature_key_id": verified.signature_key_id,
                "hash_mode": args.hash_mode,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
