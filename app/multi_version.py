# -*- coding: utf-8 -*-
"""
Multi-version output for LiveClipper

Strategy (v3): Angle-based differentiation
1. AI selects ALL good clips once (with focus tags)
2. Group products by focus/angle (面料/功能/风格/版型/工艺 etc.)
3. Each version picks a DIFFERENT angle as primary focus
4. Hook + Close shared, but products tell different stories

Key: clips tuple is (category, text, start, end, score, duration, focus)
"""
import os, re, random

# Angle grouping rules
ANGLE_GROUPS = {
    "功能": ["防晒", "降温", "导热", "透气", "吸汗", "防紫外线", "保暖", "速干", "防水", "抗菌", "功能"],
    "面料": ["面料", "材质", "成分", "亚麻", "棉", "真丝", "羊毛", "雪纺", "天丝", "莫代尔", "支数", "纱线", "编织", "工艺", "双色麻"],
    "风格": ["风格", "老钱", "气质", "洋气", "高级", "质感", "调性", "品味", "时髦", "复古", "法式", "韩系", "简约", "麻褶", "褶感", "皱"],
    "版型": ["版型", "剪裁", "显瘦", "修身", "宽松", "廓形", "垂感", "遮肉", "收腰", "A字", "直筒", "高腰"],
    "价格": ["性价比", "划算", "折扣", "优惠", "八五折", "打折", "活动", "周年"],
    "信任": ["自留", "回购", "盲拍", "不搞虚", "真的", "亲测", "实测"],
}

def _detect_angle(text, focus_hint=""):
    """Detect which angle a clip belongs to based on text + focus hint"""
    combined = focus_hint + " " + text
    best_angle = "其他"
    best_count = 0
    for angle, keywords in ANGLE_GROUPS.items():
        count = sum(1 for kw in keywords if kw in combined)
        if count > best_count:
            best_count = count
            best_angle = angle
    return best_angle


