# Runtime V4 Business Update Contract

Status: signed channel, verified download, Host bridge, update UI, and retention source integration passed; packaged acceptance pending

Date: 2026-07-31

## Scope

Runtime V4 business updates install a complete signed business bundle without
replacing the frozen launcher, frozen host, Python runtime, FFmpeg, WebView2,
or ML dependencies. The selected core version remains unchanged.

Every business manifest declares an explicit `compatible_core_versions` list.
A correctly signed bundle is still rejected when the selected core is not in
that list.

## Signed Channel

The stable V4 host verifies `liveclipper-runtime-v4-business-channel-v1`
metadata with its embedded Ed25519 release public key. The signature binds:

- channel state (`ready`, `hold`, `paused`, or `disabled`);
- exact target and allowed source application versions;
- compatible core versions;
- business ZIP filename, byte size, SHA256, and inner manifest SHA256;
- ordered credential-free HTTPS download sources;
- release notes and publication time.

Unknown fields, duplicate sources or versions, unsafe URLs, future/equal source
versions, unsupported schemas, wrong keys, or modified documents fail closed.
Only `ready` channels can produce an update decision.

The frozen Host owns `core_config/runtime_v4_update_sources.json`. Business
code receives only a narrow update-service object and cannot replace the
channel URL list, release public key, installation root, or download root.
The checked-in source list contains the verified Cloudflare Pages custom domain
and its `pages.dev` fallback. Neither endpoint can offer an update until a
correctly signed V4 channel is uploaded; a missing, unsigned, or malformed
channel still fails closed.

## Host Bridge And UI

- Runtime V4 `/api/update/check` and `/api/update/apply` use the Host-injected
  service and never load the legacy V3 updater.
- Check results, download progress, verification, installation, and localized
  failures are passed through the existing frontend status surface.
- After a successful activation, the Host schedules a restart through the
  stable root `LiveClipperWeb.exe`; it does not restart the old Host binary
  directly.
- If the Host service or signed channel source is unavailable, the API reports
  a non-installable state and does not fall back to V3 update behavior.

## Verified Download

- Channel metadata is fetched only over HTTPS and HTTPS redirects.
- Business downloads support a verified local cache, `.part` resume, bounded
  retries, and ordered mirror fallback.
- The signed byte count is enforced while streaming. The final file must match
  both signed size and SHA256 before installation begins.
- Partial files survive transient network failures, but oversized or
  full-length digest-mismatched files are removed.
- Download errors do not expose signed source URLs in user-facing summaries.

## Installation Transaction

1. Acquire the cross-process V4 update lock.
2. Read and validate the current core/application selection pair.
3. Check free space using the declared uncompressed ZIP size plus margin.
4. Extract to a unique `.v4-staging/business-*` directory on the install disk.
5. Reject traversal, duplicate paths, symlinks, encrypted entries, excessive
   entry counts, oversized expansion, unsafe compression ratio, signature,
   version, compatibility, exact-file-set, size, or SHA256 failures.
6. Move the verified `business` directory atomically to
   `versions/<application-version>/business`.
7. Re-verify the installed directory.
8. Atomically switch `current.json` to the new application/core pair, preserve
   the old pair as `previous`, and set `pending=true`.
9. Let the stable launcher confirm fresh runtime health or roll back the pair.

The installer rechecks the expected current application, core, and target
business-manifest digest while holding the update lock. A state change during
download therefore fails before target activation.

The Host update session also holds a separate cross-process lock across channel
fetch, download, installation, and cleanup. Concurrent processes therefore
cannot race between a successful check and pointer activation.

The archive is verified even when the target version already exists. Retry is
accepted only when the requested archive manifest digest exactly matches the
installed target digest. Reusing a version number for different content is
rejected.

## Retention And Cleanup

- Current, previous, and failed-selection business versions are protected.
- One additional recent unreferenced business version is retained; older
  owned business-only version directories can be removed.
- The download cache keeps two recent version directories and removes stale
  partial files after 14 days.
- Cleanup requires an ownership marker, validates that targets remain inside
  the expected roots, and rejects symlinks and Windows junctions.

## Failure Recovery

- Failure before directory move leaves `current.json` unchanged.
- Failure after move leaves an immutable orphan version; retry verifies and
  reuses it without copying again.
- Failure after pointer commit is idempotent; retry reports the version already
  installed/selected.
- Startup failure after activation restores the previous pair through the V4
  launcher.
- Staging directories owned by completed or failed transactions are removed.
- Insufficient disk space and concurrent update attempts fail before pointer
  mutation.

## Remaining Production Gates

- publish the real signed V4 channel to the configured Host sources;
- clean V3-to-V4 bootstrap/full-baseline migration;
- production signing key and Authenticode workflow;
- rebuild the frozen V4 core with this Host service and source configuration;
- packaged GUI and feature acceptance after an actual business-only update.

## Release Tools

- `tools/build_v4_update_channel.py` builds and signs a channel only after the
  referenced business archive verifies with the same key.
- `tools/verify_v4_update_channel.py` verifies a local channel and can evaluate
  an exact application/core update route.
- `tools/apply_v4_business_channel.py` is a source-level/manual integration
  harness. The Host bridge is the user-facing update path, but still requires a
  rebuilt frozen core and packaged acceptance before release.
