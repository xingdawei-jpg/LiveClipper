# LiveClipper Architecture V2

## 1. Architecture goals

1. A running process has exactly one program-code source.
2. Program files and user data never share an update lifecycle.
3. The displayed version is the version of the code that is actually running.
4. A release is either fully verified and activated or not activated at all.
5. Business modules can be split gradually without rewriting the editing pipeline.

## 2. Runtime source of truth

### Source mode

- Program code: repository `app/` and `web_client/`.
- User data: `%APPDATA%\LiveClipper` or the configured custom data directory.
- AppData `app/`, `web_client/`, and `tools/` directories are ignored.

### Packaged mode

- Python program code: frozen into `LiveClipperWeb.exe`.
- Runtime resources: `_internal/app` contains JSON, public keys, and other data files only.
- Frontend resources: `_internal/web_client/frontend`.
- Executable helper scripts: `_internal/tools`.
- User data: `%APPDATA%\LiveClipper` or the configured custom data directory.

The package must not contain loose copies of business `.py` files. This prevents a frozen module, a loose package module, and an AppData module from competing at import time.

## 3. User-data boundary

AppData may contain:

- settings and AI provider configuration;
- user keyword libraries and editing profiles;
- license tokens and device state;
- subtitle, preview, feedback, task, and log caches;
- live-recording state and user-selected output paths.

AppData must not contain executable program code. Legacy `app/`, `web_client/`, and `tools/` directories are detected for diagnostics but never loaded. The migration helper archives those directories without touching user settings.

## 4. Release and update model

`app/version.json` is the immutable runtime manifest bundled with the program. It contains:

- runtime version and build ID;
- runtime layout version;
- update strategy;
- hashes for physical runtime resources;
- source hashes for release provenance;
- release notes.

`release/stable.json` is the remote release-channel manifest. It contains:

- released version;
- full-package URL, SHA256, size, and format;
- minimum runtime-layout version;
- release notes and force-update policy.

The two manifests are intentionally separate. A package hash cannot safely be stored in the package that it hashes, and a remote release record must not change the identity of an already built runtime.

Runtime layout 2 supports full-package updates only. The old incremental endpoint remains as a compatibility response, but it never writes files and always directs the user to the full package. Legacy clients see an empty `files` map, preventing them from copying new program files into AppData.

## 5. Future automatic full-package upgrade

Automatic upgrades should be introduced as a separate bootstrap component, not inside the running application process:

1. Download the complete package into a staging directory on the installation volume.
2. Verify HTTPS, package SHA256, manifest version, required files, and optional code signature.
3. Extract into a new immutable version directory.
4. Run a package smoke test on a temporary port and temporary user-data directory.
5. Atomically switch a small `current.json` pointer after the old process exits.
6. Keep the previous version for one-click rollback and remove older versions later.

Target managed layout:

```text
LiveClipper/
  LiveClipperLauncher.exe
  current.json
  versions/
    2026.7.13.10/
      LiveClipperWeb.exe
      _internal/
    previous-version/
  staging/
```

Until that bootstrap is shipped and independently tested, updates remain explicit full-package replacements. This is slower but trustworthy.

## 6. Business-module boundaries

The current business behavior remains unchanged. Future extraction should follow these boundaries:

- `runtime`: startup, paths, manifests, diagnostics, and release checks;
- `smart_cut`: candidate preparation, AI director request, narrative validation, and final clip plan;
- `media`: probing, cutting, rendering, effects, timing, and hardware acceleration;
- `speech`: ASR providers, word timestamps, semantic sentence boundaries, and subtitle cache;
- `live`: recording, active-product detection, timelines, and split queues;
- `jobs`: batch task lifecycle, progress, retry, skip-on-failure, and result history;
- `settings`: user configuration, keyword libraries, profiles, and migrations;
- `license`: activation, verification, refresh, and device binding;
- `web`: API routers, payload validation, WebSocket logs, and static frontend delivery.

Dependency direction is `web -> application services -> domain -> adapters`. Domain code must not import FastAPI, browser UI, or AppData path logic.

## 7. Migration sequence

1. Runtime V2: remove all program-code overlays and disable incremental writes.
2. Extract API routers from `server.py` without changing endpoint contracts.
3. Extract task orchestration and persist batch item states independently.
4. Split `ai_clipper.py` into candidate, director, policy, and validation modules.
5. Introduce provider interfaces for AI, ASR, and media execution.
6. Add the external versioned launcher and automatic full-package activation.

Each stage must preserve preview/final-cut parity and must ship with package-level smoke tests. Large business rewrites are not combined with runtime or updater changes.

## 8. Required diagnostics

`/api/runtime` must expose at least:

- runtime version and build ID;
- runtime layout version;
- code source (`source` or `bundled`);
- program, frontend, and user-data roots;
- update strategy and incremental-update support;
- legacy-overlay presence and ignored state;
- runtime resource-integrity result;
- batch-resilience engine version.

Support decisions must use these fields, not a version number alone.
