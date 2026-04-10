# -*- coding: utf-8 -*-
"""
Multi-version output for LiveClipper

Strategy (v2):
1. AI selects ALL good clips once (no dedup yet)
2. Each version picks from the FULL pool with different preferences
3. V1: balanced (hook + best products + close)
4. V2: different hook if available, different product focus
5. V3: remaining strong clips, ensure completeness

Key: clips tuple is (category, text, start, end, score, duration)
"""
import os, re, random

def generate_multi_versions(all_clips, num_versions=3, log_fn=None):
    """
    From all AI-selected clips, generate multiple version playlists.
    
    策略：每个版本从全量候选池中独立挑选，保证时长和完整性。
    - Hook: 各版本尽量用不同的（有的话），没有就共享
    - Product: 各版本核心 product 不同，但允许补充共享的
    - Close: 各版本尽量用不同的
    - 目标：每个版本 55-60s，至少 6-8 个片段
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if not all_clips or num_versions <= 1:
        return [all_clips] if all_clips else []
    
    # 分类 + 按评分排序
    hooks = sorted([c for c in all_clips if _is_hook(c[0])], key=lambda c: c[4], reverse=True)
    products = sorted([c for c in all_clips if _is_product(c[0])], key=lambda c: c[4], reverse=True)
    closes = sorted([c for c in all_clips if _is_close(c[0])], key=lambda c: c[4], reverse=True)
    bridges = sorted([c for c in all_clips if _is_bridge(c[0])], key=lambda c: c[4], reverse=True)
    
    _log(f"多版本候选池: {len(hooks)}个Hook, {len(products)}个产品, {len(closes)}个收尾, {len(bridges)}个过渡")
    
    # 去重辅助：按时间区间判断是否同一段
    def _time_key(c):
        return (round(c[2], 1), round(c[3], 1))
    
    versions = []
    used_hook_indices = set()
    used_close_indices = set()
    # 核心product分配：每个版本独占一批product的时间区间
    assigned_product_times = {}  # version_index -> set of start times
    
    for v in range(min(num_versions, 3)):
        version_clips = []
        v_product_times = set()
        assigned_product_times[v] = v_product_times
        
        # ── Hook ──
        # 优先选没用过的hook
        hook = None
        for hi, h in enumerate(hooks):
            if hi not in used_hook_indices:
                hook = h
                used_hook_indices.add(hi)
                break
        if hook is None and hooks:
            # 没有独占hook了，用最好的（允许重叠）
            hook = hooks[v % len(hooks)]
        if hook:
            version_clips.append(hook)
        
        # ── Bridge ──（0-1个，允许重叠）
        if bridges:
            bridge = bridges[v % len(bridges)]
            version_clips.append(bridge)
        
        # ── Product ──（核心！每个版本差异化分配 product）
        # 策略：round-robin 轮流挑选，保证每个版本拿到不同的核心 product
        if products:
            current_dur = sum(c[5] for c in version_clips)
            close_dur = closes[0][5] if closes else 5
            target_product_dur = max(55 - current_dur - close_dur, 20)
            
            # 找出其他版本已选的 product 时间区间
            other_times = set()
            for ov, ot in assigned_product_times.items():
                if ov != v:
                    other_times.update(ot)
            
            added_starts = {c[2] for c in version_clips}
            dur = 0
            
            # 第一轮：优先选其他版本没用的（独占）
            exclusive = [p for p in products if p[2] not in other_times and p[2] not in added_starts]
            for p in exclusive:
                if dur + p[5] <= target_product_dur + 8:
                    version_clips.append(p)
                    v_product_times.add(p[2])
                    added_starts.add(p[2])
                    dur += p[5]
                if dur >= target_product_dur:
                    break
            
            # 第二轮：如果独占不够，从所有 product 中按 offset 轮流选
            # offset 让不同版本从不同位置开始挑选，避免都选评分最高的
            if dur < target_product_dur:
                offset = v * 3  # 每个版本跳过不同的前几个
                remaining = [p for p in products if p[2] not in added_starts]
                if offset < len(remaining):
                    remaining = remaining[offset:] + remaining[:offset]
                for p in remaining:
                    if dur + p[5] <= target_product_dur + 10:
                        version_clips.append(p)
                        v_product_times.add(p[2])
                        added_starts.add(p[2])
                        dur += p[5]
                    if dur >= target_product_dur:
                        break
            
            # 第三轮：实在不够就全加
            if dur < target_product_dur * 0.7:
                for p in products:
                    if p[2] not in added_starts:
                        version_clips.append(p)
                        added_starts.add(p[2])
                        dur += p[5]
                        if dur >= target_product_dur * 0.8:
                            break
        
        # ── Close ──
        close = None
        for ci, c in enumerate(closes):
            if ci not in used_close_indices:
                close = c
                used_close_indices.add(ci)
                break
        if close is None and closes:
            close = closes[v % len(closes)]
        if close:
            version_clips.append(close)
        
        # ── 排列 ──
        version_clips = _arrange_version(version_clips, log_fn, style=v)
        
        # ── 去重 ──
        if version_clips:
            version_clips = _semantic_dedup_version(version_clips, log_fn)
            version_clips = _dedup_by_time(version_clips)
        
        if version_clips:
            versions.append(version_clips)
            total_dur = sum(c[5] for c in version_clips)
            hook_type = version_clips[0][0] if version_clips else '无'
            _log(f"版本{v+1}: {len(version_clips)}片段, 开场={hook_type}, 时长={total_dur:.1f}s")
    
    return versions


def _is_hook(cat):
    cat = cat.lower()
    return 'hook' in cat or cat in ('爆料', '痛点', '信任', '夸奖', '场景')

def _is_product(cat):
    cat = cat.lower()
    return 'product' in cat or cat in ('版型', '面料', '细节', '穿搭', '对比', '产品展示')

def _is_close(cat):
    cat = cat.lower()
    return 'close' in cat or cat in ('促单', '尺码', '信任强化', '风格定位', '收尾')

def _is_bridge(cat):
    cat = cat.lower()
    return 'bridge' in cat or cat in ('过渡', '提问', '科普')


def _semantic_dedup_version(clips, log_fn=None):
    """版本内语义去重：同一版本内如果两段话关键词重叠>60%，只保留评分高的那段"""
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if len(clips) < 3:
        return clips
    
    _stop = set("的 了 在 是 我 有 和 就 都 也 不 人 这 那 他 到 说 要 会 着 过 把 得 能 可以 很 被 让 给 比 从 向 还 又 而 但".split())
    _punct = set("，。！？、；：“”‘’（）…—·")
    
    def _kw(text):
        return set(c for c in text if c not in _stop and c not in _punct and c.strip())
    
    keep = []
    kept_kws = []
    for clip in clips:
        ct, text, start, end, score, dur = clip
        # 保护hook和close不被语义去重删掉
        if 'hook' in ct.lower() or 'close' in ct.lower():
            keep.append(clip); kept_kws.append(_kw(text)); continue
        kw = _kw(text)
        if len(kw) < 2:
            keep.append(clip); kept_kws.append(kw); continue
        dup = False
        for pi, pk in enumerate(kept_kws):
            if len(pk) < 2:
                continue
            ov = len(kw & pk)
            r = ov / min(len(kw), len(pk)) if min(len(kw), len(pk)) > 0 else 0
            if r > 0.6 and ov >= 3:
                # 重复了，保留评分高的
                if score > keep[pi][4]:
                    keep[pi] = clip
                    kept_kws[pi] = kw
                dup = True
                break
        if not dup:
            keep.append(clip)
            kept_kws.append(kw)
    
    removed = len(clips) - len(keep)
    if removed > 0:
        _log(f"版本内去重: {len(clips)}→{len(keep)} (移除{removed}段重复)")
    return keep


def _arrange_version(clips, log_fn=None, style=0):
    """
    将一组片段排列成 hook→bridge→product→close 的顺序
    style=0: hook→bridge→products按时间→close
    style=1: hook→products按评分→close（最高分product紧跟hook）
    style=2: hook→products按时间倒序→close（先讲细节再讲面料）
    """
    hooks = []
    bridges = []
    products = []
    closes = []
    others = []
    
    for c in clips:
        cat = c[0].lower()
        if 'hook' in cat or cat in ('爆料', '痛点', '信任', '夸奖', '场景'):
            hooks.append(c)
        elif 'close' in cat or cat in ('促单', '尺码', '信任强化', '风格定位', '收尾'):
            closes.append(c)
        elif 'bridge' in cat or cat in ('过渡', '提问', '科普'):
            bridges.append(c)
        elif 'product' in cat or cat in ('版型', '面料', '细节', '穿搭', '对比', '产品展示'):
            products.append(c)
        else:
            others.append(c)
    
    # 如果没有明确的hook，用评分最高的product/other当开场
    if not hooks:
        candidates = products + others
        if candidates:
            candidates.sort(key=lambda c: c[4], reverse=True)
            hooks.append(candidates.pop(0))
            products = [p for p in products if p not in hooks]
            others = [o for o in others if o not in hooks]
    
    # 如果没有明确的close，用最后一个product/other当收尾
    if not closes:
        candidates = products + others
        if candidates:
            candidates.sort(key=lambda c: c[4])  # 评分最低的当close
            closes.append(candidates.pop(0))
            products = [p for p in products if p not in closes]
            others = [o for o in others if o not in closes]
    
    # 组装：hook → bridge → products → others → close
    # 不同style用不同product排列，产生版本差异
    if style == 1:
        products.sort(key=lambda c: c[4], reverse=True)  # 按评分降序
    elif style == 2:
        products.sort(key=lambda c: c[2], reverse=True)  # 按时间倒序
    else:
        products.sort(key=lambda c: c[2])  # 按时间正序（默认）
    
    arranged = []
    arranged.extend(hooks)
    arranged.extend(bridges)
    arranged.extend(products)
    arranged.extend(others)
    arranged.extend(closes)
    
    return arranged


def _dedup_by_time(clips):
    """Remove clips that overlap in time, keeping the one with higher score.
    Preserves the original order (narrative arrangement from _arrange_version)."""
    if not clips:
        return clips
    # Don't re-sort! Keep the narrative order from _arrange_version.
    # Build overlap groups, keep highest score per group, maintain original order.
    result = []
    removed_starts = set()
    for i, clip in enumerate(clips):
        if clip[2] in removed_starts:
            continue
        # Check if this clip overlaps with any later clip
        for j, other in enumerate(clips):
            if i == j or other[2] in removed_starts:
                continue
            if clip[2] < other[3] and clip[3] > other[2]:
                # Overlap: keep the one with higher score
                if other[4] > clip[4]:
                    removed_starts.add(clip[2])
                    break
                else:
                    removed_starts.add(other[2])
        if clip[2] not in removed_starts:
            result.append(clip)
    return result
