# Prompt and Model Call Audit

## Provenance rule

The source builds prompts immediately before `urllib.request.urlopen`; raw
HTTP request bodies are **not** persisted in run logs, preview JSON, or the
Content Review cache. Consequently, a byte-identical historical Director or
Review prompt cannot be recovered after a task. This package records the real
builders, variable sources and wire settings. The current preview/cache
snapshots provide the surviving input/output evidence needed for reconstruction.

## Calls that can affect selection

| Call | Builder | Model request settings | Dynamic inputs | Output |
|---|---|---|---|---|
| Content Review | `content_review._review_prompts` / `_post_review_request` | configured model; `temperature=0`, `top_p=0.8`, `max_tokens=6000`, JSON object, timeout 180s; DeepSeek thinking disabled | safe inventory, category, main product, avoid list, source requirements, content policy, retry flag; intent contract only if requested | cards, hook pairs/packages, optional intent/arcs |
| Hook repair | `_hook_pair_repair_prompts` / `repair_hook_pairs` | same review request mechanism; timeout 180s | already-approved cards plus original candidate text and content policy | focused hook pairs/packages |
| Main Director | `ai_clipper._call_ai` | configured model; dynamic `temperature`; `top_p=0.9`, `max_tokens=8192`, timeout 180s; DeepSeek thinking disabled | indexed transcript, layered selection contract, duration, category, rules, content review/card/hook constraints, sources, focus/history | primary playlist plus expansion plan/report |
| Oversize trim | `_call_director_trim_selection` | `temperature=0`, `top_p=0.8`, `max_tokens=1024`, timeout 180s | locked playlist and duration range | removal priority only |
| Incremental fill | `_call_director_incremental_fill` | `temperature=0`, `top_p=0.8`, `max_tokens=2048`, timeout 180s | locked skeleton, remaining safe product inventory, anchors, duration/source deficit | insertion operations only |
| Opening repair | `_call_ai_opening_repair` | `temperature=0`, `top_p=0.8`, `max_tokens=768`, timeout 180s | locked clips, allowed IDs and Hook thread | opening pair only |
| Final sequence review | `content_review.review_final_sequence` | see body in function; JSON review/revise schema | selected sequence, eligible inventory, duration/source/hook contract | pass or revised playlist, if this legacy branch is reached |

Model/base URL/API key come from runtime settings; API keys are intentionally
not exported. Current cases identify the configured model in their cache and
task metadata. The exact assembled messages are generated in source, not in a
separate prompt file.

## Main Director final assembly

`_call_ai` builds its user message from an indexed candidate transcript, then
appends `_layered_selection_prompt_contract(...)`. Its system message is
`DIRECTOR_SYSTEM_PROMPT` with duration placeholders and category overlay,
followed by the paragraph requiring 2–3 closed selling-point chapters.

Variables and sources:

| Prompt variable | Source |
|---|---|
| `indexed_transcript` | frozen/hard-safe/review-allowed candidates with ID, source, timing, text |
| duration low/high/target | `DurationContract` in `ai_analyze_clips` |
| category/main product/content policy | task `ai_controls`, normalized rules |
| hook pairs/packages/threads | `ContentReviewBundle` |
| narrative opportunities/topic support | Content Review metadata |
| source quotas | mix source contract |
| focus/history/diversity | page controls and persisted history |

The Director prompt therefore receives text/candidate metadata only. It does
not receive frames, waveform, speaking rate, pitch, presenter gesture or
product image recognition.

## Content Review final assembly

`_review_prompts` compacts each safe candidate to
`[srt_index, source, duration_sec, text]`; when marketing intent is requested
it adds `story_block_id` and `continuity_group_id`. It constructs a system
message defining Content Review as a non-director and a user message containing
policy, schema, card/tier rules, HookPackage contract and this compact inventory.

For current `on` cases, `ai_clipper.ai_analyze_clips` calls
`review_candidates(..., include_marketing_intent=False)`. Thus the Marketing
Intent schema/contract is not included in the live Content Review request for
these cases. In this source version it is requested for `shadow` only.

## Exact template source locations

- Content Review full template: `app/content_review.py::_review_prompts`
- Review transport/body: `app/content_review.py::_post_review_request`
- Hook repair template: `app/content_review.py::_hook_pair_repair_prompts`
- Marketing schema addition: `app/marketing_intent.py::marketing_intent_prompt_contract`
- Director dynamic user assembly: `app/ai_clipper.py::_call_ai`
- Layered Director contract: `app/ai_clipper.py::_layered_selection_prompt_contract`
- Duration fill/trim/opening templates: `app/ai_clipper.py::_call_director_incremental_fill`, `_call_director_trim_selection`, `_call_ai_opening_repair`
- Final review template: `app/content_review.py::review_final_sequence`

Read-only extracted source snapshots are in `prompt_source_snapshots/`. Complete
non-network reconstruction examples are in each current case under
`prompt_reconstruction/`; they are generated by
`scripts/export_prompt_evidence.py` and include the complete user inventory
text that survived in the preview snapshot.

## Historical expanded-example limitation

No selected audit task recorded its outgoing raw prompt. The package therefore
does **not** falsely label a regenerated prompt as the historical request.
`audit_cases/*/preview_latest_snapshot.json` plus
`content_review_cache_snapshot.json` contain all surviving real variables.
To reproduce an expanded prompt, call the named builder with those snapshots,
but note that current preview pools have already undergone preview normalization
and may be smaller than the original Review inventory. This information loss is
itself an audit result.
