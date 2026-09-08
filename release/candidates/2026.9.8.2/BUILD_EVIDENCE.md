# Runtime V4 business release evidence: 2026.9.8.2

- Source commit: `5f26ddf5c7a845d349f186b9b8eec9f82555ab72`
- Core compatibility: `4.0.0` (unchanged)
- Bundle: `LiveClipperBusiness_2026.9.8.2.zip`
- Size: `1,552,844` bytes
- Archive SHA-256: `a4bb4380d011c3f8f7c748adc09f95fe46a5f6761e7e968391b32f3579b35d33`
- Manifest SHA-256: `88a7ad81b8fd4a9d60f7a8b3503e8ef799e7dec4585526297ad943a75bd2f36d`
- Signing key ID: `1905329f73f719d3`

## Included runtime changes

- AI Director: non-thinking DeepSeek requests with fixed output budgets; tighter story, chapter, duration, sentence-group and product/content-policy contracts.
- Cost reporting: distinguish provider success from usable business output.
- Task execution: Smart Cut and Mix may analyze concurrently while final rendering stays serialized.
- Vocabulary: exact persistence of user lists, corrupted-text recovery and fail-closed writes.
- Local ASR: discover bundled FFmpeg and surface actionable Windows memory errors without the misleading macOS installation notice.

The signed bundle diff from public `2026.9.6.1` contains exactly ten changed business files: `app/ai_clipper.py`, `app/ai_cost_ledger.py`, `app/commercial_analyzer.py`, `app/config.py`, `app/cutter_logic.py`, `app/stt.py`, `app/version.json`, `web_client/frontend/assets/app.js`, `web_client/server.py`, and `web_client/tools/local_asr_worker.py`.

## Verification

- Two independent builds were byte-for-byte identical.
- 303 focused business tests passed from the clean release commit.
- 73 V4 update/signature/download/disk/launcher tests passed; one symlink test was skipped because the Windows account lacks symlink privilege.
- The public OSS bundle was downloaded again and matched the release SHA-256 and size.
- A signed `2026.9.6.1 -> 2026.9.8.2` update downloaded the remote archive, verified both signatures, atomically selected the new business version, imported the bundle-only runtime, retained rollback source and preserved user data.
- The online channel was first uploaded as signed `hold` and verified to offer no update before promotion.
- The primary OSS channel and archive pass the post-publish check. The configured jsDelivr and `pages.dev` channel URLs still return 404, so fallback parity is not claimed; current clients use the verified OSS endpoint first.

No paid model acceptance was run for this candidate. The new request construction removes hidden DeepSeek reasoning and reduces the single-plan output ceiling from the prior large dynamic budget to 3,000 story tokens plus 4,000 casting tokens. This structurally lowers the maximum spend, but live output quality and exact realized cost remain user acceptance items.