def generate_multi_versions(all_clips, num_versions=3, log_fn=None):
    """
    From all AI-selected clips, generate multiple version playlists.
    
    策略：每个版本聚焦不同卖点角度，讲不同的故事。
    - Hook: 各版本尽量用不同的
    - Product: 按角度分组，各版本主打不同角度
    - Close: 各版本尽量用不同的
    - 目标：每个版本 55-60s，至少 6-8 个片段
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if not all_clips or num_versions <= 1:
        return [all_clips] if all_clips else []
    
    # Unpack clips (support both 6-tuple and 7-tuple)
    def _focus(c):
        return c[6] if len(c) > 6 else ""
    
    def _dur(c):
        return c[5]
    
    # 分类 + 按评分排序
    hooks = sorted([c for c in all_clips if _is_hook(c[0])], key=lambda c: c[4], reverse=True)
    products = sorted([c for c in all_clips if _is_product(c[0])], key=lambda c: c[4], reverse=True)
    closes = sorted([c for c in all_clips if _is_close(c[0])], key=lambda c: c[4], reverse=True)
    bridges = sorted([c for c in all_clips if _is_bridge(c[0])], key=lambda c: c[4], reverse=True)
    
    # 给每个 product 打角度标签
    product_angles = {}
    for p in products:
        angle = _detect_angle(p[1], _focus(p))
        product_angles[id(p)] = angle
    
    # 统计角度分布
    angle_counts = {}
    for p in products:
        a = product_angles[id(p)]
        angle_counts[a] = angle_counts.get(a, 0) + 1
    _log(f"多版本候选池: {len(hooks)}个Hook, {len(products)}个产品, {len(closes)}个收尾, {len(bridges)}个过渡")
    _log(f"角度分布: {dict(sorted(angle_counts.items(), key=lambda x: -x[1]))}")
    
    # 确定每个版本主打的角度（按产品数量排序，最丰富的角度优先）
    available_angles = sorted(angle_counts.keys(), key=lambda a: -angle_counts[a])
    # 过滤掉只有1个产品的角度（太单薄，不适合做主打）
    main_angles = [a for a in available_angles if angle_counts[a] >= 2]
    if not main_angles:
        main_angles = available_angles[:1]  # 至少有一个主打角度
    
    # 为每个版本分配不同的主打角度，不够时循环+偏移
    version_angles = []
    for v in range(min(num_versions, 3)):
        if v < len(main_angles):
            version_angles.append(main_angles[v])
        else:
            # 角度不够时，用可用角度轮换+偏移
            version_angles.append(available_angles[v % len(available_angles)])
    
    versions = []
    used_hook_indices = set()
    used_close_indices = set()
    
    for v in range(min(num_versions, 3)):
        version_clips = []
        
        # ── 本版本主打角度 ──
        primary_angle = version_angles[v]
        # 次要角度：其他角度轮换
        other_angles = [a for a in available_angles if a != primary_angle]
        
        _log(f"版本{v+1}主打角度: {primary_angle}")
        
        # ── Hook ──
        hook = None
        for hi, h in enumerate(hooks):
            if hi not in used_hook_indices:
                hook = h
                used_hook_indices.add(hi)
                break
        if hook is None and hooks:
            hook = hooks[v % len(hooks)]
        if hook:
            version_clips.append(hook)
        
        # ── Bridge ──（0-1个）
        if bridges:
            bridge = bridges[v % len(bridges)]
            version_clips.append(bridge)
        
        # ── Product ──（核心！按角度分配）
        if products:
            current_dur = sum(_dur(c) for c in version_clips)
            close_dur = closes[0][5] if closes else 5
            target_product_dur = max(55 - current_dur - close_dur, 20)
            MAX_PRODUCTS = 8  # 每个版本最多8个产品片段，防止碎片化
            
            added_starts = {c[2] for c in version_clips}
            dur = 0
            product_count = 0
            
            # 第一轮：主打角度的 product（核心差异化，最多5个）
            primary_products = [p for p in products 
                               if product_angles[id(p)] == primary_angle 
                               and p[2] not in added_starts
                              ]
            for p in primary_products:
                if product_count >= 5:
                    break
                if dur + _dur(p) <= target_product_dur + 5:
                    version_clips.append(p)
                    added_starts.add(p[2])
                    dur += _dur(p)
                    product_count += 1
            
            # 第二轮：补充其他角度的产品（每个角度最多1个，总计最多2个）
            other_added = 0
            for angle in other_angles:
                if other_added >= 2 or product_count >= MAX_PRODUCTS - 1:
                    break
                angle_products = [p for p in products 
                                 if product_angles[id(p)] == angle 
                                 and p[2] not in added_starts
                                ]
                for p in angle_products[:1]:
                    if dur + _dur(p) <= target_product_dur + 8:
                        version_clips.append(p)
                        added_starts.add(p[2])
                        dur += _dur(p)
                        product_count += 1
                        other_added += 1
            
            # 第三轮：如果产品<4个且时长不足，从剩余产品中补充
            if product_count < 4 and dur < target_product_dur * 0.5:
                remaining = [p for p in products if p[2] not in added_starts]
                for p in remaining:
                    if product_count >= MAX_PRODUCTS:
                        break
                    if dur + _dur(p) <= target_product_dur + 5:
                        version_clips.append(p)
                        added_starts.add(p[2])
                        dur += _dur(p)
                        product_count += 1
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
            total_dur = sum(_dur(c) for c in version_clips)
            hook_type = version_clips[0][0] if version_clips else '无'
            _log(f"版本{v+1}: {len(version_clips)}片段, 开场={hook_type}, 主打={primary_angle}, 时长={total_dur:.1f}s")
    
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
    _punct = set("，。！？、；：""''（）…—·")
    
    def _kw(text):
        return set(c for c in text if c not in _stop and c not in _punct and c.strip())
    
    keep = []
    kept_kws = []
    for clip in clips:
        ct, text, start, end, score, dur = clip[0], clip[1], clip[2], clip[3], clip[4], clip[5]
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
    # 不重排products！保持AI叙事顺序，版本差异化靠选不同片段实现
    # （之前按时间/评分排序会破坏AI编排的叙事逻辑）
    
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
    result = []
    removed_starts = set()
    for i, clip in enumerate(clips):
        if clip[2] in removed_starts:
            continue
        for j, other in enumerate(clips):
            if i == j or other[2] in removed_starts:
                continue
            if clip[2] < other[3] and clip[3] > other[2]:
                if other[4] > clip[4]:
                    removed_starts.add(clip[2])
                    break
                else:
                    removed_starts.add(other[2])
        if clip[2] not in removed_starts:
            result.append(clip)
    return result
