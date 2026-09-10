# -*- coding: utf-8 -*-
"""Create an anonymous same-story, different-duration video review packet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from story_blind_eval import EvaluationVariant, build_blind_packet, empty_rating_sheet  # noqa: E402


DURATION_DEPTH_EVALUATION_VERSION = "commercial-story-duration-depth-blind-eval-v1"
CASE_ID = "same_story_different_depth"


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"无法读取 Selector 清单：{path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"Selector 清单不是对象：{path}")
    return value


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def variant_from_selector_manifest(
    manifest: Mapping[str, Any],
    *,
    variant_id: str,
) -> EvaluationVariant:
    """Use only M3-approved materialized ranges; never select new clips."""
    selector = manifest.get("selector_result")
    if not isinstance(selector, Mapping) or str(selector.get("status") or "") != "ok":
        raise ValueError(f"{variant_id} 的 M3 未完整通过，不能进入盲评")
    clips: list[dict[str, Any]] = []
    for order, raw in enumerate(selector.get("ranges") or (), 1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{variant_id} 的范围 {order} 不是对象")
        candidate_id = int(_number(raw.get("parent_candidate_id") or raw.get("candidate_id")))
        start = _number(raw.get("start"))
        end = _number(raw.get("end"), start)
        text = str(raw.get("text") or "").strip()
        if candidate_id <= 0 or not text or end <= start:
            raise ValueError(f"{variant_id} 的范围 {order} 不完整")
        clips.append({
            "candidate_id": candidate_id,
            "start": start,
            "end": end,
            "text": text,
        })
    return EvaluationVariant.from_mapping({"variant_id": variant_id, "clips": clips})


def build_duration_depth_packet(
    short_manifest: Mapping[str, Any],
    long_manifest: Mapping[str, Any],
    *,
    seed: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build public and private packets for two depths of the same source story."""
    short_source = str(short_manifest.get("source_srt") or "").strip()
    long_source = str(long_manifest.get("source_srt") or "").strip()
    if not short_source or short_source != long_source:
        raise ValueError("时长盲评只能比较同一份源字幕的两个版本")

    short = variant_from_selector_manifest(short_manifest, variant_id="m2:shorter_depth")
    long = variant_from_selector_manifest(long_manifest, variant_id="m2:longer_depth")
    if short.duration >= long.duration:
        raise ValueError("时长盲评需要一个明显更短和一个明显更长的版本")
    return build_blind_packet({CASE_ID: (short, long)}, seed=seed)


def _duration_depth_sheet(public_packet: Mapping[str, Any]) -> dict[str, Any]:
    """Keep depth-specific questions separate from the normal commercial score."""
    variants = list((public_packet.get("cases") or [{}])[0].get("variants") or ())
    return {
        "version": DURATION_DEPTH_EVALUATION_VERSION,
        "case_id": CASE_ID,
        "variants": [
            {"label": str(item.get("label") or ""), "duration": item.get("duration")}
            for item in variants
        ],
        "same_core_commercial_idea": None,
        "longer_version_adds_new_purchase_value": None,
        "longer_version_is_not_just_more_selling_points": None,
        "longer_version_has_a_more_complete_ending": None,
        "longer_version_naturalness": None,
        "comment": "",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("short_manifest")
    parser.add_argument("long_manifest")
    parser.add_argument("--short-video", required=True)
    parser.add_argument("--long-video", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--seed", default="duration-depth-2026-08-21")
    args = parser.parse_args()

    short_path = Path(args.short_manifest).resolve()
    long_path = Path(args.long_manifest).resolve()
    short_video = Path(args.short_video).resolve()
    long_video = Path(args.long_video).resolve()
    if not short_video.is_file() or not long_video.is_file():
        raise SystemExit("缺少已核验的渲染视频，拒绝生成盲评包。")

    public_packet, private_key = build_duration_depth_packet(
        _read_manifest(short_path), _read_manifest(long_path), seed=args.seed,
    )
    out_dir = Path(args.out_dir).resolve()
    public_dir = out_dir / "public"
    private_dir = out_dir / "private_evidence"
    public_dir.mkdir(parents=True, exist_ok=True)
    private_dir.mkdir(parents=True, exist_ok=True)

    labels = {
        str(item.get("variant_id") or ""): str(item.get("label") or "")
        for item in ((private_key.get("cases") or [{}])[0].get("variants") or ())
        if isinstance(item, Mapping)
    }
    for variant_id, video in (("m2:shorter_depth", short_video), ("m2:longer_depth", long_video)):
        label = labels.get(variant_id)
        if not label:
            raise SystemExit(f"盲评映射缺失：{variant_id}")
        shutil.copy2(video, public_dir / f"{label}.mp4")

    (public_dir / "video_packet.json").write_text(
        json.dumps(public_packet, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (public_dir / "rating_sheet.json").write_text(
        json.dumps(empty_rating_sheet(public_packet), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (public_dir / "duration_depth_checklist.json").write_text(
        json.dumps(_duration_depth_sheet(public_packet), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (public_dir / "README.md").write_text(
        "# 匿名时长深度盲评\n\n"
        "请独立观看 A.mp4 与 B.mp4。先在 `rating_sheet.json` 中按观看感受对每个版本评分，"
        "再在 `duration_depth_checklist.json` 判断：较长版本是否仍是同一个商业故事的更深展开，"
        "而不是在较短版本后堆入更多卖点。不要查看上级目录中的私有证据。\n",
        encoding="utf-8",
    )
    (private_dir / "answer_key.json").write_text(
        json.dumps(private_key, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (private_dir / "source_manifests.json").write_text(
        json.dumps({"short": str(short_path), "long": str(long_path)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Public blind packet: {public_dir}")
    print(f"Private answer key: {private_dir}")


if __name__ == "__main__":
    main()
