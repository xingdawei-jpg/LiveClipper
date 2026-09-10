# -*- coding: utf-8 -*-
"""Source-only Opening Selector benchmark v2.

This is an offline capability measurement.  It neither changes M1/M2/M3 nor
creates a publishable clip.  Unlike Gate v1, the model first ranks fixed
alternatives and may reject only when every presented alternative is weak.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
APP = ROOT / "app"
for _path in (str(ROOT), str(APP)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_cost_ledger import ai_cost_ledger_scope, generate_ai_cost_reports, record_ai_call  # noqa: E402
from ai_model_config import ai_chat_completions_url  # noqa: E402
from run_opening_benchmark_v1 import _extract_json  # noqa: E402
from ssl_context import create_ssl_context  # noqa: E402


VERSION = "opening-selector-benchmark-v2"
DEFAULT_DATASET = ROOT / "workspace" / "opening_benchmark_v2" / "opening_benchmark_v2.json"

SYSTEM_PROMPT = """你是商业短视频 Opening Selector 的离线评测员。
你不改写、不拼接、不生成新文案；只能在固定候选中排序和选择。

先比较候选的商业开场价值：具体购买结果/痛点反差/设计解决/场景收益，优先于直播过程话、泛化评价和依赖前文的残句。随后仅当所有候选都不适合发布时，返回 no_publishable_opening_found。不要因为风险规避而默认拒绝全部，也不要为了必须选一个而放行差候选。

