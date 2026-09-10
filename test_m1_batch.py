# -*- coding: utf-8 -*-
"""M1 批量测试：多份 SRT → Strategy Discovery，一轮同时跑【无合同】+【禁用合同】两种。

直接运行本文件即可。每份素材跑两次 Analyzer（无合同基线 + 禁用合同），对比策略可执行性。
key 自动从软件配置读取。结果写桌面 M1_batch_result.json。
"""

import json
import os
import sys
import time

ROOT = r"C:\Users\周美彤\Documents\GitHub\LiveClipper"
APP = os.path.join(ROOT, "app")
for _p in (APP, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from commercial_analyzer import analyze_commercial_story, compute_material_sufficiency
from version_selector import select_versions
from story_planner import plan_narrative_llm
from ai_clipper import load_settings

# (文件绝对路径, 商品名)
FILES = [
    (
        r"C:\工作\珊姐\8月14日直播\简致（4）.srt",
        "简致衬衫",
    ),
    (
        r"C:\工作\小贤\单品素材\7-22单品\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙\AYOBE_小贤 7月22日09_00新品 韩系学姐清纯减龄感学院风连衣裙_1.srt",
        "韩系学院风连衣裙",
    ),
    (
        r"C:\工作\JCCC的穿搭影记(2.25春上新）\单品素材\JCCC影子【焦糖朗姆】宽松透气针织罩衫遮肉显瘦出片套装女JE6NZZ11\2.srt",
        "焦糖朗姆针织罩衫套装",
    ),
]

# M1-E 测试：禁用合同（复用 content_policy 的 canonical kinds + block 动作）
# 注：inventory 已折入 cta（现货/库存/限量）；after_sale 是单数（app 标准名）
CONTRACT_FORBID = {
    "price": "block",
    "cta": "block",
    "source_claim": "block",
    "social_proof": "block",
    "after_sale": "block",
    "size_interaction": "block",
    "live_interaction": "block",
}

OUT_PATH = os.path.join(os.path.expanduser("~"), "Desktop", "M1_batch_result.json")


def load_subtitles(path: str) -> list[dict]:
    """自动识别格式：.words.json 读 segments，否则按 SRT 解析。"""
    if path.lower().endswith(".words.json"):
        with open(path, "r", encoding="utf-8-sig") as f:
            payload = json.load(f)
        segments = payload.get("segments") or []
        subtitles = []
        for i, seg in enumerate(segments, 1):
            if not isinstance(seg, dict):
                continue
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or start)
            if end <= start:
                continue
            subtitles.append({"id": i, "start": round(start, 3), "end": round(end, 3), "text": text})
        return subtitles

    from srt_parser import open_srt, _time_to_seconds
    subs, _enc = open_srt(path)
    return [
        {
            "id": int(s.index),
            "start": round(_time_to_seconds(s.start), 3),
            "end": round(_time_to_seconds(s.end), 3),
            "text": s.text,
        }
        for s in subs
    ]


def _strategy_summary(s, duration_map):
    return {
        "strategy_id": s.strategy_id,
        "type": s.type,
        "thesis": s.thesis,
        "story_strength": s.story_strength,
        "material_sufficiency": s.material_sufficiency,
        "contract_compatibility": s.contract_compatibility,
        "strategy_viability": s.strategy_viability,
        "blocked_evidence_types": list(s.blocked_evidence_types),
        "contract_audit_hits": [h.to_dict() for h in s.contract_audit_hits],
        "material_curve": {
            str(t): compute_material_sufficiency(s.evidence, duration_map, t)
            for t in (30, 45, 60, 90)
        },
        "evidence": [{"role": e.role, "claim": e.claim, "subtitle_ids": list(e.subtitle_ids)} for e in s.evidence],
    }


