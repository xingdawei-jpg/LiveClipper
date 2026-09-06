"""Privacy-safe, append-only accounting for OpenAI-compatible AI calls.

This module is observability only.  It never changes prompts, retries, model
selection, cache behaviour, or returned model content.  Callers pass the
provider response after a request completes; only usage metadata and hashed
request fingerprints are persisted, never API keys or prompt text.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


LEDGER_VERSION = "ai-cost-ledger-v1"
_WRITE_LOCK = threading.Lock()
_SCOPE: ContextVar[dict[str, str]] = ContextVar("ai_cost_ledger_scope", default={})


def _data_root() -> Path:
    configured = str(os.environ.get("LIVECLIPPER_DATA_DIR") or "").strip()
    if configured:
        return Path(configured)
    app_data = str(os.environ.get("APPDATA") or "").strip()
    return Path(app_data) / "LiveClipper" if app_data else Path.home() / ".liveclipper"


def default_ledger_path() -> Path:
    return _data_root() / "ai_cost_ledger.jsonl"


@contextmanager
def ai_cost_ledger_scope(
    *,
    task_id: str = "",
    session_id: str = "",
    parent_request_id: str = "",
    retry: bool = False,
):
    """Attach correlation ids to nested observations without changing calls."""
    token = _SCOPE.set({
        "task_id": str(task_id or ""),
        "session_id": str(session_id or ""),
        "parent_request_id": str(parent_request_id or ""),
        "retry": bool(retry),
    })
    try:
        yield
    finally:
        _SCOPE.reset(token)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _usage_value(usage: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = _number(usage.get(key))
        if value is not None:
            return value
    return None


def _usage_details_value(usage: Mapping[str, Any], details_key: str, *keys: str) -> int | None:
    details = usage.get(details_key)
    if not isinstance(details, Mapping):
        return None
    return _usage_value(details, *keys)


def extract_usage(response: Mapping[str, Any] | None) -> dict[str, int | None]:
    """Normalize common OpenAI-compatible usage shapes without estimating."""
    usage = response.get("usage") if isinstance(response, Mapping) else None
    if not isinstance(usage, Mapping):
        return {
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
            "total_tokens": None,
            "reasoning_tokens": None,
            "non_reasoning_output_tokens": None,
            "usage_available": False,
        }

    input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
    cached_tokens = _usage_value(usage, "prompt_cache_hit_tokens", "cached_input_tokens")
    if cached_tokens is None:
        cached_tokens = _usage_details_value(
            usage, "prompt_tokens_details", "cached_tokens", "cached_input_tokens"
        )
    total_tokens = _usage_value(usage, "total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    reasoning_tokens = _usage_details_value(usage, "completion_tokens_details", "reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = _usage_details_value(usage, "output_tokens_details", "reasoning_tokens")
    # Reasoning is already included in completion_tokens; never bill it twice.
    non_reasoning_tokens = (
        output_tokens - reasoning_tokens
        if output_tokens is not None and reasoning_tokens is not None and reasoning_tokens <= output_tokens
        else None
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_tokens,
        "total_tokens": total_tokens,
        "reasoning_tokens": reasoning_tokens,
        "non_reasoning_output_tokens": non_reasoning_tokens,
        "usage_available": True,
    }


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _message_fingerprints(payload: Mapping[str, Any] | None) -> tuple[str, str]:
    messages = payload.get("messages") if isinstance(payload, Mapping) else None
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return "", ""
    compact_messages = [
        {"role": str(item.get("role") or ""), "content": str(item.get("content") or "")}
        for item in messages if isinstance(item, Mapping)
    ]
    system_prefix = [item for item in compact_messages if item["role"] == "system"]
    return _fingerprint(compact_messages), _fingerprint(system_prefix)


def _configured_price(name: str) -> float | None:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return None
    try:
        price = float(raw)
    except ValueError:
        return None
    return price if price >= 0 else None


def _estimate_cost(input_tokens: int | None, output_tokens: int | None) -> float | None:
    """Use explicit local pricing only; never embed stale provider prices in code."""
    input_price = _configured_price("LIVECLIPPER_AI_INPUT_PER_MILLION")
    output_price = _configured_price("LIVECLIPPER_AI_OUTPUT_PER_MILLION")
    if input_price is None or output_price is None:
        return None
    return round((float(input_tokens or 0) * input_price + float(output_tokens or 0) * output_price) / 1_000_000, 10)


def record_ai_call(
    *,
    module: str,
    stage: str,
    model: str,
    request_payload: Mapping[str, Any] | None,
    response_payload: Mapping[str, Any] | None = None,
    success: bool,
    task_id: str = "",
    session_id: str = "",
    parent_request_id: str = "",
    retry: bool = False,
    error_type: str = "",
    request_started_at: str = "",
    ledger_path: Path | None = None,
) -> str:
    """Append one provider-call fact and return its immutable request id.

    Fail-open is intentional: a broken local ledger may not break a user's
    paid AI workflow.  The ledger stores no request/response body.
    """
    request_id = uuid.uuid4().hex
    try:
        scope = _SCOPE.get()
        usage = extract_usage(response_payload)
        input_fp, prefix_fp = _message_fingerprints(request_payload)
        payload = request_payload if isinstance(request_payload, Mapping) else {}
        response = response_payload if isinstance(response_payload, Mapping) else {}
        choices = response.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
        message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
        record = {
            "version": LEDGER_VERSION,
            "request_id": request_id,
            "task_id": str(task_id or scope.get("task_id") or ""),
            "session_id": str(session_id or scope.get("session_id") or ""),
            "parent_request_id": str(parent_request_id or scope.get("parent_request_id") or ""),
            "timestamp": _utc_now(),
            "request_started_at": str(request_started_at or ""),
            "module": str(module or "other"),
            "stage": str(stage or "other"),
            "model": str(model or ""),
            **usage,
            "output_limit_tokens": _number(payload.get("max_tokens")),
            "reasoning_effort": str(payload.get("reasoning_effort") or ""),
            "finish_reason": str(choice.get("finish_reason") or ""),
            "content_characters": len(message["content"]) if isinstance(message.get("content"), str) else None,
            "reasoning_characters": len(message["reasoning_content"]) if isinstance(message.get("reasoning_content"), str) else None,
            "estimated_cost": _estimate_cost(usage["input_tokens"], usage["output_tokens"]),
            "pricing_configured": _configured_price("LIVECLIPPER_AI_INPUT_PER_MILLION") is not None
            and _configured_price("LIVECLIPPER_AI_OUTPUT_PER_MILLION") is not None,
            "success": bool(success),
            "retry": bool(retry or scope.get("retry")),
            "error_type": str(error_type or ""),
            "input_fingerprint": input_fp,
            "prompt_prefix_fingerprint": prefix_fp,
        }
        target = ledger_path or default_ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with _WRITE_LOCK:
            with target.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
    except Exception:
        pass
    return request_id


def _load_records(ledger_path: Path) -> tuple[list[dict[str, Any]], int]:
    records: list[dict[str, Any]] = []
    malformed = 0
    if not ledger_path.exists():
        return records, malformed
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if isinstance(value, dict) and value.get("version") == LEDGER_VERSION:
            records.append(value)
        else:
            malformed += 1
    return records, malformed


def _sum_known(records: Sequence[Mapping[str, Any]], field: str) -> int:
    return sum(int(value) for item in records if (value := _number(item.get(field))) is not None)


def _breakdown(records: Sequence[Mapping[str, Any]], field: str) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in records:
        groups[str(item.get(field) or "other")].append(item)
    total_tokens = _sum_known(records, "total_tokens")
    rows = []
    for name, items in groups.items():
        tokens = _sum_known(items, "total_tokens")
        rows.append({
            field: name,
            "requests": len(items),
            "known_tokens": tokens,
            "token_share": round(tokens / total_tokens, 6) if total_tokens else None,
            "average_known_tokens_per_request": round(tokens / len(items), 2) if items else 0.0,
            "usage_missing_requests": sum(not bool(item.get("usage_available")) for item in items),
            "failed_requests": sum(not bool(item.get("success")) for item in items),
            "retries": sum(bool(item.get("retry")) for item in items),
        })
    return sorted(rows, key=lambda row: (row["known_tokens"], row["requests"]), reverse=True)


def generate_ai_cost_reports(
    *,
    ledger_path: Path | None = None,
    output_dir: Path | None = None,
    session_id: str = "",
    task_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build deterministic cost and cache-opportunity reports from observed facts."""
    source = ledger_path or default_ledger_path()
    records, malformed = _load_records(source)
    requested_session = str(session_id or "").strip()
    requested_task = str(task_id or "").strip()
    if requested_session:
        records = [item for item in records if str(item.get("session_id") or "") == requested_session]
    if requested_task:
        records = [item for item in records if str(item.get("task_id") or "") == requested_task]
    total_tokens = _sum_known(records, "total_tokens")
    duplicate_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    prefix_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    task_stage_counts: Counter[tuple[str, str]] = Counter()
    for item in records:
        model = str(item.get("model") or "")
        input_fp = str(item.get("input_fingerprint") or "")
        prefix_fp = str(item.get("prompt_prefix_fingerprint") or "")
        if input_fp:
            duplicate_groups[(model, input_fp)].append(item)
        if prefix_fp:
            prefix_groups[(model, prefix_fp)].append(item)
        task_id = str(item.get("task_id") or "")
        if task_id:
            task_stage_counts[(task_id, str(item.get("stage") or "other"))] += 1

    duplicate_chains = [
        {
            "model": model,
            "input_fingerprint": fingerprint,
            "count": len(items),
            "known_tokens": _sum_known(items, "total_tokens"),
            "stages": sorted({str(item.get("stage") or "other") for item in items}),
            "task_ids": sorted({str(item.get("task_id") or "") for item in items if item.get("task_id")})[:10],
        }
        for (model, fingerprint), items in duplicate_groups.items() if len(items) > 1
    ]
    duplicate_chains.sort(key=lambda row: (row["known_tokens"], row["count"]), reverse=True)
    retry_count = sum(bool(item.get("retry")) for item in records)
    report = {
        "version": "ai-cost-report-v1",
        "generated_at": _utc_now(),
        "ledger_path": str(source),
        "session_id": requested_session,
        "task_id": requested_task,
        "historical_coverage_note": "Only calls observed after the ledger is installed can be attributed by module/stage.",
        "records": {
            "total_requests": len(records),
            "successful_requests": sum(bool(item.get("success")) for item in records),
            "failed_requests": sum(not bool(item.get("success")) for item in records),
            "retry_count": retry_count,
            "usage_missing_requests": sum(not bool(item.get("usage_available")) for item in records),
            "reasoning_usage_missing_requests": sum(_number(item.get("reasoning_tokens")) is None for item in records),
            "malformed_ledger_lines": malformed,
        },
        "tokens": {
            "known_total_tokens": total_tokens,
            "known_input_tokens": _sum_known(records, "input_tokens"),
            "known_output_tokens": _sum_known(records, "output_tokens"),
            "known_cached_input_tokens": _sum_known(records, "cached_input_tokens"),
            "known_reasoning_tokens": _sum_known(records, "reasoning_tokens"),
            "known_non_reasoning_output_tokens": _sum_known(records, "non_reasoning_output_tokens"),
            "average_known_tokens_per_request": round(total_tokens / len(records), 2) if records else 0.0,
        },
        "by_stage": _breakdown(records, "stage"),
        "by_module": _breakdown(records, "module"),
        "by_model": _breakdown(records, "model"),
        "output_diagnostics": [
            {key: item.get(key) for key in (
                "request_id", "stage", "model", "timestamp", "request_started_at",
                "input_tokens", "cached_input_tokens", "success", "error_type", "output_limit_tokens",
                "output_tokens", "reasoning_tokens", "non_reasoning_output_tokens",
                "finish_reason", "content_characters", "reasoning_characters",
            )}
            for item in records
        ],
        "top_duplicate_chains": duplicate_chains[:20],
        "repeated_task_stages": [
            {"task_id": task, "stage": stage, "count": count}
            for (task, stage), count in task_stage_counts.most_common(20) if count > 1
        ],
    }
    cache_report = {
        "version": "ai-cache-candidate-report-v1",
        "generated_at": _utc_now(),
        "ledger_path": str(source),
        "exact_input_reuse_candidates": duplicate_chains[:20],
        "shared_prompt_prefix_candidates": sorted([
            {
                "model": model,
                "prompt_prefix_fingerprint": fingerprint,
                "count": len(items),
                "known_tokens": _sum_known(items, "total_tokens"),
                "stages": sorted({str(item.get("stage") or "other") for item in items}),
            }
            for (model, fingerprint), items in prefix_groups.items() if len(items) > 1
        ], key=lambda row: (row["known_tokens"], row["count"]), reverse=True)[:20],
        "note": "Hashes identify reuse opportunities without retaining prompts or source subtitles.",
    }
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "ai_cost_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        (output_dir / "cache_candidate_report.json").write_text(json.dumps(cache_report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report, cache_report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate LiveClipper AI cost ledger reports.")
    parser.add_argument("--ledger", default="", help="Optional JSONL ledger path")
    parser.add_argument("--out-dir", default="", help="Directory for ai_cost_report.json")
    args = parser.parse_args()
    report, _ = generate_ai_cost_reports(
        ledger_path=Path(args.ledger) if args.ledger else None,
        output_dir=Path(args.out_dir) if args.out_dir else _data_root() / "ai_cost_reports",
    )
    print(json.dumps(report["records"], ensure_ascii=False))
