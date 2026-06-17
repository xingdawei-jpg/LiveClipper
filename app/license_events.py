# -*- coding: utf-8 -*-
"""Privacy-safe licensing and usage event queue.

The client records a small JSONL queue locally and only uploads it when
LIVECLIPPER_EVENT_API_URL or LIVECLIPPER_AUTH_API_URL is configured.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
import urllib.request
from pathlib import Path
from typing import Any


QUEUE_FILE = "license_events_queue.jsonl"
STATE_FILE = "license_events_state.json"


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default)) or str(default)))
    except Exception:
        return default


MAX_QUEUE_EVENTS = _int_env("LIVECLIPPER_EVENT_QUEUE_MAX", 500)
UPLOAD_BATCH_SIZE = _int_env("LIVECLIPPER_EVENT_BATCH_SIZE", 50)
ASYNC_FLUSH_INTERVAL = _int_env("LIVECLIPPER_EVENT_FLUSH_INTERVAL", 60)

_LOCK = threading.Lock()
_FLUSHING = False
_LAST_ASYNC_FLUSH = 0.0


def _queue_dir() -> Path:
    override = os.environ.get("LIVECLIPPER_EVENT_QUEUE_DIR", "").strip()
    if override:
        path = Path(override)
    else:
        try:
            from platform_config import LICENSE_CACHE_DIR

            path = Path(LICENSE_CACHE_DIR)
        except Exception:
            path = Path(os.environ.get("APPDATA", Path.home())) / "LiveClipper"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _queue_path() -> Path:
    return _queue_dir() / QUEUE_FILE


def _state_path() -> Path:
    return _queue_dir() / STATE_FILE


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data: Any) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_app_version() -> str:
    candidates = [
        Path(__file__).with_name("version.json"),
        Path(__file__).resolve().parents[1] / "version.json",
    ]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return str(data.get("version") or data.get("latest_version") or "dev")
        except Exception:
            pass
    return "dev"


def _license_cache() -> dict[str, Any]:
    try:
        return _load_json(_queue_dir() / "license_cache.json", {}) or {}
    except Exception:
        return {}


def _normalize_code(code: Any) -> str:
    return str(code or "").replace("-", "").strip().lower()


def _code_hash(raw_code: str) -> str:
    if not raw_code:
        return ""
    return hashlib.sha256(raw_code.encode("utf-8")).hexdigest()


def _dist_id_from_code(raw_code: str) -> str:
    if len(raw_code) in (34, 36):
        return raw_code[2:6].upper()
    return ""


def _license_context() -> dict[str, Any]:
    cache = _license_cache()
    raw_code = _normalize_code(cache.get("code") or cache.get("activation_code"))
    context: dict[str, Any] = {
        "code_hash": _code_hash(raw_code),
        "code_suffix": raw_code[-8:] if raw_code else "",
        "distributor_id": str(cache.get("distributor_id") or _dist_id_from_code(raw_code)),
        "plan": str(cache.get("plan") or ""),
        "plan_name": str(cache.get("plan_name") or ""),
        "machine_hash": str(cache.get("machine_id") or cache.get("trial_machine_id") or ""),
    }
    return {key: value for key, value in context.items() if value}


def _sanitize_metadata(value: Any, depth: int = 0) -> Any:
    if depth > 3:
        return str(value)[:200]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (list, tuple)):
        return [_sanitize_metadata(item, depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in list(value.items())[:50]:
            clean[str(key)[:80]] = _sanitize_metadata(item, depth + 1)
        return clean
    return str(value)[:500]


def _event_key(event_type: str, feature: str) -> str:
    ctx = _license_context()
    identity = ctx.get("code_hash") or ctx.get("machine_hash") or "anonymous"
    return "|".join([event_type, feature or "", str(identity)])


def _dedupe_allowed(event_type: str, feature: str, dedupe_seconds: int) -> bool:
    if dedupe_seconds <= 0:
        return True
    now = int(time.time())
    path = _state_path()
    state = _load_json(path, {})
    key = _event_key(event_type, feature)
    last = int(state.get(key, 0) or 0)
    if last and now - last < dedupe_seconds:
        return False
    state[key] = now
    cutoff = now - 30 * 86400
    state = {k: v for k, v in state.items() if int(v or 0) >= cutoff}
    _save_json(path, state)
    return True


def _read_queue() -> list[dict[str, Any]]:
    path = _queue_path()
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if isinstance(event, dict):
                events.append(event)
    except Exception:
        return []
    return events[-MAX_QUEUE_EVENTS:]


def _write_queue(events: list[dict[str, Any]]) -> None:
    path = _queue_path()
    events = events[-MAX_QUEUE_EVENTS:]
    text = "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events)
    path.write_text(text, encoding="utf-8")


def _append_event(event: dict[str, Any]) -> None:
    with _LOCK:
        events = _read_queue()
        events.append(event)
        _write_queue(events)


def _api_endpoint() -> str:
    base = (
        os.environ.get("LIVECLIPPER_EVENT_API_URL", "").strip()
        or os.environ.get("LIVECLIPPER_AUTH_API_URL", "").strip()
    )
    if not base:
        return ""
    base = base.rstrip("/")
    if base.endswith("/api/events/batch"):
        return base
    return base + "/api/events/batch"


def record_event(
    event_type: str,
    feature: str = "",
    units: int = 1,
    metadata: dict[str, Any] | None = None,
    *,
    dedupe_seconds: int = 0,
    flush: bool = True,
) -> bool:
    """Record one event locally and optionally schedule an async upload."""
    event_type = str(event_type or "").strip()
    if not event_type:
        return False
    feature = str(feature or "").strip()
    if not _dedupe_allowed(event_type, feature, int(dedupe_seconds or 0)):
        return False

    try:
        event: dict[str, Any] = {
            "schema": 1,
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "feature": feature,
            "units": max(1, int(units or 1)),
            "occurred_at": int(time.time()),
            "app_version": _load_app_version(),
            "license": _license_context(),
            "metadata": _sanitize_metadata(metadata or {}),
        }
        _append_event(event)
        if flush:
            flush_events_async()
        return True
    except Exception:
        return False


def flush_events() -> bool:
    """Upload queued events. Returns False when no endpoint is configured."""
    endpoint = _api_endpoint()
    if not endpoint:
        return False

    with _LOCK:
        events = _read_queue()
        if not events:
            return True
        batch = events[:UPLOAD_BATCH_SIZE]

    try:
        body = json.dumps({"events": batch}).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            if resp.status >= 300:
                return False
    except Exception:
        return False

    with _LOCK:
        current = _read_queue()
        sent_ids = {event.get("event_id") for event in batch}
        remaining = [event for event in current if event.get("event_id") not in sent_ids]
        _write_queue(remaining)
    return True


def flush_events_async() -> bool:
    """Throttle background uploads so feature paths do not block the UI."""
    global _FLUSHING, _LAST_ASYNC_FLUSH
    if not _api_endpoint():
        return False
    now = time.time()
    if _FLUSHING or now - _LAST_ASYNC_FLUSH < ASYNC_FLUSH_INTERVAL:
        return False
    _FLUSHING = True
    _LAST_ASYNC_FLUSH = now

    def _worker() -> None:
        global _FLUSHING
        try:
            flush_events()
        finally:
            _FLUSHING = False

    try:
        threading.Thread(target=_worker, daemon=True).start()
        return True
    except Exception:
        _FLUSHING = False
        return False
