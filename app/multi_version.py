# -*- coding: utf-8 -*-
"""
Multi-version output for LiveClipper

Strategy:
1. AI selects ALL good clips once (just identifies good content)
2. Split clips into versions (alternating by score, no overlap)
3. Each version arranges its own clips into hook→product→close order

Key: clips tuple is (category, text, start, end, score, duration)
"""
import os, re, random

def generate_multi_versions(all_clips, num_versions=3, log_fn=None):
    """
    From all AI-selected clips, generate multiple version playlists.
    
    策略：
    1. 每个版本从全量候选中选取，允许重叠
    2. 但核心片段（开场hook + 主打product）必须不同
    3. 保证每个版本都像完整的切片视频
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if not all_clips or num_versions <= 1:
        return [all_clips] if all_clips else []
    
    # 分类
    hooks = [c for c in all_clips if _is_hook(c[0])]
    products = [c for c in all_clips if _is_product(c[0])]
    closes = [c for c in all_clips if _is_close(c[0])]
    bridges = [c for c in all_clips if _is_bridge(c[0])]
    
    # 按评分排序
    hooks.sort(key=lambda c: c[4], reverse=True)
    products.sort(key=lambda c: c[4], reverse=True)
    closes.sort(key=lambda c: c[4], reverse=True)
    bridges.sort(key=lambda c: c[4], reverse=True)
    
    _log(f"多版本: {len(hooks)}个Hook, {len(products)}个产品, {len(closes)}个收尾, {len(bridges)}个过渡")
    
    # 每个版本分配不同的"核心片段"
    # 核心片段 = 开场片段(hook或高评分product) + 1-2个独占product
    # 其余product允许重叠
    exclusive_products = set()  # 被某版本独占的product起始时间
    
    versions = []
    for v in range(min(num_versions, 3)):
        version_clips = []
        
        # 开场片段：V1用正式hook，V2+用不同hook或高评分product
        if v == 0 and hooks:
            version_clips.append(hooks[0])
        elif v > 0 and len(hooks) > v:
            version_clips.append(hooks[v])
        elif hooks:
            # 只有1个hook，V2+也用它（允许重叠）
            version_clips.append(hooks[0])
        
        # bridge（允许重叠）
        if bridges:
            version_clips.append(bridges[v % len(bridges)])
        
        # product：每个版本独占1-2个不同的，其余共享
        if products:
            # 找这个版本的独占product（未被其他版本占的）
            my_exclusive = [p for p in products if p[2] not in exclusive_products]
            # 分配1-2个独占
            assigned = 0
            for p in my_exclusive:
                if assigned >= 2:
                    break
                version_clips.append(p)
                exclusive_products.add(p[2])
                assigned += 1
            
            # 再从全量product里按评分补齐到目标时长
            current_dur = sum(c[5] for c in version_clips)
            close_dur = sum(c[5] for c in closes[:2]) if len(closes) >= 2 else (closes[0][5] if closes else 0)
            target_dur = 55
            need_dur = max(target_dur - current_dur - close_dur, 10)
            
            added_starts = {c[2] for c in version_clips}
            dur = 0
            for p in products:
                if p[2] not in added_starts and dur + p[5] <= need_dur + 5:
                    version_clips.append(p)
                    added_starts.add(p[2])
                    dur += p[5]
                if dur >= need_dur:
                    break
        
        # close（允许重叠，但尽量用不同的）
        if closes:
            close_idx = v % len(closes)
            version_clips.append(closes[close_idx])
            if len(closes) >= 2:
                second_idx = (close_idx + 1) % len(closes)
                if second_idx != close_idx and closes[second_idx][2] not in {c[2] for c in version_clips}:
                    version_clips.append(closes[second_idx])
        elif closes:
            version_clips.append(closes[0])
        
        # 排列：hook→bridge→product→close
        version_clips = _arrange_version(version_clips, log_fn)
        
        if version_clips:
            version_clips = _dedup_by_time(version_clips)
            versions.append(version_clips)
            hook_type = version_clips[0][0] if version_clips else '无'
            _log(f"版本{v+1}: {len(version_clips)}片段, 开场={hook_type}, 时长={sum(c[5] for c in version_clips):.1f}s")
    
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


def _arrange_version(clips, log_fn=None):
    """
    将一组片段排列成 hook→bridge→product→close 的顺序
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
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
            # 更新products
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
    arranged = []
    arranged.extend(hooks)
    arranged.extend(bridges)
    arranged.extend(products)
    arranged.extend(others)
    arranged.extend(closes)
    
    return arranged


def _dedup_by_time(clips):
    """Remove clips that overlap in time, keeping the one with higher score"""
    if not clips:
        return clips
    sorted_clips = sorted(clips, key=lambda c: c[2])
    result = [sorted_clips[0]]
    for clip in sorted_clips[1:]:
        last = result[-1]
        if clip[2] < last[3]:  # overlap
            if clip[4] > last[4]:
                result[-1] = clip
        else:
            result.append(clip)
    return result
