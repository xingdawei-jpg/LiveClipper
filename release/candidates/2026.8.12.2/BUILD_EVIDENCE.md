# LiveClipper V4 Business Update 2026.8.12.2

## Freeze

- Freeze commit: `905ec823ab779d33ffc480364dc170caf49361cc`
- Package type: signed Runtime V4 business bundle
- Compatible Core: `4.0.0`
- Protected/Core runtime diff: none
- Release scope: product-scan export selection and related frontend layout/status feedback

## Source validation

- Python compile check: passed
- Frontend JavaScript syntax check: passed
- Targeted Runtime V4 update bridge tests: 21 passed
- Full test suite: 572 passed, 2 skipped
- Update manifest consistency check: passed
- Development release preflight: passed (known legacy V3 baseline/channel warnings only)
- Git whitespace check: passed

## Bundle evidence

- Archive: `LiveClipperBusiness_2026.8.12.2.zip`
- Size: `1,028,823` bytes
- SHA256: `c2cd1dcad5fdf44ab3810505708cfea7ca1ef5fc9441dec217ca943ffaf9289e`
- Bundle manifest SHA256: `a05f3b8515cbe02cd82bce2afc6f7ae1aa8a85cf8d9e51255a1349b5d1bf4eac`
- Signature key id: `1905329f73f719d3`
- Signed business files: 60
- ZIP integrity test: passed
- Deterministic rebuild: byte-identical SHA256
- Strict release security audit: passed
- Core/Launcher/native/private-key boundary audit: passed
- Changed runtime files match the frozen commit byte-for-byte

## Candidate channel

- Channel: `stable`
- Candidate status: `hold`
- Hold document SHA256: `08d1991aff6fea2267d0815034c7e0eb36264d19930425f6c7b32353c78736ed`
- Allowed source versions: `2026.8.5.2`, `2026.8.7.1`, `2026.8.8.1`, `2026.8.11.1`, `2026.8.11.2`, `2026.8.12.1`
- All source/Core combinations returned `channel_hold` before publication

## Isolated update and rollback validation

- Installed and first-launch-confirmed the signed `2026.8.12.1` business bundle.
- Installed `2026.8.12.2` over `2026.8.12.1`; state recorded `previous=2026.8.12.1` and `pending=true` before launch.
- First launch confirmed `current=2026.8.12.2`, `pending=false`.
- `/api/runtime` reported Runtime V4, Core/Launcher `4.0.0`, bundled code, signed bundle verified, and integrity `60/60`.
- Launcher SHA256 remained `66a3a2b3858a849a6289a0d80490ef6f14d2775c0c92bfe301b4912b3d525194`.
- Host SHA256 remained `68d99edf504c79220d535a93accb11bfdef2d9e75c0eed142f90b5f0b7cd72a0`.
- A sentinel file in the isolated user-data directory remained present after update.
- Deliberately corrupted only the isolated `2026.8.12.2` `bundle_entry.py`.
- Launcher rejected the corrupt selection and automatically restored `2026.8.12.1`; rollback runtime integrity remained `60/60` and the user-data sentinel remained present.

## OSS evidence

- Uploaded immutable object: `liveclipper/v4/LiveClipperBusiness_2026.8.12.2.zip`
- Public HTTPS download size: `1,028,823` bytes
- Public HTTPS download SHA256: `c2cd1dcad5fdf44ab3810505708cfea7ca1ef5fc9441dec217ca943ffaf9289e`
- The public update channel was not changed before the package passed remote hash verification.
- Published `stable.json` as `ready` at `2026-08-12T11:15:32+08:00`.
- Ready channel document SHA256: `fae455ce5b970cd4c873b122d2c17939ad9e34f052eebd2704a33cf007160261`.
- Cache-busted and direct public `stable.json` downloads matched the local ready document byte-for-byte.
- Both public documents passed signature verification with key id `1905329f73f719d3`.
- A `2026.8.12.1` / Core `4.0.0` client received the public decision `update_available` for `2026.8.12.2`.

## Remaining external confirmation

- This run completed automated source, signed-package, isolated first-launch, update, health, and rollback validation on the packaging machine.
- A second physical computer receiving the public channel remains the final external-machine confirmation; do not treat that observation as a substitute for the evidence above.
