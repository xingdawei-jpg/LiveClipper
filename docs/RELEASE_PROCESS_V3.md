# LiveClipper Release Process V3

本文件是 Runtime V3 可执行发布步骤。政策见 docs/RELEASE_POLICY.md，机器规则见 release/release_policy.json。

## 1. 发布前状态

先执行 docs/PACKAGING_WINDOW_ENTRY.md 的 Cross-Window Sync Gate，并运行：

~~~powershell
python tools\release_preflight.py --phase development
~~~

必须明确区分：

- 源码运行时版本；
- 当前全量基线；
- 线上 stable 版本；
- launcher/updater 版本；
- 本次候选是否改变稳定层。

当前登记基线从 release/baselines.json 读取，禁止凭文件夹日期或记忆选择增量源。

## 2. 密钥

- Ed25519 私钥只能位于仓库和 release 输出目录之外。
- Git 只提交 app/release_update_public_key.pem。
- 私钥路径通过 --private-key 参数传入，不写入脚本、文档、日志或 PowerShell 历史模板。
- 公钥轮换必须建立新全量基线，并由旧信任根完成迁移验证。
- 正式签名后必须立即用包内同一公钥复验。

## 3. 冻结源码和版本

1. 审核所有修改和未跟踪文件。
2. 运行 Python 编译、JavaScript 语法和完整测试。
3. 决定 release_type：business_runtime 或 full_baseline。
4. 选择未使用的新业务版本。
5. 只有 full_baseline 且稳定组件真实变化时才修改 tools/runtime_v3_versions.py。
6. 最后生成 app/version.json。
7. 再次运行 git diff；若 runtime 文件在 manifest 之后变化，重新生成 manifest。
8. 提交发布源码，不包含 release/stable.json。
9. 确认构建使用的 HEAD 与 runtime_manifest.source_commit 一致。

禁止从脏源码或旧 dist 构建正式包。

## 4. 构建目标运行时

所有发布都从干净 commit 构建 frozen runtime 和 Runtime V3 staging。business_runtime 到此为止，不生成新的全量 ZIP；只有 full_baseline 执行下面的完整包步骤：

1. 用 web_client/liveclipper_web.spec 干净构建 frozen runtime。
   The PyInstaller input must contain the pinned WebView2 runtime; a missing runtime is a build failure, never a browser-fallback release.
   It must also contain _internal/ffmpeg/ffmpeg.exe and _internal/ffmpeg/ffprobe.exe; either missing tool is a build failure, never an external-installation fallback release.
2. 从 release/baselines.json 当前基线复用 launcher、updater 和公钥的精确字节。
3. 新基线才重新构建稳定组件。
4. 用 tools/build_v3_package.py 组装 V3 staging 并签名。
5. 检查根 launcher、current.json、install_manifest.json、updater 和 versions/<版本>。
6. 确认没有松散业务 Python 文件。
7. 运行 tools/audit_release_security.py。
8. full_baseline 新建 ZIP，不复用旧 ZIP；运行 zipfile.testzip。
9. full_baseline 生成 .sha256.txt。
10. full_baseline 解压到全新目录，以临时 AppData 启动目标 EXE。

/api/runtime 验收必须核对返回端口的进程属于刚解压的 EXE，并确认 active_runtime_dir 指向解压目录内 versions/<版本>。

## 5. 构建增量

business_runtime 必须：

1. 使用 release/baselines.json 当前基线的精确全量包作为 source。
2. 使用本次最终 V3 staging 目录作为 target；build_delta_package.py 支持目录，不要求先压制全量 ZIP。
3. 运行 tools/build_delta_package.py。
4. 复验外层 SHA256、patch manifest 签名、source/target runtime manifest 和 payload。
5. stable_payload_files 必须等于 0。
6. 补丁 URL 使用 GitHub Release 的 HTTPS URL。
7. 当某个受支持版本到 latest 超过 2 条边时生成直达 rollup。
8. updater 1.3.0 必须在安装前下载并验证整条链，完成后只切换一次 current.json。

full_baseline 不允许从旧稳定层生成普通 delta。必须先通过全量包迁移，再用该新基线做合成下一版本 delta 测试。

## 6. 候选清单

候选路径：

    release/candidates/<版本>/stable.hold.json
    release/candidates/<版本>/acceptance.json

生成候选时：

- channel_status 必须为 hold；
- 候选可以包含完整 patch 图；
- live release/stable.json 保持不变；
- business_runtime 的 package 元数据为空，full_baseline 的 package 元数据指向本次全量 ZIP；
- patch source 只能是 github.com HTTPS Release URL；
- minimum launcher/updater 必须与目标 runtime 和 release 类型一致。

生成并检查：

~~~powershell
# 普通业务更新
python tools\build_release_channel.py --release-type business_runtime --patch release_dist\<补丁>.zip --patch-url https://github.com/<仓库>/releases/download/<tag>/<补丁>.zip --channel-status hold --private-key <仓库外私钥路径> --output release\candidates\<版本>\stable.hold.json
python tools\release_preflight.py --phase candidate --manifest release\candidates\<版本>\stable.hold.json --patch release_dist\<补丁>.zip

