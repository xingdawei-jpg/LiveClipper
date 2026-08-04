"""Build a signed Runtime V4 business bundle."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.business_bundle import build_business_archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument(
        "--policy",
        type=Path,
        default=ROOT / "release" / "runtime_v4_business_policy.json",
    )
    args = parser.parse_args()
    result = build_business_archive(
        args.source_root,
        args.output,
        application_version=args.version,
        private_key_path=args.private_key,
        policy_path=args.policy,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
