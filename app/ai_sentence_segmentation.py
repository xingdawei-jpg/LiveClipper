"""AI 语义重分段 / 确定性重分段：把可见字幕重分成「可独立播放的完整语句」。

背景与目标（2026-10-07）
    可见字幕来自 ASR 的**声学**分段（VAD 停顿 + 标点恢复），边界经常是错的。
    实测同一素材（立暖 JE5PYH10，SenseVoice 172 条可见 cue），用项目自己的
    播放闸门 ``_director_standalone_boundary_reason`` 逐条校验：

        59 条以逗号结尾   -> 「结尾未说完」  59/59 全部不通过
        24 条以承接词开头 -> 「开头承接上句」
        19 条以语气词结尾 -> 「结尾语气残留」
        合计仅 81/172 = 47.1% 可作为可独立播放的单元

    这既是用户听到的「断句」问题的量化形式，也是选片只能拿到半句话的根源：
    导演只能整条挑 cue、不能跨 cue、不能改写，于是半句话原样进成片。

两种做法
    ``apply_deterministic_closure`` —— **纯确定性，不调用 AI**（推荐）
        起点是每条可见条目各自成组，只在不满足播放闸门处按规则合并。
    ``apply_ai_sentence_segmentation`` —— 先用 AI 把相邻条目重新分组，再闭合
        AI 只输出**条目编号的分组**，不输出任何文字。

    三池实测（同素材，同一遍单遍导演）：

        |               | 可见 | 下游可用候选 | 平均单元 | 边界通过 | AI 调用 | selector |
        | 原始           | 172  | 63          | 2.32s   | 47.1%   | 0       | ok       |
        | AI 分组+闭合    | 62   | 30          | 7.72s   | 100%    | 2       | blocked  |
        | 确定性闭合      | 82   | 48          | 5.69s   | 100%    | 0       | ok       |

    **AI 分组是反效果**：并得太粗 -> 下游可用候选只剩 30 -> 导演为填满 6 章结构
    只找到 9 个合适单元 -> 跨章节重复引用 -> ``candidate_reused_by_plan`` ->
    selector 拒绝、无 render-ready 交接。确定性闭合则全链路通过。

不变式（由构造保证，不依赖 AI 自觉）
    - 不改写任何文字：新文本 = 原词条 text 的拼接
    - 不丢内容：分组必须是 1..N 的连续升序划分，每个编号恰好一次
    - 不改变词条：每组的 words = 原条目 words 的顺序拼接
    - 默认关闭：``SEGMENTATION_AI_ENABLED`` / ``DETERMINISTIC_CLOSURE_ENABLED``
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

# ── 开关 ────────────────────────────────────────────────────────────────────
# AI 语义重分段（需要 AI 调用）。默认关闭。
SEGMENTATION_AI_ENABLED = False
# 确定性重分段（纯本地，无 AI 调用）。
# 2026-10-07 开启：同素材三池实测里它是明确的赢家 ——
#   原始 172 条 / 边界通过 47.1% / 下游可用候选 63 / <2s 碎片 13
#   开启后 82 条 / 边界通过 100% / 下游可用候选 48 / <2s 碎片 0
#   且全链路通过（plan_valid + selector ok + render-ready + quality_gate passed），
#   而 AI 分组方案（62 条）会让下游可用候选降到 30 并触发 candidate_reused_by_plan。
# 可用 LIVECLIPPER_DETERMINISTIC_CLOSURE=0 一键关闭做对照。
DETERMINISTIC_CLOSURE_ENABLED = True
# 环境变量覆盖（便于对照测试与一键回退，无需改代码）：
#   LIVECLIPPER_AI_SEGMENTATION=1/0      -> AI 语义重分段
#   LIVECLIPPER_DETERMINISTIC_CLOSURE=1/0 -> 确定性重分段
SEGMENTATION_ENV_FLAG = "LIVECLIPPER_AI_SEGMENTATION"
DETERMINISTIC_CLOSURE_ENV_FLAG = "LIVECLIPPER_DETERMINISTIC_CLOSURE"


def _env_enabled(env_name: str, default: bool) -> bool:
    import os

    raw = os.environ.get(env_name)
    if raw is not None:
        return str(raw).strip() == "1"
    return bool(default)


def segmentation_ai_enabled() -> bool:
    """是否启用 AI 语义重分段。环境变量优先，其次模块常量。"""
    return _env_enabled(SEGMENTATION_ENV_FLAG, SEGMENTATION_AI_ENABLED)


def deterministic_closure_enabled() -> bool:
    """是否启用确定性重分段。环境变量优先，其次模块常量。"""
    return _env_enabled(DETERMINISTIC_CLOSURE_ENV_FLAG, DETERMINISTIC_CLOSURE_ENABLED)


SEGMENTATION_STAGE = "M1_5_ai_sentence_segmentation"
SEGMENTATION_MAX_TOKENS = 6000
# 修正调用次数上限（第一次分组之外的额外尝试）。
SEGMENTATION_MAX_REPAIRS = 1
# 合并后单元的最短时长。过短的单元通常是残句，应继续与邻居合并。
SEGMENTATION_MIN_UNIT_SECONDS = 1.2
# 合并后单元的时长上限：超过它说明并过头了（只作为告警，不阻断）。
SEGMENTATION_MAX_UNIT_SECONDS = 12.0


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _plain(value: Any) -> str:
    """去掉标点与空白，仅用于“内容未变”的校验。"""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", _text(value))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_words(unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(item) for item in (unit.get("words") or ()) if isinstance(item, Mapping)]


def build_unit_rows(units: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把可见字幕条目整理成带稳定编号的行（AI 只认编号）。"""
    rows: list[dict[str, Any]] = []
    for index, unit in enumerate(units, start=1):
        if not isinstance(unit, Mapping):
            continue
        words = _row_words(unit)
        start = _number(unit.get("start"))
        end = _number(unit.get("end"), start)
        if words:
            start = min(start, _number(words[0].get("start"), start))
            end = max(end, _number(words[-1].get("end"), end))
        rows.append({
            "id": index,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(max(0.0, end - start), 3),
            "text": _text(unit.get("text")),
            "words": words,
        })
    return rows


