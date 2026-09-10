# -*- coding: utf-8 -*-
"""M1 一键测试（全文版）：格纹拼接 SRT → Strategy Discovery（调用 DeepSeek）

直接运行本文件即可，无需在命令行输入中文路径。
输入是完整字幕（带 ID + 时间戳），不再经过机械 StoryBlock 分块。
会调用一次 Analyzer，花费很小（单次调用）。
key 自动从软件配置读取（%APPDATA%/LiveClipper/ai_settings.json 或 app/ai_settings.json）。
"""

import json
import os
import sys

ROOT = r"C:\Users\周美彤\Documents\GitHub\LiveClipper"
APP = os.path.join(ROOT, "app")
for _p in (APP, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

SRT_PATH = r"C:\工作\珊姐\8月14日直播\格纹拼接（1）.srt"
PRODUCT = "灰蓝格纹拼接上衣"
OUT_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "M1_analyzer_result.json")


def main() -> None:
    from srt_parser import open_srt, _time_to_seconds
    from commercial_analyzer import analyze_commercial_story
    from ai_clipper import load_settings

    print("=" * 60)
    print("M1 Strategy Discovery 测试（全文输入）")
    print("=" * 60)

    # 1. 解析 SRT → 完整字幕（带 ID + 时间戳）
    subtitles_raw, encoding = open_srt(SRT_PATH)
    subtitles = [
        {
            "id": int(sub.index),
            "start": round(_time_to_seconds(sub.start), 3),
            "end": round(_time_to_seconds(sub.end), 3),
            "text": sub.text,
        }
        for sub in subtitles_raw
    ]
    print(f"[1/2] 解析 SRT 完成：{len(subtitles)} 条字幕（编码 {encoding}）")

    # 2. 加载 key 并调用 Analyzer（全文输入）
    settings = load_settings()
    api_key = str(settings.get("api_key") or "").strip()
    if not api_key:
        print("[!] 未找到 api_key：请在软件「设置」里配置 AI，或在 app/ai_settings.json 里填 key。")
        return
    base_url = str(settings.get("base_url") or "https://api.deepseek.com").strip()
    model = str(settings.get("model") or "deepseek-v4-flash").strip()
    print(f"[2/2] 调用 Analyzer（model={model}，全文 {len(subtitles)} 条字幕）")
    print("      正在请求 DeepSeek，约需几秒到几十秒……")

    result = analyze_commercial_story(
        api_key=api_key,
        base_url=base_url,
        model=model,
        product=PRODUCT,
        subtitles=subtitles,
        target_duration=45.0,
        log_fn=print,
    )

    payload = result.to_dict()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("结果已写入桌面：M1_analyzer_result.json")
    print("=" * 60)
    print(f"\n识别出的商业策略（共 {len(result.strategies)} 个）：\n")
    for s in result.strategies:
        print(f"  [{s.strategy_id}] {s.type}")
        print(f"       thesis: {s.thesis}")
        print(f"       story_strength={s.story_strength}  material_sufficiency={s.material_sufficiency}")
        print(f"       证据（{len(s.evidence)} 节点）：")
        for e in s.evidence:
            print(f"         - {e.role}: {e.claim}  字幕ID={list(e.subtitle_ids)}")
        print(f"       为什么同属一条故事: {s.coherence_reason}")
        print()


if __name__ == "__main__":
    main()
