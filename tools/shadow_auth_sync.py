#!/usr/bin/env python3
"""P1-A production-source entry point; it only performs Feishu/OSS read calls.

This is deliberately separate from live FC handlers.  --apply writes only the
local isolated shadow SQLite database; without it all transformations are dry-run.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sqlite3
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

from shadow_auth_import import FeishuReader, Importer, Report, fixture_objects, now

ROOT = Path(__file__).resolve().parents[1]


class ReadOnlyOss:
    def __init__(self) -> None:
        self.endpoint = os.environ.get("OSS_ENDPOINT", "")
        self.bucket = os.environ.get("OSS_BUCKET", "")
        self.access_key = os.environ.get("OSS_AK", "")
        self.secret = os.environ.get("OSS_SK", "")

    def configured(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key and self.secret)

    def _headers(self, method: str, key: str = "") -> dict[str, str]:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        canonical = f"{method}\\n\\n\\n{stamp}\\n/{self.bucket}/{key.lstrip('/')}"
        signature = base64.b64encode(hmac.new(self.secret.encode(), canonical.encode(), hashlib.sha1).digest()).decode()
        return {"Date": stamp, "Authorization": f"OSS {self.access_key}:{signature}"}

    def get(self, key: str = "", query: str = "") -> bytes:
        url = f"https://{self.bucket}.{self.endpoint}/"
        if key:
            url += key.lstrip("/")
        if query:
            url += "?" + query
        request = urllib.request.Request(url, headers=self._headers("GET", key), method="GET")
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def objects(self, prefix: str) -> Iterable[tuple[str, dict[str, Any]]]:
        continuation = ""
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if continuation:
                query["continuation-token"] = continuation
            root = ET.fromstring(self.get(query=urllib.parse.urlencode(query)))
            namespace = "{http://s3.amazonaws.com/doc/2006-03-01/}"
            for node in root.findall(f"{namespace}Contents/{namespace}Key"):
                if node.text:
                    yield node.text, json.loads(self.get(node.text).decode("utf-8"))
            if (root.findtext(f"{namespace}IsTruncated") or "false").lower() != "true":
                return
            continuation = root.findtext(f"{namespace}NextContinuationToken") or ""
            if not continuation:
                return


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only production-source shadow authorization import")
    parser.add_argument("--db", type=Path, default=ROOT / "admin_outputs" / "shadow_auth_p1a" / "shadow_auth.sqlite3")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "admin_outputs" / "shadow_auth_p1a")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--fixture-dir", type=Path)
    parser.add_argument("--device-limit", type=int, default=1)
    args = parser.parse_args()
    if args.device_limit < 1:
        raise SystemExit("--device-limit must be >= 1")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report = Report(str(uuid.uuid4()), dry_run=not args.apply)
    importer = Importer(args.db, report, dry_run=not args.apply, device_limit=args.device_limit)
    try:
        if args.fixture_dir:
            feishu_records = fixture_objects(args.fixture_dir, "feishu_license")
            bindings = fixture_objects(args.fixture_dir, "oss_binding")
            payments = fixture_objects(args.fixture_dir, "oss_payment")
        else:
            feishu, oss = FeishuReader(), ReadOnlyOss()
            feishu_records: list[tuple[str, dict[str, Any]]] = []
            bindings: list[tuple[str, dict[str, Any]]] = []
            payments: list[tuple[str, dict[str, Any]]] = []
            if feishu.configured():
                try:
                    feishu_records = [("record:" + str(record.get("record_id") or record.get("id")), record) for record in feishu.records()]
                except Exception as exc:
                    report.exception("feishu_license", exc)
            else:
                report.exception("feishu_license", "not configured")
            if oss.configured():
                try:
                    bindings = list(oss.objects("bindings/"))
                    payments = list(oss.objects("payments/"))
                except Exception as exc:
                    report.exception("oss", exc)
            else:
                report.exception("oss", "not configured")
        report.source("feishu_license", len(feishu_records))
        report.source("oss_binding", len(bindings))
        report.source("oss_payment", len(payments))
        for locator, payload in feishu_records:
            importer.import_license("feishu_license", locator, payload, str(payload.get("record_id") or payload.get("id") or ""))
        for locator, payload in bindings:
            importer.import_license("oss_binding", locator, payload)
        for locator, payload in payments:
            importer.import_payment(locator, payload)
        report.data["finished_at"] = now()
        report_path = args.report_dir / f"reconciliation_{report.data['run_id']}.json"
        report_path.write_text(json.dumps(report.data, ensure_ascii=False, indent=2), encoding="utf-8")
        importer.close(report_path)
        print(json.dumps({"db": str(args.db), "report": str(report_path), "totals": report.data["totals"], "exceptions": len(report.data["exceptions"])}, ensure_ascii=False))
        return 0
    except Exception:
        importer.conn.rollback()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
