# -*- coding: utf-8 -*-
"""Replay P0.5A.3 only over previously program-rejected Micro Beats.

This runner is deliberately a frozen-inventory calibration.  It does not
rediscover the SRT, alter Director/Journey/Casting, or call M3.  Explicit AI
semantic rejects stay frozen; only the historical Boundary contract rejects
are re-reviewed under the short-Beat and micro-expand rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from micro_beat_inventory import (  # noqa: E402
    MICRO_BEAT_PREFERRED_MIN_SECONDS,
    MICRO_BEAT_SHORT_BEAT_MIN_SECONDS,
    adjudicate_micro_beat_publishability,
    build_micro_beat_source_rows,
    reconstruct_frozen_micro_beat_candidates,
    replay_short_beat_contract_rejects,
)
from ai_clipper import load_settings  # noqa: E402
from run_m3_golden_source_identity import M3_GOLDEN_SOURCES  # noqa: E402
from run_m3_new_golden_plan_fidelity import _asset_ledger_for_source  # noqa: E402
from semantic_word_binder import build_semantic_srt_word_timeline  # noqa: E402


def _text(value: Any) -> str:
    return str(value or "").strip()


def _as_rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value or () if isinstance(item, Mapping)]


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _old_reason_groups(records: list[dict[str, Any]]) -> dict[str, int]:
    groups = {
        "duration_policy_false_negatives": 0,
        "short_exception_contract_false_negatives": 0,
        "lineage_program_contract_false_negatives": 0,
        "other_program_contract_false_negatives": 0,
    }
    for item in records:
        reason = _text(item.get("reject_reason"))
        if "duration" in reason:
            groups["duration_policy_false_negatives"] += 1
        elif "short_exception" in reason or "short_complete" in reason:
            groups["short_exception_contract_false_negatives"] += 1
        elif any(token in reason for token in ("lineage", "word", "source_words")):
            groups["lineage_program_contract_false_negatives"] += 1
        else:
            groups["other_program_contract_false_negatives"] += 1
    return groups


def _top_restored(
    *, restored: list[dict[str, Any]], old_by_id: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in restored:
        source_beat_id = _text(item.get("source_beat_id")) or _text(item.get("beat_id"))
        old = dict(old_by_id.get(source_beat_id) or {})
        result.append({
            "before": {
                "beat_id": source_beat_id,
                "text": _text(old.get("original_text")),
                "old_reject_reason": _text(old.get("reject_reason")),
            },
            "after": {
                "beat_id": _text(item.get("beat_id")),
                "text": _text(item.get("text")),
                "duration_seconds": item.get("duration_seconds"),
                "state": _text(item.get("publishability_status")),
                "role_permissions": list(item.get("role_permissions") or ()),
                "context_requirement": _text(item.get("context_requirement")),
                "micro_expanded": bool(item.get("micro_expanded")),
            },
            "why_now_valid": _text((item.get("p0_5a2_publishable_adjudication") or {}).get("reason")),
        })
    return sorted(
        result,
        key=lambda item: (
            item["after"]["state"] != "publishable_clean",
            -_number(item["after"]["duration_seconds"]),
            item["after"]["beat_id"],
        ),
    )[:20]


def _top_still_rejected(
    *, replay_rejected: list[dict[str, Any]], old_by_id: Mapping[str, Mapping[str, Any]],
    adjudication_rejected: list[dict[str, Any]], reviewed_ids: set[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in replay_rejected:
        source_beat_id = _text(item.get("source_beat_id")) or _text(item.get("beat_id"))
        old = dict(old_by_id.get(source_beat_id) or {})
        rows.append({
            "beat_id": source_beat_id,
            "before_text": _text(old.get("original_text")),
            "old_reject_reason": _text(old.get("reject_reason")),
            "after_reject_reason": _text(item.get("reject_reason")),
            "why_still_rejected": "短时长放宽后仍无法通过边界/语义合同",
        })
    for item in adjudication_rejected:
        source_beat_id = _text(item.get("source_beat_id")) or _text(item.get("beat_id"))
        if source_beat_id not in reviewed_ids:
            continue
        old = dict(old_by_id.get(source_beat_id) or {})
        rows.append({
            "beat_id": source_beat_id,
            "before_text": _text(old.get("original_text")),
            "old_reject_reason": _text(old.get("reject_reason")),
            "after_reject_reason": _text(item.get("reject_reason")),
            "why_still_rejected": "边界可解析但最终口播仍不成立",
        })
    return sorted(rows, key=lambda item: (item["after_reject_reason"], item["beat_id"]))[:20]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", default="jccc_deep_roast_hoodie")
    parser.add_argument("--source-srt", default="")
    parser.add_argument("--source-report", default="", help="冻结回归报告；读取其中同案例 source_srt")
    parser.add_argument("--previous-inventory", required=True)
    parser.add_argument("--discovery-dir", required=True)
    parser.add_argument("--out-dir", default="workspace/p05a3_short_beat_recall_caramel")
    args = parser.parse_args()

    previous_path = Path(args.previous_inventory).resolve()
    discovery_dir = Path(args.discovery_dir).resolve()
    output_dir = (ROOT / args.out_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    previous = json.loads(previous_path.read_text(encoding="utf-8"))
    response_prefix = previous_path.name.removesuffix(".p0_5a2.beat_inventory.json")
    raw_responses = [
        path.read_text(encoding="utf-8")
        for path in sorted(discovery_dir.glob(f"{response_prefix}.p0_5a2.discovery.*.response.txt"))
    ]
    if not raw_responses:
        raise RuntimeError("没有找到冻结的 P0.5A.2 discovery responses")

    source_srt = _text(args.source_srt)
    if not source_srt and _text(args.source_report):
        source_report = json.loads(Path(args.source_report).resolve().read_text(encoding="utf-8"))
        source_srt = _text(((source_report.get("cases") or {}).get(args.case) or {}).get("source_srt"))
    source_srt = source_srt or _text(M3_GOLDEN_SOURCES.get(args.case, {}).get("srt"))
    if not source_srt:
        raise RuntimeError("请通过 --source-srt 提供同源 SRT")
    ledger_context = _asset_ledger_for_source(args.case, source_srt=str(Path(source_srt).resolve()))
    timeline = build_semantic_srt_word_timeline(str(ledger_context["source_srt"]))
    source_rows = build_micro_beat_source_rows(
        source_units=ledger_context["source_context_units"],
        hard_safe_subtitle_ids=[int(item.get("candidate_id") or 0) for item in ledger_context["assets"]],
        word_timeline=timeline,
    )
    reconstructed = reconstruct_frozen_micro_beat_candidates(
        raw_inventory_responses=raw_responses, source_rows=source_rows,
        allow_source_batch_layout_drift=True,
    )
    if reconstructed.get("status") != "frozen_inventory_reconstructed":
        raise RuntimeError("无法重建冻结 P0.5A 原始 Beat 库：" + ";".join(reconstructed.get("errors") or ()))

    settings = load_settings()
    if not _text(settings.get("api_key")):
        raise RuntimeError("未找到 AI API Key，不能运行 P0.5A.3 回放")
    boundary_paths: dict[str, str] = {}

    def write_boundary_response(batch_id: str, value: str) -> None:
        path = output_dir / f"p0_5a3.boundary.{batch_id}.response.txt"
        path.write_text(value, encoding="utf-8")
        boundary_paths[batch_id] = path.name

    replay = replay_short_beat_contract_rejects(
        previous_inventory=previous,
        reconstructed_raw_inventory=reconstructed,
        source_rows=source_rows,
        api_key=_text(settings.get("api_key")),
        base_url=_text(settings.get("base_url")) or "https://api.deepseek.com",
        model=_text(settings.get("model")) or "deepseek-v4-flash",
        response_hook=write_boundary_response,
    )
    if replay.get("status") != "publishable_inventory_completed":
        raise RuntimeError("P0.5A.3 Boundary 回放失败：" + ";".join(replay.get("errors") or ()))

    old_contract = [
        item for item in _as_rows(previous.get("boundary_rejected_beats"))
        if _text(item.get("decision")).upper() != "REJECT"
    ]
    old_by_id = {_text(item.get("beat_id")): item for item in old_contract}
    reviewed_ids = set(old_by_id)
    # Existing clean/visual actors are retained as frozen positives.  Newly
    # recovered Boundary candidates and the frozen positives receive one
    # current three-state adjudication solely to attach P0.5A.3 role/context
    # permissions; this is not a source rediscovery or automatic selection.
    combined_boundary = {
        "status": "publishable_inventory_completed",
        "publishable_beat_inventory": (
            _as_rows(previous.get("publishable_beat_inventory"))
            + _as_rows(replay.get("publishable_beat_inventory"))
        ),
        "boundary_rejected_beats": [
            item for item in _as_rows(previous.get("boundary_rejected_beats"))
            if _text(item.get("decision")).upper() == "REJECT"
        ] + _as_rows(replay.get("boundary_rejected_beats")),
        "boundary_statistics": dict(replay.get("boundary_statistics") or {}),
        "contract": dict(replay.get("contract") or {}),
    }
    adjudication_paths: dict[str, str] = {}

    def write_adjudication_response(batch_id: str, value: str) -> None:
        path = output_dir / f"p0_5a3.adjudication.{batch_id}.response.txt"
        path.write_text(value, encoding="utf-8")
        adjudication_paths[batch_id] = path.name

    calibrated = adjudicate_micro_beat_publishability(
        boundary_result=combined_boundary,
        api_key=_text(settings.get("api_key")),
        base_url=_text(settings.get("base_url")) or "https://api.deepseek.com",
        model=_text(settings.get("model")) or "deepseek-v4-flash",
        response_hook=write_adjudication_response,
    )
    if calibrated.get("status") != "publishable_inventory_calibrated":
        raise RuntimeError("P0.5A.3 publishability 主判失败：" + ";".join(calibrated.get("errors") or ()))

    final_beats = _as_rows(calibrated.get("publishable_beat_inventory"))
    restored = [
        item for item in final_beats
        if (_text(item.get("source_beat_id")) or _text(item.get("beat_id"))) in reviewed_ids
    ]
    replay_rejected = _as_rows(replay.get("boundary_rejected_beats"))
    final_adjudication_rejected = _as_rows(calibrated.get("p0_5a2_publishable_adjudication_rejected_beats"))
    audit = {
        "stage": "P0.5A.3 Short Beat Recall Calibration",
        "frozen_contract_rejects_count": len(old_contract),
        "re_reviewed_count": int((replay.get("p0_5a3_short_recall_replay") or {}).get("re_reviewed_count") or 0),
        "restored_clean_count": sum(item.get("publishability_status") == "publishable_clean" for item in restored),
        "restored_visual_count": sum(item.get("publishability_status") == "publishable_visual" for item in restored),
        "micro_expanded_count": sum(bool(item.get("micro_expanded")) for item in restored),
        "short_beats_1_to_2_seconds_count": sum(
            MICRO_BEAT_SHORT_BEAT_MIN_SECONDS <= _number(item.get("duration_seconds")) < MICRO_BEAT_PREFERRED_MIN_SECONDS
            for item in final_beats
        ),
        "final_actor_pool_count": len(final_beats),
        "final_usable_seconds": round(sum(_number(item.get("duration_seconds")) for item in final_beats), 3),
        "still_rejected_count": len(replay_rejected) + sum(
            (_text(item.get("source_beat_id")) or _text(item.get("beat_id"))) in reviewed_ids
            for item in final_adjudication_rejected
        ),
        "categories": {
            **_old_reason_groups(old_contract),
            "micro_expand_recovered": sum(bool(item.get("micro_expanded")) for item in restored),
            "lineage_program_contract_recovered": sum(
                any(token in _text((old_by_id.get(_text(item.get("source_beat_id")) or _text(item.get("beat_id"))) or {}).get("reject_reason"))
                    for token in ("lineage", "word", "source_words"))
                for item in restored
            ),
            "true_reject": len(replay_rejected) + sum(
                (_text(item.get("source_beat_id")) or _text(item.get("beat_id"))) in reviewed_ids
                for item in final_adjudication_rejected
            ),
        },
        "top_20_restored_cases": _top_restored(restored=restored, old_by_id=old_by_id),
        "top_20_still_rejected_cases": _top_still_rejected(
            replay_rejected=replay_rejected,
            old_by_id=old_by_id,
            adjudication_rejected=final_adjudication_rejected,
            reviewed_ids=reviewed_ids,
        ),
        "boundary_response_files": boundary_paths,
        "adjudication_response_files": adjudication_paths,
    }
    calibrated["p0_5a3_short_beat_recall_audit"] = audit
    calibrated["contract"] = dict(calibrated.get("contract") or {}) | {
        "p0_5a3_short_beat_recall_calibration": True,
        "director_journey_casting_whole_video_audit_modified": False,
        "m3_modified": False,
        "actor_pool_origin": "frozen_p0_5a2_positives_plus_replayed_program_contract_rejects",
    }
    output = output_dir / "p0_5a3_actor_pool.json"
    output.write_text(json.dumps(calibrated, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"actor_pool": str(output), **audit}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
