"""Check every published Runtime V4 update endpoint and business mirror.

This is a read-only post-publish smoke check. It verifies that every configured
stable.json endpoint serves the same signed channel and that every signed
business archive source matches its declared size and SHA256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime_v4.update_channel import VerifiedUpdateChannel, load_update_channel_bytes
from runtime_v4.update_service import load_update_source_config


DEFAULT_SOURCE_CONFIG = ROOT / "release" / "runtime_v4_update_sources.json"
DEFAULT_PUBLIC_KEY = ROOT / "app" / "release_update_public_key.pem"
MAX_CHANNEL_BYTES = 1024 * 1024


def _safe_https_url(value: str, *, label: str) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError(f"{label} is not a safe HTTPS URL")
    return url


def _fetch_bytes(
    url: str,
    *,
    timeout: int,
    opener: Callable[..., Any],
    max_bytes: int | None = None,
) -> tuple[bytes, str]:
    safe_url = _safe_https_url(url, label="endpoint")
    request = urllib.request.Request(safe_url, headers={"User-Agent": "LiveClipper/4 endpoint check"})
    with opener(request, timeout=max(1, int(timeout)), context=ssl.create_default_context()) as response:
        final_url = str(response.geturl() if callable(getattr(response, "geturl", None)) else safe_url)
        _safe_https_url(final_url, label="endpoint redirect")
        payload = response.read() if max_bytes is None else response.read(max_bytes + 1)
    if max_bytes is not None and len(payload) > max_bytes:
        raise ValueError(f"endpoint response exceeds {max_bytes} bytes")
    return payload, final_url


def _archive_check(
    channel: VerifiedUpdateChannel,
    *,
    timeout: int,
    opener: Callable[..., Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for source in channel.sources:
        record: dict[str, Any] = {"name": source.name, "url": source.url}
        try:
            payload, final_url = _fetch_bytes(
                source.url,
                timeout=timeout,
                opener=opener,
                max_bytes=channel.size,
            )
            actual_sha256 = hashlib.sha256(payload).hexdigest()
            record.update(
                {
                    "ok": len(payload) == channel.size and actual_sha256 == channel.sha256,
                    "final_url": final_url,
                    "size": len(payload),
                    "sha256": actual_sha256,
                }
            )
            if not record["ok"]:
                errors.append(
                    f"business source {source.name} content mismatch: "
                    f"size={len(payload)}/{channel.size}, sha256={actual_sha256}"
                )
        except Exception as exc:
            record.update({"ok": False, "error": str(exc)})
            errors.append(f"business source {source.name} failed: {exc}")
        records.append(record)
    return records, errors


def check_endpoints(
    urls: Iterable[str],
    public_key_path: str | Path,
    *,
    timeout: int = 20,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Return a serializable health report for all channel and bundle mirrors."""
    endpoint_records: list[dict[str, Any]] = []
    errors: list[str] = []
    verified_channels: list[VerifiedUpdateChannel] = []
    seen: set[str] = set()

    for raw_url in urls:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        record: dict[str, Any] = {"url": url}
        try:
            payload, final_url = _fetch_bytes(
                url,
                timeout=timeout,
                opener=opener,
                max_bytes=MAX_CHANNEL_BYTES,
            )
            channel = load_update_channel_bytes(payload, public_key_path)
            record.update(
                {
                    "ok": True,
                    "final_url": final_url,
                    "document_sha256": channel.document_sha256,
                    "application_version": channel.application_version,
                }
            )
            verified_channels.append(channel)
        except Exception as exc:
            record.update({"ok": False, "error": str(exc)})
            errors.append(f"channel endpoint {url} failed: {exc}")
        endpoint_records.append(record)

    document_hashes = {channel.document_sha256 for channel in verified_channels}
    if len(document_hashes) > 1:
        errors.append("channel endpoints do not serve the same signed document")
    if not verified_channels:
        errors.append("no configured channel endpoint returned a valid signed document")
        return {"ok": False, "endpoints": endpoint_records, "archives": [], "errors": errors}

    channel = verified_channels[0]
    archive_records, archive_errors = _archive_check(channel, timeout=timeout, opener=opener)
    errors.extend(archive_errors)
    return {
        "ok": not errors,
        "application_version": channel.application_version,
        "channel_document_sha256": channel.document_sha256,
        "endpoints": endpoint_records,
        "archives": archive_records,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-config", type=Path, default=DEFAULT_SOURCE_CONFIG)
    parser.add_argument("--public-key", type=Path, default=DEFAULT_PUBLIC_KEY)
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    urls = load_update_source_config(args.source_config)
    report = check_endpoints(urls, args.public_key, timeout=args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
