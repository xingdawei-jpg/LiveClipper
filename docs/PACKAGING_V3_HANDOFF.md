# LiveClipper Runtime V3 打包与发布交接 Runbook

本文件用于将正式打包工作交给另一位执行者。它补充并细化
`docs/RELEASE_POLICY.md`、`docs/RELEASE_PROCESS_V3.md` 和
`docs/PACKAGING_WINDOW_ENTRY.md`。若旧文档仍写着“所有业务 runtime 变化都只发
GitHub delta”，以本文件和更新后的发布政策为准。

## 1. 不可越过的边界

- 只能在 `C:\Users\周美彤\Documents\GitHub\LiveClipper` 打包。
- 先执行 Cross-Window Sync Gate；未知、未完成、未验证的修改不得纳入。
- 不使用 `git add -A`，不提交私钥、用户配置、缓存、`build/`、`dist/`、
  `release_dist/` 或测试输出。
- 已交付版本的 ZIP、签名 manifest、GitHub Release 资产和 tag 都不可替换；修复
  必须使用更高版本。
- `release/stable.json` 是线上状态。除发布负责人明确授权的紧急停止或最终 ready
  发布外，打包者不得改动它。
- 私钥只能通过仓库外路径或本机环境传入命令，绝不写入文档、脚本、日志或 Git。

## 2. 2026.7.19.2 复盘和当前处理原则

`2026.7.19.2` 已生成三个 GitHub patch，但不应作为默认在线更新：

| 路径 | 压缩包大小 | runtime payload 文件数 |
| --- | ---: | ---: |
| 2026.7.15.1 -> 2026.7.19.2 | 321,161,826 B | 6,674 |
| 2026.7.15.2 -> 2026.7.19.2 | 689,743,798 B | 6,942 |
| 2026.7.15.3 -> 2026.7.19.2 | 321,212,062 B | 6,679 |

15.3 的正式 runtime 有 2,241 个文件；19.2 有 8,907 个文件。其中 2,229 个文件
未变、12 个旧文件变更，却新增了 6,666 个文件、约 708 MB 未压缩内容。这不是普通
UI 或业务逻辑改动，而是本地 SenseVoice 依赖树被整体带入运行时。

主要新增内容包括：

- `torch`：2,317 个文件，约 378 MB，其中 `torch_cpu.dll` 单独约 305 MB；CPU
  推理核心可能需要，但整个目录并不自动等于都需要。
- `llvmlite`：约 120 MB；`scipy`：约 54 MB；`transformers`：2,158 个源码文件、
  约 38 MB；`jieba`：约 31 MB；`babel`：约 30 MB；`sklearn`：约 12 MB。
- 15.2 路径还额外携带约 328 MB WebView2、约 202 MB ffmpeg/ffprobe，因此更大。

`web_client/liveclipper_web.spec` 明确将 `funasr`、`modelscope`、`torch`、
`torchaudio`、`scipy`、`unittest` 和 `pdb` 放入生产 hidden imports。当前
`funasr` 顶层初始化会递归导入全部子模块，导致 PyInstaller 追入模型、训练、视觉、
测试和可选依赖分支。`unittest`、`unittest.mock`、`pdb` 和 `hydra/test_utils` 是
测试/诊断痕迹，不应被当作生产功能依赖。

结论：19.2 的大包不是密钥、缓存或用户文件混入，安全审计仍应继续执行；但确实混入
了不适合自动更新的宽泛 ML/开发依赖树。不能重打或覆盖同名 19.2 资产。后续修复必须
使用一个新的、更高版本。

## 3. 发布类型决策，先测预算再决定

先构建目标 frozen runtime 和 V3 staging，再读取精确旧基线与目标的 runtime manifest
差异。不得只根据“源码改的是业务文件”判断为普通增量。

只有同时满足以下条件，才允许 `business_runtime` GitHub 自动补丁：

1. `stable_payload_files == 0`，且 launcher、updater、公钥、信任根、安装布局均
   未变。
