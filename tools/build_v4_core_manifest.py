from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.core_manifest import build_core_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign a Runtime V4 stable core directory.")
    parser.add_argument("core_root")
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--private-key", required=True)
    parser.add_argument("--entrypoint", default="LiveClipperHost.exe")
    args = parser.parse_args()
    result = build_core_manifest(
        args.core_root,
        core_version=args.core_version,
        private_key_path=args.private_key,
        entrypoint=args.entrypoint,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

