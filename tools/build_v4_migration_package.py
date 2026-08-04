"""Build a signed Runtime V3-to-V4 migration package directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.migration_package import build_migration_package


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("--application-version", required=True)
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--core-bridge", required=True)
    parser.add_argument("--business-archive", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    verified = build_migration_package(
        source_version=args.source_version,
        application_version=args.application_version,
        core_version=args.core_version,
        launcher_path=args.launcher,
        core_bridge_path=args.core_bridge,
        business_archive_path=args.business_archive,
        private_key_path=args.private_key,
        public_key_path=args.public_key,
        output_root=args.output,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "source_version": verified.source_version,
                "application_version": verified.application_version,
                "core_version": verified.core_version,
                "root": str(verified.root),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
