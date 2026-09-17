# Ground Truth Inventory

## Result

`NO_COMPLETE_GROUND_TRUTH_AVAILABLE`

The repository search found tests and review modules, but no maintained dataset
linking a whole original video to a human-approved gold candidate playlist.
The local data contains one relevant artifact:

`ground_truth_evidence/preview_selection_feedback.jsonl`

It contains 118 historical `smart_from_preview` / `mix_from_preview` events,
covering 111 preview IDs. Each event records human-kept and human-rejected
preview segments/text (and occasionally role samples). This is authentic manual
editing behavior, and can label a segment as human-kept or human-rejected for
that preview state.

It is not a complete gold standard because:

- it begins after the AI preview candidate pool already exists;
- it lacks the full original candidate population and raw video coverage;
- it does not label every unshown/filtered original candidate;
- it does not contain a human reason/rating for every decision;
- older entries may have legacy encoding/text corruption.

Accordingly, do not calculate a global candidate recall or precision score from
this file without first matching preview IDs, candidate/segment identities and
source version. The appropriate labels when a match is valid are:

`HUMAN_SELECTED_AI_MISSED`, `AI_SELECTED_HUMAN_REJECTED`, `BOTH_SELECTED`,
`BOTH_REJECTED`.
