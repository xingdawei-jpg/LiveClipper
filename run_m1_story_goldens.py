# -*- coding: utf-8 -*-
"""Commercial Story Discovery 的真实字幕黄金验收（显式执行才调用 AI）。

它不要求模型复述固定标题，而是检查是否主动发现了人工确认存在的
消费者矛盾、变化与核心购买认知。仅验证 M1；不会调用 M2，也不会改动预览或成片链路。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for path in (str(ROOT), str(APP)):
    if path not in sys.path:
        sys.path.insert(0, path)

from commercial_analyzer import analyze_commercial_story, matches_story_semantic_signature, parse_strategy_result
from srt_parser import _time_to_seconds, open_srt


CASES = {
    "jccc": {
        "product": "焦糖朗姆针织罩衫套装",
        "srt": r"C:\工作\JCCC的穿搭影记(2.25春上新）\单品素材\JCCC影子【焦糖朗姆】宽松透气针织罩衫遮肉显瘦出片套装女JE6NZZ11\5.srt",
        "goldens": ("jccc_shoulder_narrowing", "jccc_vacation_escape", "jccc_cool_comfort"),
    },
    "jianzhi": {
        "product": "简致衬衫",
        "srt": r"C:\工作\珊姐\8月14日直播\简致（1）.srt",
        "goldens": ("jianzhi_standalone_not_office",),
    },
    "hanxi": {
        "product": "韩系学院风连衣裙",
        "srt": r"C:\工作\小贤\单品素材\7-22单品\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙_1.srt",
        "goldens": ("hanxi_short_skirt_safety",),
    },
}


def _load_subtitles(path: str) -> list[dict]:
    raw, _ = open_srt(path)
    return [
        {
            "id": int(item.index),
            "start": round(_time_to_seconds(item.start), 3),
            "end": round(_time_to_seconds(item.end), 3),
            "text": item.text,
        }
        for item in raw
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=tuple(CASES) + ("all",), default="all")
    parser.add_argument("--target-duration", type=float, default=60.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--raw-dir", default="workspace/m1_story_goldens_raw")
    parser.add_argument("--from-raw", action="store_true", help="不调用 AI，重放已保存的真实响应并按当前解析合同验收")
    args = parser.parse_args()

    api_key = ""
    base_url = ""
    model = "saved-real-response"
    if not args.from_raw:
        from ai_clipper import load_settings

        settings = load_settings()
        api_key = str(settings.get("api_key") or "").strip()
        if not api_key:
            raise SystemExit("未找到 AI API Key，无法运行真实黄金验收。")
        base_url = str(settings.get("base_url") or "https://api.deepseek.com").strip()
        model = str(settings.get("model") or "deepseek-v4-flash").strip()
    fixture = ROOT / "tests" / "fixtures" / "commercial_story_semantic_goldens.json"
    signatures = json.loads(fixture.read_text(encoding="utf-8"))

    selected = CASES.keys() if args.case == "all" else (args.case,)
    raw_dir = Path(args.raw_dir)
    if not raw_dir.is_absolute():
        raw_dir = ROOT / raw_dir
    raw_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "schema_version": 1,
        "model": model,
        "target_duration": args.target_duration,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "cases": {},
    }
    passed = 0
    expected = 0
    for case_id in selected:
        case = CASES[case_id]
        subtitles = _load_subtitles(case["srt"])
        print(f"[M1] {case_id}: {len(subtitles)} 条字幕，{'重放' if args.from_raw else '调用'} {model}")
        raw_path = raw_dir / f"{case_id}.txt"
        try:
            if args.from_raw:
                result = parse_strategy_result(
                    raw_path.read_text(encoding="utf-8"),
                    product=case["product"],
                    subtitles=subtitles,
                    target_duration=args.target_duration,
                    content_contract=None,
                )
            else:
                result = analyze_commercial_story(
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    product=case["product"],
                    subtitles=subtitles,
                    target_duration=args.target_duration,
                    content_contract=None,
                    log_fn=print,
                    raw_response_hook=lambda raw, path=raw_path: path.write_text(raw, encoding="utf-8"),
                )
        except Exception as error:
            report["cases"][case_id] = {
                "source_srt": case["srt"],
                "raw_response": str(raw_path),
                "error": str(error),
            }
            print(f"[M1] {case_id} 失败：{error}")
            continue
        golden_result: dict[str, object] = {}
        for golden_id in case["goldens"]:
            expected += 1
            signature = signatures[golden_id]["signature"]
            matched_by = [
                story.strategy_id
                for story in result.strategies
                if matches_story_semantic_signature(story, signature)
            ]
            if matched_by:
                passed += 1
            golden_result[golden_id] = {"passed": bool(matched_by), "matched_by": matched_by}
        report["cases"][case_id] = {
            "source_srt": case["srt"],
            "raw_response": str(raw_path),
            "goldens": golden_result,
            "result": result.to_dict(),
        }

    report["summary"] = {"passed": passed, "expected": expected, "all_passed": passed == expected}
    output = Path(args.out) if args.out else ROOT / "workspace" / f"m1_story_goldens_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[M1] 黄金验收 {passed}/{expected}，报告：{output}")
    raise SystemExit(0 if passed == expected else 2)


if __name__ == "__main__":
    main()
