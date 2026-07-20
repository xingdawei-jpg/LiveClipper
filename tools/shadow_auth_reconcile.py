#!/usr/bin/env python3
"""Supported P1-A entry point: read-only source reconciliation into shadow SQLite."""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

from shadow_auth_import import FeishuReader, Importer, Report, fixture_objects, normal_code, now
from shadow_auth_sync import ReadOnlyOss

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "admin_outputs" / "shadow_auth_p1a" / "shadow_auth.sqlite3")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "admin_outputs" / "shadow_auth_p1a")
    parser.add_argument("--apply", action="store_true", help="only writes the isolated local shadow database")
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
            feishu_records, bindings, payments = [], [], []
            if feishu.configured():
                try:
                    feishu_records = [("record:" + str(r.get("record_id") or r.get("id")), r) for r in feishu.records()]
                except Exception as exc:
                    report.exception("feishu_license", exc)
            else:
                report.exception("feishu_license", "not configured")
            if oss.configured():
                try:
                    bindings, payments = list(oss.objects("bindings/")), list(oss.objects("payments/"))
                except Exception as exc:
                    report.exception("oss", exc)
            else:
                report.exception("oss", "not configured")
        report.source("feishu_license", len(feishu_records))
        report.source("oss_binding", len(bindings))
        report.source("oss_payment", len(payments))
        first_feishu_code: dict[str, str] = {}
        for locator, payload in feishu_records:
            fields = payload.get("fields", payload)
            code = normal_code(fields.get("激活码") or fields.get("code"))
            if code and code in first_feishu_code:
                source_id, unchanged = importer.source_object("feishu_license", locator, payload, str(payload.get("record_id") or payload.get("id") or ""))
                if unchanged:
                    importer.entry(source_id, "skipped", reason="unchanged_source")
                else:
                    importer.quarantine(source_id, locator, "duplicate_feishu_activation_code", {"first_source_locator": first_feishu_code[code], "code_suffix": code[-8:]}, "error")
                continue
            if code:
                first_feishu_code[code] = locator
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
