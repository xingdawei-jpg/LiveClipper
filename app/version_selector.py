# -*- coding: utf-8 -*-
"""M1-C 版本选择层（Version Selection Layer）

把 Analyzer 发现的 N 个策略方向，确定性转换成用户要求的 N 个可生产版本。

核心原则（David 定调）：
- 选择器只做确定性排序 + 去重，不让 LLM 直接说「我推荐这三个」。
- 去重分两层：
  1. 商业去重（commercial dedup）：同 strategy_family 默认只保留一个 Strategy Version，
     因为同 family 的策略竞争的是同一个购买动机（如 body_shaping 下腰腹遮肉 vs 背厚显薄）。
  2. 内容去重（content dedup）：不同 family 但证据/angle 高度重合时也跳过。
- 高质量 distinct 策略不足时，用 Edit Variant 补足，不硬编弱策略。
- 返回完整决策轨迹：不只「选了谁」，还「为什么跳过谁」+ overlap breakdown。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from commercial_analyzer import Strategy


@dataclass(frozen=True)
class OverlapBreakdown:
    family_overlap: float
    angle_overlap: float
    evidence_overlap: float
    total: float


def compute_overlap_breakdown(sa: Strategy, sb: Strategy) -> OverlapBreakdown:
    """overlap 分项：family +0.4、sub_angle +0.3、证据共享比例 *0.3。"""
    family_overlap = 0.4 if (sa.strategy_family and sa.strategy_family == sb.strategy_family) else 0.0
    angle_overlap = 0.3 if (sa.sub_angle and sa.sub_angle == sb.sub_angle) else 0.0
    ids_a = {sid for item in sa.evidence for sid in item.subtitle_ids}
    ids_b = {sid for item in sb.evidence for sid in item.subtitle_ids}
    union = ids_a | ids_b
    evidence_overlap = 0.3 * (len(ids_a & ids_b) / len(union)) if union else 0.0
    total = family_overlap + angle_overlap + evidence_overlap
    return OverlapBreakdown(
        family_overlap=round(family_overlap, 3),
        angle_overlap=round(angle_overlap, 3),
        evidence_overlap=round(evidence_overlap, 3),
        total=round(min(1.0, total), 3),
    )


def compute_pairwise_overlap(sa: Strategy, sb: Strategy) -> float:
    return compute_overlap_breakdown(sa, sb).total


@dataclass(frozen=True)
class SelectedVersion:
    strategy_id: str
    family: str
    angle: str
    version_type: str      # "strategy"（独立商业故事）| "variant"（同一故事的变体）
    variant_of: str        # variant 时指向哪个 strategy_id，否则 ""
    rank: int
    base_score: float
    max_overlap: float


@dataclass(frozen=True)
class SkippedStrategy:
    strategy_id: str
    family: str
    angle: str
    base_score: float
    skip_reason: str       # "same_family"（商业去重）| "overlap"（内容去重）
    overlap_with: str
    breakdown: OverlapBreakdown


@dataclass(frozen=True)
class VersionSelection:
    selected: tuple[SelectedVersion, ...]
    skipped: tuple[SkippedStrategy, ...]


def _base_score(s: Strategy) -> float:
    return s.story_strength * s.material_sufficiency * s.contract_compatibility


def select_versions(
    strategies: Sequence[Strategy],
    n: int,
    overlap_threshold: float = 0.6,
) -> VersionSelection:
    """确定性选择 n 个版本，含同 family 商业去重 + 完整决策轨迹。"""
    if n <= 0:
        return VersionSelection((), ())

    ranked = sorted(strategies, key=_base_score, reverse=True)

    selected: list[tuple[Strategy, float]] = []
    skipped: list[tuple[Strategy, str, str, OverlapBreakdown]] = []

    for s in ranked:
        # 1. 商业去重：同 family 已选 → 跳过（除非后面显式 allow_multi_strategy）
        same_family_picked: Strategy | None = None
        if s.strategy_family:
            for p, _ in selected:
                if p.strategy_family == s.strategy_family:
                    same_family_picked = p
                    break
        if same_family_picked is not None:
            bd = compute_overlap_breakdown(s, same_family_picked)
            skipped.append((s, "same_family", same_family_picked.strategy_id, bd))
            continue

        # 2. 内容去重：与已选策略的最大 overlap 超阈值 → 跳过
        max_overlap = 0.0
        overlap_with = ""
        max_bd: OverlapBreakdown | None = None
        for p, _ in selected:
            bd = compute_overlap_breakdown(s, p)
            if bd.total > max_overlap:
                max_overlap = bd.total
                overlap_with = p.strategy_id
                max_bd = bd
        if max_bd is not None and max_overlap >= overlap_threshold:
            skipped.append((s, "overlap", overlap_with, max_bd))
            continue

        selected.append((s, max_overlap))
        if len(selected) >= n:
            break

    # 构造 SelectedVersion
    result: list[SelectedVersion] = []
    for i, (s, max_overlap) in enumerate(selected, 1):
        result.append(SelectedVersion(
            strategy_id=s.strategy_id,
            family=s.strategy_family,
            angle=s.sub_angle,
            version_type="strategy",
            variant_of="",
            rank=i,
            base_score=round(_base_score(s), 3),
            max_overlap=round(max_overlap, 3),
        ))

    # Edit Variant 补足
    if result and len(result) < n:
        idx = 0
        while len(result) < n:
            base = result[idx % len(result)]
            result.append(SelectedVersion(
                strategy_id=base.strategy_id,
                family=base.family,
                angle=base.angle,
                version_type="variant",
                variant_of=base.strategy_id,
                rank=len(result) + 1,
                base_score=base.base_score,
                max_overlap=0.0,
            ))
            idx += 1

    skipped_result = tuple(SkippedStrategy(
        strategy_id=s.strategy_id,
        family=s.strategy_family,
        angle=s.sub_angle,
        base_score=round(_base_score(s), 3),
        skip_reason=reason,
        overlap_with=ow,
        breakdown=bd,
    ) for s, reason, ow, bd in skipped)

    return VersionSelection(selected=tuple(result), skipped=skipped_result)
