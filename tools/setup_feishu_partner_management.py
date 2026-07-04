"""Set up LiveClipper partner/order management tables in Feishu Bitable.

This script is intentionally idempotent: existing tables and fields are kept.
It uses the Feishu app credentials already configured in app/feishu_scheduler.py.

Typical use from repo root:
    python tools/setup_feishu_partner_management.py --apply

If Feishu returns RolePermNotAllow/Permission denied, grant the app or the
operator account Base management/edit permissions, then run the same command.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import feishu_scheduler as fs  # noqa: E402


APP_TOKEN = getattr(fs, "_BITABLE_APP_TOKEN", "")
OPEN_API = "https://open.feishu.cn/open-apis"

DATE_PROP = {"auto_fill": False, "date_formatter": "yyyy/MM/dd"}


def select_options(names: list[str]) -> dict[str, Any]:
    return {"options": [{"name": name, "color": idx % 14} for idx, name in enumerate(names)]}


ORDER_FIELDS = [
    {"field_name": "订单类型", "type": 3, "property": select_options(["新购", "续费", "升级", "补单", "退款"])},
    {"field_name": "激活码", "type": 1},
    {"field_name": "用户微信", "type": 1},
    {"field_name": "微信名称", "type": 1},
    {"field_name": "手机号", "type": 13},
    {"field_name": "套餐类型", "type": 3, "property": select_options(["3天试用", "月卡", "季卡", "5个月卡", "年卡", "永久"])},
    {"field_name": "订单金额", "type": 2},
    {"field_name": "实收金额", "type": 2},
    {"field_name": "支付方式", "type": 3, "property": select_options(["微信支付", "转账", "手动补单", "其他"])},
    {"field_name": "付款时间", "type": 5, "property": DATE_PROP},
    {"field_name": "原到期日期", "type": 5, "property": DATE_PROP},
    {"field_name": "新到期日期", "type": 5, "property": DATE_PROP},
    {"field_name": "增加天数", "type": 2},
    {"field_name": "归属代理ID", "type": 1},
    {"field_name": "归属代理名称", "type": 1},
    {"field_name": "一级代理ID", "type": 1},
    {"field_name": "一级代理名称", "type": 1},
    {"field_name": "二级代理ID", "type": 1},
    {"field_name": "二级代理名称", "type": 1},
    {"field_name": "成交时佣金比例", "type": 2},
    {"field_name": "应结佣金", "type": 2},
    {"field_name": "结算状态", "type": 3, "property": select_options(["待结算", "已结算", "不结算", "有争议"])},
    {"field_name": "结算批次", "type": 1},
    {"field_name": "备注", "type": 1},
]

SETTLEMENT_FIELDS = [
    {"field_name": "结算周期", "type": 1},
    {"field_name": "合作伙伴ID", "type": 1},
    {"field_name": "合作伙伴名称", "type": 1},
    {"field_name": "新购订单数", "type": 2},
    {"field_name": "续费订单数", "type": 2},
    {"field_name": "销售总额", "type": 2},
    {"field_name": "退款金额", "type": 2},
    {"field_name": "应结佣金", "type": 2},
    {"field_name": "已结佣金", "type": 2},
    {"field_name": "结算状态", "type": 3, "property": select_options(["未结算", "已结算", "有争议"])},
    {"field_name": "结算日期", "type": 5, "property": DATE_PROP},
    {"field_name": "打款方式", "type": 3, "property": select_options(["微信", "支付宝", "银行卡", "其他"])},
    {"field_name": "打款凭证", "type": 1},
    {"field_name": "备注", "type": 1},
]

PARTNER_EXTRA_FIELDS = [
    {"field_name": "默认佣金比例", "type": 2},
    {"field_name": "默认团队奖比例", "type": 2},
    {"field_name": "结算方式", "type": 3, "property": select_options(["微信", "支付宝", "银行卡", "其他"])},
    {"field_name": "收款信息", "type": 1},
    {"field_name": "归属规则", "type": 3, "property": select_options(["首购归属", "手动指定", "临时活动"])},
    {"field_name": "合作备注", "type": 1},
]

USER_EXTRA_FIELDS = [
    {"field_name": "累计购买次数", "type": 2},
    {"field_name": "累计实收金额", "type": 2},
    {"field_name": "首次订单号", "type": 1},
    {"field_name": "最近订单号", "type": 1},
    {"field_name": "最近购买时间", "type": 5, "property": DATE_PROP},
]


def feishu(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    token = fs._get_feishu_token()
    if not token:
        return {"code": -1, "msg": "failed to get Feishu access token"}
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        OPEN_API + path,
        data=raw,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with urllib.request.urlopen(req, context=ssl.create_default_context(), timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"raw": text}
        return {"code": exc.code, "msg": "http_error", "error": payload}
    except Exception as exc:  # pragma: no cover - operational helper
        return {"code": -2, "msg": type(exc).__name__, "error": str(exc)}


def list_tables() -> list[dict[str, Any]]:
    resp = feishu("GET", f"/bitable/v1/apps/{APP_TOKEN}/tables?page_size=100")
    if resp.get("code") != 0:
        raise RuntimeError(f"list tables failed: {resp}")
    return resp.get("data", {}).get("items", [])


def list_fields(table_id: str) -> list[dict[str, Any]]:
    fields: list[dict[str, Any]] = []
    page_token = ""
    while True:
        path = f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields?page_size=100"
        if page_token:
            path += f"&page_token={page_token}"
        resp = feishu("GET", path)
        if resp.get("code") != 0:
            raise RuntimeError(f"list fields failed for {table_id}: {resp}")
        data = resp.get("data", {})
        fields.extend(data.get("items", []))
        if not data.get("has_more"):
            return fields
        page_token = data.get("page_token", "")


def create_table(name: str, primary_field: dict[str, Any], default_view_name: str) -> dict[str, Any]:
    return feishu(
        "POST",
        f"/bitable/v1/apps/{APP_TOKEN}/tables",
        {
            "table": {
                "name": name,
                "default_view_name": default_view_name,
                "fields": [primary_field],
            }
        },
    )


def create_field(table_id: str, field: dict[str, Any]) -> dict[str, Any]:
    return feishu("POST", f"/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/fields", field)


def ensure_table(
    table_name: str,
    primary_field: dict[str, Any],
    default_view_name: str,
    apply: bool,
) -> tuple[str | None, dict[str, Any]]:
    tables_by_name = {t["name"]: t for t in list_tables()}
    if table_name in tables_by_name:
        return tables_by_name[table_name]["table_id"], {"status": "exists"}
    if not apply:
        return None, {"status": "would_create"}
    resp = create_table(table_name, primary_field, default_view_name)
    if resp.get("code") != 0:
        return None, {"status": "failed", "response": resp}
    refreshed = {t["name"]: t for t in list_tables()}
    table = refreshed.get(table_name)
    return table.get("table_id") if table else None, {"status": "created", "response": resp}


def ensure_fields(table_id: str | None, fields: list[dict[str, Any]], apply: bool) -> list[dict[str, Any]]:
    if not table_id:
        return [{"field_name": f["field_name"], "status": "skipped_no_table"} for f in fields]
    existing = {f["field_name"]: f for f in list_fields(table_id)}
    results: list[dict[str, Any]] = []
    for field in fields:
        name = field["field_name"]
        if name in existing:
            results.append({"field_name": name, "status": "exists", "field_id": existing[name].get("field_id")})
            continue
        if not apply:
            results.append({"field_name": name, "status": "would_create"})
            continue
        resp = create_field(table_id, field)
        if resp.get("code") == 0:
            results.append({"field_name": name, "status": "created", "response": resp})
        else:
            results.append({"field_name": name, "status": "failed", "response": resp})
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually create missing tables/fields.")
    parser.add_argument("--report", default="", help="Optional report path.")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "apply": bool(args.apply),
        "app_token_suffix": APP_TOKEN[-8:],
        "tables": {},
    }

    order_id, action = ensure_table("订单复购流水", {"field_name": "订单号", "type": 1}, "全部订单", args.apply)
    report["tables"]["订单复购流水"] = {
        "table_id": order_id,
        "table_action": action,
        "fields": ensure_fields(order_id, ORDER_FIELDS, args.apply),
    }

    settle_id, action = ensure_table("代理结算记录", {"field_name": "结算批次", "type": 1}, "全部结算", args.apply)
    report["tables"]["代理结算记录"] = {
        "table_id": settle_id,
        "table_action": action,
        "fields": ensure_fields(settle_id, SETTLEMENT_FIELDS, args.apply),
    }

    tables_by_name = {t["name"]: t for t in list_tables()}
    partner_id = tables_by_name.get("代理管理", {}).get("table_id")
    user_id = tables_by_name.get("用户管理", {}).get("table_id")

    report["tables"]["代理管理"] = {
        "table_id": partner_id,
        "table_action": {"status": "existing" if partner_id else "missing"},
        "fields": ensure_fields(partner_id, PARTNER_EXTRA_FIELDS, args.apply),
    }
    report["tables"]["用户管理"] = {
        "table_id": user_id,
        "table_action": {"status": "existing" if user_id else "missing"},
        "fields": ensure_fields(user_id, USER_EXTRA_FIELDS, args.apply),
    }

    out = Path(args.report) if args.report else ROOT / "admin_outputs" / f"feishu_partner_management_setup_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failures = [
        {"table": table, "field": field.get("field_name"), "response": field.get("response")}
        for table, info in report["tables"].items()
        for field in info.get("fields", [])
        if field.get("status") == "failed"
    ]
    table_failures = [
        {"table": table, "response": info.get("table_action", {}).get("response")}
        for table, info in report["tables"].items()
        if info.get("table_action", {}).get("status") == "failed"
    ]

    print(f"Report: {out}")
    print(f"Apply: {args.apply}")
    print(f"Table failures: {len(table_failures)}")
    print(f"Field failures: {len(failures)}")
    if table_failures or failures:
        print("First failure:")
        print(json.dumps((table_failures + failures)[0], ensure_ascii=False, indent=2))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
