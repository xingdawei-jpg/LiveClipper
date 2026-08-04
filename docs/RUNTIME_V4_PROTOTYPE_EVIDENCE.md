# Runtime V4 Prototype Evidence

Status: private cross-machine pilot attempted; signed architecture tests passed; not distributable

Date: 2026-07-31

Branch: `feature/runtime-v4-prototype`

## What Passed

- Deterministic signed business ZIP construction.
- Detached Ed25519 signature and canonical manifest verification.
- File path, exact file set, size, SHA256, version, duplicate ZIP entry,
  traversal, wrong-key, and symlink rejection.
- Tampered entrypoint rejection before Python import.
- Source host loaded the existing FastAPI application from the extracted,
  verified business directory.
- Frozen console host loaded the existing FastAPI application from the
  verified business directory and wrote a Runtime V4 diagnostic.
- The frozen host PYZ did not contain `server`, `ai_clipper`, `cutter_logic`,
  `stt`, or `volcengine_asr`; it did contain the stable verifier,
  `release_signing`, and desktop shell.
- A frozen-host run against a modified `bundle_entry.py` exited with code 1
  before creating its diagnostic result.
- Runtime V3 ignores an inherited external bundle directory even when a
  forged V4 verification environment flag is present.
- A complete core manifest covers the stable host and all core payload files
  with a canonical detached Ed25519 signature, exact file set, size, and
  SHA256 verification.
- `current.json` selects an application version and core version as one pair.
- Pending first launch requires matching core and business manifest digests;
  failure atomically restores the previous verified pair.
- The frozen V4 launcher uses its embedded release public key and ignores a
  deliberately different key placed in the external updater directory.
- Signed business manifests bind each application version to explicit
  compatible core versions.
- The local business updater safely extracts on the install disk, verifies
  before move, atomically changes the selection pair, and supports idempotent
  retry after injected failures.
- A signed V4 channel binds exact source/target/core routes, archive size and
  SHA256, inner business-manifest SHA256, channel state, and HTTPS mirrors.
- Verified downloads support cache reuse, partial resume, bounded mirror
  fallback, secure redirect checks, and exact size/SHA256 enforcement.
- The installer rechecks source application, core, and manifest identities
  under the update lock, so a state race or metadata mismatch cannot activate
  the target.
- Runtime V4 update API routes use the Host-injected update service and do not
  load the legacy V3 updater.
- The frontend reports channel availability, download progress, install state,
  localized failures, and schedules successful activation through the stable
  V4 launcher.
- Business-version and download-cache cleanup protects selected/rollback
  versions, validates owned roots, and rejects symlinks and Windows junctions.
- A signed Runtime V3 manifest can seed a V4 Core with hard links for identical
  files; changed files come from the V4 bridge payload, and the assembled Core
  receives a complete exact-file-set/SHA256 verification before use.

## Prototype Artifacts

These use an ephemeral prototype signing key and must not be distributed.

- Business bundle:
  `C:\LCV4Prototype_20260729\LiveClipperBusiness_2026.7.29.4_phase2.zip`
- Business ZIP SHA256:
  `3d3b4a4fdbaa6fd007e56feeefff90ca5bce7e58c6fe206036ab203382fd545b`
- Business manifest SHA256 used by the final frozen diagnostic:
  `54688c98670cd5d5c9ca24365697b2159067fff5b76fb5f89b684ebb94f8f6f7`
- Business ZIP size: 927,193 bytes; 54 payload files.
- Frozen console host:
  `C:\LCV4Prototype_20260729\host_final_dist\LiveClipperHost`
- Frozen host size: 2,032,229,881 bytes; 8,887 files.
- Frozen diagnostic:
  `C:\LCV4Prototype_20260729\packaged_final_diagnostic.json`

The diagnostic reports:

```text
runtime_layout_version = 4
code_source = bundled
v4_bundle_verified = true
runtime_integrity.ok = true
runtime_integrity.checked = 54
runtime_integrity.mismatched = []
```

## 2026-07-30 Launcher/Core/Updater Prototype

These artifacts also use the ephemeral prototype key and must not be
distributed.

- Update integration root:
  `C:\LCV4Prototype_20260730\update_integration`
- Source business 2026.7.30.2 ZIP SHA256:
  `06d845d821d4b2885ad9b15eebcc61dcff15ab9bf45eaf0b431861ca07c27e4c`
- Target business 2026.7.30.3 ZIP SHA256:
  `873670d36ab8548f665aa595248cf56788946104e52e744405f13a044563b784`
- Target business manifest SHA256:
  `7418d022855cef48c7571aa07b94396c891167a2f22c78109bff9bc34905c9dc`
- Test core manifest SHA256:
  `d7ff7196ac5df455ca18c9f8c61f6f6975776bfa1599f131148a9d48c98b164b`
- Source diagnostic:
  `C:\LCV4Prototype_20260730\update_integration\target-source-diagnostic.json`
- Source diagnostic SHA256:
  `5d964da06a4f6e61d579e51559a218cca4b1c54db4d0fabc21fa5ad044636462`
- Frozen launcher:
  `C:\LCV4Prototype_20260730\launcher_dist4\LiveClipperLauncherV4`
