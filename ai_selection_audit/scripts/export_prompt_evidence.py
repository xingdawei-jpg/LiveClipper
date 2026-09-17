"""Export prompt-builder source and non-network reconstructions for audit only."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "ai_selection_audit"
APP = REPO / "app"
sys.path.insert(0, str(APP))


TARGETS = {
    "content_review.py": ["_review_prompts", "_hook_pair_repair_prompts", "review_final_sequence", "_post_review_request"],
    "marketing_intent.py": ["marketing_intent_prompt_contract"],
    "ai_clipper.py": ["_call_ai", "_layered_selection_prompt_contract", "_call_director_trim_selection", "_call_director_incremental_fill", "_call_ai_opening_repair"],
}


def function_source(path: Path, names: list[str]) -> str:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    tree = ast.parse(text)
    chunks: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            chunks.append("\n# ===== " + node.name + " =====\n")
            chunks.append("".join(lines[node.lineno - 1:node.end_lineno]))
    return "".join(chunks)


def review_inventory(preview: dict) -> list[dict]:
    rows: list[dict] = []
    for position, clip in enumerate(preview.get("candidate_clips") or [], 1):
        semantics = clip.get("content_semantics") if isinstance(clip, dict) else {}
        ids = semantics.get("candidate_ids") if isinstance(semantics, dict) else []
        candidate_id = int(ids[0]) if ids and str(ids[0]).isdigit() else position
        rows.append({
            "srt_index": candidate_id,
            "source": clip.get("source_name") or clip.get("source") or "",
            "duration_sec": clip.get("duration") or 0,
            "text": clip.get("text") or "",
        })
    return rows


def main() -> None:
    target = AUDIT / "prompt_source_snapshots"
    target.mkdir(parents=True, exist_ok=True)
    for filename, names in TARGETS.items():
        (target / f"{filename}.txt").write_text(function_source(APP / filename, names), encoding="utf-8")

    from content_review import _review_prompts

    for case_id in ("case_001_current_single_preview", "case_002_current_mix_preview"):
        root = AUDIT / "audit_cases" / case_id
        preview = json.loads((root / "preview_latest_snapshot.json").read_text(encoding="utf-8"))
        inventory = review_inventory(preview)
        required = {"[V1]": 2, "[V2]": 2} if "mix" in case_id else None
        system_prompt, user_prompt = _review_prompts(
            inventory,
            category="unknown_from_persisted_preview",
            main_product="unknown_from_persisted_preview",
            avoid=(),
            required_sources=required,
            format_retry=False,
            content_policy=None,
            include_marketing_intent=False,
        )
        out = root / "prompt_reconstruction"
        out.mkdir(parents=True, exist_ok=True)
        (out / "README.md").write_text(
            "This is a complete, deterministic reconstruction using the persisted preview candidate pool. "
            "It is NOT labelled as the historical request: the original hard-safe inventory, task policy, "
            "main-product/category variables and raw outbound request were not persisted.\n",
            encoding="utf-8",
        )
        (out / "content_review_system_reconstructed.txt").write_text(system_prompt, encoding="utf-8")
        (out / "content_review_user_reconstructed.txt").write_text(user_prompt, encoding="utf-8")


if __name__ == "__main__":
    main()
