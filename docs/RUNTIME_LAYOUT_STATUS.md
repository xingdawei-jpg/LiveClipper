# Runtime Layout Status

Status date: 2026-08-01

## Current boundary

- Runtime V3 remains the released and supported user architecture.
- `app/version.json` is the Runtime V3 manifest. Its `schema_version` and
  `runtime_layout_version` must remain `3` until a V4 release is formally
  promoted.
- Runtime V4 is a private pilot architecture. Its signed Core, business bundle,
  launcher state, and update channel use independent layout-4 manifests under
  `runtime_v4/`, `release/`, and the generated V4 candidate directory.
- A V4 pilot must never consume `app/version.json` as proof of a layout-4
  package, and a V3 delta must never contain a V4 Core or launcher transition.

## Writable data boundary

- Signed V4 Core and business directories are immutable after installation.
- Logs, ASR models, caches, previews, task state, settings, and diagnostics must
  be written under the configured user-data root.
- Legacy run logs accidentally written under `business/app/logs` may be moved
  to `%APPDATA%\LiveClipper\recovered_logs\v4\<version>\app\logs` before
  verification. No other unlisted business file is repaired or ignored.

## Promotion gate

V4 can be called distributable only after all of the following are recorded:

1. Production-key Core and business signatures verify.
2. A clean-machine install and a real V3-to-V4 migration both launch the
   extracted EXE with `runtime_layout_version=4` and `code_source=bundled`.
3. Smart cut, mix, local/cloud ASR, word sidecars, drag-drop, cache cleanup,
   close/restart, update, rollback, and disk-usage acceptance pass on another
   computer.
4. The signed V4 channel points to the exact accepted manifests and assets.
5. V3 stable metadata remains unchanged until the V4 candidate is explicitly
   promoted.

Until that gate passes, source tests and private pilot packages are evidence,
not a release.

