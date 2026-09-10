# -*- coding: utf-8 -*-
"""P0.5A.2: calibrate publishability over the frozen P0.5A.1 Boundary snapshot."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_clipper import load_settings  # noqa: E402
from micro_beat_inventory import adjudicate_micro_beat_publishability  # noqa: E402


CASE_ID = "jccc_deep_roast_hoodie"
P05A1_BOUNDARY_SNAPSHOT = (
    ROOT / "workspace" / "p05a1_micro_beat_boundary_quality_caramel"
    / "p0_5a1_micro_beat_boundary_quality_20260828_203130.json"
)

# This is a human-curated calibration set, not a programmatic semantic selector.
# It covers every word-bound Beat in the frozen snapshot so an AI judgment can be
# audited against the concrete false-positive / false-negative cases before Arc work.
GOLDEN_BEAT_CALIBRATION: dict[str, dict[str, str]] = {
    "B002": {"expected_status": "publishable_clean", "note": "腰胯修饰结果"},
    "B007": {"expected_status": "publishable_clean", "expected_narrative_priority": "low", "note": "可发布但非商品故事主句"},
    "B008": {"expected_status": "publishable_clean", "note": "肩部往里挖机制"},
    "B011": {"expected_status": "publishable_clean", "note": "完整显瘦结果但非必选主句"},
    "B013": {"expected_status": "publishable_clean", "note": "全弹里衬风险解除"},
    "B017": {"expected_status": "reject", "note": "明显ASR异常"},
    "B022": {"expected_status": "publishable_clean", "note": "完整公开尺码表"},
    "B025": {"expected_status": "publishable_clean", "note": "盖手袖型信息"},
    "B028": {"expected_status": "publishable_clean", "note": "三伏天透薄体验"},
    "B029": {"expected_status": "publishable_clean", "note": "再生纤维材质信任"},
    "B030": {"expected_status": "publishable_clean", "note": "干爽手感"},
    "B031": {"expected_status": "reject", "note": "ASR语义不稳"},
    "B035": {"expected_status": "publishable_clean", "note": "网纱清爽工艺"},
    "B041": {"expected_status": "publishable_clean", "note": "花色调性"},
    "B042": {"expected_status": "reject", "note": "明显ASR异常"},
    "B044": {"expected_status": "publishable_clean", "note": "盖手袖型结果"},
    "B046": {"expected_status": "publishable_clean", "note": "松弛感和透风"},
    "B048": {"expected_status": "publishable_clean", "note": "材质信任信息"},
    "B049": {"expected_status": "publishable_clean", "note": "完整公开尺码表"},
    "B052": {"expected_status": "reject", "note": "个人孕期尺码回应"},
    "B062": {"expected_status": "publishable_visual", "note": "肩部位置依赖画面"},
    "B067": {"expected_status": "publishable_clean", "note": "搭配麻烦痛点"},
    "B072": {"expected_status": "publishable_visual", "note": "揉搓展示依赖画面"},
    "B073": {"expected_status": "reject", "note": "未完成谓语"},
    "B074": {"expected_status": "reject", "note": "无独立商业结果"},
    "B079": {"expected_status": "publishable_visual", "note": "用料展示依赖画面"},
    "B080": {"expected_status": "reject", "note": "对象不明的直播回应"},
    "B082": {"expected_status": "reject", "note": "ASR错乱"},
    "B084": {"expected_status": "publishable_clean", "note": "大码整体显瘦"},
    "B085": {"expected_status": "publishable_clean", "note": "马甲形状藏肉机制"},
    "B086": {"expected_status": "publishable_clean", "note": "拜拜肉藏肉结果"},
    "B087.A": {"expected_status": "publishable_visual", "note": "腋下透气展示"},
    "B087.B": {"expected_status": "publishable_clean", "note": "出汗仍清爽体验"},
    "B087.C": {"expected_status": "publishable_clean", "note": "不闷热结果已落地"},
    "B088": {"expected_status": "reject", "note": "ASR材质词异常"},
    "B089.A": {"expected_status": "publishable_clean", "note": "再生纤维信任说明"},
    "B089.B": {"expected_status": "reject", "note": "因果和指代未落地"},
}


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _boundary_children(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    beats: list[dict[str, Any]] = []
    for audit in snapshot.get("boundary_decision_audit") or ():
        if not isinstance(audit, Mapping):
            continue
        for child in audit.get("split_children") or ():
            if not isinstance(child, Mapping) or not str(child.get("beat_id") or ""):
                continue
            beats.append(dict(child))
    seen: set[str] = set()
    duplicates: set[str] = set()
    for beat in beats:
        beat_id = str(beat["beat_id"])
        if beat_id in seen:
            duplicates.add(beat_id)
        seen.add(beat_id)
    if duplicates:
        raise RuntimeError(f"冻结 Boundary Snapshot 存在重复 Beat：{sorted(duplicates)}")
    return beats


def _frozen_boundary_result(snapshot: Mapping[str, Any], beats: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "frozen_boundary_snapshot_loaded",
        "publishable_beat_inventory": beats,
        "boundary_statistics": {
            "frozen_boundary_beat_count": len(beats),
            "frozen_boundary_usable_seconds": round(sum(float(item.get("duration_seconds") or 0.0) for item in beats), 3),
        },
        "contract": {
            "stage": "P0_5_micro_beat_boundary_quality_snapshot",
            "frozen_raw_beat_candidates_only": True,
            "complete_srt_rescanned_for_discovery": False,
            "word_boundary_recomputed": False,
            "source_snapshot": "p0_5a1_boundary_decision_audit_before_legacy_p0_2_veto",
            "arc_assembly_performed": False,
            "dense_composition_performed": False,
            "m3_invoked": False,
        },
    }


def _actual_adjudications(result: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    actual: dict[str, dict[str, Any]] = {}
    for beat in result.get("publishable_beat_inventory") or ():
        if isinstance(beat, Mapping):
            actual[str(beat.get("beat_id") or "")] = _mapping(beat.get("p0_5a2_publishable_adjudication"))
    for beat in result.get("p0_5a2_publishable_adjudication_rejected_beats") or ():
        if isinstance(beat, Mapping):
            actual[str(beat.get("beat_id") or "")] = _mapping(beat.get("ai_publishable_adjudication"))
    return actual


def _evaluate_golden_calibration(result: Mapping[str, Any], frozen_beats: list[dict[str, Any]]) -> dict[str, Any]:
    actual = _actual_adjudications(result)
    frozen_ids = {str(item["beat_id"]) for item in frozen_beats}
    if frozen_ids != set(GOLDEN_BEAT_CALIBRATION):
        raise RuntimeError("Golden Beat Calibration Set 与冻结 Boundary Beat 集不一致，已停止。")
    cases: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    for beat in frozen_beats:
        beat_id = str(beat["beat_id"])
        expected = GOLDEN_BEAT_CALIBRATION[beat_id]
        declared = actual.get(beat_id, {})
        actual_status = str(declared.get("publishability_status") or "missing")
        actual_priority = str(declared.get("narrative_priority") or "missing")
        status_matches = actual_status == expected["expected_status"]
        priority_expected = expected.get("expected_narrative_priority")
        priority_matches = not priority_expected or actual_priority == priority_expected
        case = {
            "beat_id": beat_id,
            "text": str(beat.get("text") or ""),
            "duration_seconds": beat.get("duration_seconds"),
            "expected_status": expected["expected_status"],
            "actual_status": actual_status,
            "expected_narrative_priority": priority_expected or None,
            "actual_narrative_priority": actual_priority,
            "note": expected["note"],
            "passed": status_matches and priority_matches,
        }
        cases.append(case)
        if not case["passed"]:
            mismatches.append(case)
    return {
        "case_count": len(cases),
        "passed": not mismatches,
        "mismatch_count": len(mismatches),
        "cases": cases,
        "mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run P0.5A.2 one-pass publishable Beat calibration from a frozen Boundary snapshot."
    )
    parser.add_argument("--boundary-snapshot", default=str(P05A1_BOUNDARY_SNAPSHOT))
    parser.add_argument("--output-dir", default=str(ROOT / "workspace" / "p05a2_publishable_beat_calibration_caramel"))
    args = parser.parse_args()
    snapshot_path = Path(args.boundary_snapshot)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    frozen_beats = _boundary_children(snapshot)
    frozen_seconds = round(sum(float(item.get("duration_seconds") or 0.0) for item in frozen_beats), 3)
    if len(frozen_beats) != 37 or frozen_seconds != 120.31:
        raise RuntimeError(
            f"P0.5A.2 只接受 37 条 / 120.31 秒的冻结 Boundary Snapshot；当前为 {len(frozen_beats)} 条 / {frozen_seconds} 秒。"
        )
    if {str(item["beat_id"]) for item in frozen_beats} != set(GOLDEN_BEAT_CALIBRATION):
        raise RuntimeError("Golden Beat Calibration Set 未覆盖所有冻结 Boundary Beat，已停止。")
    settings = _mapping(load_settings())
    if not str(settings.get("api_key") or ""):
        raise RuntimeError("未找到 AI API Key，无法进行 P0.5A.2 Publishable Beat Calibration。")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    response_paths: list[Path] = []

    def write_response(batch_id: str, content: str) -> None:
        path = output_dir / f"{CASE_ID}_{stamp}.{batch_id}.p0_5a2_publishable_adjudication.response.txt"
        path.write_text(content, encoding="utf-8")
        response_paths.append(path)

    result = adjudicate_micro_beat_publishability(
        boundary_result=_frozen_boundary_result(snapshot, frozen_beats),
        api_key=str(settings["api_key"]),
        base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
        model=str(settings.get("model") or "deepseek-v4-flash"),
        response_hook=write_response,
        calibration_expectations=GOLDEN_BEAT_CALIBRATION,
    )
    golden = _evaluate_golden_calibration(result, frozen_beats)
    report = {
        "version": "p0_5a2_publishable_beat_adjudication_calibration_caramel_v1",
        "mode": "frozen_p0_5a1_boundary_snapshot_one_principal_three_state_adjudication_only_no_srt_rescan_no_reboundary_no_arc_no_m3",
        "case_id": CASE_ID,
        "source_srt": snapshot.get("source_srt"),
        "frozen_p0_5a1_boundary_snapshot": str(snapshot_path),
        "frozen_boundary_beat_count": len(frozen_beats),
        "frozen_boundary_beat_seconds": frozen_seconds,
        "frozen_boundary_decision_audit": snapshot.get("boundary_decision_audit"),
        "publishability_adjudication_responses": [path.name for path in response_paths],
        "golden_beat_calibration": golden,
        **result,
    }
    report_path = output_dir / f"p0_5a2_publishable_beat_adjudication_calibration_{stamp}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    stats = _mapping(report.get("boundary_statistics"))
    print(json.dumps({
        "report": str(report_path),
        "status": report.get("status"),
        "frozen_boundary_beats": len(frozen_beats),
        "frozen_boundary_seconds": frozen_seconds,
        "publishable_clean_count": stats.get("publishable_clean_count"),
        "publishable_clean_seconds": stats.get("publishable_clean_seconds"),
        "publishable_visual_count": stats.get("publishable_visual_count"),
        "publishable_visual_seconds": stats.get("publishable_visual_seconds"),
        "final_usable_seconds": stats.get("final_usable_seconds"),
        "golden_calibration_passed": golden["passed"],
        "golden_mismatch_count": golden["mismatch_count"],
        "arc_assembly_performed": _mapping(report.get("contract")).get("arc_assembly_performed"),
        "m3_invoked": _mapping(report.get("contract")).get("m3_invoked"),
    }, ensure_ascii=False))
    return 0 if report.get("status") == "publishable_inventory_calibrated" and golden["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
