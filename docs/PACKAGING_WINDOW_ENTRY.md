# LiveClipper Packaging Window Entry

本文件是打包窗口入口。发布政策以 docs/RELEASE_POLICY.md 和 release/release_policy.json 为准。

## Working Directory

只能使用：

    C:\Users\周美彤\Documents\GitHub\LiveClipper

禁止从 C:\Users\周美彤\Documents\live clipper、旧 dist、临时目录或用户解压目录打包。

## 必读文件

1. docs/RELEASE_POLICY.md
2. docs/PACKAGING_V3_HANDOFF.md
3. docs/ARCHITECTURE_V3.md
4. docs/RELEASE_PROCESS_V3.md
5. release/release_policy.json
6. release/baselines.json
7. .project_docs/docs/SOURCE_OF_TRUTH.md
8. .project_docs/docs/PACKAGING_WINDOW_RUNBOOK.md
9. .project_docs/docs/UPGRADE_RELEASE_RUNBOOK.md
10. docs/RUNTIME_LAYOUT_STATUS.md

## Mandatory Cross-Window Sync Gate

任何版本、清单、构建或发布操作前执行：

~~~powershell
git status --short --branch
git diff --stat
git diff
git ls-files --others --exclude-standard -- app web_client tools vendor assets packaging docs release tests

Get-ChildItem release_dist -File -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 20 Name,Length,LastWriteTime
~~~

逐项记录：

- 文件用途；
- 完成状态；
- 验证证据；
- 是否进入本次版本；
- 是否与其他窗口修改冲突。

临时脚本、缓存、备份、密钥、本地配置、测试产物和用途不明文件不得纳入。不得使用 git add -A。

## 状态核对

同步门后必须同时核对：

- app/version.json；
- tools/runtime_v3_versions.py；
- release/baselines.json；
- release/stable.json；
- release/candidates/<目标版本>/；
- 当前正式全量 ZIP 和 SHA256；
- GitHub patch Release 规格；
- launcher、updater 和公钥 hash。
- 目标 runtime 相对每个受支持基线的新增/变更文件数、压缩 patch 大小和目录级依赖差异。
- 本次目标是 V3 还是 V4；不得通过把 `app/version.json` 的布局字段直接改成 4 来制作 V4。

先运行：

~~~powershell
python tools\release_preflight.py --phase development
~~~

开发状态允许 app/version.json 领先 stable.json，但必须明确报告。stable.json 绝不能领先 app/version.json。

## 发布类型

- launcher、updater、公钥、信任根、布局或安装状态格式变化：新全量基线，只通过百度网盘分发全量包。
- V4 私测使用独立的 Core、business bundle、`current.json` 和签名 channel；不得混入 V3 delta。
- 新增/整体替换原生或 ML runtime，或任一直接 patch 超过 50 MiB、500 个 runtime payload 文件：同样是新全量基线。不得因为源码文件属于业务层而降级为 GitHub delta。
- 仅业务 runtime 变化且通过 docs/PACKAGING_V3_HANDOFF.md 的增量预算：才构建并发布 GitHub 签名 delta。
- 单台电脑的本地配置或权限问题：先单机修复，不立即发布全体版本。

GitHub Release 禁止上传全量 ZIP。自动补丁只使用 GitHub HTTPS Release URL，不再要求 OSS。

## Channel Gate

- 验收候选写入 release/candidates/<版本>/stable.hold.json。
- 构建和验收期间不得改动 live release/stable.json。
- 所有必需验收项写入 acceptance.json 且为 pass。
- ready stable 必须最后单独生成、提交和推送。
- 发布后核对远端 commit、GitHub 资产和 stable 签名内容。

## Required Final Report

最终报告必须包括：

- 纳入和排除文件；
- 版本、build ID、launcher 和 updater 版本；
- 当前正式基线与线上 stable 版本；
- 本次 release_type 和对应发布资产的路径、大小、SHA256；
- 签名、ZIP 和安全审计；
- business_runtime 的真实旧版本升级、错误补丁拒绝、回滚、用户数据和更新后 runtime 完整性；
- full_baseline 的解压包 /api/runtime、进程归属和干净/污染 AppData；
- business_runtime 的 GitHub 回下载验证；full_baseline 另含百度网盘回下载验证；
- acceptance.json 结果；
- stable 最后发布的 commit；
- 未测试和人工确认项。

没有这些证据时只能报告“候选未发布”，不能说“发布完成”。
