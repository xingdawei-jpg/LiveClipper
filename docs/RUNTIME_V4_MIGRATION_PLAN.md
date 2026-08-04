# LiveClipper Runtime V4 Migration Plan

Status: planning only

Date: 2026-07-27

## 1. Objective

Runtime V4 separates the rarely changing desktop foundation from frequently
changing business code.

The practical goal is to make normal updates to AI selection, mixing, ASR,
subtitles, frontend, rules, and editing logic small signed patches. A normal
feature change must not require rebuilding or replacing the large
LiveClipperWeb.exe and its ML/native dependency tree.

This is a packaging and runtime-loading migration. It is not a rewrite of the
AI selection, mixing, subtitle, authorization, cache, or user-data features.

## 2. Problems To Solve

The current Runtime V3 versioned-directory and rollback model is sound, but
the frozen business runtime contains almost all Python code in the executable
PYZ archive. A small business-code change therefore changes the large runtime
EXE and frequently makes the patch exceed the automatic-update budget.

The desired operating model is:

1. New users still receive one verified full package.
2. Existing V4 users receive small verified patches for ordinary business work.
3. A failed update always starts the previously confirmed version.
4. User data never becomes a code-import path.
5. A modified business package must be rejected before any business code is
   imported.

## 3. Chosen Architecture

### 3.1 Stable Core

The V4 full baseline contains a frozen, rarely changed core:

    LiveClipperWeb/
      LiveClipperWeb.exe                 stable launcher
      updater/
        LiveClipperUpdater.exe
        release_update_public_key.pem
      core/
        4.0.0/
          LiveClipperHost.exe            frozen desktop/WebView2 host
          _internal/                     Python, WebView2, ffmpeg, ML/native deps
          core_manifest.json
          core_manifest.sig
      versions/
      current.json
      install_manifest.json

The core owns:

- version selection, startup health, rollback, and update transaction;
- release public key and signature verification;
- WebView2 shell, native Explorer CF_HDROP bridge, and local HTTP startup;
- Python runtime, ffmpeg/ffprobe, fixed WebView2, and installed ML/native
  dependencies;
- a minimal bundle loader.

Changing the core requires a new full baseline. Examples include Python ABI,
PyInstaller, launcher/updater, public key/trust policy, fixed WebView2,
ffmpeg, native bridge, Torch/FunASR/ModelScope runtime, or a new native ML
dependency.

### 3.2 Signed Business Bundle

Each product version contains only business code and web assets:

    versions/
      2026.8.x/
        runtime_manifest.json
        runtime_manifest.sig
        business/
          bundle_manifest.json
          bundle_manifest.sig
          app/
          web_client/
            frontend/
          bundle_entry.py

The delivery artifact may use a ZIP for transport, but the updater extracts it
to the immutable version directory before activation. The host verifies the
bundle manifest, signature, file paths, and SHA256 values before adding this
directory to the Python import path or exposing its frontend files.

The business bundle owns:

- AI selection and candidate rules;
- smart cut, mix, subtitle, ASR adapters, and cache policy;
- FastAPI feature routes and task orchestration;
- frontend HTML, JavaScript, CSS, and feature-specific assets;
- non-secret default rules and keyword defaults.

The host must expose the business bundle through a narrow entrypoint named
bundle_entry.create_application(context). Business code must not select its
own bundle path or bypass the verified context.

### 3.3 Security Boundary

V4 must not use noarchive=True as a shortcut. Merely externalizing Python
files would make them easy to replace before import.

The required boundary is:

1. The public key remains frozen in the stable core.
2. The updater verifies signed target manifests before staging a version.
3. The host verifies the signed business manifest and every listed file before
   importing bundle_entry.
4. Failed verification starts the previous confirmed version; it never falls
   back to an unverified bundle.
5. The bundle path is constrained to the selected immutable version directory.
6. AppData, temp directories, cache directories, and download directories are
   never Python import roots.

The small business bundle can be hashed on every startup. This is deliberate:
it removes the risk of loading changed external code without a trusted
verification step.

## 4. Update And Rollback Model

V4 keeps the useful Runtime V3 properties:

- immutable version directories;
- signed source and target manifests;
- source-specific patch graph;
- atomic current.json activation;
- first-launch health confirmation;
- previous-version rollback;
- hard links for unchanged files on the same volume.

For a normal business update, the updater creates a new version directory,
hard-links unchanged core-compatible files, writes the verified business
bundle, verifies it, then switches current.json once. The active version is
never patched in place.

The runtime endpoint must report at least:

- runtime_layout_version 4;
- active application version and core version;
- active bundle path, bundle SHA256, and signature-verification status;
- code_source bundled;
- current/previous version and first-launch health state.

## 5. Migration Strategy

V3 clients cannot safely receive a V4 core/layout migration through the normal
in-app delta path. V4 therefore starts as a new signed full baseline and is
distributed through the full-package channel.