def main() -> None:
    settings = load_settings()
    key_value = str(settings.get("api_key") or "").strip()
    if not key_value:
        print("[!] 未找到 api_key，请在软件设置里配置 AI。")
        return
    base_url = str(settings.get("base_url") or "https://api.deepseek.com").strip()
    model = str(settings.get("model") or "deepseek-v4-flash").strip()

    all_results = []
    print("=" * 70)
    print(f"M1 批量测试：{len(FILES)} 份素材，每份跑【无合同 + 禁用合同】两次（model={model}）")
    print("=" * 70)

    for idx, (path, product) in enumerate(FILES, 1):
        print(f"\n[{idx}/{len(FILES)}] {product}")
        if not os.path.exists(path):
            print(f"    [跳过] 文件不存在：{path}")
            all_results.append({"product": product, "error": "file_missing", "path": path})
            continue

        subtitles = load_subtitles(path)
        duration_map = {sub["id"]: max(0.0, sub["end"] - sub["start"]) for sub in subtitles}
        if not subtitles:
            print(f"    [跳过] 无有效字幕内容")
            all_results.append({"product": product, "error": "empty", "path": path})
            continue
        print(f"    字幕 {len(subtitles)} 条")

        try:
            result_none = analyze_commercial_story(
                api_key=key_value,
                base_url=base_url,
                model=model,
                product=product,
                subtitles=subtitles,
                target_duration=45.0,
                content_contract=None,
                log_fn=None,
            )
            time.sleep(2)
            result_forbid = analyze_commercial_story(
                api_key=key_value,
                base_url=base_url,
                model=model,
                product=product,
                subtitles=subtitles,
                target_duration=45.0,
                content_contract=CONTRACT_FORBID,
                log_fn=None,
            )
        except Exception as error:
            print(f"    [失败] {error}")
            all_results.append({"product": product, "error": str(error), "path": path})
            continue

        # M1-C 版本选择（对基线策略）
        sel = select_versions(result_none.strategies, n=3)

        # M2-A 叙事计划（LLM narrative judgment，对选中的 strategy version）
        strategy_map = {s.strategy_id: s for s in result_none.strategies}
        plans = {}
        for v in sel.selected:
            if v.version_type == "strategy":
                s = strategy_map.get(v.strategy_id)
                if s:
                    try:
                        plans[v.strategy_id] = plan_narrative_llm(
                            **{
                                "strategy": s,
                                "target_duration": 45.0,
                                "api_key": key_value,
                                "base_url": base_url,
                                "model": model,
                            }
                        )
                    except Exception as error:
                        print(f"        [叙事规划失败] {v.strategy_id}: {error}")

        all_results.append({
            "product": product,
            "path": path,
            "subtitle_count": len(subtitles),
            "baseline": [_strategy_summary(s, duration_map) for s in result_none.strategies],
            "forbid_contract": [_strategy_summary(s, duration_map) for s in result_forbid.strategies],
            "version_selection": {
                "selected": [
                    {"rank": v.rank, "strategy_id": v.strategy_id, "version_type": v.version_type,
                     "variant_of": v.variant_of, "base_score": v.base_score, "max_overlap": v.max_overlap}
                    for v in sel.selected
                ],
                "skipped": [
                    {"strategy_id": k.strategy_id, "family": k.family, "angle": k.angle,
                     "skip_reason": k.skip_reason, "overlap_with": k.overlap_with,
                     "overlap": k.breakdown.total, "family_overlap": k.breakdown.family_overlap,
                     "angle_overlap": k.breakdown.angle_overlap, "evidence_overlap": k.breakdown.evidence_overlap}
                    for k in sel.skipped
                ],
            },
            "narrative_plans": {sid: p.to_dict() for sid, p in plans.items()},
        })

        print(f"    [无合同] 基线 {len(result_none.strategies)} 条：")
        for s in result_none.strategies:
            print(f"      [{s.strategy_id}] {s.type} | family={s.strategy_family} angle={s.sub_angle} | strength={s.story_strength} | {s.thesis[:24]}")
        print(f"    [禁用合同] 对比 {len(result_forbid.strategies)} 条：")
        for s in result_forbid.strategies:
            blocked = "、".join(s.blocked_evidence_types) if s.blocked_evidence_types else "无"
            print(f"      [{s.strategy_id}] {s.type} | contract={s.contract_compatibility}({s.strategy_viability}) | 被禁={blocked} | {s.thesis[:24]}")
            for h in s.contract_audit_hits:
                print(f"          ↳ {h.type} 命中「{h.matched_keyword}」字幕{h.subtitle_id}: {h.raw_text[:40]}")

        print(f"    [版本选择 n=3]：")
        for v in sel.selected:
            tag = "Strategy" if v.version_type == "strategy" else f"Variant({v.variant_of})"
            print(f"      V{v.rank} {tag} {v.strategy_id} | family={v.family} angle={v.angle} | base={v.base_score}")
        if sel.skipped:
            print(f"    [跳过]：")
            for k in sel.skipped:
                bd = k.breakdown
                print(f"      {k.strategy_id} family={k.family} angle={k.angle} | {k.skip_reason} overlap_with={k.overlap_with} total={bd.total} (family={bd.family_overlap} angle={bd.angle_overlap} evidence={bd.evidence_overlap})")

        if plans:
            print(f"    [叙事计划]：")
            for sid, plan in plans.items():
                print(f"      {sid}（{plan.total_seconds}s，{plan.status}，建议{plan.recommended_duration}s，valid={plan.plan_valid}）：")
                if plan.issues:
                    print(f"        问题: {plan.issues}")
                if plan.removed_beats:
                    print(f"        已删除(重复): {[(b.role, list(b.subtitle_ids)) for b in plan.removed_beats]}")
                for b in plan.beats:
                    flag = "必选" if b.required else "可选"
                    print(f"        {b.narrative_role}({b.target_seconds}s) ← {b.source_role} | {flag} | {b.goal} → 字幕{list(b.candidate_evidence)}")

        if idx < len(FILES):
            time.sleep(2)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 70)
    print(f"结果已写入桌面：M1_batch_result.json（共 {len(all_results)} 份）")
    print("=" * 70)


if __name__ == "__main__":
    main()
