# Runtime V4 Launcher Contract

Status: launcher, rollback, and Host-owned update restart source integration passed; not a user release

Date: 2026-07-30

## Installation Layout

```text
LiveClipperWeb/
  LiveClipperWeb.exe
  current.json
  core/
    4.0.0/
      LiveClipperHost.exe
      _internal/
      core_manifest.json
      core_manifest.sig
  versions/
    2026.7.30.1/
      business/
        bundle_manifest.json
        bundle_manifest.sig
        ...
```

The production root launcher keeps the historical `LiveClipperWeb.exe` name so
existing shortcuts continue to work. `LiveClipperLauncherV4.exe` is accepted
only as a prototype compatibility name during development.

The launcher owns the embedded Ed25519 release public key. A frozen launcher
must not trust an adjacent or updater-provided replacement key. Source tests
may use an explicit installation key.

## Selection State

`current.json` stores the core and application as one selection pair:

```json
{
  "schema_version": 1,
  "runtime_layout_version": 4,
  "current": {
    "application_version": "2026.7.30.1",
    "core_version": "4.0.0"
  },
  "previous": {
    "application_version": "2026.7.29.4",
    "core_version": "4.0.0"
  },
  "pending": true,
  "verified_cores": {
    "4.0.0": {
      "verification_mode": "full",
      "manifest_sha256": "...",
      "metadata_sha256": "..."
    }
  }
}
```

The launcher never composes a core from one selection with a business bundle
from another selection. State changes use flush, `fsync`, and atomic replace.

## Full Verification Gate

On first installation, a core change, a missing verification receipt, or an
explicit validation run, the launcher verifies:

- canonical detached signatures for the complete core and business manifests;
- exact version identity for both layers;
- every declared file's size and SHA256;
- exact file sets with no extras, missing files, traversal, or symlinks;
- the core entrypoint is declared by the signed core manifest.

Only then does it pass the verified core and business manifest digests to the
host process. The host independently verifies the business directory again
before importing business Python.

## Immutable Business Files And Mutable User Data

The signed `versions/<application>/business` directory is immutable. Cut run
reports are written under the configured user-data root at `logs/runs`, and
feedback backups are written under `feedback`; neither path is eligible for a
business archive.

Early V4 test bundles wrote cut reports to `business/app/logs`. During startup,
the launcher and Host may remove only unlisted files whose direct path and name
match the historical cut-report format
`app/logs/YYYYMMDD_HHMMSS_<source>_<status>.json`. Signed files, nested paths,
symlinks, unknown JSON files, Python files, and every other unlisted artifact
remain fatal verification errors.

## Confirmed-Launch Fast Gate

The frozen core contains about 2 GB and 8,887 files. Blocking every normal
launch on a complete SHA256 pass is not acceptable. After one full verification
has produced a receipt bound to the signed core manifest, normal launch checks:

- the embedded release public key and detached core signature;
- the selected core and application versions;
- all signed manifest paths and metadata structure;
- the core entrypoint's existence, size, and SHA256;
- the complete signed business bundle;
- a fresh runtime health receipt for every process launch.

Any missing full-verification receipt forces a complete core verification.
Internal core corruption that prevents startup fails the fresh health check and
rolls back the selection. Protection against a local administrator deliberately
modifying internal files while preserving runtime health requires Authenticode
or an equivalent operating-system trust layer and remains a production gate.

## First-Launch Health And Rollback

Every launched selection must report all of the following. A pending selection
is confirmed only after the report succeeds:

- one-time launcher token and application version;
- runtime layout 4;
- expected core version and core manifest SHA256;
- expected business manifest SHA256;
- successful runtime integrity.

Failure, timeout, early process exit, or identity mismatch terminates the
pending host, atomically restores the previous selection pair, and launches
that verified previous selection. An invalid current selection is rejected
before execution and follows the same rollback path.

## Remaining Production Gates

- publish the real signed update channel to the configured frozen Host sources;
- production-key core and business signing;
- Authenticode signing or an explicitly approved local-tamper threat model;
- rebuild the 4.0.0 frozen Host with the update bridge and source config;
- built/frozen V3-to-V4 migrator and packaged user-data preservation
  acceptance;
- GUI, native drag-drop, SenseVoice, smart cut, mix, subtitle, words sidecar,
  cache, and shutdown acceptance.
