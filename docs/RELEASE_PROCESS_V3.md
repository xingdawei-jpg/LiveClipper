# LiveClipper Release Process V3

## 1. Release artifacts

Every Runtime V3 release produces:

- a signed immutable runtime manifest;
- a V3 full package for clean installation and repair;
- one signed direct patch for every supported source version;
- a signed stable-channel manifest;
- SHA256 sidecars for the full package, patches, and bridge executable;
- a one-time embedded bridge when a supported source is Runtime V2.

The full package may be distributed through an external file service. Every
automatic patch must have at least two stable direct HTTPS downloads in the
signed channel: GitHub primary and Aliyun OSS fallback. An interactive share
page is not a valid automatic-update URL.

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
    ZIP, not only the Python-level updater tests. Record elapsed time, durable
    progress, final pointer state, and first-launch health.
14. Reject a release if the reference-device patch exceeds 60 seconds, shows no
    visible progress, or rereads unchanged hard-linked runtime files.
15. Upload patch assets to GitHub and Aliyun OSS. Verify exact size and SHA256
    from both direct HTTPS URLs.
16. Test DNS resolution, download progress, interruption/resume, and source
    fallback from a separate Windows device and network.
17. Generate and sign release/stable.json with channel_status=hold. A hold
    manifest contains no published patch records.
18. Run the clean-install and exact published-source delta acceptance tests.
19. Regenerate the signed manifest with channel_status=ready and publish it
    last.

Publishing a ready channel before every referenced source is remotely
verifiable is forbidden.

## 4. Package acceptance gates

A release is accepted only when:

- the root executable is the stable launcher;
- current.json points to an existing signed version directory;
- the runtime contains no loose business Python duplicates;
- runtime and install signatures verify with the committed public key;
- every runtime and stable file matches its signed manifest;
- every omitted runtime payload has identical signed source/target metadata;
- changed payloads and copy fallbacks are hash-verified;
- the installed updater meets the channel minimum updater version;
- the runtime remains open until the patch has passed size and SHA256 checks;
- the in-app card shows byte and percentage progress during the download;
- an interrupted download preserves its partial file and resumes with Range;
- failure of the GitHub source automatically falls back to Aliyun OSS;
- update progress remains visible from process exit through pointer activation;
- partial stable-file replacement restores every completed replacement;
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

Keep channel_status=hold throughout build and acceptance. Use staged
release-channel exposure when a new updater or launcher is involved:

1. internal package smoke test;
2. a small controlled device set;
3. broader rollout after update and rollback logs are clean;
4. full stable rollout.

Keep direct patches from at least the previous two stable versions when their
runtime layouts are compatible. Older, damaged, or unsupported installations
fall back to the full package.

## 7. Full-package baseline policy

A new full-package baseline is mandatory when the launcher, updater, release
trust root, runtime layout, or install-state format changes. This is not the
normal release path.

The baseline sequence is:

1. bump the stable component versions from their single source;
2. rebuild both stable executables and the frozen business runtime;
3. assemble and sign a clean V3 full package;
4. test clean install, polluted AppData, health confirmation, and rollback;
5. build a synthetic next-version runtime delta from that exact package;
6. prove the normal delta has zero stable payload files;
7. apply it with the updater executable from the exact baseline package;
8. keep the public channel on hold until a separate device passes.

After the baseline is accepted, normal business releases reuse the exact stable
launcher and updater from that baseline. They publish signed direct runtime
deltas. Users do not need another full package unless the stable layer changes
again or their installation is damaged or unsupported.

## 8. Rollback

Do not lower the remote version. For a runtime regression:

- let affected pending launches roll back automatically;
- stop channel rollout;
- fix the code and publish a higher version;
- retain the previous signed runtime until the replacement is healthy.

User-data migrations remain backward compatible for one retained release or
provide their own transactional backup and restore procedure.

When the launcher or updater binary changes, publish a new full package and make
that package the next incremental baseline. Do not attempt to replace a running
stable updater through a normal V3-to-V3 delta.
