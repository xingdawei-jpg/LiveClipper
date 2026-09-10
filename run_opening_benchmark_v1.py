# -*- coding: utf-8 -*-
"""Source-only Opening Benchmark v1.

This is an offline judge evaluation, not an M1/M2/M3 production component.
It presents fixed A/B opening units, lets a model select one or explicitly
reject all, then compares the answer with the human-curated seed labels.
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
from ssl_context import create_ssl_context  # noqa: E402


VERSION = "opening-benchmark-v1"
DEFAULT_DATASET = ROOT / "workspace" / "opening_benchmark_v1" / "opening_benchmark_v1.json"

SYSTEM_PROMPT = """你是商业短视频 Opening 的离线评测员，不是剪辑师，也不生成新文案。
你只能在给定 Opening Unit 中选择一个最适合发布的开场，或明确拒绝全部。
判断聚焦：独立可理解、具体购买价值或矛盾、立即可兑现、自然且没有直播过程感。
不要把“听起来像卖点”误判为商业开场；没有合格项时必须拒绝全部。
返回 JSON，不要解释性散文。"""


def load_dataset(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("Opening benchmark dataset must contain cases")
    return payload


def build_prompt(case: Mapping[str, Any]) -> str:
    candidates = [
        {"candidate_id": item.get("candidate_id"), "opening_unit": item.get("text")}
        for item in case.get("candidates", []) if isinstance(item, Mapping)
    ]
    return "\n".join((
        "任务：在固定候选中做选择，绝不生成、改写、拼接任何新句子。",
        "商业故事上下文：" + str(case.get("context") or ""),
        "候选 Opening Unit：",
        json.dumps(candidates, ensure_ascii=False, indent=2),
        "返回：",
        '{"verdict":"select"|"no_publishable_opening_found", "selected_candidate_id":"候选ID或空字符串", '
        '"candidate_decisions":[{"candidate_id":"...","decision":"keep"|"reject",'
        '"failure_reasons":["context_dependent"|"live_process_talk"|"generic_claim"|"weak_purchase_value"|"payoff_not_self_contained"]}],'
        '"reason":"一句中文理由"}',
    ))


def _extract_json(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    try:
        value = json.loads(raw)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        value = json.loads(raw[start:end + 1])
        if isinstance(value, dict):
            return value
    raise ValueError("benchmark judge did not return a JSON object")


def judge_case(*, case: Mapping[str, Any], api_key: str, base_url: str, model: str) -> tuple[dict[str, Any], str]:
    prompt = build_prompt(case)
    body = {
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
        record_ai_call(module="opening_benchmark", stage="opening_benchmark_judge", model=model,
                       request_payload=body, success=False, error_type=f"http_{error.code}")
        raise RuntimeError(f"Opening benchmark HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        record_ai_call(module="opening_benchmark", stage="opening_benchmark_judge", model=model,
                       request_payload=body, success=False, error_type=type(error).__name__)
        raise RuntimeError(f"Opening benchmark network error: {error}") from error
    record_ai_call(module="opening_benchmark", stage="opening_benchmark_judge", model=model,
                   request_payload=body, response_payload=response_payload, success=True)
    content = str(response_payload.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
    return _extract_json(content), content


def evaluate_case(case: Mapping[str, Any], answer: Mapping[str, Any]) -> dict[str, Any]:
    expected_verdict = str(case.get("expected_verdict") or "")
    actual_verdict = str(answer.get("verdict") or "")
    expected_choice = str(case.get("expected_candidate_id") or "")
    actual_choice = str(answer.get("selected_candidate_id") or "")
    expected_by_id = {
        str(item.get("candidate_id")): str(item.get("decision"))
        for item in case.get("candidates", []) if isinstance(item, Mapping)
    }
    actual_by_id = {
        str(item.get("candidate_id")): str(item.get("decision"))
        for item in answer.get("candidate_decisions", []) if isinstance(item, Mapping)
    }
    labeled = len(expected_by_id)
    correct_labels = sum(1 for key, decision in expected_by_id.items() if actual_by_id.get(key) == decision)
    return {
        "case_id": case.get("case_id"),
        "expected_verdict": expected_verdict,
        "actual_verdict": actual_verdict,
        "verdict_correct": actual_verdict == expected_verdict,
        "expected_candidate_id": expected_choice,
        "actual_candidate_id": actual_choice,
        "top1_correct": expected_verdict == "select" and actual_choice == expected_choice,
        "no_publishable_correct": expected_verdict == "no_publishable_opening_found" and actual_verdict == expected_verdict,
        "unit_labels": {"correct": correct_labels, "total": labeled},
        "expected_candidate_decisions": expected_by_id,
        "answer": dict(answer),
    }


def summarize(results: list[Mapping[str, Any]]) -> dict[str, Any]:
    selection = [item for item in results if item.get("expected_verdict") == "select"]
    rejection = [item for item in results if item.get("expected_verdict") == "no_publishable_opening_found"]
    total_labels = sum(int((item.get("unit_labels") or {}).get("total") or 0) for item in results)
    correct_labels = sum(int((item.get("unit_labels") or {}).get("correct") or 0) for item in results)
    selected_rejects = 0
    selected_total = 0
    for item in results:
        answer = item.get("answer") or {}
        selected_id = str(answer.get("selected_candidate_id") or "")
        if str(answer.get("verdict") or "") == "select":
            selected_total += 1
            expected_decisions = item.get("expected_candidate_decisions") or {}
            if expected_decisions.get(selected_id) == "reject":
                selected_rejects += 1
    return {
        "case_count": len(results),
        "unit_label_accuracy": round(correct_labels / total_labels, 4) if total_labels else None,
        "top1_choice_accuracy": round(sum(bool(item.get("top1_correct")) for item in selection) / len(selection), 4) if selection else None,
        "no_publishable_recall": round(sum(bool(item.get("no_publishable_correct")) for item in rejection) / len(rejection), 4) if rejection else None,
        "selected_reject_rate": round(selected_rejects / selected_total, 4) if selected_total else 0.0,
        "selected_reject_count": selected_rejects,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run source-only Opening Benchmark v1.")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--out-dir", default="workspace/opening_benchmark_v1")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--reuse-run-dir", default="", help="Re-score saved response files only; never calls AI.")
    args = parser.parse_args()
    dataset = load_dataset(Path(args.dataset).resolve())
    reuse_dir = Path(args.reuse_run_dir).resolve() if str(args.reuse_run_dir or "").strip() else None
    run_id = str(args.run_id or "").strip() or (reuse_dir.name if reuse_dir else dt.datetime.now().strftime("%Y%m%d_%H%M%S"))
    out_dir = reuse_dir or (ROOT / args.out_dir / run_id).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    if reuse_dir:
        for case in dataset["cases"]:
            raw = (out_dir / f"{case['case_id']}.response.txt").read_text(encoding="utf-8")
            results.append(evaluate_case(case, _extract_json(raw)))
    else:
        from ai_clipper import load_settings  # noqa: E402
        settings = load_settings()
        if not str(settings.get("api_key") or "").strip():
            raise RuntimeError("未找到 AI API Key，不能运行 Opening Benchmark。")
        with ai_cost_ledger_scope(task_id=f"opening_benchmark:{run_id}", session_id=run_id):
            for case in dataset["cases"]:
                answer, raw = judge_case(
                    case=case, api_key=str(settings["api_key"]),
                    base_url=str(settings.get("base_url") or "https://api.deepseek.com"),
                    model=str(settings.get("model") or "deepseek-v4-flash"),
                )
                (out_dir / f"{case['case_id']}.response.txt").write_text(raw, encoding="utf-8")
                results.append(evaluate_case(case, answer))
    report, cache = generate_ai_cost_reports(session_id=run_id, task_id=f"opening_benchmark:{run_id}")
    payload = {
        "version": VERSION,
        "mode": "offline_opening_judgement_no_production_logic",
        "formal_paths_changed": False,
        "run_id": run_id,
        "dataset": str(Path(args.dataset).resolve()),
        "reused_saved_responses": bool(reuse_dir),
        "metrics": summarize(results),
        "cases": results,
    }
    (out_dir / "benchmark_report.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "cost_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "cache_candidate_report.json").write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[Opening Benchmark] report={out_dir / 'benchmark_report.json'}")


if __name__ == "__main__":
    main()
