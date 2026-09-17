# Filters, Thresholds and Fallbacks

## Observed constants and gate rules

| Gate | Source | Threshold / rule | Permanent for current task? | Fallback |
|---|---|---|---|---|
| Candidate merge | `_freeze_director_candidates` | same source, gap `-0.15..2.2s`, incomplete-boundary condition; typical cap 22s, completion cap 32s | Yes, boundaries change | Leave separate if condition fails |
| Candidate edge/tail quality | freeze helpers / `candidate_quality.py` | text completeness, filler, obvious ASR/fragment rules | Yes for removed candidate | None at Director level |
| Content safety | `_filter_price_and_cta` and safety/policy helpers | task price/CTA/source/social/after-sale/size/interaction policy plus forbidden vocabulary | Yes | Policy may allow/body-only/block categories |
| Live interaction/size | `_filter_live_interaction_or_size_responses` | live Q&A, personal height/weight, size response rules | Yes when blocked by policy | None |
| Product/subject relation | exact-product and subject filters | category/main-product relation and role permissions | Yes or role-limited | Some supporting/unknown material can remain depending on policy |
| Content Review cap | `content_review.py` | maximum 80 cards; target retained source duration 180s; cache 60 days/128 files/50MB | In `on`, may narrow Director input only when coverage is adequate | Revert to hard-safe pool on review fault/insufficient coverage |
| HookPackage | `content_review.py` | 0–8 packages; only complete A/B packages are executable Director hook source; C is diagnostic | Role-limits opening | Pair/natural/lexical fallback branches |
| Product segment cap | `ai_clipper.py` | `_DIRECTOR_PRODUCT_MAX_DURATION_SECONDS = 8.0` (small tolerance in audit) | Can remove/cap a selected product | Boundary-safe split only; otherwise remove |
| Opening segment cap | `content_review.py` | `_DIRECTOR_OPENING_SEGMENT_MAX_SECONDS = 8.0` | Hook package validity | natural opening fallback |
| History similarity | `_filter_recent_similar_clips` | default similarity threshold 0.62, protected structure/minimum keep | Can filter/rank candidates | Preserve floor to avoid duration loss |
| Duration | `DurationContract` | target and source bounds calculated with speed factor; user tolerance supplies normal range | Final success/status gate | partial/shortfall behavior depends on route/payload |
| Mix source | source quota/distribution helpers | required source count; dominance/run diagnostics | Candidate/playlist constraint | warn/fill/retry branches |
| Preview standalone | `_director_standalone_boundary_reason` / server conversion | terminal filler such as `呃/嗯/啊/哦/诶`, incomplete start/end, condition/preamble rules | Yes at preview-to-final conversion | Segment is omitted |

## Policy scope note

Candidate freeze accepts the task content policy explicitly. The preview final
hard recheck currently calls `_filter_price_and_cta(items, None)` without an
explicit task policy in `server.py::_hard_filter_preview_selection`; this is
recorded as a policy-source divergence for independent audit, not modified.

## Current case counts

| Case | Freeze / safe / review / Director-visible evidence |
|---|---|
| `case_001_current_single_preview` | task log: 73 frozen candidates, 59 hard-safe (291.9s), 28 review cards (179.7s); preview endpoint retains 57 visible candidate rows |
| `case_002_current_mix_preview` | task log: 152 frozen candidates, 115 hard-safe (520.2s), 53 review cards (276.1s); preview endpoint retains 94 visible candidate rows |
| `case_003_historic_success_limited_lineage` | final run record only; no candidate-stage counts persisted |

Preview-visible count is not necessarily the original Director input count;
preview normalization itself removes/rearranges unavailable units.