2. 每条直接 patch 的压缩传输大小不超过 **50 MiB**。
3. 每条 patch 的 `runtime_payload_files` 不超过 **500**。
4. 不新增或整体替换原生/ML 运行时家族，例如 `_internal/torch`、
   `_internal/torchaudio`、`_internal/transformers`、`_internal/modelscope`、
   `_internal/scipy`、`_internal/llvmlite`、`_internal/webview2_runtime`、
   `_internal/ffmpeg`。
5. payload 不含测试、示例、训练、文档、调试或开发目录；任何例外必须有运行时
   import 证据和真实功能验收。
6. GitHub 回下载、断点续传、真实旧版本更新、回滚和用户数据保持均通过。

任一条件不满足，即使 launcher/updater 没变，也必须按 `full_baseline` 处理：

- 新建更高版本的完整包和 `release/baselines.json` 登记；
- 全量 ZIP 只通过百度网盘人工分发，不上传 GitHub Release；
- 旧版本不再尝试下载巨型普通 patch，而是通过 signed `package.url` 打开全量包下载页；
- 从新基线再做一次小型的合成下一版本 delta，证明后续普通更新可行。

50 MiB/500 文件是 GitHub 单源自动更新的硬上限，不是建议值。需要超过上限时，必须
改为全量基线，而不是由打包者临时放宽。

## 4. 依赖审计和瘦身要求

### 4.1 构建环境

1. 使用干净、锁定版本的 Python 环境构建，不从旧 `build/`、`dist/` 或用户目录
   复制内容。
2. 固定 PyInstaller、Python、FunASR、ModelScope、Torch 和 Torchaudio 版本，并把
   实际版本记录到候选验收证据。
3. 保留固定 WebView2、`ffmpeg.exe` 和 `ffprobe.exe`；它们缺失是构建失败，不是
   浏览器或系统工具回退的理由。

### 4.2 打包 spec

1. 生产 `hiddenimports` 只保留真实启动或真实 ASR 作业所需模块。
2. 从生产 spec 移除 `unittest`、`unittest.mock`、`pdb` 和仅为冻结测试工具加入的
   import；测试必须由构建外的测试 harness 执行。
3. 不要因为 `from funasr import AutoModel` 就把完整 FunASR/ModelScope/Transformers
   生态全部冻结。需要为 SenseVoice 建立最小、固定、可审计的 import 集，或把本地
   ASR 引擎做成独立、版本化的人工下载组件。
4. 不得仅靠删除目录“瘦身”。每次删除后都要在全新用户数据目录完成真实的 SenseVoice
   首次模型下载、转写、SRT 和 `.words.json` 侧车文件验收。
5. 明确检查并拒绝 `*/tests/*`、`*/test_*`、`*/testing/*`、`*/test_utils/*`、示例、
   训练器和与 SenseVoice 无关的视觉/视频模型。动态 import 所需文件必须以实际运行
   证据保留，而不是凭猜测保留整棵目录。

### 4.3 每个 patch 的预算报告

在签名候选前，对每个生成的 patch 执行以下只读检查，并把输出存入
`release/candidates/<版本>/acceptance.json` 的证据字段：

~~~powershell
@'
import json
import sys
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

patch = Path(sys.argv[1])
with ZipFile(patch) as archive:
    manifest = json.loads(archive.read("patch_manifest.json").decode("utf-8-sig"))
payload = manifest.get("runtime_payload") or {}
groups = defaultdict(lambda: [0, 0])
for name, meta in payload.items():
    parts = name.split("/")
    group = "/".join(parts[:2]) if len(parts) > 1 else name
    groups[group][0] += 1
    groups[group][1] += int(meta.get("size") or 0)
