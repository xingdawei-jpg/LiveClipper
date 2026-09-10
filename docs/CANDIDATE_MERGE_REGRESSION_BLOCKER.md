# Candidate Merge Regression Blocker

Status: active blocker for M3 integration and any live M1/M2 connection.

The Candidate Ledger proves ancestry, but it does not certify that a frozen
candidate is a complete semantic expression. The following regressions must
remain at zero before the Clip Selector is connected to preview or rendering.

## Frozen-boundary regressions

1. `test_frozen_candidates_join_a_specific_incomplete_material_clause`
   - `它做的是。` must retain `加棉`.
2. `test_frozen_candidates_join_a_specific_unfinished_morning_scene`
   - `早上一觉睡醒。` must retain `穿个这个衣服`.
3. `test_director_candidate_can_exceed_soft_cap_to_finish_sentence`
   - A continuing travel list must retain its destination/result rather than
     being split into independently misleading rows.
4. `test_director_merges_only_dependent_fragments_and_keeps_complete_blocks_free`
   - `整个人的气质` must retain its predicate, while the unrelated complete
     later statement remains independent.
5. `test_hook_boundary_gate_rejects_tail_particle_and_freeze_rejoins_exact_word_tails`
   - `三角的立` must retain `体捏褶`; the earlier exact word continuation
     `聚 + 酯纤维` must continue to work.

## Per-beat duration regression

6. `test_director_duration_contract_preserves_unsplittable_complete_product`
   - A complete 17-second Product without a frozen internal boundary must be
     retained and flagged for pacing. Deleting it is not a duration solution.

## Root cause recorded on 2026-08-20

The current uncommitted `_director_freeze_requires_followup` gate treated a
terminal `。` as decisive before checking whether the text was a known semantic
tail. It also allowed the short-form classifier to override a confirmed tail.
Separately, the per-clip duration gate deleted an unsplittable complete Product
above 12 seconds. Neither behavior was introduced by Candidate Ledger lineage
metadata.

## Integration rule

M3 may be prototyped offline. It cannot be wired to preview, direct render, or
batch render until this regression suite is green and the rendered selector
blind review is complete.
