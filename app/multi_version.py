# -*- coding: utf-8 -*-
"""
Multi-version output for LiveClipper

Strategy: AI selects all candidate clips once, then code generates multiple versions
by picking different hooks and rearranging product/close segments.

Implementation plan:
1. Add `multi_version` parameter to ai_analyze_clips (return ALL candidates, not just best)
2. Add `generate_multi_versions()` function that takes all candidates and creates 2-3 versions
3. Add `num_versions` parameter to process_video
4. GUI: add a spinbox for number of versions (1-3, default 1)

Each version strategy:
- V1: Best hook + all product clips + best close (current behavior, "safest")
- V2: Different hook + subset of product clips + alternative close  
- V3: Most dramatic hook + different product order + urgency close

Key: clips tuple is (category, text, start, end, score, duration)
"""
import os, re, random, itertools

def generate_multi_versions(all_clips, num_versions=3, log_fn=None):
    """
    From all AI-selected clips, generate multiple version playlists.
    
    all_clips: list of (category, text, start, end, score, duration)
    Returns: list of clip lists, each being a version
    
    Strategy:
    - Separate clips by type: hooks, products, closes, bridges
    - V1: highest-scored hook + all products + highest-scored close
    - V2: different hook (if available) + rotated products + alt close
    - V3: remaining hook + remaining products (different combination)
    """
    def _log(msg):
        if log_fn: log_fn(msg)
    
    if not all_clips or num_versions <= 1:
        return [all_clips] if all_clips else []
    
    # Categorize clips
    hooks = [c for c in all_clips if c[0] in ('HOOK', 'hook', 'Hook', '爆料hook', '痛点hook', '信任hook', '夸奖hook', '场景hook')]
    products = [c for c in all_clips if c[0] in ('PRODUCT', 'product', 'Product', '版型', '面料', '细节', '穿搭', '对比', '产品展示')]
    closes = [c for c in all_clips if c[0] in ('CLOSE', 'close', 'Close', '促单', '尺码', '信任强化', '风格定位', '收尾')]
    bridges = [c for c in all_clips if c[0] in ('BRIDGE', 'bridge', 'Bridge', '过渡', '提问', '科普')]
    
    # Fallback: if category names don't match, sort by score
    if not hooks and not products and not closes:
        # All clips are same type or unknown - just split by position
        _log(f"多版本: 无法按类型分组，按评分排序分配")
        hooks = [c for c in all_clips if 'hook' in c[0].lower() or c[0] in ('爆料', '痛点', '信任', '夸奖', '场景')]
        products = [c for c in all_clips if c[0] not in ('HOOK', 'hook', 'CLOSE', 'close') and 'hook' not in c[0].lower() and 'close' not in c[0].lower()]
        closes = [c for c in all_clips if 'close' in c[0].lower() or c[0] in ('促单', '尺码', '收尾')]
        
        # If still nothing categorized, treat first as hook, last as close, middle as product
        if not hooks and not products and not closes and len(all_clips) >= 3:
            hooks = [all_clips[0]]
            products = all_clips[1:-1]
            closes = [all_clips[-1]]
        elif not hooks and not products:
            return [all_clips]
    
    # Sort by score (higher is better)
    hooks.sort(key=lambda c: c[4], reverse=True)
    products.sort(key=lambda c: c[4], reverse=True)
    closes.sort(key=lambda c: c[4], reverse=True)
    bridges.sort(key=lambda c: c[4], reverse=True)
    
    _log(f"多版本: {len(hooks)}个Hook, {len(products)}个产品, {len(closes)}个收尾, {len(bridges)}个过渡")
    
    versions = []
    
    for v in range(min(num_versions, 3)):
        version_clips = []
        
        # Pick hook for this version
        hook_idx = v % len(hooks) if hooks else -1
        if hook_idx >= 0:
            version_clips.append(hooks[hook_idx])
            # If only 1 hook, reuse it for all versions
            if len(hooks) == 1 and v > 0:
                pass  # same hook, different products make it different enough
        
        # Add bridge (if available, rotate)
        if bridges:
            bridge_idx = v % len(bridges)
            version_clips.append(bridges[bridge_idx])
        
        # Pick product clips (rotate subset)
        if products:
            # V1: top products, V2: rotated, V3: different subset
            if v == 0:
                # Top products by score
                version_clips.extend(products[:min(3, len(products))])
            elif v == 1 and len(products) >= 2:
                # Rotate: skip first, add rest, wrap around
                rotated = products[1:] + products[:1]
                version_clips.extend(rotated[:min(3, len(rotated))])
            else:
                # Random-ish subset
                shuffled = products[:]
                random.shuffle(shuffled)
                version_clips.extend(shuffled[:min(3, len(shuffled))])
        
        # Pick close
        close_idx = v % len(closes) if closes else -1
        if close_idx >= 0:
            version_clips.append(closes[close_idx])
        elif closes:
            version_clips.append(closes[0])
        
        if version_clips:
            # Deduplicate by time overlap
            version_clips = _dedup_by_time(version_clips)
            versions.append(version_clips)
            _log(f"版本{v+1}: {len(version_clips)}片段, Hook={hooks[hook_idx][0] if hook_idx >= 0 else '无'}, 时长={sum(c[5] for c in version_clips):.1f}s")
    
    return versions


def _dedup_by_time(clips):
    """Remove clips that overlap in time, keeping the one with higher score"""
    if not clips:
        return clips
    # Sort by start time
    sorted_clips = sorted(clips, key=lambda c: c[2])
    result = [sorted_clips[0]]
    for clip in sorted_clips[1:]:
        # Check overlap with last added clip
        last = result[-1]
        if clip[2] < last[3]:  # overlap
            # Keep the one with higher score
            if clip[4] > last[4]:
                result[-1] = clip
        else:
            result.append(clip)
    return result
