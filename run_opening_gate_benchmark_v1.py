# -*- coding: utf-8 -*-
"""Evaluate a source-only Opening Gate against confirmed benchmark labels.

The gate makes one narrow decision for one frozen Opening Unit: publishable or
not.  It never chooses an alternative, requests a replan, edits text, or
touches M1/M2/M3/user-facing routes.  Its purpose is to measure the rejection
boundary before any future source-only integration is considered.
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
from run_opening_benchmark_v1 import DEFAULT_DATASET, _extract_json, load_dataset  # noqa: E402
from ssl_context import create_ssl_context  # noqa: E402


VERSION = "opening-gate-benchmark-v1"
REASON_CODES = ("context_dependent", "live_process_talk", "generic_claim", "weak_purchase_value", "payoff_not_self_contained")
SYSTEM_PROMPT = """你是商业短视频 Opening Gate，只负责发布门槛判断，不是导演或文案生成器。
面对单个固定 Opening Unit，只返回 publishable=true 或 false。若陌生用户在第一秒不能独立理解为什么继续看，
或 Hook/Payoff 不是完整的购买承诺与即时兑现，就应拒绝。宁可拒绝，也不要因为有卖点词就放行。
不得改写、补充、排序或推荐另一个候选。返回 JSON。"""


def build_gate_prompt(case: Mapping[str, Any], candidate: Mapping[str, Any]) -> str:
    return "\n".join((
        "商业故事上下文：" + str(case.get("context") or ""),
        "固定 Opening Unit：" + str(candidate.get("text") or ""),
        "只做发布判断，不生成替代文本。",
        '{"publishable":true|false,"reason_codes":["context_dependent"|"live_process_talk"|"generic_claim"|"weak_purchase_value"|"payoff_not_self_contained"],'
        '"confidence":0.0,"reason":"一句中文理由"}',
    ))


def judge_opening(*, case: Mapping[str, Any], candidate: Mapping[str, Any], api_key: str, base_url: str, model: str) -> tuple[dict[str, Any], str]:
    prompt = build_gate_prompt(case, candidate)
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
        "temperature": 0.0,
        "top_p": 0.8,
        "max_tokens": 500,
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
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        record_ai_call(module="opening_gate", stage="opening_gate_benchmark", model=model,
                       request_payload=body, success=False, error_type=f"http_{error.code}")
        raise RuntimeError(f"Opening Gate HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(module="opening_gate", stage="opening_gate_benchmark", model=model,
                       request_payload=body, success=False, error_type=type(error).__name__)
        raise RuntimeError(f"Opening Gate network error: {error}") from error
    record_ai_call(module="opening_gate", stage="opening_gate_benchmark", model=model,
                   request_payload=body, response_payload=payload, success=True)
    raw = str(payload.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
    return _extract_json(raw), raw


def evaluate_gate(candidate: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, Any]:
    expected_publishable = str(candidate.get("decision") or "") == "keep"
    actual_publishable = bool(answer.get("publishable"))
    expected_reasons = set(str(item) for item in candidate.get("failure_reason", []) if str(item))
    actual_reasons = set(str(item) for item in answer.get("reason_codes", []) if str(item) in REASON_CODES)
    return {
        "candidate_id": candidate.get("candidate_id"),
        "gold": bool(candidate.get("gold")),
        "expected_publishable": expected_publishable,
        "actual_publishable": actual_publishable,
        "decision_correct": expected_publishable == actual_publishable,
        "false_publish": not expected_publishable and actual_publishable,
        "expected_reason_codes": sorted(expected_reasons),
        "actual_reason_codes": sorted(actual_reasons),
        "reason_overlap": sorted(expected_reasons.intersection(actual_reasons)),
        "answer": dict(answer),
    }


def summarize(results: list[Mapping[str, Any]]) -> dict[str, Any]:
    rejects = [item for item in results if not item.get("expected_publishable")]
    keeps = [item for item in results if item.get("expected_publishable")]
    total = len(results)
    return {
        "unit_count": total,
        "decision_accuracy": round(sum(bool(item.get("decision_correct")) for item in results) / total, 4) if total else None,
        "reject_recall": round(sum(not bool(item.get("actual_publishable")) for item in rejects) / len(rejects), 4) if rejects else None,
        "false_publish_rate": round(sum(bool(item.get("false_publish")) for item in rejects) / len(rejects), 4) if rejects else None,
        "keep_recall": round(sum(bool(item.get("actual_publishable")) for item in keeps) / len(keeps), 4) if keeps else None,
        "false_publish_candidates": [item.get("candidate_id") for item in results if item.get("false_publish")],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run source-only Opening Gate benchmark v1.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out-dir", default="workspace/opening_gate_benchmark_v1")
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    dataset = load_dataset(Path(args.dataset).resolve())
    if dataset.get("label_status") != "human_confirmed_v1":
        raise RuntimeError("Opening Gate benchmark requires human_confirmed_v1 labels.")
    candidates = [candidate for case in dataset["cases"] for candidate in case.get("candidates", []) if candidate.get("gold")]
    if not candidates:
        raise RuntimeError("Opening Gate benchmark has no gold candidates.")
    from ai_clipper import load_settings  # noqa: E402
    settings = load_settings()
    if not str(settings.get("api_key") or "").strip():
        raise RuntimeError("未找到 AI API Key，不能运行 Opening Gate benchmark。")
    run_id = str(args.run_id or "").strip() or dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = (ROOT / args.out_dir / run_id).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    with ai_cost_ledger_scope(task_id=f"opening_gate_benchmark:{run_id}", session_id=run_id):
        for case in dataset["cases"]:
            for candidate in case.get("candidates", []):
                if not candidate.get("gold"):
                    continue
                answer, raw = judge_opening(
                    case=case, candidate=candidate, api_key=str(settings["api_key"]),
                    base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                    model=str(settings.get("model") or "deepseek-v4-flash"),
                )
                (out_dir / f"{candidate['candidate_id']}.response.txt").write_text(raw, encoding="utf-8")
                results.append(evaluate_gate(candidate, answer))
    report, cache = generate_ai_cost_reports(session_id=run_id, task_id=f"opening_gate_benchmark:{run_id}")
    payload = {
        "version": VERSION,
        "mode": "source_only_opening_gate_evaluation_no_m123_integration",
        "formal_paths_changed": False,
        "run_id": run_id,
        "dataset": str(Path(args.dataset).resolve()),
        "metrics": summarize(results),
        "results": results,
    }
    (out_dir / "gate_benchmark_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "cost_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "cache_candidate_report.json").write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Opening Gate Benchmark] report={out_dir / 'gate_benchmark_report.json'}")


if __name__ == "__main__":
    main()
