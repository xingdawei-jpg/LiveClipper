#!/usr/bin/env python3
"""Read-only P1-A importer for the isolated LiveClipper authorization shadow DB.

No production write API is implemented here.  The importer only calls Feishu and
OSS GET/List APIs, and it stores source locators plus SHA-256 payload hashes; it
never persists activation-code or OpenID plaintext in the shadow database.
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
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "deploy" / "shadow_auth" / "schema.sql"
IMPORTER_VERSION = "p1-a.1"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normal_code(value: Any) -> str:
    return str(value or "").replace("-", "").strip().lower()


def text(value: Any) -> str:
    if isinstance(value, list):
        return "".join(text(v.get("text") or v.get("name") or v.get("value")) if isinstance(v, dict) else text(v) for v in value).strip()
    return str(value or "").strip()


def epoch(value: Any) -> int | None:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value // 1000 if value > 10_000_000_000 else value


def plan_from_code(code: str) -> tuple[str, str]:
    key = normal_code(code)[:2]
    return {"00": ("00", "trial"), "01": ("01", "monthly"), "02": ("02", "quarterly"), "03": ("03", "yearly"), "04": ("04", "lifetime")}.get(key, (key or "unknown", "unknown"))


def state(value: Any) -> str:
    value = text(value).lower()
    return {"未激活": "unused", "unused": "unused", "已激活": "active", "active": "active", "已过期": "expired", "expired": "expired", "已禁用": "disabled", "禁用": "disabled", "disabled": "disabled"}.get(value, "unknown")


class Report:
    def __init__(self, run_id: str, dry_run: bool):
        self.data: dict[str, Any] = {"run_id": run_id, "dry_run": dry_run, "created_at": now(), "sources": {}, "totals": {"source_objects": 0, "imported": 0, "skipped": 0, "quarantined": 0, "conflicts": 0}, "exceptions": [], "conflicts": []}

    def source(self, source: str, count: int) -> None:
        self.data["sources"][source] = {"objects_seen": count}
        self.data["totals"]["source_objects"] += count

    def exception(self, source: str, error: Exception | str) -> None:
        self.data["exceptions"].append({"source": source, "error": str(error)})

    def action(self, action: str) -> None:
        self.data["totals"][action] += 1

    def conflict(self, kind: str, locator: str, details: dict[str, Any], severity: str = "warning") -> None:
        self.data["totals"]["conflicts"] += 1
        self.data["conflicts"].append({"severity": severity, "type": kind, "source_locator": locator, "details": details})


class FeishuReader:
    def __init__(self) -> None:
        self.app_id = os.environ.get("FEISHU_APP_ID") or os.environ.get("LIVECLIPPER_FEISHU_APP_ID", "")
        self.secret = os.environ.get("FEISHU_APP_SECRET") or os.environ.get("LIVECLIPPER_FEISHU_APP_SECRET", "")
        self.app_token = os.environ.get("LIVECLIPPER_FEISHU_APP_TOKEN") or os.environ.get("FEISHU_BITABLE_APP_TOKEN", "")
        self.table_id = os.environ.get("LIVECLIPPER_FEISHU_LICENSE_TABLE_ID") or os.environ.get("FEISHU_LICENSE_TABLE_ID", "tblWZH21Y2cXotHw")
        self.token = ""

    def configured(self) -> bool:
        return bool(self.app_id and self.secret and self.app_token and self.table_id)

    def _token(self) -> str:
        if self.token:
            return self.token
        body = json.dumps({"app_id": self.app_id, "app_secret": self.secret}).encode()
        request = urllib.request.Request("https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal", body, {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode())
        if result.get("code") != 0:
            raise RuntimeError("Feishu app token request failed")
        self.token = result["app_access_token"]
        return self.token

    def records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page_token = ""
        while True:
            url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.app_token}/tables/{self.table_id}/records?page_size=500"
            if page_token:
                url += "&page_token=" + urllib.parse.quote(page_token)
            request = urllib.request.Request(url, headers={"Authorization": "Bearer " + self._token()})
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode())
            if result.get("code") != 0:
                raise RuntimeError("Feishu records request failed")
            data = result.get("data") or {}
            records.extend(data.get("items") or [])
            if not data.get("has_more"):
                return records
            page_token = data.get("page_token") or ""
            if not page_token:
                return records


class OssReader:
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

    def _request(self, method: str, key: str = "") -> bytes:
        url = f"https://{self.bucket}.{self.endpoint}/{key.lstrip('/')}" if key else f"https://{self.bucket}.{self.endpoint}/"
        request = urllib.request.Request(url, headers=self._headers(method, key), method=method)
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()

    def objects(self, prefix: str) -> Iterable[tuple[str, dict[str, Any]]]:
        marker = ""
        while True:
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if marker:
                query["continuation-token"] = marker
            raw = self._request("GET", "?" + urllib.parse.urlencode(query))
            root = ET.fromstring(raw)
            namespace = "{http://s3.amazonaws.com/doc/2006-03-01/}"
            keys = [node.text for node in root.findall(f"{namespace}Contents/{namespace}Key") if node.text]
            for key in keys:
                body = self._request("GET", key)
                yield key, json.loads(body.decode("utf-8"))
            truncated = (root.findtext(f"{namespace}IsTruncated") or "false").lower() == "true"
            marker = root.findtext(f"{namespace}NextContinuationToken") or ""
            if not truncated or not marker:
                return


class Importer:
    def __init__(self, db_path: Path, report: Report, dry_run: bool, device_limit: int):
        self.db_path, self.report, self.dry_run, self.device_limit = db_path, report, dry_run, device_limit
        self.conn = sqlite3.connect(":memory:" if dry_run else db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        self.run_id = report.data["run_id"]
        self.conn.execute("INSERT INTO import_run(run_id, started_at, dry_run, importer_version, source_config_json) VALUES (?, ?, ?, ?, ?)", (self.run_id, now(), int(dry_run), IMPORTER_VERSION, json.dumps({"feishu": "read_only", "oss": "read_only"})))

    def close(self, report_path: Path) -> None:
        self.conn.execute("UPDATE import_run SET finished_at=?, outcome=?, report_path=? WHERE run_id=?", (now(), "completed", str(report_path), self.run_id))
        self.conn.commit()
        self.conn.close()

    def source_object(self, system: str, locator: str, payload: dict[str, Any], record_id: str = "") -> tuple[int, bool]:
        digest = sha(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
        row = self.conn.execute("SELECT source_object_id, payload_hash FROM source_object WHERE source_system=? AND source_locator=?", (system, locator)).fetchone()
        if row and row["payload_hash"] == digest:
            return int(row["source_object_id"]), True
        if row:
            self.conn.execute("UPDATE source_object SET payload_hash=?, observed_at=?, last_seen_run_id=? WHERE source_object_id=?", (digest, now(), self.run_id, row["source_object_id"]))
            return int(row["source_object_id"]), False
        cur = self.conn.execute("INSERT INTO source_object(source_system, source_locator, source_record_id, payload_hash, observed_at, first_seen_run_id, last_seen_run_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (system, locator, record_id, digest, now(), self.run_id, self.run_id))
        return int(cur.lastrowid), False

    def entry(self, source_id: int, action: str, entity_type: str = "", entity_id: str = "", reason: str = "", details: dict[str, Any] | None = None) -> None:
        self.conn.execute("INSERT OR REPLACE INTO import_entry(run_id, source_object_id, action, entity_type, entity_id, reason_code, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)", (self.run_id, source_id, action, entity_type, entity_id, reason, json.dumps(details or {}, ensure_ascii=False, sort_keys=True)))
        self.report.action(action)

    def quarantine(self, source_id: int, locator: str, kind: str, details: dict[str, Any], severity: str = "warning") -> None:
        self.conn.execute("INSERT INTO import_conflict(run_id, severity, conflict_type, source_object_id, details_json) VALUES (?, ?, ?, ?, ?)", (self.run_id, severity, kind, source_id, json.dumps(details, ensure_ascii=False, sort_keys=True)))
        self.entry(source_id, "quarantined", reason=kind, details=details)
        self.report.conflict(kind, locator, details, severity)

    def account(self, code_hash: str, openid: str | None, source_id: int) -> str:
        if openid:
            identity_hash = sha(openid)
            row = self.conn.execute("SELECT account_id FROM account_identity WHERE identity_type='wechat_openid' AND identity_hash=?", (identity_hash,)).fetchone()
            if row:
                return str(row["account_id"])
            key, kind = "wechat:" + identity_hash, "wechat_identity"
        else:
            key, kind = "legacy:" + code_hash, "legacy_code"
        row = self.conn.execute("SELECT account_id FROM account WHERE source_account_key=?", (key,)).fetchone()
        account_id = str(row["account_id"]) if row else str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        if not row:
            self.conn.execute("INSERT INTO account(account_id, account_kind, created_at, source_account_key) VALUES (?, ?, ?, ?)", (account_id, kind, now(), key))
        if openid:
            self.conn.execute("INSERT OR IGNORE INTO account_identity(identity_id, account_id, identity_type, identity_hash, source_object_id, created_at) VALUES (?, ?, 'wechat_openid', ?, ?, ?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, "openid:" + identity_hash)), account_id, identity_hash, source_id, now()))
        return account_id

    def import_license(self, system: str, locator: str, payload: dict[str, Any], record_id: str = "", openid: str | None = None) -> None:
        source_id, unchanged = self.source_object(system, locator, payload, record_id)
        if unchanged:
            self.entry(source_id, "skipped", reason="unchanged_source")
            return
        fields = payload.get("fields", payload)
        code = normal_code(fields.get("激活码") or fields.get("code"))
        if not code:
            self.quarantine(source_id, locator, "missing_activation_code", {"source": system}, "error")
            return
        code_hash = sha(code)
        plan_code, inferred_plan = plan_from_code(code)
        plan_name = text(fields.get("套餐") or fields.get("plan_name") or fields.get("plan")) or inferred_plan
        raw_status = fields.get("状态") or fields.get("status")
        grant_status = state(raw_status)
        starts_at = epoch(fields.get("激活日期") or fields.get("activated_at"))
        expires_at = epoch(fields.get("到期日期") or fields.get("expires_at"))
        if plan_code == "04":
            expires_at = None
        if grant_status == "active" and plan_code != "04" and not expires_at:
            self.quarantine(source_id, locator, "active_license_without_expiry", {"code_suffix": code[-8:]})
            return
        account_id = self.account(code_hash, openid, source_id)
        license_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "license:" + code_hash))
        entitlement_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "entitlement:" + code_hash))
        existing = self.conn.execute("SELECT entitlement_id, starts_at, expires_at, status FROM entitlement_grant WHERE license_id=?", (license_id,)).fetchone()
        if existing and system != "feishu":
            self.entry(source_id, "skipped", "entitlement_grant", entitlement_id, "lower_priority_source")
            return
        self.conn.execute("INSERT INTO license_code(license_id, code_hash, code_suffix, plan_code, plan_name, legacy_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(license_id) DO UPDATE SET plan_code=excluded.plan_code, plan_name=excluded.plan_name, legacy_status=excluded.legacy_status", (license_id, code_hash, code[-8:], plan_code, plan_name, grant_status, now()))
        self.conn.execute("INSERT INTO entitlement_grant(entitlement_id, account_id, license_id, plan_code, plan_name, status, starts_at, expires_at, active_device_limit, source_of_truth, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(license_id) DO UPDATE SET account_id=excluded.account_id, plan_code=excluded.plan_code, plan_name=excluded.plan_name, status=excluded.status, starts_at=excluded.starts_at, expires_at=excluded.expires_at, active_device_limit=excluded.active_device_limit, source_of_truth=excluded.source_of_truth, updated_at=excluded.updated_at", (entitlement_id, account_id, license_id, plan_code, plan_name, grant_status, starts_at, expires_at, self.device_limit, "feishu" if system == "feishu_license" else system, now(), now()))
        machine = text(fields.get("设备ID") or fields.get("machine_id"))
        if machine and machine not in {"unassigned", "未分配"}:
            machine_hash = sha(machine)
            device_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "device:" + machine_hash))
            self.conn.execute("INSERT INTO device(device_id, machine_hash, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?) ON CONFLICT(machine_hash) DO UPDATE SET last_seen_at=excluded.last_seen_at", (device_id, machine_hash, now(), now()))
            activation_status = "active" if grant_status == "active" else "unbound"
            self.conn.execute("INSERT INTO activation(activation_id, entitlement_id, device_id, status, activated_at, source_object_id) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(entitlement_id, device_id) DO UPDATE SET status=excluded.status, activated_at=excluded.activated_at, source_object_id=excluded.source_object_id", (str(uuid.uuid5(uuid.NAMESPACE_URL, "activation:" + entitlement_id + ":" + device_id)), entitlement_id, device_id, activation_status, starts_at, source_id))
        self.entry(source_id, "imported", "entitlement_grant", entitlement_id)

    def import_payment(self, key: str, payload: dict[str, Any]) -> None:
        source_id, unchanged = self.source_object("oss_payment", key, payload)
        if unchanged:
            self.entry(source_id, "skipped", reason="unchanged_source")
            return
        order_no = text(payload.get("order_no"))
        if not order_no:
            self.quarantine(source_id, key, "missing_order_no", {})
            return
        code = normal_code(payload.get("code"))
        code_hash = sha(code) if code else ""
        openid = text(payload.get("openid")) or None
        account_id = self.account(code_hash, openid, source_id) if (code_hash or openid) else None
        license_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "license:" + code_hash)) if code_hash else None
        order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "order:" + order_no))
        existing = self.conn.execute("SELECT order_id FROM customer_order WHERE external_order_no=?", (order_no,)).fetchone()
        if existing:
            self.quarantine(source_id, key, "duplicate_order_no", {"order_no": order_no})
            return
        amount = payload.get("amount_fen")
        try:
            amount = int(amount) if amount is not None else None
        except (TypeError, ValueError):
            self.quarantine(source_id, key, "invalid_payment_amount", {"order_no": order_no, "amount": amount})
            return
        self.conn.execute("INSERT INTO customer_order(order_id, source_system, external_order_no, account_id, license_id, plan_code, plan_name, amount_fen, status, source_object_id, created_at, updated_at) VALUES (?, 'oss_payment', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (order_id, order_no, account_id, license_id, text(payload.get("plan")) or None, text(payload.get("plan_name")) or None, amount, text(payload.get("status")) or None, source_id, text(payload.get("created_at")) or None, now()))
        transaction_id = text(payload.get("transaction_id")) or None
        if transaction_id:
            duplicate = self.conn.execute("SELECT order_id FROM payment_transaction WHERE provider='wechat_v3' AND provider_transaction_id=?", (transaction_id,)).fetchone()
            if duplicate:
                self.quarantine(source_id, key, "duplicate_wechat_transaction", {"transaction_id_suffix": transaction_id[-8:]})
                return
        self.conn.execute("INSERT INTO payment_transaction(payment_id, order_id, provider, provider_transaction_id, amount_fen, payment_status, source_object_id) VALUES (?, ?, 'wechat_v3', ?, ?, ?, ?)", (str(uuid.uuid5(uuid.NAMESPACE_URL, "payment:" + (transaction_id or order_no))), order_id, transaction_id, amount, text(payload.get("status")) or None, source_id))
        self.entry(source_id, "imported", "customer_order", order_id)


def fixture_objects(path: Path, name: str) -> list[tuple[str, dict[str, Any]]]:
    file = path / f"{name}.json"
    if not file.exists():
        return []
    data = json.loads(file.read_text(encoding="utf-8"))
    return [(str(row["locator"]), dict(row["payload"])) for row in data]


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only LiveClipper P1-A shadow authorization importer")
    parser.add_argument("--db", type=Path, default=ROOT / "admin_outputs" / "shadow_auth_p1a" / "shadow_auth.sqlite3")
    parser.add_argument("--report-dir", type=Path, default=ROOT / "admin_outputs" / "shadow_auth_p1a")
    parser.add_argument("--apply", action="store_true", help="write only the isolated shadow DB; never writes Feishu/OSS")
    parser.add_argument("--fixture-dir", type=Path, help="offline JSON fixtures for deterministic dry runs")
    parser.add_argument("--device-limit", type=int, default=1)
    args = parser.parse_args()
    if args.device_limit < 1:
        raise SystemExit("--device-limit must be >= 1")
    args.report_dir.mkdir(parents=True, exist_ok=True)
    args.db.parent.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    report = Report(run_id, dry_run=not args.apply)
    importer = Importer(args.db, report, dry_run=not args.apply, device_limit=args.device_limit)
    try:
        if args.fixture_dir:
            feishu_records = fixture_objects(args.fixture_dir, "feishu_license")
            bindings = fixture_objects(args.fixture_dir, "oss_binding")
            payments = fixture_objects(args.fixture_dir, "oss_payment")
        else:
            feishu, oss = FeishuReader(), OssReader()
            feishu_records = []
            bindings = []
            payments = []
            if feishu.configured():
                try:
                    feishu_records = [("record:" + str(r.get("record_id") or r.get("id")), r) for r in feishu.records()]
                except Exception as exc:
                    report.exception("feishu_license", exc)
            else:
                report.exception("feishu_license", "not configured: set FEISHU_APP_ID, FEISHU_APP_SECRET, LIVECLIPPER_FEISHU_APP_TOKEN")
            if oss.configured():
                try:
                    bindings = list(oss.objects("bindings/"))
                    payments = list(oss.objects("payments/"))
                except Exception as exc:
                    report.exception("oss", exc)
            else:
                report.exception("oss", "not configured: set OSS_ENDPOINT, OSS_BUCKET, OSS_AK, OSS_SK")
        report.source("feishu_license", len(feishu_records))
        report.source("oss_binding", len(bindings))
        report.source("oss_payment", len(payments))
        for locator, payload in feishu_records:
            importer.import_license("feishu_license", locator, payload, str(payload.get("record_id") or payload.get("id") or ""))
        for locator, payload in bindings:
            importer.import_license("oss_binding", locator, payload)
        for locator, payload in payments:
            importer.import_payment(locator, payload)
        report_path = args.report_dir / f"reconciliation_{run_id}.json"
        report.data["finished_at"] = now()
        report_path.write_text(json.dumps(report.data, ensure_ascii=False, indent=2), encoding="utf-8")
        importer.close(report_path)
        print(json.dumps({"db": str(args.db), "report": str(report_path), "totals": report.data["totals"], "exceptions": len(report.data["exceptions"])}, ensure_ascii=False))
        return 0
    except Exception:
        importer.conn.rollback()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
