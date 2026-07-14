# LiveClipper Packaging Window Entry

Use this document when opening a dedicated Codex window for packaging or update
release work.

## Working Directory

Use only:

C:\Users\周美彤\Documents\GitHub\LiveClipper

Do not package from C:\Users\周美彤\Documents\live clipper, old dist contents,
or temporary workspaces.

## First Message For The Packaging Window

~~~text
这是 LiveClipper 打包专用窗口。目标：只负责 Runtime V3 全量基线、签名增量包、发布清单和发布验证。

先读取：
1. docs\PACKAGING_WINDOW_ENTRY.md
2. docs\ARCHITECTURE_V3.md
3. docs\RELEASE_PROCESS_V3.md
4. .project_docs\docs\SOURCE_OF_TRUTH.md
5. .project_docs\docs\PACKAGING_WINDOW_RUNBOOK.md
6. .project_docs\docs\UPGRADE_RELEASE_RUNBOOK.md

不要假设其他窗口的修改已经包含进来。先检查 git status、git diff、未跟踪文件、
app/version.json、release/stable.json、稳定启动器/更新器版本、现有正式基线和
release_dist。远程通道必须保持 hold，直到目标设备完成全量包和增量更新验收。
~~~

## Mandatory Cross-Window Sync Check

Run this before building or publishing:

~~~powershell
cd C:\Users\周美彤\Documents\GitHub\LiveClipper
git status --short
git diff --stat
Get-ChildItem release_dist -File |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 10 Name,Length,LastWriteTime
~~~

Rules:

- Treat modified tracked files as possible release content.
- Inspect untracked files under app, web_client, tools, vendor, assets,
  packaging, and docs.
- Do not ignore untracked files that runtime code references.
- If a runtime file changes after app/version.json is generated, regenerate
  the manifest.
- A launcher, updater, trust-root, or runtime-layout change requires a new
  full-package baseline.
- A normal business-runtime release uses a signed direct V3 delta and must
  contain zero stable-component payload files.
- Every automatic patch has at least two signed direct HTTPS sources:
  GitHub primary and Aliyun OSS fallback.
- Never use an interactive share page as an automatic-download URL.
- Publish release/stable.json last. Before that, keep channel_status=hold.
- Do not open the channel until both a clean install and an exact
  published-source delta pass on a separate Windows device.

## Required Final Report

The packaging window final answer must include:

- package and patch paths;
- version, build ID, launcher version, and updater version;
- SHA256 and size for every artifact;
- ZIP integrity and release security audit results;
- extracted-package /api/runtime result;
- polluted-AppData smoke result;
- proof that no loose business .py files exist in the package;
- direct-download verification for GitHub and OSS;
- interruption/resume, fallback, activation, health, and rollback results;
- signed channel status and proof that it was published last;
- modified/untracked files considered;
- untested areas.

The authoritative architecture and release process are:

- docs\ARCHITECTURE_V3.md
- docs\RELEASE_PROCESS_V3.md
