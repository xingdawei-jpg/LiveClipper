# Windows continuation handoff — Mac WIP branch

## Scope

Branch: `codex/mac-wip-handoff`.

This WIP hands over the current unfinished Director business changes together
with the already committed macOS arm64 Runtime V4 preparation in its ancestry.
It changes no stable manifest, no ready manifest, and no update endpoint.

The files staged with this handoff are limited to these source and test files:

- `app/commercial_analyzer.py`
- `app/director_opening_audit.py`
- `web_client/server.py`
- `web_client/frontend/assets/app.js`
- `tools/run_director_hook_ab.py`
- `tests/test_commercial_analyzer.py`
- `tests/test_commerce_director_preview_route.py`
- `tests/test_director_hook_comparison.py`
- `tests/test_director_product_contract.py`

No keys, user configuration, model data, video, cache, PyInstaller output, or
workspace artifact is included.

## Problem being solved

The two-pass Director pipeline previously removed non-selectable source lines
from both prompts. That avoided selecting disallowed price/size/CTA content,
but it could also hide a product switch, correction, negation, or pronoun
referent. The model could then attach a safe selectable sentence to the wrong
product.

The opening receipt also needed a stronger delivery gate: a plan with valid
duration must still remain editable and blocked from formal export when its
opening evidence disagrees with the executed clips or the model says the source
does not contain a sufficiently strong opening.

## Completed in this WIP

- Sends filtered source lines back to M1 and M2 as chronological
  `[context ID]` evidence. Context is visible for product identity and
  counter-evidence, but cannot be selected as a beat, Hook, or payoff.
- Allows up to three distinct Hook → immediate-payoff packages in M1, while
  preserving the existing exact-ID audit in M2.
- Allows context IDs only as `product_evidence_ids` during opening review; the
  audit rejects them if they appear in Hook or payoff lists.
- Holds Director export when opening verification warns or opening quality is
  `limited`; the frontend exposes the reason as an actionable review warning.
- Makes the hook A/B runner capture only the current arm's provider exchanges,
  omits API key and response-hook data, and uses a workspace-derived preview ID
  to avoid cross-experiment review-cache collisions.

## Validation

Run from the repository root with the local project environment:

```sh
PYTHONPATH=.:app:web_client:tests .venv/bin/python -m unittest -v \
  test_commercial_analyzer \
  test_director_hook_comparison \
  test_director_product_contract \
  test_commerce_director_preview_route
```

Result on 2026-09-14: **186 tests passed**. The suite uses fixtures and does
not require an API key, model cache, user video, or release credential.

For a real provider comparison, use `tools/run_director_hook_ab.py` only with
approved Windows credentials and a separately provisioned test workspace. Its
generated output is experimental evidence and must remain outside Git.

## Remaining work for Windows

1. Review the prompt wording with a real approved Director provider run to
   confirm that visible context improves product identity without causing
   disallowed content to be paraphrased into a story or opening.
2. Exercise the held-preview UI with a real video, checking both opening
   mismatch and `limited` cases. Verify that normal manual review remains
   possible while formal export stays blocked.
3. If the changes are accepted, make a separate Windows release decision and
   package from a Windows-native checkout. Do not reuse Mac build products,
   user data, model caches, or media.

## macOS-only work already in this branch ancestry

The current dirty WIP above is platform-neutral. The following existing parent
commits are macOS-specific and should remain isolated from Windows packaging:

- `2ede5d4`: Mac arm64 V4 Core identity, Mac-only update-source configuration,
  Cocoa desktop behavior, dedicated Host/Launcher specs, Mac install root, and
  Mac business policy.
- `5a69ffa`, `3d18b90`: frozen `.app` FFmpeg resolution and Host spec build
  validation.
- `489818b`: Mac ffprobe recovery after the local x265 upgrade.
- `release/candidates/macos-arm64-v4/BUILD_HOLD_20260914.md`: local Mac build
  hold evidence.

The Mac Host may read only
`release/runtime_v4_macos_arm64_update_sources.json`, which points to
`v4/macos-arm64/stable.json`. Do not replace it with the Windows stable route,
and do not change either platform's update channel as part of this WIP.

macOS formal release work remains blocked by absent Developer ID Application
signing and notarization credentials, so there is no Mac baseline package or
business increment to transfer to Windows.
