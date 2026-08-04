# Runtime V4 Phase 1 Contract

Status: prototype only, not a user release

Date: 2026-07-29

## Scope

Phase 1 proves that frequently changing LiveClipper business code can be
delivered separately from the frozen desktop and ML runtime without loading
unverified external Python.

This phase does not modify Runtime V3, the production launcher, the updater,
the update channel, or user data.

## Trust Boundary

The stable host owns:

- the Ed25519 release public key;
- ZIP path validation and safe extraction;
- canonical manifest and detached-signature verification;
- file-set, size, and SHA256 verification;
- the only code path that may add a business directory to `sys.path`;
- desktop/WebView2, native drag-drop, ffmpeg, Python, and ML dependencies.

The signed business bundle may own:

- `app` business Python modules except release and license trust roots;
- `web_client/server.py` and feature workers;
- frontend HTML, JavaScript, CSS, and icons;
- `bundle_entry.py:create_application(context)`.

The bundle may not own the desktop host, native drag-drop bridge, updater,
release signing code or keys, license verification public key, PyInstaller
specifications, caches, temporary files, or user data.

## On-Disk Bundle

```text
business/
  bundle_manifest.json
  bundle_manifest.sig
  bundle_entry.py
  app/
  web_client/
```

The manifest and detached signature use canonical UTF-8 JSON. Every payload
file must be listed exactly once. Extra files, missing files, symlinks,
non-canonical paths, path traversal, wrong signatures, wrong versions, and
digest mismatches are rejected before `bundle_entry.py` is imported.

## Phase 1 Exit Gate

- Two builds from the same source and key are byte-for-byte identical.
- The standalone verifier accepts the original archive.
- Tampered, missing, extra, traversal, duplicate, stale-version, and wrong-key
  cases are rejected.
- A tampered entrypoint cannot execute before rejection.
- No Runtime V3 production file or release channel is changed.

Passing this gate permits Phase 2 host adaptation. It does not permit a V4
user release.
