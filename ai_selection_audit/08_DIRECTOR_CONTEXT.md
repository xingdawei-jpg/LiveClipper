# Director Context and Permission Boundary

## What the Director sees

The main Director gets an indexed transcript constructed from upstream
candidates. Each candidate is text/timing/source-oriented. The layered prompt
adds duration contract, policies, category/product instruction, focus, mix
requirements, review annotations, Hook constraints and narrative hints.

The Director is allowed to choose/order candidate IDs and return an expansion
plan. Local code rejects IDs that are absent, policy-forbidden, invalid as a
Hook, outside source/subject constraints, or invalid after boundary checks.

## What the Director does not see

- original video stream or frames
- presenter facial expression, vocal prosody, pitch/volume/tempo
- garment/item recognition from image
- product close-up, try-on proof, comparison action, screen text
- the original candidate before an upstream merge/trim/filter
- raw rejected candidate list and its detailed rejection reason

## Case evidence of narrowing

`case_001_current_single_preview` task log records `73 -> 59 -> 28` across
freeze/hard-safe/Content Review card stages. `case_002_current_mix_preview`
records `152 -> 115 -> 53`. In both cases Content Review retained enough
duration to be allowed as a narrowed Director pool. This is material
pre-Director candidate loss, not merely final ranking.

## Final preview conversion boundary

The UI can display a parent clip split into selected word-level segments.
`_merge_selected_segments` calls `_preview_segment_selection_units`, which
silently skips a unit when standalone boundary logic says it ends in a residual
or is incomplete. The selected UI row and final manifest can therefore differ.
See both current cases’ `final_playlist.json` and their `preview_latest_snapshot.json`.
