# Runtime V4 business candidate: 2026.9.9.1

## Release boundary

- Frozen source commit: `d91ff70bb8fe11740994ae05fa2da5d63f1ea571`
- Core version: `4.0.0` (unchanged)
- Delivery: signed Runtime V4 business bundle; not a full installer.
- Bundle: `LiveClipperBusiness_2026.9.9.1.zip`, 1,564,400 bytes, 87 payload files.
- Archive SHA-256: `2aecdda386b245cc42e89c7c52e8655a5dcdcec44980a1c38979d878e237c644`
- Business manifest SHA-256: `fbb6261d905603e913137eb73292ec8720ecfa8b7cd051f6db42da89d165c828`
- Signing key id: `1905329f73f719d3`

The clean source was built twice. Archive and business-manifest SHA-256 values matched exactly. Compared with the published 2026.9.8.2 bundle, the file layout is unchanged; the changed runtime payload is limited to the AI Director, local ASR, version manifest, web UI, and server files. No launcher, Core, native runtime, model payload, or user-data file is in this update.

## Functional validation

- Clean-worktree suite: 296 passed, 1 Windows symlink skip.
- Local ASR quality suite: all 34 cases passed when each was launched in its own Python process.
- Total validated test cases: 330 passed, 1 skipped.
- `python -m compileall -q app web_client/server.py`: pass.
- `node --check web_client/frontend/assets/app.js`: pass.

The combined local-ASR quality module completes its assertions but can hit a native Windows interpreter-shutdown access violation after the tests finish. That native shutdown result is not claimed as a clean monolithic test run. The release validation uses the product's isolated-process model and records the 34 clean, independent-process results above.

## Update and rollback validation

Starting from the actually published `2026.9.8.2` V4 business bundle on Core `4.0.0`, the isolated acceptance run downloaded the remote 2026.9.9.1 archive, verified its signed channel and hash, activated it atomically, retained the previous version for rollback, imported the updated runtime, and confirmed that workspace content outside the bundle and user data were preserved.

All of the following passed: signed remote update, atomic activation, rollback-source retention, bundle-only runtime import, updated-module imports, outside-workspace preservation, and user-data preservation.

## Channel publication sequence

1. Uploaded the immutable archive to the primary Aliyun OSS source and re-downloaded it with the recorded SHA-256.
2. Uploaded `stable.hold.json`; a `2026.9.8.2` client correctly received `channel_hold` and no update offer.
3. Generated and locally verified the signed `stable.json` ready document. The final ready document is published only after this candidate evidence is committed.

Eligible source versions are `2026.8.5.1`, `2026.8.5.2`, `2026.8.7.1`, `2026.8.8.1`, `2026.8.11.1`, `2026.8.11.2`, `2026.8.12.1`, `2026.8.12.2`, `2026.8.15.1`, `2026.9.3.2`, `2026.9.3.3`, `2026.9.4.1`, `2026.9.6.1`, and `2026.9.8.2`.

## Scope notes

This candidate improves AI Director direction and preview behavior, duration and content safeguards, local ASR device fallback and segment processing, plus the related settings and interface. It does not establish a measured paid-model token-cost or quality claim; a real paid-model production acceptance remains separate work.
