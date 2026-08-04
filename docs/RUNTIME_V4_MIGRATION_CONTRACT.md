# Runtime V4 Migration Contract

Status: signed package, hard-link Core reuse, root/state activation, first-health rollback, interruption recovery, and optional legacy cleanup passed in source tests; frozen migrator build and packaged acceptance pending

Date: 2026-07-31

## Objective

Move an existing verified Runtime V3 installation to Runtime V4 without
downloading or physically copying another complete ML/native runtime. After a
successful migration and legacy cleanup, the installation contains one shared
`core/<core-version>` directory and small signed business versions.

The production root launcher keeps the existing `LiveClipperWeb.exe` name so
desktop shortcuts continue to work. The internal desktop process is
`core/<core-version>/LiveClipperHost.exe`.

## Trust Inputs

The migration process accepts only:

- a Runtime V3 `current.json` selecting a safe numeric version;
- that version's Ed25519-signed Runtime V3 `runtime_manifest.json`;
- a detached Ed25519-signed Runtime V4 Core manifest;
- Core bridge payload files whose size and SHA256 are declared by that V4
  manifest;
- the frozen migration tool's release public key.

Neither the unsigned local state nor the informational bridge plan can add a
file, change a target hash, or choose an arbitrary executable.

## Core Reuse Algorithm

1. Verify the selected V3 manifest and validate every declared path, size, and
   SHA256 field.
2. Verify the detached V4 Core manifest before creating any target file.
3. For every V4 Core file, compare the signed V3 and V4 metadata.
4. When path, size, and SHA256 are identical, create an NTFS hard link from the
   V3 runtime into the new Core.
5. Otherwise require the file in the bridge payload and verify its size and
   SHA256 before copying.
6. Perform a complete exact-file-set and SHA256 verification of the assembled
   Core.
7. On any failure, remove only the newly created Core destination. The V3
   source and its root launcher/state remain unchanged.

There is deliberately no copy fallback when an expected hard link cannot be
created. That machine must use a clean V4 full baseline instead of silently
consuming another 2 GB.

## Measured Feasibility

The signed Runtime V3 2026.7.27.5 full-baseline manifest was compared with the
2026-07-29 prototype V4 Core manifest:

- target files: 8,887;
- reusable files: 8,884 (99.97%);
- target bytes: 2,032,229,881;
- reusable bytes: 1,935,787,352 (95.25%);
- bridge payload: 3 files, 96,442,529 bytes.

The three changed files were `LiveClipperHost.exe`,
`_internal/base_library.zip`, and
`_internal/core_keys/license_public_key.txt`. The final production Core must be
rebuilt and measured again; these numbers prove feasibility but are not a
release-size promise.

Hard links share physical disk allocation even though Explorer may temporarily
count both directory entries in its logical folder-size total. The old V3
directory must not be removed until the first V4 health confirmation succeeds.

## Source Activation Transaction

The source-level external transaction now performs:

1. Stop and verify all LiveClipper processes belonging to the installation.
2. Assemble and fully verify the V4 Core in staging.
3. Install and verify the initial signed business bundle under a new
   application version that does not collide with a V3 version directory.
4. Verify the production root launcher from a signed/embedded payload.
5. Preserve the V3 root launcher and state as transaction rollback material.
6. Atomically select the V4 Core/business pair and replace the root launcher.
7. Start V4 and require the exact Core/business health receipt.
8. Restore V3 root files and state on failure.
9. After successful confirmation, remove only verified legacy program-owned
   version directories; preserve AppData and unknown user files.

Legacy cleanup is explicit. A directory is removed only when its V3 manifest
signature, layout, and version are valid and its actual file set contains no
unknown files. Otherwise it is preserved and reported.

## Remaining Migration Gates

- build and inspect the external migrator from its PyInstaller specification;
  the frozen entrypoint has no public-key override and reads only its embedded
  release key;
- bind the production migration EXE to the release Authenticode workflow;
- rebuild the current V4 Core and build source-specific bridges for every
  supported V3 version;
- run a real extracted-package migration, packaged WebView2 health check,
  rollback, AppData preservation, and disk-allocation measurement.

Until these gates pass, the migration tools are release-engineering prototypes,
not an end-user update.

## Source And Tests

- `runtime_v4/migration.py` verifies V3, plans reuse, builds bridge payloads,
  and assembles/fully verifies the Core.
- `tools/build_v4_migration_bridge.py` builds a source-specific bridge.
- `runtime_v4/migration_package.py` binds source version, root launcher, Core,
  and initial business archive in one signed migration manifest.
- `runtime_v4/migration_transaction.py` performs the external switch, health
  confirmation, V3 restore, interruption recovery, and optional exact-file
  legacy cleanup.
- `runtime_v4/migrator.py` and `liveclipper_migrator_v4.spec` define the
  embedded-key external migrator; the frozen artifact is not built yet.
- `tools/build_v4_migration_package.py` and `tools/migrate_v3_to_v4.py` are
  source-level release-engineering CLIs.
- `tests/test_runtime_v4_migration.py` covers reuse, hard-link identity,
  tampered V3 files, tampered bridge payloads, wrong keys/versions, and an
  existing destination.

The combined migration run passed 16 tests, including two abrupt-termination
cases: unconfirmed V4 state is restored to V3 before retry, while an already
confirmed V4 health state is adopted without an incorrect rollback.
