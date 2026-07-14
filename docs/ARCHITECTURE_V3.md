# LiveClipper Runtime Architecture V3

## 1. Goals

Runtime V3 extends the immutable-code boundary introduced by Runtime V2:

1. The running process uses one frozen runtime directory.
2. An update never modifies the active runtime directory.
3. A release is authenticated before it is downloaded or activated.
4. Activation is an atomic version-pointer change.
5. A failed first launch returns to the retained previous version.
6. AppData remains user data, updater logs, and rollback backups only; it is
   never a business-code import root.

## 2. Managed layout

    LiveClipperWeb/
      LiveClipperWeb.exe
      current.json
      install_manifest.json
      updater/
        LiveClipperUpdater.exe
        release_update_public_key.pem
      versions/
        2026.7.13.11/
          LiveClipperWeb.exe
          runtime_manifest.json
          _internal/
        2026.7.13.12/
          LiveClipperWeb.exe
          runtime_manifest.json
          _internal/

The root LiveClipperWeb.exe is the stable launcher. Existing shortcuts keep
using that path. The actual desktop runtime is selected from versions/.

current.json contains only local activation state:

- current and previous versions;
- a monotonic generation number;
- whether the current version is awaiting first-launch health confirmation.

The launcher accepts only safe version names and signed runtime manifests. It
never executes an arbitrary path from current.json.

## 3. Components

### Stable launcher

The launcher verifies the selected runtime manifest and entrypoint hash, starts
the frozen runtime, and watches first-launch health after an update. A failed or
timed-out health check switches current.json back to the previous version and
starts that retained runtime.

### Frozen business runtime

The runtime contains the web server, frontend, AI selection logic, media
pipeline, and adapters. It may check for updates and download a patch, but it
does not write program files or activate a version. The runtime remains open
while the patch is downloaded and SHA256-verified. It exits only after the
verified patch and a temporary updater copy are ready.

### Independent updater

The updater runs from a temporary copy after the business runtime exits. It:

1. verifies the outer patch hash and signed patch manifest;
2. verifies the exact source version, layout, and signed source manifest;
3. validates that every target-manifest change has a matching payload file;
4. constructs a new immutable version directory;
5. hard-links unchanged files only when signed source and target metadata are
   identical, without rereading the same NTFS file object;
6. hashes every changed payload and every copy fallback before activation;
7. records durable progress and shows a visible progress window;
8. updates stable launcher/updater files with a per-file rollback journal;
9. verifies the signed install manifest and stable-file result;
10. atomically writes current.json with pending=true and starts the launcher.

It never patches an active version in place.

A stable launcher or updater change requires a new full-package baseline. A
normal V3 delta never replaces the updater that is currently executing.

## 4. Release trust

Runtime V3 uses a dedicated Ed25519 release key. The private key is kept outside
Git and outside release packages. The public key is frozen into the runtime and
bundled beside the updater.

The following documents are signed:

- each immutable runtime manifest;
- each stable install manifest;
- each delta patch manifest;
- each remote release-channel manifest.

The signed channel carries ordered patch download sources, sizes, SHA256
values, exact source and target versions, and full-package fallback
information. Each automatic patch has a GitHub primary direct HTTPS URL and an
Aliyun OSS fallback direct HTTPS URL. SHA256 protects transport integrity; the
Ed25519 signature authenticates the publisher.

The runtime downloader:

1. rejects non-HTTPS sources before download;
2. retries each source with a bounded socket timeout;
3. switches to the next signed source automatically;
4. keeps a partial .part file and resumes with an HTTP Range request;
5. accepts a cached file only after exact size and SHA256 verification;
6. exposes downloaded bytes, total bytes, stage, and error state through
   /api/update/status;
7. never sends URL query secrets to user-facing error messages.

Remote channel states ready, hold, and awaiting-external-distribution are
explicit. Only ready can advertise an installable patch. A hold or awaiting
channel cannot start an update, even when its version is newer.

## 5. Delta format

A delta is source-version specific. It includes:

- signed source and target runtime manifests;
- the signed target install manifest;
- changed or new runtime payload files;
- changed stable launcher/updater payload files;
- hashes and sizes for all target runtime and stable files.

The target directory is built from the target manifest, not by recursively
copying the old directory. Files removed in the target release therefore cannot
survive as stale executable content.

Initially the stable channel publishes direct patches from each supported base
version to the current version. The client does not chain several patches in a
single update transaction.

## 6. Health and rollback

After activation, the launcher gives the runtime a one-time health token. The
desktop runtime reports healthy only after:

- the local HTTP service accepts connections;
- /api/runtime reports the expected active version and Runtime V3;
- bundled runtime integrity passes.

The launcher confirms the version only after receiving the matching token. If
the process exits or the timeout expires, it terminates the failed process,
atomically restores the previous pointer, and launches the previous runtime.

User-data migrations must be backward compatible for at least one retained
version. Irreversible data migrations require a transactional user-data backup;
otherwise executable rollback is not sufficient.

## 7. V2 bridge

Runtime V2 cannot acquire a stable launcher through its disabled legacy updater.
Version 2026.7.13.10 therefore uses one externally launched bridge executable.

The bridge validates the V2 package, creates signed versions/2026.7.13.10 and
versions/2026.7.13.11 directories from the existing files plus a small payload,
installs the stable launcher/updater, and activates V3. Settings, licenses,
material paths, caches, and logs are not moved.

This is a one-time architecture migration. Later supported releases use the
normal in-app V3 delta path.

## 8. Storage and cleanup

The current and previous runtime versions are retained. Unchanged files use
hard links on the installation volume, so two logical versions do not normally
double disk consumption. Copy fallback is used when hard links are unavailable.

Transaction work is staged under `.lc-update/<8-character-id>` on the install
volume. The short path avoids Windows path-length failures in deeply nested
dependencies while preserving same-volume atomic directory replacement. The
directory is removed after success or failure.

Staging directories are ignored until complete and may be removed after a
failed transaction. Older confirmed versions are removed only after the current
version has passed health confirmation.

## 9. Diagnostics

/api/runtime exposes:

- runtime and active versions;
- install and active runtime directories;
- current and previous version state;
- pending-health state;
- launcher and updater versions;
- update-engine and runtime-layout versions;
- signed-delta capability;
- bundled resource integrity;
- ignored legacy AppData overlays;
- installed launcher and updater versions;
- the minimum updater version required by the release channel;
- durable update progress under LocalAppData.

/api/update/status exposes the in-runtime download phase:

- running state and stage;
- downloaded and total bytes;
- percentage and current source message;
- final result or resumable failure reason.

Support decisions use these fields rather than a displayed version alone.