print(json.dumps({
    "archive_bytes": patch.stat().st_size,
    "runtime_payload_files": len(payload),
    "stable_payload_files": len(manifest.get("stable_payload") or {}),
    "largest_groups": sorted(
        ({"path": key, "files": value[0], "bytes": value[1]} for key, value in groups.items()),
        key=lambda item: item["bytes"], reverse=True,
    )[:20],
}, ensure_ascii=False, indent=2))
'@ | python - release_dist\<patch>.zip
~~~

任何超预算、未知目录或测试目录都阻止候选签名。先修构建依赖，再从新的干净 staging
重新生成 runtime manifest、包、hash 和验收记录。

## 5. 正确的 full_baseline 流程

当第 3 节判定为 full_baseline 时：

1. 选择未交付过的新版本号，冻结源码后重新生成 `app/version.json`。
2. 从干净 commit 构建 `web_client/liveclipper_web.spec`，确认包内有固定 WebView2、
   ffmpeg 和 ffprobe。
3. 组装并签名 V3 staging；验证 launcher、updater 和公钥只在允许的全量基线变更中
   改动。
4. 生成全量 ZIP 和 `.zip.sha256.txt`，运行 `zipfile.testzip()`、严格安全审计、
   解压启动和 `/api/runtime`。
5. 用临时干净 AppData 和已有用户数据 AppData 分别启动；验证授权、AI 配置、ASR
   配置、输出目录、用户数据保留和首次健康回滚。
6. 上传原始 ZIP 与 `.sha256.txt` 到百度网盘；从百度网盘回下载一份，核对大小、
   SHA256、ZIP 和解压启动结果。
7. 以 `release_type=full_baseline` 生成签名 `stable.hold.json`，其 `package.url` 是
   百度网盘人工分享页，`package` hash/size/filename 必须是回下载验证过的 ZIP。
8. 更新 `release/baselines.json`，记录精确 ZIP、SHA256、稳定组件版本和 source commit。
9. 从该精确新基线构造一次很小的合成下一版本 delta；只有这条 delta 通过预算、真实
   升级和回滚后，才证明未来业务增量恢复可用。
10. 所有验收为 pass 后，才由发布负责人将候选提升为 ready 并单独推送
    `release/stable.json`。

全量 ZIP 通过百度网盘人工分发，不上传 GitHub Release。GitHub Release 仅允许通过
预算的 `LiveClipperPatch_*.zip` 和对应 `.sha256.txt`。

## 6. 必须通过的验收

除现有语法、完整单元测试、签名、SHA256、ZIP 和安全审计外，必须有：

- frozen 包的 `/api/runtime` 显示 `code_source=bundled`，且
  `active_runtime_dir` 位于解压的 V3 目录内；
- `ffmpeg.exe`、`ffprobe.exe`、固定 WebView2 都存在；
- 真实本地 SenseVoice 首次作业成功，不是只验证 `import funasr`；输出 SRT 和
  `.words.json` 保持既有字段契约；
- 全量包在百度网盘回下载后 hash、解压和启动成功；
- 共享盘场景只分发原始 ZIP。用户必须先复制到本机、解除文件阻止、解压到新空目录，
  不能从共享盘或别人解压后的目录直接运行；
- 自动 delta 仅在通过第 3 节预算后验证 GitHub 回下载、旧版升级、损坏包拒绝、
  中途失败回滚、用户数据保持和更新后完整性；
- acceptance.json 逐条记录输入包、命令、输出版本、SHA256、`/api/runtime` 摘要和
  未通过项。没有证据只可标记为 hold，不能发布 ready。

## 7. 交接完成时必须报告

- 选定的 release_type，以及预算判定数据；
- 纳入/排除文件和依赖目录，尤其是 ASR 依赖；
- 全量 ZIP 或每个合格 patch 的路径、大小、文件数、SHA256；
- 百度网盘或 GitHub 回下载证据；
- 解压运行、真实 ASR 作业、升级/回滚和用户数据保持证据；
- `release/baselines.json`、候选 hold、acceptance、ready stable、commit 和 tag 的
  各自状态；
- 尚未人工确认的事项。
