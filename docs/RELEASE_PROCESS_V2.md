# LiveClipper Release Process V2

## Release rules

- Program updates are full-package releases.
- AppData is never a program installation target.
- `app/version.json` identifies the built runtime.
- `release/stable.json` identifies the downloadable package.
- A source-only test is not sufficient evidence for a user release.
- A failed package gate blocks publication.

## Build sequence

1. Confirm the tracked worktree and review unrelated untracked files.
2. Run Python compilation, frontend syntax checks, unit tests, and manifest checks.
3. Generate `app/version.json` with `tools/build_update_manifest.py`.
4. Build `web_client/liveclipper_web.spec` as an onedir package.
5. Verify that the package contains no loose business `.py` files under `_internal/app` or `_internal/web_client`.
6. Run the release security audit.
7. Create the full ZIP and run `zipfile.testzip()`.
8. Generate `release/stable.json` from the final ZIP with `tools/build_release_channel.py`.
9. Extract the ZIP to a clean directory and start it with a temporary AppData root.
10. Repeat the smoke test with deliberately polluted legacy AppData `app/` and `web_client/` directories.
11. Verify `/api/runtime`, `/api/update/check`, the main page, batch continuation, and clean shutdown.
12. Publish the ZIP, SHA256, source commit, runtime manifest, and stable-channel manifest together.

## Package acceptance gates

The package is accepted only when all are true:

- the executable starts from the extracted package;
- `code_source` is `bundled`;
- `runtime_layout_version` is `2` or newer;
- `app_dir` and `web_dir` point into the extracted `_internal` directory;
- legacy AppData overlays are reported as ignored;
- runtime integrity passes;
- no packaged business `.py` duplicates exist;
- the stable-channel package SHA256 matches the final ZIP;
- a single failed batch item does not stop later items;
- no process remains after the smoke test.

## Rollback

Runtime V2 uses roll-forward rollback: rebuild the last known-good code as a new, higher version and publish it as a full package. Do not lower the remote version or copy selected old files into a current installation.

After the external versioned launcher is introduced, it may atomically switch back to the retained previous version. User data migrations must remain backward compatible for at least one release so that rollback is safe.

## Legacy cleanup

`tools/repair_liveclipper_update.cmd` no longer installs program files. It archives legacy AppData program directories under `legacy_runtime_backup/<timestamp>` and leaves settings, licenses, caches, and user files in place.

## Future release channels

The channel manifest can later support `stable`, `beta`, and `internal`, but a client follows only one configured channel. Every channel points to complete immutable packages and never to mutable source files on a branch.