只返回 JSON，不要返回散文。"""


def load_dataset(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("Opening Selector benchmark dataset must contain cases")
    return payload


def _candidate_id(item: Mapping[str, Any]) -> str:
    return str(item.get("id") or item.get("candidate_id") or "")


def build_prompt(case: Mapping[str, Any]) -> str:
    task = str(case.get("task") or "")
    candidates = [
        {"candidate_id": _candidate_id(item), "opening_unit": item.get("text")}
        for item in case.get("candidates", [])
        if isinstance(item, Mapping)
    ]
    task_instruction = {
        "pair_choice": "这是二选一：比较两个候选，选择更适合商业视频开场的一个；只有两个都不合格才拒绝。",
        "rank_three": "这是三选排序：把三个候选从最适合到最不适合商业视频开场完整排序；只有三个都不合格才拒绝。",
        "reject_all": "这是拒绝压力测试：只有确认三个候选都不适合商业视频开场时才拒绝；如确有合格候选，仍须选择并完整排序。",
    }.get(task, "比较固定候选，先排序，再决定是否选择。")
    return "\n".join((
        "任务：" + task_instruction,
        "商业故事上下文：" + str(case.get("context") or ""),
        "候选 Opening Unit：",
        json.dumps(candidates, ensure_ascii=False, indent=2),
        "严格输出：",
        '{"verdict":"select"|"no_publishable_opening_found",'
        '"selected_candidate_id":"选择时为候选ID，否则为空字符串",'
        '"ranking":["候选ID，按强到弱完整排列；拒绝时为空数组"],'
        '"confidence":0.0,"reason":"一句中文理由"}',
    ))


def judge_case(*, case: Mapping[str, Any], api_key: str, base_url: str, model: str) -> tuple[dict[str, Any], str]:
    prompt = build_prompt(case)
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        "temperature": 0.0,
        "top_p": 0.8,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    if "deepseek" in model.lower() and "seed" not in model.lower():
        body["thinking"] = {"type": "disabled"}
    request = urllib.request.Request(
        ai_chat_completions_url(base_url), data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120, context=create_ssl_context()) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        record_ai_call(module="opening_selector_benchmark", stage="opening_selector_benchmark_v2", model=model,
                       request_payload=body, success=False, error_type=f"http_{error.code}")
        raise RuntimeError(f"Opening Selector benchmark HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(module="opening_selector_benchmark", stage="opening_selector_benchmark_v2", model=model,
                       request_payload=body, success=False, error_type=type(error).__name__)
        raise RuntimeError(f"Opening Selector benchmark network error: {error}") from error
    record_ai_call(module="opening_selector_benchmark", stage="opening_selector_benchmark_v2", model=model,
                   request_payload=body, response_payload=response_payload, success=True)
    content = str(response_payload.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
    return _extract_json(content), content


def _ranking_pairs(order: list[str]) -> set[tuple[str, str]]:
    return {(higher, lower) for index, higher in enumerate(order) for lower in order[index + 1:]}


def evaluate_case(case: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, Any]:
    expected_verdict = str(case.get("expected_verdict") or "")
    expected_order = [str(item) for item in case.get("expected_order", [])]
    candidate_ids = [_candidate_id(item) for item in case.get("candidates", []) if isinstance(item, Mapping)]
    actual_verdict = str(answer.get("verdict") or "")
    selected = str(answer.get("selected_candidate_id") or "")
    ranking = [str(item) for item in answer.get("ranking", []) if str(item)]
    ranking_valid = set(ranking) == set(candidate_ids) and len(ranking) == len(candidate_ids)
    expected_pairs = _ranking_pairs(expected_order)
    actual_pairs = _ranking_pairs(ranking) if ranking_valid else set()
    labels = [str(item.get("label") or "") for item in case.get("candidates", []) if isinstance(item, Mapping)]
    return {
        "case_id": str(case.get("case_id") or ""),
        "task": str(case.get("task") or ""),
        "expected_verdict": expected_verdict,
        "actual_verdict": actual_verdict,
        "verdict_correct": actual_verdict == expected_verdict,
        "expected_order": expected_order,
        "actual_ranking": ranking,
        "ranking_valid": ranking_valid,
        "top1_correct": expected_verdict == "select" and ranking_valid and ranking[0] == expected_order[0] and selected == expected_order[0],
        "ranking_pair_accuracy": round(len(expected_pairs & actual_pairs) / len(expected_pairs), 4) if expected_pairs else None,
        "no_publishable_correct": expected_verdict == "no_publishable_opening_found" and actual_verdict == expected_verdict,
        "selected_reject": expected_verdict == "no_publishable_opening_found" and actual_verdict == "select",
        "labels": labels,
        "all_candidates_v1_gold": bool(labels) and all(label == "v1_gold" for label in labels),
        "answer": dict(answer),
    }


def summarize(results: list[Mapping[str, Any]]) -> dict[str, Any]:
    selecting = [item for item in results if item.get("expected_verdict") == "select"]
    rejecting = [item for item in results if item.get("expected_verdict") == "no_publishable_opening_found"]
    pair_cases = [item for item in selecting if item.get("task") == "pair_choice"]
    rank_cases = [item for item in selecting if item.get("task") == "rank_three"]
    selected_rejects = [str(item.get("case_id")) for item in results if item.get("selected_reject")]
    return {
        "case_count": len(results),
        "selection_case_count": len(selecting),
        "pair_top1_accuracy": round(sum(bool(item.get("top1_correct")) for item in pair_cases) / len(pair_cases), 4) if pair_cases else None,
        "rank_top1_accuracy": round(sum(bool(item.get("top1_correct")) for item in rank_cases) / len(rank_cases), 4) if rank_cases else None,
        "ranking_pair_accuracy": round(sum(float(item.get("ranking_pair_accuracy") or 0) for item in selecting) / len(selecting), 4) if selecting else None,
        "selection_recall": round(sum(item.get("actual_verdict") == "select" for item in selecting) / len(selecting), 4) if selecting else None,
        "reject_recall": round(sum(bool(item.get("no_publishable_correct")) for item in rejecting) / len(rejecting), 4) if rejecting else None,
        "false_publish_rate": round(len(selected_rejects) / len(rejecting), 4) if rejecting else None,
        "false_publish_cases": selected_rejects,
    }


def _scope_summary(results: list[Mapping[str, Any]]) -> dict[str, Any]:
    confirmed = [item for item in results if item.get("all_candidates_v1_gold")]
    return {
        "all_labeled_cases": summarize(results),
        "confirmed_v1_gold_only": summarize(confirmed),
        "confirmed_v1_gold_case_count": len(confirmed),
        "provisional_or_mixed_case_count": len(results) - len(confirmed),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run source-only Opening Selector Benchmark v2.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out-dir", default="workspace/opening_benchmark_v2")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--allow-provisional", action="store_true", help="Required because v2 contains clearly marked provisional labels.")
    args = parser.parse_args()
    dataset_path = Path(args.dataset).resolve()
    dataset = load_dataset(dataset_path)
    label_status = str(dataset.get("label_status") or "")
    if "provisional" in label_status and not args.allow_provisional:
        raise RuntimeError("v2 includes provisional labels; rerun only with --allow-provisional after acknowledging the report is exploratory.")
    run_id = str(args.run_id or "").strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = (ROOT / args.out_dir / run_id).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    from ai_clipper import load_settings  # noqa: E402
    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("AI API Key is required to run the source-only benchmark.")
    results: list[dict[str, Any]] = []
    task_id = f"opening_selector_benchmark_v2:{run_id}"
    with ai_cost_ledger_scope(task_id=task_id, session_id=run_id):
        for case in dataset["cases"]:
            answer, raw = judge_case(
                case=case, api_key=str(settings["api_key"]),
                base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                model=str(settings.get("model") or "deepseek-v4-flash"),
            )
            (out_dir / f"{case['case_id']}.response.txt").write_text(raw, encoding="utf-8")
            results.append(evaluate_case(case, answer))
    report, cache = generate_ai_cost_reports(session_id=run_id, task_id=task_id)
    payload = {
        "version": VERSION,
        "mode": "offline_opening_selector_ranking_only_no_m123_or_shadow",
        "formal_paths_changed": False,
        "run_id": run_id,
        "dataset": str(dataset_path),
        "dataset_label_status": label_status,
        "provisional_labels_acknowledged": bool(args.allow_provisional),
        "metrics": _scope_summary(results),
        "cases": results,
    }
    (out_dir / "benchmark_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "cost_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "cache_candidate_report.json").write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Opening Selector Benchmark v2] report={out_dir / 'benchmark_report.json'}")


if __name__ == "__main__":
    main()