- Frozen launcher EXE SHA256:
  `5905c7507c1665082d5d48d0fe491b2b4008b2856aa9883859c0157f82548576`
- Frozen launcher validation exit code: 0.
- Frozen launcher directory: 44,346,844 bytes and 1,115 files, including the
  two-version integration layout.
- Frozen launcher EXE: 1,941,188 bytes.
- Existing frozen host core manifest: 8,887 payload files and 1,266,914 bytes.
- Full core hashing took more than two minutes on the current machine and is
  therefore restricted to first verification/core changes.
- Confirmed-launch entrypoint verification after path-resolution optimization:
  0.948 seconds on the same 2 GB core.

The target source diagnostic reports layout 4, core 4.0.0, both manifest
digests, `v4_bundle_verified=true`, and runtime integrity `ok=true` for 54
business files.

The real-directory update integration passed:

- atomic activation `2026.7.30.2 -> 2026.7.30.3` with core 4.0.0;
- deliberate host exit produced launcher code 2 and restored 2026.7.30.2;
- retry reused the already installed matching 2026.7.30.3 directory;
- retry restored `pending=true` with zero staging entries.

## Tests

- The final 2026-07-31 broad source run passed 399 tests across 39 modules; four
  permission/environment cases were skipped.
- The two FC authorization deployment-only modules remain excluded because
  this independent V4 worktree deliberately does not contain
  `deploy/aliyun_fc_license_auth`.
- The Host bridge, signed-channel, transaction, retention, restart, and UI
  contract-focused run passed 26 tests.
- An earlier expanded Core, bundle, migration, launcher, update, channel, and
  Host bridge run passed 55 tests; two symlink cases were skipped because the
  current Windows session lacks symlink creation permission. Those paths are
  included in the final 399-test run.
- ZIP symlink rejection remains covered without requiring that permission.
- The packaged frozen diagnostic passed with exit code 0.
- The packaged tamper diagnostic failed closed with exit code 1.

## 2026-07-31 Business Baseline Sync

The current 30.1 business changes were merged into the V4 business layer while
retaining the V4 verified-bundle server bootstrap and health identity fields.
The merged source includes output naming modes, parameter-card ordering,
head-and-bottom crop protection, transient video inspection retry, preview
scroll retention, invocation-local smart/mix preview result caches, and shared
media-pipeline exclusion. A focused combined run passed 104 tests before the
broader 383-test run.

After the Host update bridge and frontend changes, two current-source business
bundles built with an ephemeral test key were byte-for-byte identical and both
verified successfully:

- Business ZIP SHA256:
  `b10401ba88cda3a5a5bba4a59375e8fa218a2cd20169caa36cf7305ef4bac587`
- Business manifest SHA256:
  `ae9109948bc696c0a2dd4453e197cd6c2077b64e2f3c5f1cb1a6248dbfe2882d`
- ZIP size: 931,014 bytes; 54 payload files.

The temporary private/public keys and ZIPs were deleted after verification.

## 2026-07-31 V3-to-V4 Core Reuse Measurement

The signed 2026.7.27.5 V3 full-baseline manifest and the prototype V4 Core
manifest matched on 8,884 of 8,887 target files and 1,935,787,352 of
2,032,229,881 bytes. The calculated bridge payload was 3 files and 96,442,529
bytes, so 95.25% of the Core bytes can be reused without another physical copy.

Five migration-specific tests passed for hard-link reuse, full assembled-Core
verification, source tamper, bridge-payload tamper, wrong key/version, cleanup,
and existing-destination protection. Eight additional signed-package and
transaction tests passed for launcher/business tamper, wrong key, version
collision, successful root/state activation, health failure rollback, injected
post-state failure rollback, and post-confirmation V3 cleanup.

The final combined migration run passed 16 tests. It additionally covered
abrupt termination with pending V4 state, automatic V3 restore before retry,
termination after health confirmation, confirmed-state adoption, and the
frozen migrator's embedded-key/no-key-override contract.

## Deliberately Not Complete

This is not a V4 release package. A private test package has been launched on
another computer, including one fail-closed start caused by legacy run logs
inside the signed business directory. That case is now repaired by moving only
recognized legacy logs to the user-data recovery directory before exact-file-set
verification. The following remain before any public pilot or release:

- publication of the real signed V4 channel; the checked-in source list now
  targets the verified Pages custom domain and fallback, but neither endpoint
  currently contains a signed channel;
- production-key core and business signatures;
- rebuilt Runtime V4 core 4.0.0 frozen-host validation with the Host update
  service and source configuration;
- production release key signing instead of the prototype key;
- repeatable GUI host packaging and native Explorer drag-drop acceptance on a clean machine;
- packaged local SenseVoice, smart cut, mix, subtitle, words sidecar, and cache
  workflow acceptance;
- a built and inspected frozen V3-to-V4 migrator, Authenticode integration,
  and real-package user-data/disk-allocation tests;
- release policy, channel, and full-baseline promotion evidence.

Runtime V3 remains the user-facing architecture. Private V4 test artifacts are
not a release and no V4 channel has been promoted from this worktree.
