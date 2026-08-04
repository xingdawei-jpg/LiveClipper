"""Build the minimal signed-core payload needed to migrate Runtime V3 to V4."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.migration import build_core_bridge, inspect_v3_install


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a Runtime V3 to V4 hard-link migration bridge."
    )
    parser.add_argument("--v3-install", required=True)
    parser.add_argument("--v4-core", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-source-version", default="")
    args = parser.parse_args(argv)

    legacy = inspect_v3_install(
        args.v3_install,
        args.public_key,
        expected_version=args.expected_source_version or None,
    )
    plan = build_core_bridge(
        legacy,
        args.v4_core,
        args.public_key,
        args.output,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "source_version": plan.source_version,
                "core_version": plan.core_version,
                "target_files": plan.target_files,
                "target_bytes": plan.target_bytes,
                "reusable_files": plan.reusable_files,
                "reusable_bytes": plan.reusable_bytes,
                "payload_files": len(plan.payload_files),
                "payload_bytes": plan.payload_bytes,
                "output": str(Path(args.output).resolve()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
