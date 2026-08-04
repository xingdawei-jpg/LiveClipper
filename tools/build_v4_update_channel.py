"""Build a signed Runtime V4 business update channel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.update_channel import build_signed_update_channel


def _source(value: str) -> dict[str, str]:
    name, separator, url = value.partition("=")
    if not separator or not name.strip() or not url.strip():
        raise argparse.ArgumentTypeError("source must use NAME=HTTPS_URL")
    return {"name": name.strip(), "url": url.strip()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--from-version", action="append", required=True)
    parser.add_argument("--core-version", action="append", required=True)
    parser.add_argument("--source", action="append", type=_source, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--channel", default="stable")
    parser.add_argument(
        "--status",
        choices=("ready", "hold", "paused", "disabled"),
        default="ready",
    )
    parser.add_argument("--release-notes", default="")
    parser.add_argument("--published-at", default="")
    args = parser.parse_args()
    verified = build_signed_update_channel(
        args.output,
        args.bundle,
        application_version=args.version,
        allowed_source_versions=args.from_version,
        compatible_core_versions=args.core_version,
        sources=args.source,
        private_key_path=args.private_key,
        channel=args.channel,
        channel_status=args.status,
        release_notes=args.release_notes,
        published_at=args.published_at,
    )
    print(
        json.dumps(
            {
                "ok": True,
                "output": str(args.output.resolve()),
                "channel": verified.channel,
                "channel_status": verified.channel_status,
                "application_version": verified.application_version,
                "allowed_source_versions": list(verified.allowed_source_versions),
                "compatible_core_versions": list(verified.compatible_core_versions),
                "archive_sha256": verified.sha256,
                "bundle_manifest_sha256": verified.bundle_manifest_sha256,
                "signature_key_id": verified.signature_key_id,
                "document_sha256": verified.document_sha256,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