def build_segmentation_prompt(
    rows: Sequence[Mapping[str, Any]],
    *,
    failures: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    lines = [
        "你是短视频字幕的断句编辑。下面是同一条直播素材的分段字幕，编号从 1 开始。",
        "",
        "这些分段来自语音识别的**声学切分**（按停顿与自动标点），边界经常是错的：",
        "一句话被切成两半、组尾停在逗号、或者以承接词开头。",
        "",
        "你的任务：把这些编号**重新分组**，使每一组成为「能独立播放的完整语句」。",
        "",
        "硬性规则（三条都要满足，机器会逐条校验）：",
        "1. 每组的文本必须以句末标点「。！？」结尾。",
        "   —— 不要以逗号「，」结尾；不要把语气词（呃/嗯/啊/哦/诶）留在组尾。",
        "2. 每组不能以承接词开头：",
        "   而且/还有/然后/所以/但是/不过/就是/也/还/都/又/再/因为/首先/以及 等。",
        "   —— 这类编号说明它在承接上一句，必须和**它前面的组**合并。",
        "3. 编号必须连续、升序、每个编号恰好出现一次：不丢内容、不重复、不跳号。",
        "",
        "★最重要★ 合并是有传递性的，必须一直合到整组同时满足规则 1 和 2：",
        "  只做「两两合并」是不够的。判断方法：",
        "  · 某一组以「，」结尾 —— 这句话还没说完，必须把**它的下一组**也并进来；",
        "  · 某一组的第一个编号以承接词开头 ——",
        "    必须把**整组并入前一组**，而不是把它当成新组的开头；",
        "  · 并入后重新检查这两条，直到都满足为止。连续并 3-4 个编号是正常的。",
        "",
        "例子（第 3 组连续并了 3 个编号）：",
        "  输入: 12|你不要只看它的充绒，你要看它的蓬松度。",
        "       13|而且有时候充绒量没那么高，",
        "       14|但就证明有点什么公司不让穿牛仔裤啊。",
        '  正确: {"units": [..., {"source_ids": [12, 13, 14]}, ...]}',
        "        （13 以承接词开头且以逗号结尾，14 以句末标点收尾 —— 三个必须同组）",
        "  错误: [12] 单独一组 —— 虽然它以「。」结尾，但 13 在承接它",
        "        [13, 14] 一组 —— 13 以「而且」开头，违反规则 2",
        "",
        "补充要求：",
        "- 只做分组，**不要改写任何文字**，也不要输出字幕原文。",
        "- **单元长度目标 2-8 秒。** 合并到「刚好满足规则 1 和 2」就停，不要继续往后并。",
        "  能 2 条解决就不要并 3 条；能 3 条解决就不要并 4 条。",
        "- 如果继续合并会让整组超过 8 秒，而当前组已经满足规则 1-2，就停在这里。",
        "- **宁可多留一个边界，也不要把整段话并成 15 秒以上的长单元。**",
        "  长单元会让后续剪辑失去精细控制，比留一点瑕疵更糟。",
        "- 过短（<1.2 秒）的编号应该和邻居合并，让它成为一句完整的话。",
        "",
        "严格只输出下面这个 JSON（不要输出任何解释）：",
        '{"units": [{"source_ids": [1, 2]}, {"source_ids": [3]}, {"source_ids": [4, 5, 6]}]}',
        "",
        "编号与文本：",
    ]
    for row in rows:
        lines.append(f"{row['id']} | {row['duration']:.2f}s | {row['text']}")
    if failures:
        lines.extend([
            "",
            "=" * 60,
            "上一次的分组里，下面这些组**没有通过校验**。请修正后重新输出**完整**分组：",
        ])
        for item in failures[:24]:
            ids = item.get("source_ids") or []
            lines.append(
                f"  {ids}  ->  「{item.get('reason')}」  文本: {str(item.get('text'))[:70]}"
            )
        lines.extend([
            "",
            "修正方法：",
            "  · 「结尾未说完」= 该组以逗号结尾 -> 把**它的下一组**并进来，必要时继续往后并；",
            "  · 「开头承接上句」= 该组以承接词开头 -> 把**整组并入前一组**；",
            "  · 「结尾语气残留」= 组尾是语气词 -> 把**下一组**并进来，用句末标点收尾。",
            "覆盖全部编号，不只是被修正的部分。",
        ])
    return "\n".join(lines)


def parse_segmentation(data: Any, row_count: int) -> tuple[list[list[int]] | None, str]:
    """解析并严格校验 AI 的分组。返回 (groups, reason)；groups 为 None 表示非法。"""
    if not isinstance(data, Mapping):
        return None, "response_not_mapping"
    raw_units = data.get("units")
    if not isinstance(raw_units, list) or not raw_units:
        return None, "units_missing_or_empty"

    groups: list[list[int]] = []
    for item in raw_units:
        if not isinstance(item, Mapping):
            return None, "unit_not_mapping"
        ids = item.get("source_ids")
        if not isinstance(ids, list) or not ids:
            return None, "source_ids_missing_or_empty"
        group: list[int] = []
        for value in ids:
            try:
                group.append(int(value))
            except (TypeError, ValueError):
                return None, f"source_id_not_int:{value!r}"
        groups.append(group)

    flat = [index for group in groups for index in group]
    if len(flat) != row_count:
        return None, f"coverage_count_mismatch:{len(flat)}!={row_count}"
    if sorted(flat) != list(range(1, row_count + 1)):
        return None, "coverage_not_exact_partition"
    if flat != sorted(flat):
        return None, "groups_not_ascending"
    for group in groups:
        if group != list(range(group[0], group[0] + len(group))):
            return None, f"group_not_contiguous:{group[:4]}"
    return groups, ""


def merge_rows(rows: Sequence[Mapping[str, Any]], groups: Sequence[Sequence[int]]) -> list[dict[str, Any]]:
    """按分组合并条目。只重排/拼接既有词条，不改写文字、不改变词条内容。"""
    by_id = {int(row["id"]): row for row in rows}
    merged: list[dict[str, Any]] = []
    for group in groups:
        members = [by_id[index] for index in group if index in by_id]
        if not members:
            continue
        words: list[dict[str, Any]] = []
        texts: list[str] = []
        for member in members:
            words.extend(member["words"])
            texts.append(_text(member["text"]))
        text = "".join(texts).strip()
        if words:
            start = _number(words[0].get("start"), _number(members[0]["start"]))
            end = _number(words[-1].get("end"), _number(members[-1]["end"]))
        else:
            start = _number(members[0]["start"])
            end = _number(members[-1]["end"])
        merged.append({
            "start": round(start, 3),
            "end": round(end, 3),
            "text": text,
            "words": words,
            "source_unit_ids": [int(index) for index in group],
            "merged_count": len(members),
        })
    return merged


def audit_units(units: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """用项目自己的播放闸门逐条校验，并统计时长分布。"""
    from ai_clipper import _director_standalone_boundary_reason

    total = 0
    passed = 0
    reasons: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    short_ids: list[int] = []
    long_ids: list[int] = []
    for index, unit in enumerate(units, start=1):
        text = _text(unit.get("text"))
        if not text:
            continue
        total += 1
        reason = _text(_director_standalone_boundary_reason(text))
        duration = _number(unit.get("end")) - _number(unit.get("start"))
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
            if len(samples) < 12:
                samples.append({"index": index, "reason": reason, "text": text[:60]})
        else:
            passed += 1
        if duration < SEGMENTATION_MIN_UNIT_SECONDS:
            short_ids.append(index)
        if duration > SEGMENTATION_MAX_UNIT_SECONDS:
            long_ids.append(index)
    span = 0.0
    if units:
        span = max(0.0, _number(units[-1].get("end")) - _number(units[0].get("start")))
    return {
        "unit_count": total,
        "passed": passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "failure_samples": samples,
        "short_unit_ids": short_ids,
        "long_unit_ids": long_ids,
        "span_seconds": round(span, 3),
    }


def _group_failures(
    merged: Sequence[Mapping[str, Any]], groups: Sequence[Sequence[int]]
) -> list[dict[str, Any]]:
    """按组收集闸门违规项，供修正调用反馈。"""
    from ai_clipper import _director_standalone_boundary_reason

    items: list[dict[str, Any]] = []
    for unit, group in zip(merged, groups):
        text = _text(unit.get("text"))
        reason = _text(_director_standalone_boundary_reason(text))
        if reason:
            items.append({
                "source_ids": [int(index) for index in group],
                "reason": reason,
                "text": text[:120],
            })
    return items


def _verify_no_text_change(
    rows: Sequence[Mapping[str, Any]], merged: Sequence[Mapping[str, Any]]
) -> tuple[bool, str]:
    """确认合并没有改写任何文字（去标点后逐段一致）。"""
    before = _plain("".join(_text(row["text"]) for row in rows))
    after = _plain("".join(_text(unit["text"]) for unit in merged))
    if before == after:
        return True, ""
    return False, f"text_changed:{len(before)}->{len(after)}"


def close_gate_violations(
    rows: Sequence[Mapping[str, Any]],
    groups: Sequence[Sequence[int]],
    *,
    max_rounds: int | None = None,
    max_unit_seconds: float | None = None,
) -> tuple[list[list[int]], dict[str, Any]]:
    """机械闭合：按闸门规则把违规单元与**指定方向的邻居**合并，直到无违规。

    这是执行提示词里已经声明的硬规则，不是程序在做语义决策：

        开头承接上句            -> 并入**前一个**单元
        结尾未说完 / 结尾语气残留 -> 与**后一个**单元合并
        其余原因                -> 不动（无法用合并修，如实留给报告）

    ``max_rounds`` 为 ``None`` 时按输入规模自动取（见下）。**不要写死小常数**：
    实测 2121 段的素材需要 ~2000 轮，写死 400 会在半途截断，产出"半合并"的
    畸形字幕（实测通过率只从 27.9% 升到 33.9%）。

    ``max_unit_seconds`` 给出时长上限：若一次合并会让结果超过该上限，则**不做这次
    合并**（违规保留并进入报告）。这是"可播放性 vs 单元粒度"的调节旋钮。

    程序员只读闸门的判定并搬运边界；不选内容、不改文字、不新增语义。
    """
    from ai_clipper import _director_standalone_boundary_reason

    work = [list(group) for group in groups]
    # 每轮至少能消掉一个违规；留 3 倍余量容纳合并后的连锁反应。
    rounds_cap = (
        max(200, len(work) * 3 + 50) if max_rounds is None else max(1, int(max_rounds))
    )
    merge_prev = 0
    merge_next = 0
    skipped_for_length = 0
    rounds = 0

    def _span(group: Sequence[int]) -> float:
        merged = merge_rows(rows, [list(group)])
        if not merged:
            return 0.0
        return _number(merged[0].get("end")) - _number(merged[0].get("start"))

    while rounds < rounds_cap and len(work) > 1:
        rounds += 1
        merged = merge_rows(rows, work)
        changed = False
        for index in range(len(work)):
            reason = _text(_director_standalone_boundary_reason(_text(merged[index].get("text"))))
            if not reason:
                continue
            if reason == "开头承接上句" and index > 0:
                candidate_group = work[index - 1] + work[index]
                if max_unit_seconds is not None and _span(candidate_group) > float(max_unit_seconds):
                    skipped_for_length += 1
                    continue
                work[index - 1] = candidate_group
                del work[index]
                merge_prev += 1
                changed = True
                break
            if reason in ("结尾未说完", "结尾语气残留") and index < len(work) - 1:
                candidate_group = work[index] + work[index + 1]
                if max_unit_seconds is not None and _span(candidate_group) > float(max_unit_seconds):
                    skipped_for_length += 1
                    continue
                work[index] = candidate_group
                del work[index + 1]
                merge_next += 1
                changed = True
                break
        if not changed:
            break

    merged = merge_rows(rows, work)
    unresolved: list[dict[str, Any]] = []
    for index, unit in enumerate(merged):
        reason = _text(_director_standalone_boundary_reason(_text(unit.get("text"))))
        if reason:
            unresolved.append({
                "source_ids": list(work[index]),
                "reason": reason,
                "text": _text(unit.get("text"))[:120],
            })
    durations = sorted(
        (_number(u.get("end")) - _number(u.get("start")) for u in merged), reverse=True
    )
    return work, {
        "rounds": rounds,
        "merged_into_previous": merge_prev,
        "merged_with_next": merge_next,
        "skipped_for_length": skipped_for_length,
        "max_unit_seconds": max_unit_seconds,
        "remaining_violations": len(unresolved),
        "unresolved": unresolved[:24],
        "unit_count": len(merged),
        "longest_units": [round(d, 2) for d in durations[:8]],
    }


def split_long_units(
    rows: Sequence[Mapping[str, Any]],
    groups: Sequence[Sequence[int]],
    *,
    max_unit_seconds: float = SEGMENTATION_MAX_UNIT_SECONDS,
) -> tuple[list[list[int]], dict[str, Any]]:
    """在**长单元内部**找一个切点，使切开后的两半**都通过闸门**。

    纯确定性搜索，不调用 AI、不新增语义：只是把已声明的闸门规则反过来用——
    「两半都能独立播放」是切开的前提。找不到切点就**保持原样**（如实报告），
    绝不为了短而切出一个不通过的半句。切点偏好靠近中点，使两半长度均衡。
    """
    from ai_clipper import _director_standalone_boundary_reason

    work = [list(group) for group in groups]
    split_count = 0
    kept_long: list[float] = []
    scanned = 0

    def _passes(group: Sequence[int]) -> bool:
        if len(group) < 1:
            return False
        merged = merge_rows(rows, [list(group)])
        if not merged:
            return False
        return not _text(_director_standalone_boundary_reason(_text(merged[0].get("text"))))

    def _seconds(group: Sequence[int]) -> float:
        merged = merge_rows(rows, [list(group)])
        if not merged:
            return 0.0
        return _number(merged[0].get("end")) - _number(merged[0].get("start"))

    index = 0
    while index < len(work):
        group = work[index]
        duration = _seconds(group)
        if duration <= float(max_unit_seconds) or len(group) < 2:
            if duration > float(max_unit_seconds):
                kept_long.append(round(duration, 2))
            index += 1
            continue
        scanned += 1
        midpoint = len(group) / 2.0
        candidates = sorted(range(1, len(group)), key=lambda k: (abs(k - midpoint), k))
        chosen = 0
        for k in candidates:
            left, right = group[:k], group[k:]
            if _passes(left) and _passes(right):
                chosen = k
                break
        if chosen:
            work[index:index + 1] = [group[:chosen], group[chosen:]]
            split_count += 1
            continue
        kept_long.append(round(duration, 2))
        index += 1

    return work, {
        "scanned_long_units": scanned,
        "split_count": split_count,
        "unsplit_long_units": len(kept_long),
        "unsplit_durations": kept_long[:12],
        "max_unit_seconds": max_unit_seconds,
    }


def apply_deterministic_closure(
    units: Sequence[Mapping[str, Any]],
    *,
    log_fn: Callable[[str], None] | None = None,
    max_unit_seconds: float | None = None,
    min_pass_rate: float = 0.85,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """纯确定性重分段：把闸门报违规的相邻条目合并。**不调用 AI。**

    起点是每条可见条目各自成组，只在不满足播放闸门处按规则合并
    （``close_gate_violations``），可选再做长单元确定性拆分（``split_long_units``）。

    ``min_pass_rate``：**只有闭合后确实达到这个通过率才改写可见字幕**；达不到就
    原样返回，让 ASR 自己的分段留着。依据是 2026-10-07 的六素材实测：
      · SenseVoice 素材：闭合前 83% → 闭合后 100%，可以放心改写；
      · Paraformer 老素材：闭合前只有 24–28%（<1.2s 碎片 97–721 条），尽力合并也远
        达不到目标；此时把字幕改成"半合并"状态是**拿风险换不到收益**，因此不做。

    实测（立暖 JE5PYH10）：边界通过率 83% → 100%；单元 172 → 62；碎片 0；
    文字 / 词条 / 覆盖跨度均未改。

    返回 ``(units, report)``；任何异常或未达标时**原样返回输入**。
    """
    def _log(message: str) -> None:
        if log_fn:
            log_fn(message)

    report: dict[str, Any] = {
        "enabled": True, "mode": "deterministic_closure", "applied": False, "reason": "",
    }
    try:
        rows = build_unit_rows(units)
        report["before"] = audit_units([dict(r) for r in rows])
        if len(rows) < 2:
            report["reason"] = "too_few_units"
            return [dict(u) for u in units], report

        singletons = [[row["id"]] for row in rows]
        closed, closure = close_gate_violations(rows, singletons, max_unit_seconds=max_unit_seconds)
        split: dict[str, Any] = {}
        if max_unit_seconds is not None:
            closed, split = split_long_units(rows, closed, max_unit_seconds=max_unit_seconds)
        merged = merge_rows(rows, closed)

        ok, detail = _verify_no_text_change(rows, merged)
        if not ok:
            report["reason"] = f"invariant_violated:{detail}"
            _log(f"确定性重分段违反不变式（{detail}），保留原分段")
            return [dict(u) for u in units], report

        report["closure"] = closure
        report["long_unit_split"] = split
        report["after"] = audit_units(merged)
        before_rate = float(report["before"]["pass_rate"] or 0.0)
        after_rate = float(report["after"]["pass_rate"] or 0.0)
        if after_rate <= before_rate:
            report["reason"] = f"no_improvement:{after_rate:.4f}<={before_rate:.4f}"
            _log("确定性重分段未改善闸门通过率，保留原分段")
            return [dict(u) for u in units], report
        # 只有真的做到"接近完整"才改写可见字幕。差得远时改写是把用户看到的字幕
        # 换成"半合并"状态 —— 风险换不到收益（见 docstring 的六素材实测）。
        target = max(float(min_pass_rate), before_rate)
        if after_rate < target:
            report["reason"] = (
                f"insufficient_pass_rate:{after_rate:.4f}<{target:.4f}"
            )
            _log(
                f"确定性重分段只到 {after_rate:.1%}（目标 {target:.1%}），"
                "保留 ASR 原分段"
            )
            return [dict(u) for u in units], report

        report["applied"] = True
        report["reason"] = "ok" if not report["after"]["reasons"] else "applied_with_violations"
        _log(
            "确定性重分段: "
            f"{len(rows)} 条 -> {report['after']['unit_count']} 条"
            f"（闸门通过率 {before_rate:.1%} -> {after_rate:.1%}，"
            f"并前 {closure.get('merged_into_previous')}/并后 {closure.get('merged_with_next')}）"
        )
        return merged, report
    except Exception as exc:  # noqa: BLE001 - 重分段失败绝不能影响识别结果
        report["reason"] = f"failed:{type(exc).__name__}"
        _log(f"确定性重分段异常，保留原分段: {type(exc).__name__}: {exc}")
        return [dict(u) for u in units], report


def _request_groups(
    *,
    rows: Sequence[Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    response_hook: Callable[[str], None] | None = None,
    failures: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[list[list[int]] | None, str]:
    from commerce_planner_lite import _extract_json, _post_lite_request

    response = _post_lite_request(
        api_key=api_key, base_url=base_url, model=model,
        prompt=build_segmentation_prompt(rows, failures=failures),
        stage=SEGMENTATION_STAGE, max_tokens=SEGMENTATION_MAX_TOKENS,
    )
    content = _text((response.get("choices") or [{}])[0].get("message", {}).get("content"))
    if response_hook and content:
        response_hook(content)
    try:
        data = _extract_json(content) if content else None
    except (RuntimeError, ValueError, TypeError):
        data = None
    return parse_segmentation(data, len(rows))


def apply_ai_sentence_segmentation(
    units: Sequence[Mapping[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    log_fn: Callable[[str], None] | None = None,
    response_hook: Callable[[str], None] | None = None,
    max_repairs: int = SEGMENTATION_MAX_REPAIRS,
    close_violations: bool = True,
    split_long: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """执行 AI 语义重分段（含最多 ``max_repairs`` 次带违规反馈的修正调用）。

    返回 ``(units, report)``。任何一步不满足不变式时**原样返回输入**，
    并在 report 里写明原因——绝不静默产出改写过或丢内容的字幕。

    注意：2026-10-07 同素材实测显示，AI 分组会让下游可用候选从 48 降到 30 并
    触发 ``candidate_reused_by_plan``；**优先使用 ``apply_deterministic_closure``**。
    """
    def _log(message: str) -> None:
        if log_fn:
            log_fn(message)

    rows = build_unit_rows(units)
    report: dict[str, Any] = {
        "enabled": True,
        "mode": "ai_segmentation",
        "stage": SEGMENTATION_STAGE,
        "input_units": len(rows),
        "before": audit_units([dict(r) for r in rows]),
        "attempts": [],
        "applied": False,
        "reason": "",
    }
    if len(rows) < 2:
        report["reason"] = "too_few_units"
        return [dict(u) for u in units], report

    best: dict[str, Any] | None = None
    failures: list[dict[str, Any]] | None = None
    for attempt in range(1, max(0, int(max_repairs)) + 2):
        try:
            groups, reason = _request_groups(
                rows=rows, api_key=api_key, base_url=base_url, model=model,
                response_hook=response_hook, failures=failures,
            )
        except Exception as exc:  # noqa: BLE001 - 任何失败都必须回退
            report["attempts"].append({"attempt": attempt, "reason": f"request_failed:{type(exc).__name__}"})
            _log(f"AI 语义重分段不可用，保留原分段：{exc}")
            break

        if groups is None:
            report["attempts"].append({"attempt": attempt, "reason": f"invalid_output:{reason}"})
            _log(f"AI 语义重分段输出非法（{reason}）")
            failures = None
            continue

        merged = merge_rows(rows, groups)
        ok, detail = _verify_no_text_change(rows, merged)
        if not ok:
            report["attempts"].append({"attempt": attempt, "reason": f"invariant_violated:{detail}"})
            _log(f"AI 语义重分段违反不变式（{detail}）")
            failures = None
            continue

        audit = audit_units(merged)
        group_failures = _group_failures(merged, groups)
        report["attempts"].append({
            "attempt": attempt,
            "groups": len(groups),
            "merged_groups": sum(1 for g in groups if len(g) > 1),
            "audit": audit,
            "violation_count": len(group_failures),
        })
        if best is None or audit["pass_rate"] > best["audit"]["pass_rate"]:
            best = {
                "merged": merged, "audit": audit, "attempt": attempt,
                "groups": len(groups), "groups_list": groups,
            }
        if not group_failures:
            _log(f"AI 语义重分段第 {attempt} 次尝试即全部通过校验")
            break
        failures = group_failures
        _log(
            f"AI 语义重分段第 {attempt} 次尝试：仍有 {len(group_failures)} 组不通过，"
            f"通过率 {audit['pass_rate']:.1%}"
        )

    before_rate = float(report["before"]["pass_rate"] or 0.0)
    if best is None:
        report["reason"] = report["attempts"][-1]["reason"] if report["attempts"] else "no_attempt"
        return [dict(u) for u in units], report

    if close_violations:
        closed_groups, closure = close_gate_violations(rows, best["groups_list"])
        report["closure"] = closure
        closed_merged = merge_rows(rows, closed_groups)
        closed_ok, closed_detail = _verify_no_text_change(rows, closed_merged)
        if closed_ok:
            closed_audit = audit_units(closed_merged)
            report["closure"]["audit"] = closed_audit
            if closed_audit["pass_rate"] > best["audit"]["pass_rate"]:
                _log(
                    "AI 语义重分段 + 机械闭合: "
                    f"并前 {closure['merged_into_previous']} 处 / 并后 {closure['merged_with_next']} 处，"
                    f"通过率 {best['audit']['pass_rate']:.1%} -> {closed_audit['pass_rate']:.1%}"
                )
                best = {
                    "merged": closed_merged, "audit": closed_audit,
                    "attempt": best["attempt"], "groups": len(closed_groups),
                    "groups_list": closed_groups,
                }
        else:
            report["closure"]["skipped"] = closed_detail

    if split_long:
        split_groups, split_report = split_long_units(
            rows, best["groups_list"], max_unit_seconds=SEGMENTATION_MAX_UNIT_SECONDS,
        )
        report["long_unit_split"] = split_report
        if split_report.get("split_count"):
            split_merged = merge_rows(rows, split_groups)
            split_ok, split_detail = _verify_no_text_change(rows, split_merged)
            if split_ok:
                split_audit = audit_units(split_merged)
                report["long_unit_split"]["audit"] = split_audit
                if split_audit["pass_rate"] >= best["audit"]["pass_rate"]:
                    _log(
                        "长单元拆分: "
                        f"切开 {split_report['split_count']} 个，"
                        f"单元 {best['audit']['unit_count']} -> {split_audit['unit_count']}，"
                        f"通过率 {split_audit['pass_rate']:.1%}"
                    )
                    best = {
                        "merged": split_merged, "audit": split_audit,
                        "attempt": best["attempt"], "groups": len(split_groups),
                        "groups_list": split_groups,
                    }
            else:
                report["long_unit_split"]["skipped"] = split_detail

    if best["audit"]["pass_rate"] <= before_rate:
        report["reason"] = f"no_improvement:{best['audit']['pass_rate']:.4f}<={before_rate:.4f}"
        _log("AI 语义重分段未改善闸门通过率，保留原分段")
        return [dict(u) for u in units], report

    report["after"] = best["audit"]
    report["applied"] = True
    report["reason"] = "ok" if not best["audit"]["reasons"] else "applied_with_violations"
    report["best_attempt"] = best["attempt"]
    report["groups"] = best["groups"]
    _log(
        "AI 语义重分段: "
        f"{len(rows)} 条 -> {best['audit']['unit_count']} 条"
        f"（闸门通过率 {before_rate:.1%} -> {best['audit']['pass_rate']:.1%}）"
    )
    return best["merged"], report
