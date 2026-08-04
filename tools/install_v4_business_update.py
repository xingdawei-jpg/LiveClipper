from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.update_agent import install_business_archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a signed Runtime V4 business update.")
    parser.add_argument("archive")
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--public-key")
    parser.add_argument("--install-only", action="store_true")
    args = parser.parse_args()
    result = install_business_archive(
        args.install_root,
        args.archive,
        application_version=args.version,
        public_key_path=args.public_key or None,
        activate=not args.install_only,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "application_version": result.application_version,
                "core_version": result.core_version,
                "manifest_sha256": result.manifest_sha256,
                "activated": result.activated,
                "already_installed": result.already_installed,
                "business_root": str(result.business_root),
                "state_path": str(result.state_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

