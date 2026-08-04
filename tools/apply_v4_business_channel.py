"""Apply one locally supplied signed Runtime V4 business update channel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.update_channel import apply_signed_business_update, load_update_channel_bytes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", type=Path)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--download-root", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--install-only", action="store_true")
    args = parser.parse_args()
    channel = load_update_channel_bytes(args.channel.read_bytes(), args.public_key)
    result = apply_signed_business_update(
        args.install_root,
        channel,
        args.download_root,
        public_key_path=args.public_key,
        activate=not args.install_only,
    )
    payload = {
        "ok": True,
        "available": result.decision.available,
        "reason": result.decision.reason,
        "archive_path": str(result.archive_path) if result.archive_path else "",
        "installed": result.install_result is not None,
    }
    if result.install_result is not None:
        payload.update(
            {
                "application_version": result.install_result.application_version,
                "core_version": result.install_result.core_version,
                "manifest_sha256": result.install_result.manifest_sha256,
                "activated": result.install_result.activated,
                "already_installed": result.install_result.already_installed,
            }
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
