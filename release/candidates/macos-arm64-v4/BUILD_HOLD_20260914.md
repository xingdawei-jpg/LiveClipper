# macOS arm64 Runtime V4 candidate hold evidence — 2026-09-14

## Disposition

**HOLD — no release artifact is eligible for upload or publication.** No
`stable.ready.json` was created or changed, no object was uploaded, and no
Windows channel or Windows installation root was read by the macOS build.

This record describes local, non-distributable acceptance artifacts only. It
does not declare a macOS full baseline or an incremental update candidate.

## Source and environment

| Item | Evidence |
| --- | --- |
| Repository / branch | `xingdawei-jpg/LiveClipper`, `codex/macos-arm64-adaptation` |
| Source revision | `3d18b9067bf52b82f34abd6e282b28600d89f152` |
| Application metadata | `app/version.json`: `2026.9.9.1`, Runtime V3 / layout 3; it was not reused as a Mac V4 release manifest |
| Native build host | macOS 26.4.1, `arm64` (Apple Silicon), Python 3.11.16 arm64, PyInstaller 6.22.3 |
| Mac Core identity | `4.0.0-macos-arm64`; it is deliberately distinct from Windows Core `4.0.0` |
| Committed Mac work | `d9fec8b`, `2ede5d4`, `5a69ffa`, `3d18b90`; each was pushed to the Mac development branch |

Focused validation after the final code change passed: 54 tests covering the
macOS contract, V4 channel/launcher/update transaction, and desktop media
import behavior (one Windows-only OLE test skipped). The Mac contract subset
also passes with 10 tests, including compilation of the macOS Host PyInstaller
specification.

## Platform boundary audit

- User data resolves to `~/Library/Application Support/LiveClipper` on macOS,
  including the server and local-ASR error path. A real device identifier was
  generated locally as a 24-character hash using the macOS CPU identifier,
  root-volume UUID, and MAC address; no identifier value is retained here.
- The desktop uses pywebview Cocoa and a Cocoa DOM drop bridge; macOS restart
  reopens its containing `.app` with `/usr/bin/open -n`.
- The host embeds arm64 `ffmpeg` and `ffprobe` under
  `Contents/Frameworks/ffmpeg`; frozen resolution now prefers that path. The
  selected `ffmpeg-full` build contains `ass`, `subtitles`, and `drawtext`.
  A generated one-second local subtitle export succeeded (4,667 bytes,
  SHA-256 `3857af3ebded5059835e94b22981fc2f9ac2bc4ab921e32276c7068be5c36315`).
- The local ASR dependency set is native arm64: Torch 2.14.0,
  `kaldi-native-fbank` 1.22.3, FunASR 1.4.15, and ModelScope 1.40. `torchaudio`
  is deliberately absent on macOS. No model cache was copied or downloaded.
- Windows-only code remains in the repository for the Windows product:
  `runtime_v4/liveclipper_host_v4.spec`, `web_client/liveclipper_web.spec`,
  and the Windows branch of `web_client/desktop.py` reference WebView2,
  OLE/CF_HDROP, registry APIs, `.dll`, and `.exe` files. The macOS Host spec
  excludes the Edge/WinForms webview backends and `torchaudio`, embeds no
  WebView2/DLL/Windows FFmpeg, and is the only spec used for these artifacts.

## Mac-only V4 update boundary

The frozen Mac Host embeds only
`release/runtime_v4_macos_arm64_update_sources.json`, whose stable endpoint is
`https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/macos-arm64/stable.json`.
The launcher uses the macOS install root
`LiveClipper.app/Contents/Resources/LiveClipperV4`. Contract tests prove that
the Mac source configuration contains `/v4/macos-arm64/` and rejects the
Windows `/v4/stable.json` route. The Mac business policy permits only
`4.0.0-macos-arm64`.

The V4 transaction tests cover signed download verification, resumable fetch,
atomic selection switch, health receipt, rollback, and user-data retention
logic. They use ephemeral test keys and test archives; they are not a release
channel or a published package.

## Native build artifacts (local hold only)

| Artifact | Size (`du -sk`) | Executable SHA-256 | Signature assessment |
| --- | ---: | --- | --- |
| `workspace/macos-v4-hold-20260913/v4-dist/LiveClipperHost.app` | 1,051,440 KiB | `07a8d6fe1faeba27afa82a4ba3a6b3846e0772fdaee03093e67a7f2025e262e2` | `codesign --verify --deep --strict`: pass; `TeamIdentifier=not set`; `spctl --assess`: rejected |
| `workspace/macos-v4-hold-20260913/v4-dist/LiveClipper.app` | 38,864 KiB | `005e775bc6670cfeaa45790497a325d9587143064c98a843297090cb0dfc8bff` | `codesign --verify --deep --strict`: pass; `TeamIdentifier=not set`; `spctl --assess`: rejected |

Both artifacts were built natively with the macOS-specific specs. The Host
executable and its embedded FFmpeg are Mach-O arm64. They carry PyInstaller's
ad-hoc local signature only; no shipping archive, signed Core manifest,
signed business archive, signed channel manifest, or notarized package exists.

A separate legacy-layout acceptance shell was started twice from a clean
override user directory and served `/api/runtime`; it confirmed bundled code,
the macOS default user-data root, and restart after process termination. Its
runtime layout was V3 and its legacy integrity manifest was unsuitable for
Mac, so it is expressly excluded from V4 baseline evidence.

## Blocking conditions and minimum repair

1. **Developer ID signing is unavailable.** `security find-identity -v -p
   codesigning` found zero valid identities. Both local apps have no Team ID
   and Gatekeeper rejects them. Install the intended **Developer ID
   Application** certificate and private key in this Mac keychain, then set
   `LIVECLIPPER_MACOS_CODESIGN_IDENTITY` for the exact Mac specs and repeat
   deep verification plus `spctl` assessment.
2. **Notarization credentials are unavailable.**
   `xcrun notarytool history --keychain-profile LiveClipperNotary` has no
   keychain profile. Add the approved notarization profile, submit the signed
   archive, wait for acceptance, staple the ticket, and verify Gatekeeper.
3. **A real V4 baseline cannot be assembled before 1–2.** Freeze a Mac
   business version, regenerate the Mac business manifest from the selected
   source, sign it with the existing release signing mechanism, assemble it
   under the Mac V4 install root, and only then calculate the shipping archive
   size/SHA-256 and signed stable manifest. Do not reuse `app/version.json`'s
   V3 manifest for that step.
4. **End-to-end product acceptance remains unperformed.** No production
   activation code, AI credentials, local SenseVoice model, or user video was
   introduced on this Mac. Supply approved test credentials, a clean test
   user, a licensed test video, and an approved local model acquisition path
   after signing is unblocked. Validate first launch, activation, AI
   connection/selection, SenseVoice, subtitles, actual export, UI close and
   restart, then repeat from the signed baseline with one small business
   update and a forced unhealthy-start rollback.

Until those four conditions are satisfied, the requested full baseline,
clean-user installation acceptance, real business increment, and ready
publication remain intentionally incomplete.
