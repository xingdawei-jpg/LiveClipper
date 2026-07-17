# LiveClipper 稳定发布策略

本文件是 LiveClipper 打包、版本、全量包和自动更新的唯一发布政策。实现细节见 docs/ARCHITECTURE_V3.md，执行步骤见 docs/RELEASE_PROCESS_V3.md。发生冲突时，以本文件和 release/release_policy.json 为准。

## 1. 不可违反的规则

1. 只从 C:\Users\周美彤\Documents\GitHub\LiveClipper 构建。
2. 发布前必须执行 Cross-Window Sync Gate，逐项确认已修改和未跟踪文件。
3. 开发完成不等于已经发布。普通业务更新没有签名补丁、远端下载验证和 stable 通道发布，用户就没有收到更新；新全量基线还必须完成百度网盘验证。
4. app/version.json、稳定组件版本、正式基线和线上通道是四种不同状态，禁止互相代替。
5. 稳定 launcher、updater、公钥、信任根或安装布局变化时，必须建立新全量基线，禁止放进普通增量包。
6. 普通业务版本的增量包必须包含零个稳定组件 payload。
7. 全量包通过百度网盘人工分发；GitHub Release 只存放自动更新使用的签名补丁及校验文件。
8. 候选通道必须是 hold，并放在 release/candidates/<版本>/；验收期间不得覆盖线上 release/stable.json。
9. release/stable.json 必须最后单独发布。没有完整验收记录时禁止改为 ready。
10. 不降低远端版本，不覆盖运行中的版本目录，不删除用户的整个 AppData 目录，不把私钥、用户配置或测试产物提交到 Git。

## 2. 四个真相源

| 状态 | 唯一真相源 | 含义 |
| --- | --- | --- |
| 业务运行时版本 | app/version.json | 当前源码准备构建的完整运行时版本、build ID 和资源清单 |
| 稳定组件版本 | tools/runtime_v3_versions.py | launcher 和 updater 的唯一版本源 |
| 正式全量基线 | release/baselines.json | 可作为增量源的完整包文件名、SHA256、大小和稳定组件版本 |
| 线上自动更新 | release/stable.json | 用户当前能看到并安装的签名自动更新通道 |

release/github/v<版本>.json 只描述 GitHub 补丁 Release 的资产，不是版本源。release_dist 只是本机构建输出，不是发布事实。

## 3. 当前登记状态

截至 2026-07-16：

- 当前全量基线：2026.7.15.1。
- 稳定组件：launcher 1.1.0，updater 1.3.0。
- 15.1 全量包已经通过百度网盘人工交付给部分用户，没有发布 GitHub 全量包。
- 线上 release/stable.json 仍是 2026.7.14.7，updater 1.2.0。
- 14.7 及更早的旧稳定层不能用普通 delta 跨到 updater 1.3.0；需要安装 15.1 或更高版本全量包。
- 下一次普通业务更新必须以 release/baselines.json 登记的 15.1 精确包为增量源。

这个分离状态是允许的，但必须显式登记。不得因为 app/version.json 较新就声称 stable 已发布。

## 4. 版本号规则

版本格式为 YYYY.M.D.N。

- 只有确认进入发布候选时才升级版本号。
- 同一天每次用户可见发布递增 N；失败的候选版本号不重复使用。
- app/version.json 中 version、latest_version 和 build_id 必须完全一致。
- 所有源码修改完成后再生成 app/version.json；生成后若任何 runtime 文件变化，必须重新生成。
- launcher/updater 版本不跟随业务版本自动增加，只在稳定组件代码真实变化时增加。
- release/stable.json 的版本可以暂时落后 app/version.json，但绝不能领先。
- 已交付给用户的版本不可重打同名包；修复后使用更高版本。

## 5. 发布类型决策

### 普通业务运行时版本

适用于 app、web_client 前端和服务端、AI、ASR、剪辑逻辑等变化，且稳定组件和信任根未变化。

必须产出：

- 最终 frozen runtime 和 Runtime V3 staging 目录，不要求生成或上传新的全量 ZIP；
- 从当前正式基线或当前受支持版本到目标版本的签名 delta；
- 仍受支持且路径过长的旧基线直达目标版本 rollup；
- GitHub 补丁 Release；
- 候选 hold 清单和最终 ready stable 清单。

普通 delta 的 stable_payload_files 必须为 0。新用户继续使用最近一次人工分发并验证过的完整包，启动后再通过 signed delta 升到最新业务版本。普通业务更新不得因为没有新的百度网盘全量包而被阻止。

### 新全量基线

以下任一变化都必须建立新基线：

- tools/liveclipper_launcher.py 或 launcher 版本；
- tools/liveclipper_update_agent.py 或 updater 版本；
- release_update_public_key.pem 或信任根；
- Runtime V3 目录布局、current.json、install_manifest 格式；
- 普通补丁无法安全迁移的稳定安装状态。

新基线通过百度网盘分发，不在 GitHub Release 上传全量 ZIP。旧稳定层用户必须安装全量包。基线被登记后，必须用该精确包构造一个合成的下一版本 delta，证明后续普通更新可用。

### 不发布

只有本机故障、用户配置损坏、权限或安全软件问题时，先完成单机诊断。没有确认产品代码缺陷时，不为单台电脑紧急升级全体用户。

## 6. 固定发布顺序

