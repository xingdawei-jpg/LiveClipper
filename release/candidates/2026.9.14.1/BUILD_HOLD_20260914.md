# Windows V4 business candidate: 2026.9.14.1 (ready promotion pending)

## Boundary and provenance

- Frozen commit: `9421f4a90da09141e7cef67adbca57ebd2b6e6b6`
- Previous published Windows business version: `2026.9.9.1`
- Windows Core: `4.0.0` (unchanged)
- Delivery: signed Windows V4 business bundle only; not a full installer.

This candidate selectively carries the platform-neutral Director and provider-setting improvements from the macOS development branch. It deliberately excludes all macOS Cocoa, arm64 host/launcher, FFmpeg, requirements, `.spec`, Core, update-channel, signing, notarization, and candidate files. No Mac artifact is eligible for this Windows bundle.

## Product scope

- Director opening evidence and Hook-to-payoff validation improve complete, non-repetitive openings.
- Director story and beat handling improves coherent transitions, bounded responses, and malformed-wire recovery.
- The settings UI preserves distinct credentials and models for DeepSeek, Doubao Ark, and other OpenAI-compatible providers.
- Provider configuration validation and Director error accounting are strengthened.

## Bundle evidence

- Archive: `LiveClipperBusiness_2026.9.14.1.zip`
- Archive SHA-256: `33931dcf193e49bf3d43e8b2e406f4bcec6ec43f60731f134f37dff94da5bb8a`
- Inner manifest SHA-256: `12c5798d98db9e54eba88ffa9ab1a2996f6d011f89df010bfca45967825a7a4f`
- Signing key id: `1905329f73f719d3`
- Size: 1,575,183 bytes; 88 payload files.
- Two clean builds produced byte-identical ZIP files and matching manifests.
- Standalone signature and file-set verification passed for application `2026.9.14.1`, Core `4.0.0`.

Compared with the 2026.9.9.1 business bundle, the payload adds only `business/app/director_opening_audit.py` and changes `app/ai_model_config.py`, `app/commercial_analyzer.py`, `app/content_review.py`, `app/version.json`, `web_client/server.py`, and the related frontend HTML, JavaScript, and CSS. No Core, launcher, native runtime, ML payload, user data, or macOS file is present.

## Validation completed

- Python compile and frontend JavaScript syntax checks: pass.
- Focused Windows suite: 406 passed, 1 Windows symlink case skipped.
- Covered Director analysis, preview route, Hook comparison, duration and cost controls, provider UI/settings, content review/safety, runtime V4 business bundle, update channel, update transaction, and release architecture.
- The imported Hook-comparison test was made Windows-portable by explicitly reading UTF-8 frontend and Node output. Its semantic assertions now match the current Hook-to-payoff contract; no product rule was weakened.
- Locally signed `stable.hold.json` verifies successfully for `2026.9.9.1` on Core `4.0.0` and correctly returns `channel_hold`.

## Remote and update acceptance

- The immutable archive was uploaded to `liveclipper/v4/LiveClipperBusiness_2026.9.14.1.zip` in Aliyun OSS. A separate HTTPS download returned the expected 1,575,183 bytes and SHA-256 `33931dcf193e49bf3d43e8b2e406f4bcec6ec43f60731f134f37dff94da5bb8a`.
- The signed ready document has SHA-256 `6d7982ab1098c9627102eb9c01594d39b062933c674b9173a8e031a6b8af68ac`. It verifies for a Windows `2026.9.9.1` client on Core `4.0.0` and returns `update_available`.
- In a fresh isolated installation, the actual `2026.9.9.1 -> 2026.9.14.1` transaction passed signature verification, download hash verification, atomic activation, and target manifest verification. The old `2026.9.9.1` bundle remains available as the rollback source.
- The target runtime imported `server`, Director product/wire/runtime modules, and the newly added `director_opening_audit` exclusively from the verified bundle; it reported application `2026.9.14.1`. A user-data preservation sentinel was unchanged.
- The online stable document was first uploaded as signed `hold`, re-downloaded byte-for-byte (SHA-256 `5ac0eeccaa1111a08441c1d944aba7d4cc1772a311ef0d6c15ef9d61731fed93`), and correctly returned `channel_hold` for `2026.9.9.1`.

## Promotion state

The archive and acceptance prerequisites are complete. At this evidence point the public Windows stable channel is still the verified `hold` document; publishing the already verified signed `ready` document is the final external promotion action.
