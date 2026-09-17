# Information Loss Map

| Original information | Compression/loss point | What remains downstream | Reversible? | Selection consequence |
|---|---|---|---|---|
| Raw video, facial expression, item shown, gesture, camera angle | ASR becomes selection source | text, sentence time, optional word times | No in current Director path | Director cannot verify visual product, visual proof, emotional delivery or screen text |
| Continuous speech | ASR punctuation, pause/semantic segmentation | sentence/word entries | Partly | Bad segmentation can create false fragments or hide a good 2–5s statement |
| Adjacent ASR entries | `_freeze_director_candidates` merge/edge trim/dangling-tail trim | merged candidate text/time only | No for the task | Original smaller units and exact deletion reasons are not retained downstream |
| Frozen candidates | quality, price/CTA, interaction/size, subject/product filters | safe inventory/IDs and aggregate logs | No | Director cannot see rejected candidates; individual rejection ledger is absent |
| Hard-safe inventory | Content Review cards in `on` if coverage proves sufficient | reviewed main/reserve IDs, hint and selected metadata | No to Director | High-quality safe but uncarded material can be hidden from Director |
| Review request | compact inventory fields and 240-char text cap | ID/source/duration/text, limited context fields | No | Full local SRT neighborhood, visual/audio context and some metadata are absent |
| Review result | cache | cards/packages, digest; not original inventory/prompt/raw model answer | No historically | Cannot reconstruct exact historical LLM request or why every omitted candidate was omitted |
| Director request | HTTP request body not stored | parsed playlist/log counts/preview representation | No historically | Cannot attribute every non-selected candidate to model decision vs non-exposure |
| Director playlist | hard audit/cap/duration/source operations | adjusted playlist | Partly | Initial order can differ from final renderable selection |
| UI preview selection | `_preview_segment_selection_units` and standalone checks | renderable word/segment units | No for discarded units | A visibly selected UI segment can be removed before final manifest |
| Final manifest | rendering | clip bounds/text digest/source only | No | Export does not preserve entire candidate competition |

The current selection path is therefore text-dominated and contains multiple
irreversible gates before the Director. The Director cannot restore content
that was removed or never encoded in its candidate table.