1. 执行 Cross-Window Sync Gate，建立明确的纳入和排除清单。
2. 运行语法、前端、单元测试和安全检查。
3. 冻结源码，决定普通业务版本或新全量基线。
4. 升级业务版本；仅在稳定层变化时升级 launcher/updater。
5. 重新生成 app/version.json，确认所有 runtime hash 与最终源码一致。
6. 提交发布源码，但不修改 release/stable.json。
7. 从该干净 commit 构建 frozen runtime 和 V3 staging；只有 full_baseline 才生成全量 ZIP。
8. business_runtime 直接从精确旧包和目标 staging 生成签名 delta；full_baseline 生成完整包和基线登记材料。
9. 生成校验文件和 release/candidates/<版本>/stable.hold.json，运行 candidate preflight。
10. business_runtime 只上传补丁到 GitHub Release；full_baseline 只把全量 ZIP 上传百度网盘。
11. business_runtime 从 GitHub 重新下载补丁；full_baseline 另外从百度网盘重新下载全量包，核对大小、SHA256 和 ZIP。
12. business_runtime 完成一次精确旧版本升级、失败回滚、用户数据保持和更新后 runtime 完整性；full_baseline 另外完成干净/污染 AppData、首次启动和健康回滚。
13. 填写 release/candidates/<版本>/acceptance.json，所有必需 gate 都必须是 pass。
14. 把 hold 候选 SHA256 写入 acceptance.json，使用 tools/promote_release_channel.py 从该候选生成 ready stable。
15. 运行 tools/release_preflight.py --phase publish；通过后只提交并推送 release/stable.json。
16. 远端核对 commit、GitHub Release 资产和 stable.json；保留全部验收证据。

任何一步失败都停在当前步骤。修复源码后回到第 1 步，不得沿用旧 hash、旧 ZIP 或旧验收记录。

## 7. 候选与 stable 通道

- live release/stable.json 在整个构建和验收期间保持原样，继续服务现有用户。
- 候选清单放在 release/candidates/<版本>/stable.hold.json。
- hold 候选可以包含完整补丁图，但客户端因 channel_status=hold 不会安装。
- 候选和最终清单都必须签名。
- 最终 ready 清单只能由同一候选和同一组远端资产产生。
- stable 发布必须是最后一个发布 commit，不得和源码、构建脚本或功能修改混在一起。
- 回滚时不降低 stable 版本；停止通道并发布更高修复版本。

## 8. 分发规则

### 百度网盘全量包

以下规则只适用于 full_baseline。普通 business_runtime 不生成或上传新的全量包。

- 上传原始全量 ZIP 和 .sha256.txt。
- 分享页可以用于人工下载，但不能作为自动补丁 URL。
- 下载后的 ZIP 必须与本地正式包大小和 SHA256 完全一致。
- 用户先把 ZIP 复制到本机，解除 Windows 文件阻止，再解压到新的空目录。
- 不覆盖旧解压目录，不从共享盘直接运行，不把别人已解压的文件夹重新压缩分发。

### GitHub 自动更新

- GitHub Release 只允许 LiveClipperPatch_*.zip 及其 .sha256.txt。
- 禁止上传 LiveClipperWeb_*_full*.zip 或 baseline 全量包。
- stable 清单中的补丁源必须是 github.com 的 HTTPS Release URL。
- 当前策略只使用一个 GitHub 主源，不再要求阿里云 OSS。
- GitHub 下载失败时保留 .part 和错误信息；没有镜像时提示稍后重试或使用百度网盘全量包。
- 每个 GitHub Release 都由 .github/workflows/publish-incremental-release.yml 下载并复验资产集合、大小、SHA256 和 ZIP 完整性。

## 9. 必须保留的验收证据

所有发布都保留：

- Cross-Window Sync Gate 文件清单；
- 版本、build ID、launcher/updater 版本；
- 每个本次发布资产的路径、大小、SHA256；
- 签名、manifest、ZIP 和安全审计结果；
- 对应 release_type 要求的目标运行时或完整包运行结果；
- 15.1 精确基线到目标版本的真实更新结果；
- 错误补丁拒绝、失败回滚和用户数据保持结果；
- 更新后 runtime 文件完整性；
- GitHub 重新下载后的 hash；
- 用户设置、授权、AI/ASR 配置和输出目录保持结果；
- stable 最后发布的 commit 和远端内容。

full_baseline 另外保留：全量包路径和 SHA256、百度网盘回下载 hash、干净/污染 AppData、目标 EXE 的 /api/runtime、稳定组件完整性和首次启动健康回滚。

补丁链、断点续传、签名错误和磁盘不足由 source test suite 持续覆盖；只有 updater、launcher 或安装事务代码变化时，才重复对应的人工端到端矩阵。

只写“测试通过”不算证据，必须记录命令、输入包、输出版本和 hash。

## 10. 禁止操作

- git add -A；
- 从旧 dist 或用户机器复制文件拼正式包；
- 在 app/version.json 生成后继续改 runtime 文件而不重建；
- 用源码启动结果代替解压包验收；
- 在 8765 上未核对进程归属就相信 /api/runtime；
- 给普通 delta 加 launcher、updater、公钥或信任根；
- 验收前把 live stable 改为 hold 或 ready；
- 把 GitHub tag、GitHub commit、百度网盘文件或本地 release_dist 单独当作“发布完成”；
- 删除整个 %APPDATA%\LiveClipper 解决配置问题；
- 提交 release private key、授权密钥、用户配置、缓存和测试输出。

## 11. 标准检查命令

~~~powershell
git status --short
git diff --stat
git ls-files --others --exclude-standard -- app web_client tools vendor assets packaging docs release tests

python tools\release_preflight.py --phase development
python -m py_compile app\updater.py tools\liveclipper_update_agent.py tools\build_update_manifest.py tools\build_release_channel.py tools\release_preflight.py tools\promote_release_channel.py
node --check web_client\frontend\assets\app.js
python -m unittest discover -s tests -p "test_*.py" -v
~~~

候选和发布命令见 docs/RELEASE_PROCESS_V3.md。私钥路径只通过命令参数或本机环境传入，任何文档、日志和清单都不得记录私钥内容。
