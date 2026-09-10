# -*- coding: utf-8 -*-
"""M1 验证脚本（全文版）：SRT → 完整字幕 → (可选) Commercial Story Analyzer

用法：
  # 仅转换（免费，查看完整字幕输入格式）
  python run_m1_analyzer.py "C:\\工作\\珊姐\\8月14日直播\\格纹拼接（1）.srt"

  # 转换 + 调 Analyzer（会花 DeepSeek 调用，用 app 配置里的 key）
  python run_m1_analyzer.py "C:\\工作\\珊姐\\8月14日直播\\格纹拼接（1）.srt" --run --product "灰蓝格纹拼接上衣"
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.dirname(__file__))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

from srt_parser import open_srt, _time_to_seconds


def parse_srt_to_subtitles(srt_path: str) -> list[dict]:
    subtitles_raw, encoding = open_srt(srt_path)
    subtitles = [
        {
            "id": int(sub.index),
            "start": round(_time_to_seconds(sub.start), 3),
            "end": round(_time_to_seconds(sub.end), 3),
            "text": sub.text,
        }
        for sub in subtitles_raw
    ]
    return subtitles


def main() -> None:
    parser = argparse.ArgumentParser(description="M1: SRT -> 完整字幕 -> Analyzer")
    parser.add_argument("srt_path", help="SRT 文件绝对路径")
    parser.add_argument("--product", default="灰蓝格纹拼接上衣", help="商品名")
    parser.add_argument("--run", action="store_true", help="转换后调用 Analyzer（需 app 配置里有 key）")
    parser.add_argument("--out", default="", help="输出 JSON 路径（默认打印到 stdout）")
    args = parser.parse_args()

    subtitles = parse_srt_to_subtitles(args.srt_path)

    if args.run:
        from commercial_analyzer import analyze_commercial_story
        try:
            from ai_clipper import load_settings
            settings = load_settings()
        except Exception as error:
            print(f"[!] 无法加载 app 配置: {error}")
            return
        api_key = str(settings.get("api_key") or "").strip()
        base_url = str(settings.get("base_url") or "https://api.deepseek.com").strip()
        model = str(settings.get("model") or "deepseek-v4-flash").strip()
        if not api_key:
            print("[!] ai_settings.json 里 api_key 为空，无法调用 Analyzer。")
            return
        result = analyze_commercial_story(
            api_key=api_key,
            base_url=base_url,
            model=model,
            product=args.product,
            subtitles=subtitles,
            log_fn=print,
        )
        payload = result.to_dict()
    else:
        payload = {
            "product": args.product,
            "subtitle_count": len(subtitles),
            "subtitles": subtitles,
        }

    output = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"[OK] 已写入 {args.out}")
    else:
        print(output)


if __name__ == "__main__":
    main()
