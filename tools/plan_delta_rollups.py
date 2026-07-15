"""Plan Runtime V3 direct rollup patches from an existing release channel."""

from __future__ import annotations

import argparse
import json
from collections import deque
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANNEL = ROOT / "release" / "stable.json"


def _version(data: dict[str, Any]) -> str:
    return str(data.get("version") or data.get("latest_version") or "")


def _patches(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("patches")
    if isinstance(raw, dict):
        raw = list(raw.values())
    return [item for item in (raw or []) if isinstance(item, dict)]


def _shortest_path(
    edges: dict[str, list[str]],
    source: str,
    target: str,
) -> list[str] | None:
    queue: deque[tuple[str, list[str]]] = deque([(source, [source])])
    seen = {source}
    while queue:
        version, path = queue.popleft()
        for next_version in edges.get(version, []):
            if next_version in seen:
                continue
            next_path = [*path, next_version]
            if next_version == target:
                return next_path
            seen.add(next_version)
            queue.append((next_version, next_path))
    return None


def plan_rollups(channel: dict[str, Any], *, rollup_after_versions: int = 2) -> dict[str, Any]:
    target = _version(channel)
    edges: dict[str, list[str]] = {}
    sources: set[str] = set()
    for patch in _patches(channel):
        source = str(patch.get("from_version") or "")
        dest = str(patch.get("to_version") or "")
        if not source or not dest or source == dest:
            continue
        edges.setdefault(source, []).append(dest)
        sources.add(source)
    required = []
    for source in sorted(sources):
        if source == target:
            continue
        path = _shortest_path(edges, source, target)
        if not path:
            continue
        edge_count = len(path) - 1
        has_direct = edge_count == 1
        if not has_direct and edge_count > rollup_after_versions:
            required.append(
                {
                    "from_version": source,
                    "to_version": target,
                    "chain_length": edge_count,
                    "chain": path,
                }
            )
    return {
        "schema_version": 1,
        "target_version": target,
        "rollup_after_versions": rollup_after_versions,
        "rollups_required": required,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan direct rollup patches for Runtime V3 channels.")
    parser.add_argument("--channel", type=Path, default=DEFAULT_CHANNEL)
    parser.add_argument("--rollup-after-versions", type=int, default=2)
    args = parser.parse_args()
    channel = json.loads(args.channel.read_text(encoding="utf-8-sig"))
    result = plan_rollups(
        channel,
        rollup_after_versions=max(1, int(args.rollup_after_versions)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
