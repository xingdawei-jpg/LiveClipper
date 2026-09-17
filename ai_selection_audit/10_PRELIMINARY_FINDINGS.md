# Preliminary Findings (No Code Changes)

## Evidence-backed observations

1. **The Director is not a full-livestream retriever.** It receives an
   upstream text candidate table only. Removed candidates and all visual/audio
   delivery information are unavailable to it.
2. **There is substantial pre-Director narrowing in the two current cases.**
   Single preview: 73 frozen -> 59 hard-safe -> 28 review cards. Mix preview:
   152 -> 115 -> 53. The source shows that, when Review coverage is enough,
   `on` can make this narrowed review set the Director’s allowed IDs.
3. **Candidate death is not fully auditable after the fact.** Aggregate log
   counts survive, but a per-ID ledger for raw ASR, each hard filter, Review
   omission and Director non-selection is not persisted. The case lifecycle
   JSONL files deliberately mark unknown stages rather than assign a reason.
4. **Marketing Intent is not an active input in the captured `on` cases.**
   Its cached response is absent and code supplies the intent contract only for
   `shadow` review. Marketing labels therefore cannot explain their live
   Director choices.
5. **HookPackage did not furnish a high-tier opening contract in either current
   case.** Cached packages are C tier only; A/B is the executable Director
   tier. This is not proof of source quality by itself, but establishes what
   the Director received.
6. **Current mix evidence distinguishes initial Director duration from final
   preview duration.** Task log reports initial 20 segments / 101.2 source
   seconds (projected 88.0) within a 90s task range. The saved final manifest
   reports 19 render parts / 75.77 source seconds (projected 65.887) and
   `partial_insufficient`. This indicates downstream loss or conversion after
   initial Director selection, not simply an absence of initial candidate time.
7. **Preview UI and manifest do not always agree.** In both current cases,
   UI-selected word/segment rows include terminal fragments ending in `啊`; the
   final manifest omits such units under standalone-boundary conversion. This
   affects actual downstream duration and content.
8. **Task policy can diverge at final preview recheck.** Freeze paths use the
   supplied content policy; `_hard_filter_preview_selection` invokes
   `_filter_price_and_cta` without passing it. This is a source-level
   observation requiring independent evaluation of actual settings behavior.

## Audit limitations to preserve

- No raw historical prompt payloads or raw LLM responses exist in the retained
  artifacts.
- No complete human gold playlist dataset exists.
- Case 003 is a real historic success record but its older run-log schema lacks
  candidate lineage. It cannot prove why a candidate was chosen or lost.
- Current cases are previews, not final rendered video. The package records
  final selection manifests where available; no video binary was exported.
