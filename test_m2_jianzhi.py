# -*- coding: utf-8 -*-
"""M2-A 单素材测试：简致衬衫 → 策略 → 版本选择 → LLM 叙事计划

直接运行，无需输中文路径。key 走软件配置。
重点看 LLM 叙事规划：角色重映射、hook 压缩、immediate payoff、required/optional。
"""

import sys

ROOT = r"C:\Users\周美彤\Documents\GitHub\LiveClipper"
APP = ROOT + r"\app"
for _p in (APP, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from srt_parser import open_srt, _time_to_seconds
from commercial_analyzer import analyze_commercial_story
from version_selector import select_versions
from story_planner import plan_narrative_llm
from ai_clipper import load_settings

SRT_PATH = r"C:\工作\珊姐\8月14日直播\简致（4）.srt"
PRODUCT = "简致衬衫"


def main() -> None:
    settings = load_settings()
    key_value = str(settings.get("api_key") or "").strip()
    base_url = str(settings.get("base_url") or "https://api.deepseek.com").strip()
    model = str(settings.get("model") or "deepseek-v4-flash").strip()
    if not key_value:
        print("[!] 未找到 api_key，请在软件设置里配置 AI。")
        return

    # 1. 解析 SRT
    subs_raw, _enc = open_srt(SRT_PATH)
    subtitles = [
        {
            "id": int(s.index),
            "start": round(_time_to_seconds(s.start), 3),
            "end": round(_time_to_seconds(s.end), 3),
            "text": s.text,
        }
        for s in subs_raw
    ]
    print(f"[1/4] 解析 SRT：{len(subtitles)} 条")

    # 2. Analyzer（基线，M2 关注叙事不关注合同）
    print("[2/4] Strategy Discovery……")
    result = analyze_commercial_story(
        api_key=key_value,
        base_url=base_url,
        model=model,
        product=PRODUCT,
        subtitles=subtitles,
        target_duration=45.0,
        log_fn=print,
    )
    print(f"      发现 {len(result.strategies)} 条策略")

    # 3. 版本选择
    sel = select_versions(result.strategies, n=3)
    print("\n[3/4] 版本选择：")
    for v in sel.selected:
        tag = "Strategy" if v.version_type == "strategy" else f"Variant({v.variant_of})"
        print(f"    V{v.rank} {tag} {v.strategy_id} | base={v.base_score}")
    if sel.skipped:
        for k in sel.skipped:
            print(f"    跳过 {k.strategy_id} ({k.skip_reason}, overlap_with={k.overlap_with})")

    # 4. LLM 叙事计划
    strategy_map = {s.strategy_id: s for s in result.strategies}
    print("\n[4/4] 叙事计划（LLM narrative judgment）：")
    for v in sel.selected:
        if v.version_type != "strategy":
            continue
        s = strategy_map.get(v.strategy_id)
        if not s:
            continue
        print(f"\n  === {v.strategy_id} ===")
        print(f"  thesis: {s.thesis}")
        try:
            plan = plan_narrative_llm(
                strategy=s,
                target_duration=45.0,
                api_key=key_value,
                base_url=base_url,
                model=model,
            )
        except Exception as error:
            print(f"  [规划失败] {error}")
            continue
        print(f"  总时长 {plan.total_seconds}s | 状态 {plan.status} | 建议 {plan.recommended_duration}s | valid={plan.plan_valid}")
        if plan.issues:
            print(f"  问题: {plan.issues}")
        if plan.removed_beats:
            print(f"  已删除(重复): {[(b.role, list(b.subtitle_ids)) for b in plan.removed_beats]}")
        for b in plan.beats:
            flag = "必选" if b.required else "可选"
            print(f"    [{b.narrative_role} {b.target_seconds}s] ← source={b.source_role} | {flag}")
            print(f"      goal: {b.goal}")
            print(f"      字幕{list(b.candidate_evidence)} | 选句: {b.selection_instruction}")


if __name__ == "__main__":
    main()
