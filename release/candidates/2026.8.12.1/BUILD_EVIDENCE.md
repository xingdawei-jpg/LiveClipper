# V4 业务增量 2026.8.12.1 构建与验收证据

状态：业务 ZIP 已上传 OSS 并回下载验真；候选通道保持 `hold`；线上 `stable.json` 仍为 `2026.8.11.2 / ready`

## 冻结来源

- 分支：`codex/v4-business-2026.8.12.1`
- 干净构建提交：`26c8083113fb98c1a35f5778240b266dd5e662a9`
- 发布类型：签名业务增量
- 稳定 Core / Launcher：`4.0.0`，本轮不替换
- 纳入 5 个业务运行文件、2 个专项测试文件、`app/version.json` 和维修清单。
- 未修改 `runtime_v4/`、`web_client/desktop.py`、PyInstaller spec、`vendor/`、Core、Launcher、FFmpeg、WebView2、模型或原生依赖。

## 自动化检查

- Python 编译、`node --check web_client/frontend/assets/app.js` 和 `git diff --check` 通过。
- 单品扫描与 V4 Host 专项：30 项通过。
- 完整测试：568 项通过，2 项因 Windows 符号链接权限跳过。
- `tools/build_update_manifest.py --check`：版本、说明和运行文件哈希一致。
- `tools/release_preflight.py --phase development`：通过；仅保留旧 V3 基线包不在 `release_dist`、V4 业务版本领先旧 V3 stable 的已知提示。

## 签名业务包

- 文件：`C:\Users\周美彤\Desktop\LiveClipperBusiness_2026.8.12.1.zip`
- 大小：`1,026,439` 字节
- SHA-256：`83a4a99f4a2ec98b116391832084e26d65646ce47ca1b2fb3541ede551291723`
- 业务 manifest SHA-256：`a46ccebc47721928525a82a6d50f23a9a75746bc3908d5494a5c4d3a49fa6386`
- 签名密钥 ID：`1905329f73f719d3`
- 业务文件数：60；ZIP 条目数：62；解压总字节数：3,939,269。
- 兼容 Core：仅 `4.0.0`。
- 独立包验签、解压目录验签、ZIP 完整性和严格安全审计通过。
- 两次从同一干净提交构建的 ZIP SHA-256 完全一致。
- 包内 `schedule_splitter.py`、`server.py`、`version.json` 和 3 个前端文件与冻结提交逐字节一致。
- 包内没有 Launcher、Host、Core、updater、私钥、spec、`runtime_v4/` 或 `vendor/` payload。
- 一次错误旧密钥签名被公钥验签立即拒绝；无效 ZIP 已删除且从未上传。

## 签名候选通道

- 文件：`stable.hold.json`
- 状态：`hold`
- 允许来源：`2026.8.5.2`、`2026.8.7.1`、`2026.8.8.1`、`2026.8.11.1`、`2026.8.11.2`
- 兼容 Core：仅 `4.0.0`
- 文档 SHA-256：`31e17458c3a015a46f35c462acbecdcc391039fc92a821556ba3e9bda95ee57d`
- 每个允许来源的验证结果均为 `available=false`、`reason=channel_hold`。
- 未生成或上传 `2026.8.12.1 ready stable.json`。

## 真实版本链升级

- 从桌面正式全量基线 `LiveClipperWeb_v4.0.0_2026.8.7.1_全量包.zip` 解压到全新隔离目录 `C:\lc_v4_update_verify_20260812_1`。
- 先安装并启动确认 `2026.8.11.2 / Core 4.0.0`，状态变为 `pending=false`，完整性通过。
- 再安装 `2026.8.12.1`，更新指针的 `previous` 为 `2026.8.11.2`，首次启动健康后 `pending=false`。
- `/api/runtime`：HTTP 200，业务版本 `2026.8.12.1`、Runtime Layout `4`、Core `4.0.0`、Launcher `4.0.0`、`code_source=bundled`。
- 目标业务完整性：`ok=true`，检查 60 个文件，无不一致，manifest SHA-256 与 ZIP 一致。
- 根 Launcher SHA-256 在两次业务更新前后均为 `66a3a2b3858a849a6289a0d80490ef6f14d2775c0c92bfe301b4912b3d525194`。
- Host SHA-256 在两次业务更新前后均为 `68d99edf504c79220d535a93accb11bfdef2d9e75c0eed142f90b5f0b7cd72a0`。
- 隔离 AppData 哨兵在升级前后保留。

## 自动回退

- 在隔离验收目录中故意修改 `2026.8.12.1/business/bundle_entry.py`，制造签名清单大小不一致。
- Launcher 拒绝损坏的新业务选择，并自动恢复到 `2026.8.11.2 / Core 4.0.0`。
- 回退状态：`pending=false`，`failed=2026.8.12.1`。
- 回退原因：`current selection failed verification: bundle file size mismatch: bundle_entry.py`。
- 回退版 `/api/runtime` 完整性 `ok=true`，检查 60 个文件，无不一致。
- 隔离 AppData 哨兵在回退后仍保留，测试 Host 已停止。

## OSS 资产与线上边界

- 新对象：`https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/LiveClipperBusiness_2026.8.12.1.zip`
- 上传前对象不存在；没有覆盖同名旧资产。
- 公网回下载大小为 `1,026,439` 字节，SHA-256 与本地 ZIP 完全一致。
- 公网正式 `stable.json` 在本次验收后仍为 `2026.8.11.2 / ready`，未被修改。

## 未完成与发布边界

- 常用工作区、桌面和交接目录未发现可配对的真实排品表与直播原片，因此本轮没有完成真实素材的单品扫描人工验收。
- 另一台电脑尚未从公网通道自动下载并安装 `2026.8.12.1`。
- 当前 Core `4.0.0` 没有独立 RC/灰度通道；正式上传 `ready stable.json` 会同时向所有符合版本条件的 V4 用户开放。
- 在真实素材验收和另一台电脑确认前，本候选保持 `hold`，不能宣称已经正式发布。
