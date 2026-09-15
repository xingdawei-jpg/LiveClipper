"""把已解析字幕适配成 P0.5A.4 独立 Hook 召回所需的输入。

背景：线上链路（commercial_analyzer.analyze_commercial_story）只拿得到已经
解析好的 ``subtitles``（id / text / start / end），拿不到 SRT 文件路径，也没有
词级时间轴。而 ``hook_opening_recall`` 需要一个带词级血缘的 source_rows。

本模块只做两件事：

1. ``build_source_rows_from_subtitles``：把字幕补成 hook 召回能用的行结构
   （``word_lineage`` / ``word_tokens`` / ``hard_safe`` / ``materializable``）。
2. ``recall_opening_hooks``：跑一次**独立于正文池**的全文 Hook 召回。

设计取舍：默认「一条字幕 = 一个词元」（``word_level=False``）。
这样 ``micro_beat_inventory._boundary_segment_source`` 能精确回放原文
（``_punctuation_after_words`` 只要求词元拼接等于原文），
Hook 因此只能落在**完整字幕边界**上 —— 宁可粗，也绝不伪造词边界把不连续的
字拼成一个看起来成立的开场。

这一层不改变任何既有判定：它只负责「找」，不做「选」。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Sequence


ADAPTER_VERSION = "opening-hook-recall-adapter-v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _empty(status: str, error: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": status,
        "hook_candidates": [],
        "hook_candidate_count": 0,
        "pool_ids": [],
        "adapter_version": ADAPTER_VERSION,
        "word_level": False,
    }
    if error:
        result["error"] = error
    return result


def build_source_rows_from_subtitles(
    subtitles: Sequence[Mapping[str, Any]],
    *,
    executable_subtitle_ids: Sequence[int] | None = None,
) -> list[dict[str, Any]]:
    """把 subtitles 适配成 hook 召回可用的 source_rows（一条字幕 = 一个词元）。"""
    allowed: set[int] | None = None
    if executable_subtitle_ids is not None:
        allowed = set()
        for value in executable_subtitle_ids:
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                continue
            if candidate > 0:
                allowed.add(candidate)

    rows: list[dict[str, Any]] = []
    word_cursor = 0
    position = 0
    for item in subtitles or ():
        if not isinstance(item, Mapping):
            continue
        position += 1
        text = _text(item.get("text"))
        if not text:
            continue
        try:
            subtitle_id = int(item.get("id") or item.get("index") or position)
        except (TypeError, ValueError):
            continue
        if allowed is not None and subtitle_id not in allowed:
            continue
        start = _number(item.get("start"))
        end = _number(item.get("end"), start)
        if end <= start:
            end = start + 0.5
        rows.append({
            "subtitle_id": subtitle_id,
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
            "hard_safe": True,
            "materializable": True,
            "word_lineage": {"word_start_index": word_cursor, "word_end_index": word_cursor},
            "word_tokens": [{
                "offset": 0,
                "text": text,
                "start": round(start, 3),
                "end": round(end, 3),
            }],
        })
        word_cursor += 1
    return rows


def recall_opening_hooks(
    *,
    subtitles: Sequence[Mapping[str, Any]],
    api_key: str,
    base_url: str,
    model: str,
    opening_promise: str = "",
    allowed_opening_answer_roles: Sequence[str] = (),
    executable_subtitle_ids: Sequence[int] | None = None,
    response_hook: Callable[[str, str], None] | None = None,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """执行一次独立的全源 Hook 召回。

    这是「找」的环节，独立于正文 Beat 池。任何异常都不会抛出，
    只返回 ``status=hook_recall_failed``，保证不阻断导演主流程。
    """
    def _log(message: str) -> None:
        if log_fn is None:
            return
        try:
            log_fn(message)
        except Exception:
            pass

    try:
        rows = build_source_rows_from_subtitles(
            subtitles, executable_subtitle_ids=executable_subtitle_ids,
        )
        if not rows:
            return _empty("hook_material_limited", "no_source_rows")
        # 惰性导入：默认关闭时不给导演链路增加任何导入开销
        from hook_opening_recall import (
            build_hook_recall_batches,
            recall_hooks_from_complete_source,
        )
        # 单次调用模式（默认）：整片 SRT 一个窗口，和 M1/M2 一样。
        # 用 LIVECLIPPER_HOOK_RECALL_SINGLE=0 回退到分窗。
        single_call = str(os.environ.get("LIVECLIPPER_HOOK_RECALL_SINGLE", "1")).strip() != "0"
        call_kwargs: dict[str, Any] = {}
        if single_call:
            call_kwargs["batch_seconds"] = 10 ** 9
            call_kwargs["max_tokens"] = min(32000, max(8192, 2000 + 18 * len(rows)))
            _log(
                f"独立 Hook 召回：单次调用模式，{len(rows)} 条字幕一次送入，"
                f"max_tokens={call_kwargs['max_tokens']}"
            )
        else:
            try:
                from hook_opening_recall import build_hook_recall_batches
                window_count = len(build_hook_recall_batches(rows))
            except Exception:
                window_count = -1
            _log(f"独立 Hook 召回：分窗模式，{len(rows)} 条字幕 → {window_count} 个来源窗口")
        result = recall_hooks_from_complete_source(
            source_rows=rows,
            api_key=api_key,
            base_url=base_url,
            model=model,
            opening_promise=_text(opening_promise),
            allowed_opening_answer_roles=tuple(allowed_opening_answer_roles or ()),
            response_hook=response_hook,
            **call_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - 召回失败绝不允许打断主流程
        detail = f"{type(exc).__name__}: {exc}"[:300]
        _log(f"独立 Hook 召回失败（已降级，不阻断）：{detail}")
        return _empty("hook_recall_failed", detail)

    result = dict(result or {})
    candidates = list(result.get("hook_candidates") or ())
    result["hook_candidates"] = candidates
    result["hook_candidate_count"] = len(candidates)
    pool: list[int] = []
    for candidate in candidates:
        for value in candidate.get("source_subtitle_ids") or ():
            try:
                pool.append(int(value))
            except (TypeError, ValueError):
                continue
    result["pool_ids"] = sorted(set(pool))
    result["adapter_version"] = ADAPTER_VERSION
    result["word_level"] = False
    _log(
        f"独立 Hook 召回完成：status={result.get('status')}，"
        f"候选 {len(candidates)} 条，覆盖字幕 {len(result['pool_ids'])} 条"
    )
    return result


def hook_pool_prompt_rows(
    hook_recall: Mapping[str, Any] | None,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """把召回结果压成给 story prompt 用的紧凑清单。"""
    rows: list[dict[str, Any]] = []
    candidates = list((hook_recall or {}).get("hook_candidates") or ())[:max(1, int(limit))]
    for candidate in candidates:
        subtitle_ids: list[int] = []
        for value in candidate.get("source_subtitle_ids") or ():
            try:
                subtitle_ids.append(int(value))
            except (TypeError, ValueError):
                continue
        rows.append({
            "hook_id": _text(candidate.get("hook_id")),
            "subtitle_ids": subtitle_ids,
            "start": candidate.get("start"),
            "end": candidate.get("end"),
            "duration": candidate.get("duration"),
            "text": _text(candidate.get("text")),
            "hook_type": _text(candidate.get("hook_type")),
            "core_purchase_value": _text(candidate.get("core_purchase_value")),
            "stop_reason": _text(candidate.get("stop_reason")),
            "hook_strength": candidate.get("hook_strength"),
        })
    return rows