# 新全量基线
python tools\build_release_channel.py release_dist\<全量包>.zip --release-type full_baseline --url https://pan.baidu.com/<人工分享地址> --channel-status hold --private-key <仓库外私钥路径> --output release\candidates\<版本>\stable.hold.json
python tools\release_preflight.py --phase candidate --manifest release\candidates\<版本>\stable.hold.json --package release_dist\<全量包>.zip
~~~

build_release_channel.py 不能生成 ready，也不能写 release/stable.json。任何 error 都阻止上传。

## 7. 分发

### 全量包

本节只适用于 full_baseline。business_runtime 跳过百度网盘。

- 上传百度网盘，不上传 GitHub Release。
- 同时提供 .sha256.txt。
- 从百度网盘重新下载一份，复验大小、SHA256 和 ZIP。
- 共享盘用户先复制 ZIP 到本机、解除文件阻止、解压到新目录。

### 自动补丁

- GitHub Release 仅上传 LiveClipperPatch_*.zip 和对应 .sha256.txt。
- release/github/v<版本>.json 资产列表不得包含 full 或 baseline ZIP。
- 发布后等待 GitHub Actions 验证资产集合、大小、SHA256 和 ZIP 完整性。
- 从 GitHub Release URL 重新下载补丁用于真实升级验收。

仅创建 tag、仅推送 commit、仅上传百度网盘或仅生成本地 ZIP，都不等于发布完成。

## 8. 验收矩阵

所有版本：

- 签名、SHA256、大小、ZIP；
- 安全审计和无松散业务 Python；
- 授权、AI 配置、ASR 配置和输出路径保持；

business_runtime 额外要求：

- 一个仍受支持的真实旧版本到目标版本；
- GitHub 回下载后的大小、SHA256 和 ZIP 完整性；
- 错误补丁拒绝、失败回滚和用户数据保持；
- 更新后 runtime manifest 文件完整性。

补丁链、断点续传、签名错误和磁盘不足由 source test suite 持续覆盖；只有 updater、launcher 或安装事务代码变化时，才重复对应的人工端到端矩阵。

full_baseline 额外要求：

- 百度网盘下载 hash；
- 干净/污染 AppData 和 /api/runtime；
- Windows 文件阻止/共享盘复制后的启动说明；
- launcher/updater/公钥 hash；
- 旧版本不能通过普通 delta 覆盖稳定层；
- clean install、首次健康确认和回滚；
- 从该基线构造合成下一版本 delta，stable payload 为 0；
- exact baseline updater 成功应用合成 delta。

每个结果写入 acceptance.json，证据字段记录命令、输入文件、hash、设备和 /api/runtime 摘要。

## 9. 发布 stable

验收完成后：

1. acceptance.json 所有必需 gate 为 pass。
2. GitHub patch Release 已通过 workflow。
3. business_runtime 的 GitHub 回下载 hash 已通过；full_baseline 另外要求百度网盘回下载 hash。
4. 源码 commit、候选 manifest、包和补丁版本完全一致。
5. 计算 stable.hold.json 的规范化内容 SHA256，写入 acceptance.json 的 candidate_sha256；禁止使用文件字节哈希，避免 Windows/Git 换行转换破坏绑定。
6. 使用 promote_release_channel.py 从同一签名 hold 候选生成 ready stable。
7. 运行 publish preflight。
8. 确认工作树除 release/stable.json 和候选证据外没有其他变化。
9. 单独提交并推送 release/stable.json。
10. 核对远端 main、GitHub API 和签名清单，最后才通知用户“更新已发布”。

~~~powershell
python tools\promote_release_channel.py --candidate release\candidates\<版本>\stable.hold.json --print-candidate-sha256
python tools\promote_release_channel.py --candidate release\candidates\<版本>\stable.hold.json --acceptance release\candidates\<版本>\acceptance.json --private-key <仓库外私钥路径> --confirm-publish-ready
# business_runtime 不传 --package；full_baseline 必须传 --package
python tools\release_preflight.py --phase publish --manifest release\stable.json --patch release_dist\<补丁>.zip --acceptance release\candidates\<版本>\acceptance.json
~~~

raw.githubusercontent.com 可能有 CDN 延迟。先以 git ls-remote、origin/main 和 GitHub API 为准。

## 10. 回滚和紧急停止

- 验收前失败：删除候选 staging，live stable 不变。
- ready 发布后发现问题：停止通道，不降低版本，发布更高修复版本。
- current.json 切换前失败：旧版本保持不变。
- 首次健康失败：launcher 自动恢复 previous。
- 用户配置迁移必须向后兼容；不可逆迁移需要独立事务备份。
- 单台电脑配置损坏时只备份 ai_settings.json 或 user_data_location.json，禁止删除整个 LiveClipper AppData。

## 11. 最终报告

报告格式必须按 docs/PACKAGING_WINDOW_ENTRY.md。未测试项目必须直接列出；缺少远端下载、解压运行或真实升级证据时，状态只能是“未发布”或“候选 hold”。
