# LiveClipper Packaging Window Entry

Use this document when opening a dedicated Codex window for packaging or update
release work.

## Working Directory

Use only:

`C:\Users\周美彤\Documents\GitHub\LiveClipper`

Do not package from `C:\Users\周美彤\Documents\live clipper`, old `dist`
contents, or temporary OpenClaw workspaces.

## First Message For The Packaging Window

```text
这是 LiveClipper 打包专用窗口。目标：只负责最终全量包、发布清单和发布验证。

先读取：
1. docs\PACKAGING_WINDOW_ENTRY.md
2. .project_docs\docs\PACKAGING_WINDOW_RUNBOOK.md
3. .project_docs\docs\UPGRADE_RELEASE_RUNBOOK.md
4. .project_docs\docs\SOURCE_OF_TRUTH.md
5. docs\ARCHITECTURE_V2.md
6. docs\RELEASE_PROCESS_V2.md

不要假设其他窗口的修改已经包含进来。先做跨窗口同步检查：git status、git diff --stat、未跟踪文件、release_dist 最新包、app/version.json、release/stable.json、web_client/liveclipper_web.spec、web_client/desktop.py、tools/build_update_manifest.py。Runtime V2 只发布全量包，禁止 AppData 程序增量更新。
```

## Mandatory Cross-Window Sync Check

Run this before building or publishing:

```powershell
cd C:\Users\周美彤\Documents\GitHub\LiveClipper
& "C:\Users\周美彤\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe" status --short
& "C:\Users\周美彤\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe" diff --stat
Get-ChildItem release_dist -File | Sort-Object LastWriteTime -Descending | Select-Object -First 10 Name,Length,LastWriteTime
```

Rules:

- Treat modified tracked files as possible release content.
- Inspect untracked files under `app`, `web_client`, `tools`, `vendor`, `assets`, `packaging`, and `docs`.
- Do not ignore untracked files that runtime code references.
- If a file changes after `app/version.json` is generated, regenerate the manifest.
- Every program release is a full package. Never publish program-file deltas.

## Required Final Report

The packaging window final answer must include:

- package path
- version and build ID from `app/version.json` and `/api/runtime`
- SHA256
- zip integrity result
- release security audit result
- extracted-package `/api/runtime` result
- polluted-AppData smoke result and proof that legacy overlays were ignored
- proof that no loose business `.py` files exist in the package
- modified/untracked files considered
- untested areas

Detailed runbook lives in:

`.project_docs\docs\PACKAGING_WINDOW_RUNBOOK.md`
