"""Read-only evidence exporter for the external AI-selection audit.

This script never imports or invokes the selection pipeline. It only copies
already-produced local preview/API/cache/run-log evidence into this audit
directory and derives JSONL views from those immutable snapshots.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "ai_selection_audit"
APPDATA = Path.home() / "AppData" / "Roaming" / "LiveClipper"

CASES = {
    "case_001_current_single_preview": {
        "endpoint": "http://127.0.0.1:8765/api/smart-cut/preview/latest",
        "task_id": "smart-cut-1786874941-3bd2ea",
        "cache": "6560faa2b690c6655e544a8142bceb1aa37f78c0f0fbb946dfc96d38df4cfcdd.json",
        "kind": "current_single_preview",
    },
    "case_002_current_mix_preview": {
        "endpoint": "http://127.0.0.1:8765/api/mix/preview/latest",
        "task_id": "mix-1786875397-7785b1",
        "cache": "9307ccb71027dc29134e57453b791c8f98986a92c755e0ebcffc4c41387218bb.json",
        "kind": "current_mix_preview",
    },
    "case_003_historic_success_limited_lineage": {
        "run_log": "20260815_200615_1_成功.json",
        "kind": "historic_completed_render",
    },
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def candidate_rows(preview: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, clip in enumerate(preview.get("candidate_clips") or []):
        item = dict(clip) if isinstance(clip, dict) else {"raw": clip}
        item.update({
            "audit_stage": "preview_candidate_pool_snapshot",
            "audit_candidate_position": index,
            "visibility_note": "Persisted preview pool, not raw ASR or a complete per-stage ledger.",
        })
        rows.append(item)
    return rows


def review_rows(cache: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    payload = cache.get("payload") if isinstance(cache.get("payload"), dict) else cache
    for index, card in enumerate(payload.get("cards") or []):
        item = dict(card) if isinstance(card, dict) else {"raw": card}
        item.update({"audit_stage": "content_review_card", "audit_card_position": index})
        rows.append(item)
    return rows


def export_current_case(case_id: str, case: dict[str, str]) -> None:
    root = AUDIT / "audit_cases" / case_id
    preview = fetch_json(case["endpoint"])
    write_json(root / "preview_latest_snapshot.json", preview)
    write_json(root / "source_info.json", {
        "case_kind": case["kind"],
        "task_id": preview.get("task_id") or case.get("task_id"),
        "preview_id": preview.get("id"),
        "source_video": preview.get("video"),
        "source_videos": preview.get("sources") or [],
        "srt_path": preview.get("srt_path"),
        "target_duration": preview.get("target_duration"),
        "created_at": preview.get("created_at"),
        "snapshot_limits": [
            "No original video binary copied.",
            "No raw network LLM request was historically persisted.",
            "Per-candidate hard-filter rejection identities are unavailable unless present in the snapshot.",
        ],
    })
    write_jsonl(root / "candidates_generated.jsonl", candidate_rows(preview))

    logs_url = f"http://127.0.0.1:8765/api/tasks/{case['task_id']}/logs"
    try:
        write_json(root / "task_logs_snapshot.json", fetch_json(logs_url))
    except Exception as exc:
        write_json(root / "task_logs_snapshot.json", {"available": False, "error": str(exc)})

    cache_path = APPDATA / "cache" / "ai_content_review" / case["cache"]
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        write_json(root / "content_review_cache_snapshot.json", cache)
        write_jsonl(root / "after_review.jsonl", review_rows(cache))
    else:
        write_json(root / "content_review_cache_snapshot.json", {"available": False})
        write_jsonl(root / "after_review.jsonl", [])

    analysis = preview.get("analysis") or preview.get("analysis_metadata") or {}
    write_json(root / "director_input_available_metadata.json", {
        "analysis": analysis,
        "content_review_summary": preview.get("content_review_summary"),
        "selection_context_summary": preview.get("selection_context_summary"),
        "note": "Original Director request body was not persisted; surviving metadata only.",
    })
    write_json(root / "director_output.json", {
        "selection_draft": preview.get("selection_draft"),
        "clips": preview.get("clips"),
        "quality_report": preview.get("quality_report"),
        "plan_quality_report": preview.get("plan_quality_report"),
    })
    write_json(root / "final_playlist.json", {
        "selection_result": preview.get("selection_result"),
        "selection_manifest": preview.get("selection_manifest"),
        "final_selection": preview.get("final_selection"),
        "clips": preview.get("clips"),
    })

    final_keys = set()
    manifest = ((preview.get("selection_result") or {}).get("manifest") or preview.get("selection_manifest") or {})
    for item in manifest.get("items") or []:
        final_keys.add((str(item.get("source_id") or ""), round(float(item.get("start") or 0), 3), round(float(item.get("end") or 0), 3)))
    lifecycle: list[dict[str, Any]] = []
    for item in candidate_rows(preview):
        start, end = float(item.get("start") or 0), float(item.get("end") or 0)
        key = (str(item.get("source") or item.get("source_id") or ""), round(start, 3), round(end, 3))
        lifecycle.append({
            "candidate_key": item.get("candidate_key"),
            "source_video": key[0], "start": start, "end": end, "asr": item.get("text"),
            "candidate_generation_reason": "unknown_from_preview_snapshot",
            "hard_filter_result": "unknown_from_preview_snapshot",
            "content_review": "joinable_only_via_after_review_cards",
            "marketing_intent": "see_content_review_cache_snapshot",
            "director_result": "final_manifest_selected" if key in final_keys else "not_selected_or_not_exposed",
            "dedupe_result": "not_individually_persisted", "duration_result": "see_final_playlist",
            "final_selected": key in final_keys,
            "rejection_stage": "none" if key in final_keys else "not_traceable_from_current_persisted_evidence",
            "rejection_reason": "No per-candidate stage ledger is persisted for this task.",
        })
    write_jsonl(root / "candidate_lifecycle.jsonl", lifecycle)
    write_json(root / "stage_availability.json", {
        "raw_asr": "not exported; source SRT path recorded only",
        "semantic_candidates_before_freeze": "not separately persisted",
        "after_hard_filter": "counts may be in task logs; identities not persisted",
        "after_review": "available as content review cards",
        "director_input_request": "not historically persisted; metadata only",
        "director_output": "available as preview/selection draft fields",
        "final_playlist": "available as selection manifest/result fields",
    })


def export_historic_case(case_id: str, case: dict[str, str]) -> None:
    root = AUDIT / "audit_cases" / case_id
    source = APPDATA / "logs" / "runs" / case["run_log"]
    run = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {"available": False, "path": str(source)}
    write_json(root / "run_log.json", run)
    write_json(root / "source_info.json", {
        "case_kind": case["kind"], "source_video": run.get("视频"),
        "run_timestamp": run.get("时间"), "result": run.get("结果"),
        "note": "Historic run record lacks raw candidate and per-stage lineage snapshots.",
    })
    write_jsonl(root / "candidate_lifecycle.jsonl", [])
    write_json(root / "stage_availability.json", {
        "all_candidate_lifecycle_stages": "not available in historic run log",
        "final_selection_audit": (run.get("选片审计") or run.get("选片") or {}),
    })


def main() -> None:
    write_json(AUDIT / "runtime_snapshot.json", fetch_json("http://127.0.0.1:8765/api/runtime"))
    for case_id, case in CASES.items():
        export_current_case(case_id, case) if "endpoint" in case else export_historic_case(case_id, case)
    feedback = APPDATA / "ai_feedback" / "preview_selection_feedback.jsonl"
    target = AUDIT / "ground_truth_evidence" / "preview_selection_feedback.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    if feedback.exists():
        shutil.copy2(feedback, target)
    print(AUDIT)


if __name__ == "__main__":
    main()
