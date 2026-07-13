# LiveClipper Release Process V3

## 1. Release artifacts

Every Runtime V3 release produces:

- a signed immutable runtime manifest;
- a V3 full package for clean installation and repair;
- one signed direct patch for every supported source version;
- a signed stable-channel manifest;
- SHA256 sidecars for the full package, patches, and bridge executable;
- a one-time embedded bridge when a supported source is Runtime V2.

The full package may be distributed through an external file service. Automatic
patch URLs must be stable direct HTTPS downloads; an interactive share page is
not a valid automatic-update URL.

## 2. Key handling

- Generate one dedicated Ed25519 release key pair.
- Commit only app/release_update_public_key.pem.
- Keep the private key outside the repository and release directories.
- Back up the private key securely; losing it prevents authenticated updates.
- A key rotation requires a release signed by the old key that installs the new
  trust root before releases signed only by the new key are published.

## 3. Build order

1. Confirm a clean tracked worktree and review release inputs.
2. Run Python compilation, frontend syntax checks, and unit tests.
3. Generate app/version.json with tools/build_update_manifest.py.
4. Build the frozen business runtime with web_client/liveclipper_web.spec.
5. For a normal V3 runtime release, extract and reuse the exact launcher,
   updater, and update public key from the supported source package. Rebuilding
   unchanged one-file executables changes their hashes and would turn them into
   unsafe self-update payloads.
6. Build new launcher/updater binaries only for a separately tested signed
   bridge or full-package stable-layer migration.
7. Assemble the V3 directory with tools/build_v3_package.py.
8. Run the release security audit and verify no loose business Python files.
9. ZIP the assembled package and run zipfile.testzip().
10. Build direct patches with tools/build_delta_package.py. A V3-to-V3 patch
    must contain zero stable-component payload files.
11. For a V2 source, build the embedded bridge with tools/build_bridge_exe.py.
12. Exercise bridge, normal delta, interruption, tamper, and rollback tests.
13. Run one patch with the updater executable from the exact published source
    ZIP, not only the Python-level updater tests.
14. Upload package and patch assets, then verify their remote hashes.
15. Generate and sign release/stable.json.
16. Publish the signed channel last.

Publishing the channel before all referenced assets are remotely verifiable is
forbidden.

## 4. Package acceptance gates

A release is accepted only when:

- the root executable is the stable launcher;
- current.json points to an existing signed version directory;
- the runtime contains no loose business Python duplicates;
- runtime and install signatures verify with the committed public key;
- every runtime and stable file matches its signed manifest;
- a clean extracted package starts and reports runtime_layout_version=3;
- /api/runtime reports the expected active directory and version;
- polluted legacy AppData code directories remain ignored;
- an incorrect source version is rejected before activation;
- a corrupt payload or signature is rejected before activation;
- interruption before pointer switch leaves the current version untouched;
- failed first-launch health switches back to the previous version;
- one failed batch item still allows later batch items to continue.

## 5. V2 bridge acceptance gates

The bridge must be tested against the exact published V2 ZIP. It must:

- reject modified or unsupported V2 installations;
- avoid downloading unchanged runtime files;
- preserve a signed previous V2 runtime directory;
- produce and verify the target V3 runtime directory;
- install and verify the signed stable install manifest;
- keep the root executable path stable for existing shortcuts;
- leave user data and configured output/material paths unchanged;
- support automatic rollback to the retained V2 runtime.

## 6. Rollout

Use staged release-channel exposure when a new updater or launcher is involved:

1. internal package smoke test;
2. a small controlled device set;
3. broader rollout after update and rollback logs are clean;
4. full stable rollout.

Keep direct patches from at least the previous two stable versions when their
runtime layouts are compatible. Older, damaged, or unsupported installations
fall back to the full package.

## 7. Rollback

Do not lower the remote version. For a runtime regression:

- let affected pending launches roll back automatically;
- stop channel rollout;
- fix the code and publish a higher version;
- retain the previous signed runtime until the replacement is healthy.

User-data migrations remain backward compatible for one retained release or
provide their own transactional backup and restore procedure.
