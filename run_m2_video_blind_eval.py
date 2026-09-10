# -*- coding: utf-8 -*-
"""Create an anonymous Legacy-vs-M2 video review packet for one real case."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from run_m3_selector_render import (  # noqa: E402
    _source_video,
    _tool,
    render_source_ranges,
    write_output_timeline_srt,
)
from story_blind_eval import empty_rating_sheet  # noqa: E402


def legacy_ranges(raw_clips: Any) -> list[dict[str, Any]]:
    """Convert the recorded old director output without selecting anything new."""
    ranges: list[dict[str, Any]] = []
    for order, raw in enumerate(raw_clips or (), 1):
        if not isinstance(raw, (list, tuple)) or len(raw) < 4:
            raise ValueError(f"旧链路片段 {order} 格式无效")
        text = str(raw[1] or "").strip()
        try:
            start, end = float(raw[2]), float(raw[3])
        except (TypeError, ValueError) as error:
            raise ValueError(f"旧链路片段 {order} 时间无效") from error
        if not text or end <= start:
            raise ValueError(f"旧链路片段 {order} 内容不完整")
        ranges.append({"order": order, "start": start, "end": end, "text": text})
    if not ranges:
        raise ValueError("旧链路没有可比较片段")
    return ranges


def _render_captioned_ranges(
    source_video: Path,
    ranges: list[Mapping[str, Any]],
    subtitle_path: Path,
    output_path: Path,
) -> None:
    ffmpeg = _tool("ffmpeg")
    plain_video = output_path.with_name(f"{output_path.stem}.plain.mp4")
    render_source_ranges(source_video, ranges, plain_video)
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-i", str(plain_video), "-i", str(subtitle_path),
        "-map", "0", "-map", "1:0", "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=chi", str(output_path),
    ], check=True)


def _case_payload(packet: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    matched = next(
        (item for item in (packet.get("cases") or ())
         if isinstance(item, Mapping) and str(item.get("case_id") or "") == case_id),
        None,
    )
    if not isinstance(matched, Mapping):
        raise ValueError(f"匿名片单中没有案例：{case_id}")
    result = dict(packet)
    result["cases"] = [dict(matched)]
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("comparison_report")
    parser.add_argument("selector_manifest")
    parser.add_argument("--case", required=True)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    report_path = Path(args.comparison_report).resolve()
    manifest_path = Path(args.selector_manifest).resolve()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    case_id = str(args.case).strip()
    case = dict((report.get("cases") or {}).get(case_id) or {})
    if not case:
        raise SystemExit(f"比较报告中没有案例：{case_id}")
    selector = dict(manifest.get("selector_result") or {})
    if str(manifest.get("case_id") or "") != case_id:
        raise SystemExit("Selector 清单与比较案例不一致。")
    if str(selector.get("status") or "") != "ok":
        raise SystemExit("M3 未完整通过，不能伪造视频 A/B。")
    m2_video = manifest_path.parent / "m3_render" / "selector_preview_captioned.mp4"
    if not m2_video.is_file():
        raise SystemExit(f"缺少已核验 M3 视频：{m2_video}")
    source_video = _source_video(manifest, "")
    legacy = legacy_ranges(case.get("legacy_clips"))

    output_dir = Path(args.out_dir) if args.out_dir else report_path.parent / "video_blind"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    public_dir = output_dir / "public"
    private_dir = output_dir / "private_evidence"
    public_dir.mkdir(parents=True, exist_ok=True)
    private_dir.mkdir(parents=True, exist_ok=True)
    legacy_srt = private_dir / "legacy_subtitles.srt"
    legacy_video = private_dir / "legacy_captioned.mp4"
    write_output_timeline_srt(legacy, legacy_srt)
    _render_captioned_ranges(source_video, legacy, legacy_srt, legacy_video)

    public_packet = _case_payload(
        json.loads((report_path.parent / "public_packet.json").read_text(encoding="utf-8")), case_id
    )
    private_key = _case_payload(
        json.loads((report_path.parent / "private_answer_key.json").read_text(encoding="utf-8")), case_id
    )
    labels = {
        str(item.get("label") or ""): str(item.get("variant_id") or "")
        for item in ((private_key.get("cases") or [])[0].get("variants") or ())
        if isinstance(item, Mapping)
    }
    for label, variant_id in labels.items():
        source = m2_video if variant_id == f"m2:{case_id}" else legacy_video if variant_id == f"legacy:{case_id}" else None
        if source is None:
            raise SystemExit(f"私有映射含未知版本：{variant_id}")
        shutil.copy2(source, public_dir / f"{label}.mp4")
    (public_dir / "video_packet.json").write_text(
        json.dumps(public_packet, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (public_dir / "rating_sheet.json").write_text(
        json.dumps(empty_rating_sheet(public_packet), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (public_dir / "README.md").write_text(
        "# 匿名视频盲评\n\n"
        "请独立观看 A.mp4 与 B.mp4，并在 `rating_sheet.json` 中对六项各打 0-2 分。"
        "`max_issue` 只记录最大问题，不计入总分。不要查看上级目录中的私有证据文件。\n",
        encoding="utf-8",
    )
    (private_dir / "answer_key.json").write_text(
        json.dumps(private_key, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"公开盲评包：{public_dir}")
    print(f"私有映射与旧链路渲染：{private_dir}")


if __name__ == "__main__":
    main()
