# M2 Commercial Story Blind Evaluation

This evaluation judges whether an ordered playlist feels like a commercial
short video. It is not a schema-completeness check and it does not select,
rewrite, or rank candidates.

## Required inputs

Each case needs at least two comparable, already-selected playlists for the
same source, product and content contract. Typical variants are:

- a human-approved playlist;
- the legacy director playlist; and
- the M2 planner playlist.

Do not invent a human baseline. If there is no approved manual playlist, retain
`NO_GROUND_TRUTH_AVAILABLE` for the later human-reference comparison, but run
the same-source **Legacy vs M2** anonymous A/B now. That A/B answers whether
the new planning direction is visibly better than the current path; it does
not claim a human upper bound.

## Blind packet

`story_blind_eval.build_blind_packet` produces:

- a public packet containing only anonymous labels, ordered subtitle text, and
  individual duration; and
- a separate private answer key mapping labels to variant IDs.

The reviewer must receive only the public packet and rating sheet. The public
packet intentionally omits candidate IDs, source IDs, roles, M1 assets,
strategy names and generator identity.

## Rating dimensions

Rate every anonymous variant `0`, `1`, or `2`.

1. Opening genuinely catches attention.
2. The opening promise is paid off promptly.
3. The story progresses rather than becoming a selling-point list.
4. The content is rich but stays focused.
5. The reason to buy becomes clearer as it goes.
6. The cut feels natural rather than like stitched subtitles.

Use comments for a concrete break: the exact opening, transition, repeat, or
unfinished line that caused the score. Do not score chapter count, theme count,
or whether a field exists in JSON.

## Evaluation gates

The offline Clip Selector prototype may proceed while Legacy vs M2 packets are
being prepared. Preview or rendering integration requires real cases showing
that M2's *ordered playlist* is commercially acceptable, followed by a second
blind review of selector-produced subtitles/video. The Selector must preserve
each approved semantic unit at the real video boundary; it must not rewrite the
story, choose a different hook, or compensate for a weak plan.
