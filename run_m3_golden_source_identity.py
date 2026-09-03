# -*- coding: utf-8 -*-
"""Register and verify source identities for the M3 golden-source set.

This is intentionally before M1/M2 planning.  A verified managed ASR cache
proves that one video, its SRT and its word sidecar are a matched source pair.
It does *not* claim that the present M3 adapter can already bind every
post-resegmented SRT candidate to timed words.
"""

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

from asr_cache import inspect_cache  # noqa: E402
from srt_parser import open_srt  # noqa: E402


M3_GOLDEN_SOURCES: dict[str, dict[str, str]] = {
    "jccc_deep_roast_hoodie": {
        "label": "焦糖深烘拿铁连帽卫衣",
        "srt": r"C:\工作\JCCC的穿搭影记(2.25春上新）\新建文件夹\新建文件夹\JCCC影子_深烘拿铁_300G针织面料中长款抽绳廓形连帽卫衣JE5PBY20\JCCC影子_深烘拿铁_300G针织面料中长款抽绳廓形连帽卫衣JE5PBY20_1.srt",
    },
    "shanjie_plaid_splice": {
        "label": "珊姐格纹拼接轻姿",
        "srt": r"C:\工作\珊姐\8月14日直播\格纹拼接-轻姿（1）.srt",
    },
    "xiaoxian_relaxed_autumn_set": {
        "label": "小贤松弛早秋连帽套装",
        "srt": r"C:\工作\小贤\单品素材\7-22单品\AYOBE_小贤 7月22日09_00新品 松弛早秋连帽温柔奶茶色休闲套装\AYOBE_小贤 7月22日09_00新品 松弛早秋连帽温柔奶茶色休闲套装_1_1.srt",
    },
}


def _plain_text(value: object) -> str:
    return "".join(
        char for char in str(value or "")
        if char.isalnum() or "\u4e00" <= char <= "\u9fff"
    )


def assess_source_identity(srt_path: str | Path) -> dict[str, Any]:
    """Return cache-backed pair identity and adapter readiness separately."""
    srt = Path(srt_path).resolve()
    video = srt.with_suffix(".mp4")
    sidecar = srt.with_suffix(".words.json")
    cache = inspect_cache(video, srt) if video.is_file() else {
        "valid": False,
        "managed": srt.with_suffix(".asr-cache.json").is_file(),
        "reason": "source_video_missing",
    }
    try:
        source_rows, _encoding = open_srt(str(srt))
        source_texts = [_plain_text(row.text) for row in source_rows]
    except Exception as exc:
        source_rows, source_texts = [], []
        cache = {**cache, "valid": False, "reason": "srt_unreadable", "detail": type(exc).__name__}
    try:
        raw_sidecar = json.loads(sidecar.read_text(encoding="utf-8-sig"))
        segments = [item for item in raw_sidecar.get("segments") or () if isinstance(item, Mapping)]
        sidecar_texts = [_plain_text(item.get("text")) for item in segments]
        sidecar_word_texts = [
            "".join(_plain_text(word.get("text")) for word in item.get("words") or () if isinstance(word, Mapping))
            for item in segments
        ]
    except Exception as exc:
        segments, sidecar_texts, sidecar_word_texts = [], [], []
        cache = {**cache, "valid": False, "reason": "word_sidecar_unreadable", "detail": type(exc).__name__}

    direct_row_identity = bool(
        len(source_texts) == len(sidecar_texts)
        and source_texts == sidecar_texts == sidecar_word_texts
    )
    source_identity_verified = bool(
        cache.get("valid")
        and cache.get("managed")
        and cache.get("timing_precision") == "word"
        and sidecar.is_file()
    )
    return {
        "source_srt": str(srt),
        "source_video": str(video),
        "word_sidecar": str(sidecar),
        "asr_cache": cache,
        "source_subtitle_count": len(source_rows),
        "sidecar_segment_count": len(segments),
        "direct_row_identity": direct_row_identity,
        "source_identity_verified": source_identity_verified,
        "m3_candidate_word_binding": (
            "direct_row_binding_ready"
            if direct_row_identity
            else "requires_cache_backed_semantic_resegmentation_adapter"
        ),
        "selected_as_m3_source_golden": source_identity_verified,
        "ready_for_m3_plan_fidelity": bool(source_identity_verified and direct_row_identity),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(M3_GOLDEN_SOURCES) + ("all",), default="all")
    parser.add_argument("--out-dir", default="workspace/m3_golden_source_identity")
    args = parser.parse_args()
    case_ids = tuple(M3_GOLDEN_SOURCES) if args.case == "all" else (args.case,)
    report: dict[str, Any] = {
        "version": "m3-golden-source-identity-v1",
        "mode": "source_identity_only_no_m1_m2_m3_plan_or_render",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": {},
    }
    for case_id in case_ids:
        item = M3_GOLDEN_SOURCES[case_id]
        identity = assess_source_identity(item["srt"])
        report["cases"][case_id] = {"label": item["label"], **identity}
        print(
            f"[M3 Golden Source] {case_id}: source_identity={identity['source_identity_verified']} "
            f"binding={identity['m3_candidate_word_binding']}"
        )
    selected = sum(bool(item["selected_as_m3_source_golden"]) for item in report["cases"].values())
    ready = sum(bool(item["ready_for_m3_plan_fidelity"]) for item in report["cases"].values())
    report["summary"] = {
        "selected_m3_source_goldens": selected,
        "expected": len(case_ids),
        "all_source_identities_verified": selected == len(case_ids),
        "ready_for_current_m3_plan_fidelity": ready,
        "selection_boundary": (
            "已选为 M3 源素材黄金集，不等于已通过 M1→M2→M3 Plan Fidelity；"
            "后者仍需 cache-backed semantic resegmentation candidate-to-word adapter。"
        ),
    }
    output_dir = ROOT / args.out_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"m3_golden_source_identity_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M3 Golden Source] {selected}/{len(case_ids)} report={output_path}")
    raise SystemExit(0 if report["summary"]["all_source_identities_verified"] else 2)


if __name__ == "__main__":
    main()
