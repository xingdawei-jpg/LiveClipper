# Mac mini M4 source handoff

## Verified source

- Repository: https://github.com/xingdawei-jpg/LiveClipper.git
- Current development branch: `codex/v4-business-2026.8.15.1`
- Verified source baseline on 2026-09-09 (local and remote): `8056fe80066b258285aeb06aef78070ee5c34e4b`. This handoff document is committed after that baseline.
- Includes `4d710c6` (director flow and local ASR runtime improvements) and the subsequent 2026.9.9.1 release records. The previous 2026-09-08 source reference is superseded.
- `main` is a different revision. Clone the repository, then select the development branch in GitHub Desktop before opening it in Codex.
- Suggested Mac destination: `~/Documents/GitHub/LiveClipper`.

## Local cleanup scope

Four ignored historical copies are archived under `.codex_backups/mac_source_cleanup_20260908/app/`: `gui_clean.py`, `gui_fresh.py`, `gui_tmp.py`, and `live_recorder_page_BACKUP.py`. References found outside those files are exclusion lists and historical documentation, not runtime imports. They were already absent from Git and will not be cloned onto the Mac.

Cache, virtual environment, macOS metadata, agent logs/state, and local `artifacts/` and `workspace/` outputs are ignored. These directories remain on the Windows machine. Untracked experiment scripts, audit notes, and acceptance records remain in place for individual review; they are not automatically included in the handoff. Do not use a blanket stage-all operation.

Keep production source, tests, release history, public verification keys, and current packaging contracts. Do not copy Windows virtual environments, vendor binaries, build outputs, private credentials, user settings, or video materials as source dependencies.

## Mac work still required

This is a source handoff, not a working Mac release. The desktop entry imports `winreg`; existing packaging bundles Windows WebView2, a native DLL, and `.exe` FFmpeg tools. Adapt the desktop backend, drag/drop, executable paths, fonts, user-data directories, licensing/device identity, and V4 launch/update behavior for macOS before packaging.

Build arm64 dependencies on the Mac. Validate local ASR compatibility before making GPU acceleration claims. Run import, authorization, ASR, AI selection, subtitle rendering, and actual video export acceptance on the Mac, then create and test an `.app`. Signing, notarization, distribution, and updates are separate release work. Follow `docs/PACKAGING_WINDOW_ENTRY.md` before any version or build operations; a Mac native runtime requires a separate full baseline.

This synchronization does not change runtime code, versions, or release channels. The ignore rules are already committed in `4d710c6`; this document is included in the source synchronization commit. Untracked experiments and local audit materials remain on Windows and are not required additions to this source handoff.

## Refresh an existing Mac checkout

In GitHub Desktop, select `LiveClipper`, switch to `codex/v4-business-2026.8.15.1`, click Fetch origin, and then Pull origin if offered. Preserve any local Mac changes before resolving a checkout or merge conflict; do not discard them. Open the resulting project directory in Codex and verify the checked-out revision before starting macOS adaptation. A fresh clone should select the same branch. No Windows installation package or update-channel publication is part of this synchronization.
