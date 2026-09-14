# Windows V4 candidate: 2026.9.14.2 (HOLD)

## Candidate identity

- Frozen source commit: `b7c4d2065bb39c8fcd55119de2421aa21bbd35be`
- Verified repair parent: `85d4cb44bd93003a0bd1006218140859959be525`
- Candidate branch: `codex/release-candidate-2026.9.14.2`
- Business version: `2026.9.14.2`
- Core and Launcher version: `4.0.0`
- Candidate status: `hold`; no `stable.json` ready document has been generated, uploaded, or promoted.

The candidate worktree was created directly from `85d4cb4`. It contains none of the uncommitted Mac/Core changes in the primary source worktree.

## Functional scope

- Recover only the malformed AI Director stage response without rerunning an already completed stage.
- Reject a held/failed Director draft before direct rendering.
- Retain the verified provider configuration and Director business changes already contained in the selected source baseline.
- This candidate does **not** claim Hook, residual-sentence, or filler-word quality changes; those are intentionally deferred.

## Source validation

```text
python -m unittest tests.test_commercial_analyzer tests.test_commerce_director_preview_route tests.test_director_duration_control tests.test_director_product_contract tests.test_director_hook_comparison tests.test_director_on_demand_cost
Ran 248 tests: OK

node --check web_client\\frontend\\assets\\app.js
OK
```

The repair commit modifies only these business source/test files:

```text
app/commercial_analyzer.py
web_client/server.py
tests/test_commercial_analyzer.py
tests/test_commerce_director_preview_route.py
```

## Signed business update candidate

- Archive: `C:\\lc_v4_candidate_build_2026_9_14_2\\business\\LiveClipperBusiness_2026.9.14.2.zip`
- Size: `1,580,572` bytes
- Archive SHA-256: `ed1f03bd6f517829211e3c924793cc03bde2691f5580a415054b08900d766b88`
- Bundle manifest SHA-256: `c803cf851b21ef65ca30ed9ac81d1637c4b0c0afca8ad8505400d391763f813f`
- Signing key id: `1905329f73f719d3`
- File count: `88`
- Archive signature and target application version were verified locally.

The signed `stable.hold.json` permits only `2026.9.14.1` on Core `4.0.0`. A local `2026.9.14.1 / 4.0.0` update check verifies the channel and returns `channel_hold`; it offers no update before acceptance.

## Full-package candidate

- Archive: `C:\\Users\\周美彤\\Desktop\\LiveClipperWeb_v4.0.0_2026.9.14.2_全量包.zip`
- Size: `930,293,507` bytes
- SHA-256: `b31a6b0841428518c1bafee6c1c2d3f1b6fcd94337b56a8c40ee1d9dd7400181`
- ZIP CRC verification: passed (`9,079` entries)
- Package root: `LiveClipperWeb`
- User data paths in archive: none detected
- Built-in rollback business archive: verified `2026.9.14.1`, key id `1905329f73f719d3`, `88` files.

The full builder verified signed Core files, embedded business version, package version, and rollback target before writing the ZIP.

The clean source worktree does not version third-party Core binaries. The builder consumed them read-only through explicit environment paths, without copying them into source:

- Microsoft-signed fixed WebView2 runtime `149.0.4022.98 x64`; `msedgewebview2.exe` SHA-256 `d9d0140160e18fd2235335eeee60eebfa9e8ba8d9046346846142e2f30b0149a`.
- Windows FFmpeg `ffmpeg.exe` SHA-256 `1a65d5b0b10d8d9a81d2824a3538046a40ed3607c906b335a166add87613f705`.
- Windows FFprobe `ffprobe.exe` SHA-256 `54b944d6095c4588d7548424d7acd5118390a9d4f01b1c92f841547fc9a7429c`.

## Acceptance remaining

Before promotion, perform and record on a separate clean Windows machine:

1. New-directory full installation and startup health check.
2. Existing `2026.9.14.1` to `2026.9.14.2` business update, including automatic restart.
3. Preserve user data through the update.
4. Induced first-start failure and automatic rollback to `2026.9.14.1`.
5. Offline, insufficient disk space, download hash error, and channel signature error handling.

Only after those checks pass may the signed channel be regenerated as `ready` and published to the client-facing `stable.json` endpoint.
