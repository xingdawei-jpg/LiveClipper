# Real Execution Pipeline

This is traced from actual entry functions, not inferred from product design
documents. Names in parentheses are source functions.

```text
Browser request
  -> preview/direct render worker (web_client/server.py)
  -> process_video / process_video_mix (app/cutter_logic.py)
  -> source validation and optional TS remux
  -> existing validated SRT OR cloud/local ASR
  -> SRT + word-timing sidecar
  -> ai_analyze_clips (app/ai_clipper.py)
  -> word-level semantic segmentation / candidate freeze
  -> candidate quality + safety + product/subject filtering
  -> hard-safe inventory
  -> Content Review (optional LLM call)
  -> Director LLM selection and JSON parse
  -> duration, source-mix, boundary and hard audits
  -> optional LLM incremental fill / opening repair / remove ranking
  -> final-sequence audit (diagnostic / optional review path)
  -> preview normalization and selected-word rendering conversion
  -> final SelectionManifest / render or preview response
```

## Entrypoints

| Product route | Server worker | Downstream |
|---|---|---|
| AI preview | `web_client/server.py::_run_smart_preview` | `cutter_logic.process_video(..., _clips_only=True)` |
| Smart render without preview | `web_client/server.py::_run_smart_cut` | `cutter_logic.process_video(...)` |
| Mix preview | `web_client/server.py::_run_mix_preview` | `cutter_logic.process_video_mix(..., _clips_only=True)` |
| Mix render without preview | `web_client/server.py::_run_mix` | `cutter_logic.process_video_mix(...)` |
| Render a manually edited preview | `_run_smart_cut_from_preview`, `_run_mix_from_preview` | preview-selected exact clips; it must not initiate normal selection |

Preview, direct smart render, and direct mix render are separate tasks. A
direct render does not silently reuse the last preview playlist.

## Stage-by-stage trace

| Stage | Caller / function | Input -> output | Candidate loss / boundary change | LLM |
|---|---|---|---|---|
| Input normalization | `process_video`, `process_video_mix` | media path -> edit-safe input path | A TS source can be remuxed; no text selection yet | No |
| ASR/cache | `process_video`; local/cloud adapters | media or cached `.srt` -> SRT plus `.words.json` | ASR text and timing become the semantic source; original audio/visual performance is not represented as a selection feature | No |
| Semantic segmentation | AI path in `ai_analyze_clips` plus local ASR quality/context modules | SRT + words -> semantic sentence entries | Segments can split/merge and receive word boundaries | No |
| Candidate freeze | `_freeze_director_candidates` | cleaned SRT -> immutable `CandidateSet` | Adjacent incomplete candidates merge; leading filler/tails can be trimmed; quality/safety/subject filters remove candidates before Director | No |
| Hard-safe inventory | `ai_analyze_clips`, `_filter_price_and_cta`, safety/subject helpers | frozen candidates -> safe inventory and IDs | Price/CTA/prohibited, interaction/size, invalid boundary and subject/product rules remove candidates | No |
| Content Review | `content_review.review_candidates` | safe inventory -> `ContentReviewBundle` | LLM emits up to 80 cards and HookPackage data. In `on`, reviewed IDs can reduce Director-visible candidates when review duration covers the duration floor; otherwise safe reserve is restored | Yes |
| Marketing Intent | returned inside Content Review schema | review response -> marketing intent bundle | Not a separate model request. Current source passes `include_marketing_intent=True` only in review `shadow` mode | Yes, same review response only when enabled |
| Director | `_call_ai` | indexed eligible candidate table -> JSON playlist, expansion plan, report | Director can only name supplied IDs. Parsed IDs are rechecked against allowed IDs, hook policy, source and subject constraints | Yes |
| Local and AI duration actions | `ai_analyze_clips`, incremental-fill/opening/trim helpers | Director playlist -> duration/source-compliant playlist | Candidate boundaries can be capped/split only on existing candidate boundaries; invalid long products may be removed; fallback behavior is branch-dependent | Sometimes |
| Final audit | `_director_hard_audit`; `content_review.audit_final_sequence` | playlist -> pass/flag / audit metadata | Hard safety can remove clips. The current formal path records audit; historic legacy review/rewrite code also exists and must be call-site checked | Sometimes / branch dependent |
| Preview-to-final conversion | server `_merge_selected_segments`, `_hard_filter_preview_selection`, `_sync_preview_final_selection_metadata` | UI rows and selected words -> renderable tuples -> manifest | A UI-visible selected segment can be dropped if it is standalone-invalid or later hard-filtered. This is a second irreversible conversion | No |
| Render | `process_video`, `process_video_mix` | final tuples -> FFmpeg processing/output | Visual transforms/dedup occur after selection; no evidence that they supply visual signals to the Director | No |

## Director authority

**B/C, not A:** the Director cannot search the original livestream freely. It
can select only candidate IDs supplied in `indexed_transcript`, after the
freeze, hard-safe inventory, and optional Content Review restriction. It
cannot recover a clip removed before that input. The practical pipeline is
therefore `TEXT_DOMINATED_PIPELINE` for selection: video frames are used later
for visual processing/dedup, but not supplied to the Director as product,
gesture, emotion, or item-recognition evidence.
