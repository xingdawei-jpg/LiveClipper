# M2 Clip Selector Plan

## Purpose

Clip Selector is the execution layer after a commercially acceptable M2
`NarrativePlan`. It turns the approved chapter order into exact playable
source ranges. It is **not** a second director and must never replace M1/M2
commercial judgment.

`Candidate != Final Clip`. M2 approves the semantic content for a beat; the
Selector may materialize the smallest complete spoken expression *inside that
approved candidate*. It may not turn a complete expression into a label such
as `显瘦` or `特别好看`.

`Commercial Story Brief -> M2 NarrativePlan -> Clip Selector -> deterministic QC -> render`

## Preconditions

The offline prototype may run in parallel with blind evaluation. It may not be
connected to preview, direct render, or batch render until all of the following
are true:

- candidate-boundary regressions are zero;
- an anonymous same-source Legacy vs M2 review has been completed; and
- Selector output has been reviewed as rendered subtitle/video material.

An approved human playlist is valuable as a later third comparison, but is not
the only way to begin the Legacy vs M2 evaluation.

Every selector task must receive:

- one valid `NarrativePlan`;
- its exact immutable `Selection Contract`;
- the selected `PlanningCandidate` IDs in beat order;
- the Candidate Ledger candidate-to-semantic-ancestor mapping; and
- word-level timing for the source candidate, when an edge needs refining.

No missing lineage or timing may be recreated with timestamp containment.
Older `.words.json` files that lack subtitle IDs may only be used after every
sidecar row is verified against the ordered original SRT text. A mismatch
blocks edge trimming for that run; it is never repaired by time overlap.

## Allowed work

1. Preserve M2 beat order and selected candidate ownership.
2. Keep a complete selected candidate as-is when it is already short enough.
3. When exact word timing exposes an already-spoken semantic boundary, remove
   only a prefix/suffix of live filler, residual interaction, or redundant
   setup and choose the smallest *complete* contiguous expression from that
   same candidate.
4. Keep Hook and immediate payoff as a contiguous opening unit.
5. Keep the whole approved candidate when it is the smallest complete option.
   Report `long_evidence` when an important complete explanation has no safe
   shorter boundary. A soft duration contract must not authorize a
   mid-sentence cut.
6. Record the parent candidate ID, source semantic ancestors, exact words and
   exact start/end for every final range.

## Prohibited work

- selecting a candidate that M2 did not approve for the relevant beat;
- changing M2 chapter order, Hook, payoff, close or commercial thesis;
- filling duration from a reserve pool;
- rewriting transcript text or inventing a boundary;
- midpoint/character-ratio cutting;
- choosing an arbitrary middle sentence that changes the approved semantic
  content;
- splitting a phrase unless the retained expression independently passes the
  existing semantic-boundary checks; or
- deleting a weak chapter and quietly substituting a different selling point.

If a beat cannot become playable under these rules, Selector returns a
structured `selector_blocked` result naming the beat, parent candidate, and
boundary problem. M2, not local code, owns any narrative replan.

## Proposed data contract

```json
{
  "plan_id": "...",
  "selection_contract_hash": "...",
  "beats": [
    {
      "chapter_id": "C1",
      "parent_candidate_ids": [21],
      "ranges": [
        {
          "parent_candidate_id": 21,
          "source_id": "V1",
          "start": 77.82,
          "end": 82.71,
          "origin_subtitle_ids": [23],
          "word_boundary_kind": "semantic_sentence"
        }
      ]
    }
  ]
}
```

The public preview can display the range text, but the audit record retains
the parent candidate and Ledger lineage. This is required to answer whether a
bad final cut was already bad in M2 or was damaged while materializing it.

## Deterministic validator

The validator may only check facts:

- every range belongs to the stated parent candidate and source;
- start/end are exact word or candidate boundaries and form a contiguous slice;
- no partial word, orphaned connective or duplicate overlapping range exists;
- final text is a complete expression, rather than a label or a semantic tail;
- each final range still matches its selected candidate's role permissions;
- Hook and payoff occur in the first opening unit;
- all source lineage is present; and
- rendered media duration from `ffprobe` matches the final range manifest.

It must not decide that another safe candidate would make a better proof,
transition, Hook or closing line.

## Evaluation before integration

For each real case, preserve four artifacts:

1. M2 plan and blind-evaluation result.
2. Selector range manifest with parent candidate lineage.
3. Preview subtitle manifest.
4. Rendered video `ffprobe` duration and final subtitle timings.

The final review checks commercial feel first, then whether Selector introduced
any new cut-off sentence, chapter-order change, or mismatch from the approved
M2 plan. A Selector that makes more 2-5 second pieces but damages naturalness
does not pass.