Migration rules:

1. Do not overwrite the delivered V3 directory.
2. Install V4 to a new clean directory and retain the V3 installation as a
   manual fallback until V4 acceptance is complete.
3. Reuse the existing user-data directory only through a versioned,
   backward-compatible data schema.
4. Preserve license, settings, ASR configuration, media paths, output paths,
   cache configuration, and update logs.
5. Back up a data-schema snapshot before an irreversible migration.
6. Keep V3 maintenance patches possible while V4 is in pilot; do not block
   urgent user fixes on this migration.

## 6. Delivery Phases

### Phase 0: Freeze The Contract

Deliverables:

- V4 manifest schemas and protected-path policy;
- explicit core versus business ownership table;
- V4 bundle allowlist;
- updated release-policy design and acceptance matrix.

Exit gate:

- one written architecture decision is approved;
- no V3 packaging code is changed;
- the exact V3-to-V4 full-baseline migration path is documented.

Estimate: 1 to 2 engineering days.

### Phase 1: Verified Bundle Prototype

Deliverables:

- tools/build_business_bundle.py;
- deterministic bundle manifest and Ed25519 signature;
- standalone verifier CLI;
- tamper, missing-file, path-traversal, stale-manifest, and wrong-key tests.

Exit gate:

- a tiny sample bundle is built twice with stable file hashes;
- verification succeeds for the original bundle and rejects every tampered
  fixture before import.

Estimate: 2 to 3 engineering days.

### Phase 2: Frozen Host And Loader

Deliverables:

- minimal frozen host entrypoint;
- verified bundle loader and restricted import context;
- business bundle entrypoint that initially adapts the existing server and
  frontend with minimal behavior changes;
- development-mode bundle workflow that is explicitly marked as development
  and cannot be used by a packaged production runtime.

Exit gate:

- source mode and frozen-host mode start the same smart-cut and mix workflows;
- bundle integrity is checked before application import;
- WebView2 native file drag and in-page pointer sorting both work in the
  frozen host.

Estimate: 4 to 6 engineering days.

### Phase 3: V4 Updater And Version Activation

Deliverables:

- V4 runtime-manifest builder and delta builder;
- updater support for business bundle payloads;
- V4 runtime diagnostics and rollback reporting;
- V3 full-baseline migration installer/checklist.

Exit gate:

- update from V4 baseline to a changed frontend/business bundle without
  replacing LiveClipperHost.exe;
- simulate interrupted download, invalid signature, disk-full staging, failed
  first launch, and successful rollback;
- user data remains intact.

Estimate: 3 to 5 engineering days.

### Phase 4: Release Pipeline And Acceptance

Deliverables:

- V4 release runbook and candidate evidence files;
- patch-size and protected-file gates;
- clean-install, package-extraction, and packaged-runtime test scripts;
- a compact desktop acceptance checklist.

Exit gate:

- a real frontend-only/business-only update is below the automatic-update
  budget and does not contain the host EXE or ML/native runtime;
- an extracted full package and an installed delta both report code_source
  bundled with valid bundle verification;
- automatic update, rollback, license, ASR configuration, smart cut, mix,
  words sidecar generation, selected-clip ordering, and cache cleanup pass the
  matrix.

Estimate: 3 to 4 engineering days plus pilot feedback.

### Phase 5: Pilot And Promotion

Start with internal machines and a small set of users who can report runtime
diagnostics. Keep the V4 release channel on hold until every acceptance item
is recorded. Promote only the exact accepted candidate; do not rebuild or
replace it after acceptance.

Estimated total: 13 to 20 engineering days, normally about three calendar
weeks including packaged desktop and pilot verification.

## 7. First Implementation Slice

The first coding slice is intentionally small:

1. Add the V4 bundle manifest schema and verifier.
2. Add a deterministic business-bundle builder with a strict allowlist.
3. Package one harmless test entrypoint through the new loader.
4. Prove signature verification and rejection behavior.

Do not move AI selection, ASR, mixing, license logic, or the updater until
this slice is verified. This prevents the architecture migration from becoming
another broad product rewrite.

## 8. Success Metrics

The migration is successful only when:

- a frontend or business-Python change does not change the host EXE;
- a representative business patch is less than 10 MiB compressed;
- bundle tampering is rejected before any business import;
- V4 rollback returns to a known-good business bundle automatically;
- existing user data is preserved across V3-to-V4 installation;
- packaged desktop validation, not source-only validation, is the release
  acceptance source.

## 9. Deferred Work

These are valuable but are not prerequisites for V4:

- splitting the large frontend app.js into feature modules;
- splitting the monolithic FastAPI server into domain services;
- redesigning the AI candidate workbench;
- a general cache-management rewrite;
- account, payment, and authorization product changes.

They should be done later as ordinary V4 business-bundle changes, not mixed
into the runtime migration.
