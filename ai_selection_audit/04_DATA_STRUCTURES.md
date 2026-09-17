# Data Structures and Candidate Identity

| Structure | Defined / produced by | Essential fields | Meaning |
|---|---|---|---|
| SRT entry | ASR parsers / `ai_clipper` parse helpers | start, end, text, optional `[Vn]` marker | Original text-time unit after ASR processing |
| Word timing | ASR `.words.json` | word text, start, end, index/key | Enables semantic boundary splits and preview word edits |
| `SelectionCandidate` | `ai_clipper._freeze_director_candidates` / `selection_contracts.py` | candidate_id, source_id, start/end, text, hook_eligible, story_block_id, continuity_group_id, subject relation/role permissions | Immutable candidate contract exposed downstream |
| Candidate inventory item | `ai_analyze_clips` | `srt_index`, source, duration_sec, text, context fields | LLM-facing candidate record |
| `ContentCard` | `content_review.py` | candidate_id, topic/subtopic, buyer_value, evidence type/quote, roles, dependency, tags, tier, subject evidence | Review’s grounded annotation; it must reference real candidate ID |
| `HookPackage` | `content_review.py` | hook_id, followup_id, promise, proof relation, complete, semantic signals, opening tier | A/B packages are Director-eligible; C is diagnostic/natural fallback only |
| Marketing intent bundle | `marketing_intent.py` | intent evidence and narrative arcs | Returned inside Review schema only when requested |
| Director JSON playlist | `_call_ai` parse helpers | role, `srt_indices`, focus, reason, trim priority, expansion plan | Director selection intent; IDs are revalidated locally |
| Preview clip | `server.py` preview serializer | index, text, start/end, segments, words, selected, `candidate_key`, semantics | Editable UI representation, can be split into word/segment units |
| Selection manifest | selection contracts/server sync | clip_id, order, role, start/end, source, text digest, duration contract | Renderable final selection record |

## Identity limitations

`candidate_id`/`srt_index` is the robust selection identity during one task.
Preview `candidate_key`, segment index, word key and final manifest clip ID are
derived at different stages. Current persisted artifacts do not carry a full
cross-stage mapping for every dropped candidate. This is why the audit case
lifecycle files explicitly distinguish `final_manifest_selected` from
`not_traceable_from_current_persisted_evidence`.
