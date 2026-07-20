#!/usr/bin/env python3
"""Read-only OSS verification for P1-A.1; contains no mutation method."""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

from shadow_auth_sync import ReadOnlyOss
from shadow_auth_import import now

ROOT = Path(__file__).resolve().parents[1]


def error_summary(exc: urllib.error.HTTPError) -> dict[str, object]:
    raw = exc.read().decode("utf-8", errors="replace")[:4096]
    summary: dict[str, object] = {"http_status": exc.code, "reason": str(exc.reason)}
    try:
        root = ET.fromstring(raw)
        for key in ("Code", "Message", "RequestId", "HostId"):
            value = root.findtext(key)
            if value:
                summary[key.lower()] = value[:512]
    except ET.ParseError:
        summary["body_preview"] = raw[:512]
    return summary


def read_operation(oss: ReadOnlyOss, operation: str, prefix_or_key: str) -> dict[str, object]:
    try:
        if operation == "list":
            query = urllib.parse.urlencode({"list-type": "2", "prefix": prefix_or_key, "max-keys": "1"})
            oss.get(query=query)
            return {"operation": "ListObjectsV2", "target": prefix_or_key, "result": "ok"}
        oss.get(prefix_or_key)
        return {"operation": "GetObject", "target": prefix_or_key, "result": "ok"}
    except urllib.error.HTTPError as exc:
        return {"operation": "ListObjectsV2" if operation == "list" else "GetObject", "target": prefix_or_key, "result": "http_error", "error": error_summary(exc)}
    except Exception as exc:
        return {"operation": "ListObjectsV2" if operation == "list" else "GetObject", "target": prefix_or_key, "result": "error", "error": {"message": str(exc)[:512]}}


def main() -> int:
    parser = argparse.ArgumentParser(description="P1-A.1 OSS read-only verification")
    parser.add_argument("--report", type=Path, default=ROOT / "admin_outputs" / "shadow_auth_p1a" / "p1a_oss_verify.json")
    args = parser.parse_args()
    oss = ReadOnlyOss()
    report: dict[str, object] = {"created_at": now(), "mode": "read_only", "configured": oss.configured(), "signature_contract": {"canonicalized_resource_for_list": f"/{oss.bucket}/", "query_in_signature": False, "url_shape": f"https://{oss.bucket}.{oss.endpoint}/?list-type=2&prefix=<prefix>&max-keys=1"}, "operations": []}
    if oss.configured():
        operations = report["operations"]
        assert isinstance(operations, list)
        operations.extend([
            read_operation(oss, "list", "payments/"),
            read_operation(oss, "list", "bindings/"),
            read_operation(oss, "get", "payments/__shadow_read_probe__.json"),
            read_operation(oss, "get", "bindings/__shadow_read_probe__.json"),
        ])
    else:
        report["error"] = "OSS environment is not configured"
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(args.report), "configured": report["configured"], "operations": report["operations"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
