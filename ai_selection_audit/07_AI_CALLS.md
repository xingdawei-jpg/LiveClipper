# AI Calls and Runtime Behavior

## Active facts from current cases

- Both current cases ran with Content Review mode `on` according to task logs
  and review cache evidence.
- Their cached `marketing_intent.response_present` is `false`; this matches
  source behavior where `include_marketing_intent` is passed only in `shadow`
  mode.
- Both current cases recorded two complete HookPackages, but all were tier C;
  no tier A/B package was passed as an executable Director Hook contract.
- The first Director selection is not necessarily the final renderable
  playlist. Later safety/duration/boundary/preview conversion may modify or
  reject it.

## Retry/fallback behavior visible in source

1. Content Review format/cache/transport failure returns control to the normal
   hard-safe Director path; it is intended not to block the task.
2. Absent A/B HookPackage can trigger one focused Hook-pair repair request.
3. Main Director response is parsed against known candidate IDs and policies.
4. Duration shortage can request an incremental insertion plan; over-length can
   request a delete-priority ranking; opening repair has its own bounded call.
5. Hard audit and renderable-preview conversion can remove material after the
   main Director response without a new full narrative selection.

## What is not recorded per task

- request URL, API key and Authorization header (intentionally omitted)
- raw system/user messages actually sent
- raw model response text
- request/response token counts and model latency per call
- per-candidate rationale for model omission

The absence of this data is material to an independent quality audit: it
prevents exact replay of a historical model decision.
