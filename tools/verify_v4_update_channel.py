"""Verify and optionally plan a Runtime V4 business update channel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.update_channel import load_update_channel_bytes, plan_business_update


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", type=Path)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--current-version")
    parser.add_argument("--core-version")
    args = parser.parse_args()
    verified = load_update_channel_bytes(args.channel.read_bytes(), args.public_key)
    result = {
        "ok": True,
        "channel": verified.channel,
        "channel_status": verified.channel_status,
        "application_version": verified.application_version,
        "allowed_source_versions": list(verified.allowed_source_versions),
        "compatible_core_versions": list(verified.compatible_core_versions),
        "filename": verified.filename,
        "size": verified.size,
        "sha256": verified.sha256,
        "bundle_manifest_sha256": verified.bundle_manifest_sha256,
        "sources": [source.__dict__ for source in verified.sources],
        "signature_key_id": verified.signature_key_id,
        "document_sha256": verified.document_sha256,
    }
    if bool(args.current_version) != bool(args.core_version):
        parser.error("--current-version and --core-version must be supplied together")
    if args.current_version and args.core_version:
        decision = plan_business_update(
            verified,
            current_version=args.current_version,
            current_core_version=args.core_version,
        )
        result["decision"] = {
            "available": decision.available,
            "reason": decision.reason,
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
