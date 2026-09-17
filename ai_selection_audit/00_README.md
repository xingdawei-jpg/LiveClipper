# LiveClipper AI Selection External Audit Package

## Scope and freeze

This package is a read-only snapshot of the active source runtime captured on
2026-08-16. It was produced without invoking a new ASR or LLM selection task,
without changing an AI prompt, threshold, code path, cache, or test result.

Runtime evidence is in `runtime_snapshot.json`. The source checkout used by
the runtime is `C:\Users\周美彤\Documents\GitHub\LiveClipper`; it was already a
dirty working tree when this audit began. The package contains no source-video
binary and no credentials.

## How to use this package

1. Start with `01_PIPELINE.md`, then cross-check every named function in the
   source tree.
2. Use `02_CODE_MAP.md` as the search index, not as a substitute for source.
3. Read `03_PROMPTS.md` together with the exact builder functions. Historic
   raw LLM request bodies were not persisted, so this limitation is explicit.
4. Inspect all three directories under `audit_cases/` before drawing quality
   conclusions. `stage_availability.json` tells which lifecycle evidence did
   not survive at the time each task ran.
5. Treat `10_PRELIMINARY_FINDINGS.md` as observations only. It contains no
   proposed repair.

## Case index

| Case | Type | Evidence completeness |
|---|---|---|
| `case_001_current_single_preview` | Current single-material AI preview | Preview, task log, content-review cache, final manifest; partial lineage |
| `case_002_current_mix_preview` | Current two-source mixed AI preview | Preview, task log, content-review cache, final manifest; partial lineage |
| `case_003_historic_success_limited_lineage` | Historic completed output | Run log and final audit only; no candidate lifecycle |

`ground_truth_evidence/preview_selection_feedback.jsonl` contains 118 historic
manual preview-edit events across 111 preview IDs. It is usable as partial
human preference evidence, not as a complete per-video human gold standard.

## Evidence boundaries

- The system does not persist raw LLM HTTP requests/responses alongside a task.
  Templates and request settings are source-verifiable; historical prompt text
  can only be reconstructed where input inventory survived.
- The active preview response persists a candidate pool, but not a complete
  per-candidate ledger for raw ASR, every hard-filter rejection, and every
  Director decision. Therefore a non-selected candidate cannot always be
  assigned one definitive death stage.
- All source paths are retained because they are needed to locate original
  media/SRT. No video was copied.

## Key source files to inspect

- `app/cutter_logic.py`
- `app/local_asr.py`, `app/local_asr_chunking.py`, `app/local_asr_quality.py`,
  `app/volcengine_asr.py`
- `app/ai_clipper.py`
- `app/content_review.py`
- `app/marketing_intent.py`
- `app/selection_context.py`, `app/selection_contracts.py`,
  `app/selection_safety.py`, `app/candidate_quality.py`, `app/content_policy.py`
- `web_client/server.py`

The reproducible, audit-only evidence collector is
`scripts/export_evidence.py`. It only reads the local API, cache and run logs.
