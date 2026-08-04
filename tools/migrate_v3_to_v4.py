"""Run the source-level Runtime V3-to-V4 migration transaction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.migration_transaction import migrate_v3_install


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-root", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--cleanup-legacy", action="store_true")
    parser.add_argument("--health-timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    result = migrate_v3_install(
        args.install_root,
        args.package,
        args.public_key,
        cleanup_legacy=args.cleanup_legacy,
        health_timeout=max(10.0, args.health_timeout),
    )
    print(
        json.dumps(
            {
                "ok": True,
                "source_version": result.source_version,
                "application_version": result.application_version,
                "core_version": result.core_version,
                "core_reused_files": result.core_reused_files,
                "core_payload_files": result.core_payload_files,
                "removed_legacy_versions": list(
                    result.legacy_cleanup.removed_versions
                ),
                "preserved_legacy_versions": list(
                    result.legacy_cleanup.preserved_versions
                ),
                "backup_root": str(result.backup_root or ""),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
