# -*- coding: utf-8 -*-
"""Render one already-materialized M3 plan outside every product task path."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent


def _tool(name: str) -> str:
    local = ROOT / "app" / "ffmpeg" / f"{name}.exe"
    if local.is_file():
        return str(local)
    resolved = shutil.which(name)
    if resolved:
        return resolved
    raise RuntimeError(f"未找到 {name}。")


def _duration(probe: str, video: Path) -> float:
    completed = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload = json.loads(completed.stdout)
    return float((payload.get("format") or {}).get("duration") or 0.0)


def _source_video(manifest: Mapping[str, Any], explicit: str) -> Path:
    if explicit:
        source = Path(explicit).resolve()
    else:
        source_srt = Path(str(manifest.get("source_srt") or ""))
        source = source_srt.with_suffix(".mp4")
    if not source.is_file():
        raise RuntimeError(f"未找到原视频：{source}")
    return source


def _output_srt_time(seconds: float) -> str:
    milliseconds = int(round(max(0.0, float(seconds)) * 1000.0))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{milliseconds:03d}"


def write_output_timeline_srt(ranges: list[Mapping[str, Any]], path: Path) -> None:
    """Write captions on the concatenated output timeline, never source time."""
    cursor = 0.0
    blocks = []
    for order, item in enumerate(ranges, 1):
        duration = float(item["end"]) - float(item["start"])
        if duration <= 0:
            raise RuntimeError(f"范围 {order} 时间不合法。")
        next_cursor = cursor + duration
        blocks.append(
            f"{order}\n{_output_srt_time(cursor)} --> {_output_srt_time(next_cursor)}\n"
            f"{str(item.get('text') or '').strip()}\n"
        )
        cursor = next_cursor
    path.write_text("\n".join(blocks), encoding="utf-8")


def _concat_filter(input_count: int) -> str:
    if input_count <= 0:
        raise RuntimeError("没有可连接的视频范围。")
    labels: list[str] = []
    for index in range(input_count):
        labels.extend((f"[{index}:v]", f"[{index}:a]"))
    return "".join(labels) + f"concat=n={input_count}:v=1:a=1[vout][aout]"


def render_source_ranges(
    source_video: Path,
    ranges: list[Mapping[str, Any]],
    output_path: Path,
) -> None:
    """Seek only the approved ranges, then concatenate them in playback order.

    The older one-pass trim graph decoded every source frame from time zero to
    the latest selected range.  Per-range input seeks preserve the exact
    approved boundaries while making an offline 40-second blind render scale
    with selected duration instead of full livestream length.
    """
    ffmpeg = _tool("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="liveclipper_m3_ranges_") as temp_dir:
        segment_paths: list[Path] = []
        for index, item in enumerate(ranges, 1):
            start = float(item["start"])
            end = float(item["end"])
            duration = end - start
            if duration <= 0:
                raise RuntimeError(f"范围 {index} 时间不合法。")
            segment = Path(temp_dir) / f"range_{index:03d}.mp4"
            subprocess.run([
                ffmpeg, "-y", "-v", "error", "-ss", f"{start:.6f}", "-i", str(source_video),
                "-t", f"{duration:.6f}", "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "faster", "-crf", "21", "-c:a", "aac",
                "-movflags", "+faststart", str(segment),
            ], check=True)
            segment_paths.append(segment)
        command = [ffmpeg, "-y", "-v", "error"]
        for segment in segment_paths:
            command.extend(("-i", str(segment)))
        command.extend((
            "-filter_complex", _concat_filter(len(segment_paths)),
            "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "faster", "-crf", "21",
            "-c:a", "aac", "-movflags", "+faststart", str(output_path),
        ))
        subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("selector_manifest")
    parser.add_argument("--source-video", default="")
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    manifest_path = Path(args.selector_manifest).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selector = dict(manifest.get("selector_result") or {})
    if str(selector.get("status") or "") != "ok":
        data_status = str(manifest.get("word_timeline_status") or "")
        semantic_replan = bool(manifest.get("semantic_replan_required"))
        detail = ""
        if data_status == "identity_mismatch":
            detail += "词级时间资产与当前 SRT 身份不一致；需先重新生成或定位正确 sidecar。"
        if semantic_replan:
            detail += "已批准候选含不能独立播出的语义残句；需由 M2 重选表达。"
        raise SystemExit("Selector 未完全通过，拒绝输出部分成片。" + detail)
    ranges = [item for item in (selector.get("ranges") or ()) if isinstance(item, Mapping)]
    if not ranges:
        raise SystemExit("Selector 没有可渲染范围。")

    source = _source_video(manifest, args.source_video)
    output_dir = Path(args.out_dir) if args.out_dir else manifest_path.parent / "m3_render"
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / "selector_preview.mp4"
    source_captions_path = manifest_path.parent / "selector_subtitles.srt"
    if not source_captions_path.is_file():
        raise RuntimeError(f"缺少 Selector 字幕：{source_captions_path}")
    captioned_path = output_dir / "selector_preview_captioned.mp4"
    captions_path = output_dir / "selector_preview_subtitles.srt"
    write_output_timeline_srt(ranges, captions_path)
    ffmpeg = _tool("ffmpeg")
    ffprobe = _tool("ffprobe")
    render_source_ranges(source, ranges, video_path)
    subprocess.run([
        ffmpeg, "-y", "-v", "error", "-i", str(video_path), "-i", str(captions_path),
        "-map", "0", "-map", "1:0", "-c", "copy", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=chi", str(captioned_path),
    ], check=True)
    expected = round(sum(float(item["end"]) - float(item["start"]) for item in ranges), 3)
    observed = round(_duration(ffprobe, video_path), 3)
    duration_flow = dict(manifest.get("duration_flow") or {})
    planned = round(float(duration_flow.get("planned_duration") or expected), 3)
    materialized = round(float(duration_flow.get("materialized_duration") or expected), 3)
    render_delta = round(observed - materialized, 3)
    receipt = {
        "source_video": str(source),
        "selector_manifest": str(manifest_path),
        "source_subtitle_manifest": str(source_captions_path),
        "rendered_subtitle": str(captions_path),
        "rendered_video": str(video_path),
        "captioned_video": str(captioned_path),
        "range_count": len(ranges),
        "planned_duration": planned,
        "materialized_duration": materialized,
        "rendered_duration": observed,
        "materialization_delta": round(materialized - planned, 3),
        "render_delta": render_delta,
        "duration_alignment_status": str(duration_flow.get("duration_alignment_status") or "unreported"),
        "duration_replan_required": bool(duration_flow.get("duration_replan_required")),
        "expected_duration": expected,
        "ffprobe_duration": observed,
        "duration_delta": round(observed - expected, 3),
    }
    receipt_path = output_dir / "render_receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rendered video: {video_path}")
    print(f"Captioned video: {captioned_path}")
    print(f"Render receipt: {receipt_path}")
    print(
        f"duration planned={planned:.3f}s materialized={materialized:.3f}s "
        f"rendered={observed:.3f}s"
    )


if __name__ == "__main__":
    main()
