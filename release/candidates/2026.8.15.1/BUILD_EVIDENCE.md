# LiveClipper V4 Business Update 2026.8.15.1

状态：本地签名候选已构建并完成自动化、隔离升级与回滚验证；通道保持 `hold`，未上传、未发布。

## 冻结信息

- 冻结提交：`f9521ed`
- 业务版本 / build ID：`2026.8.15.1`
- 包类型：Runtime V4 签名业务更新包
- 兼容 Core：仅 `4.0.0`
- Launcher、Host、Core、Python、FFmpeg、WebView2、模型、原生依赖和信任根改动：无

## 源码门禁

- Cross-Window Sync Gate：完成；纳入 24 个明确文件，无用途不明文件。
- Python 语法编译：通过。
- 前端 JavaScript 语法检查：通过。
- Git 差异格式检查：通过。
- 针对性测试：287 项通过。
- 最终版本清单生成后完整测试：629 项通过，2 项跳过。
- `app/version.json` 版本、build ID、说明和运行文件哈希复核：通过。
- development preflight：通过；保留 V3 旧基线 ZIP 不在 `release_dist`、V3 live channel 落后和候选工作区文件提示，不影响独立 V4 业务包边界。

## 签名业务包

- 文件：`release_artifacts/v4_business/LiveClipperBusiness_2026.8.15.1.zip`
- 大小：`1,070,534` 字节
- SHA256：`fb4ed09bbeb404f76c41b9cf68f570de522d8856ae7c4a56c3bb3bdf3dffa225`
- Bundle manifest SHA256：`6cd5f5b1d31de024733f277b7dc89ea8d2b49d2956ed322b0947f316c6d299f5`
- 签名 key id：`1905329f73f719d3`
- 签名业务文件：60
- ZIP 条目：62
- 解压总大小：`4,094,250` 字节
- 两次独立构建 SHA256 完全一致。
- Bundle 签名、目标版本和 Core 兼容校验：通过。
- ZIP 物理完整性：通过。
- 严格发布安全审计：通过。
- 冻结提交中所有本次修改的业务运行文件与 ZIP 内文件逐字节一致。
- Core/Launcher/native/private-key/tests/docs/cache/logs 边界审计：未发现越界文件。

## Hold 候选通道

- 文件：`release/candidates/2026.8.15.1/stable.hold.json`
- 状态：`hold`
- 文档 SHA256：`07201060350493dea21bb9c49fe39baff8f486e930f109194b837c6a1165677c`
- 允许来源：`2026.8.5.2`、`2026.8.7.1`、`2026.8.8.1`、`2026.8.11.1`、`2026.8.11.2`、`2026.8.12.1`、`2026.8.12.2`
- 上述所有来源在 Core `4.0.0` 下都得到 `channel_hold`，不会被客户端安装。

## 隔离升级与回滚

- 从已签名 `2026.8.12.2` 业务包恢复并首次启动确认隔离来源版本。
- 来源运行时：`2026.8.12.2`，Runtime V4，Core/Launcher `4.0.0`，bundled code，完整性 `60/60`。
- 安装 `2026.8.15.1` 后，状态正确记录 `current=2026.8.15.1`、`previous=2026.8.12.2`、`pending=true`。
- 首次启动健康确认后：`current=2026.8.15.1`、`pending=false`。
- `/api/runtime`：版本 `2026.8.15.1`、Runtime V4、Core/Launcher `4.0.0`、签名 bundle verified、完整性 `60/60`。
- Launcher SHA256 更新前后保持 `66a3a2b3858a849a6289a0d80490ef6f14d2775c0c92bfe301b4912b3d525194`。
- Host SHA256 更新前后保持 `68d99edf504c79220d535a93accb11bfdef2d9e75c0eed142f90b5f0b7cd72a0`。
- 隔离用户数据 sentinel 更新后仍存在。
- 故意仅损坏隔离 `2026.8.15.1` 的 `bundle_entry.py` 后，Launcher 拒绝新版本并自动恢复 `2026.8.12.2`。
- 回滚后：`current=2026.8.12.2`、`failed=2026.8.15.1`、`pending=false`、运行时完整性 `60/60`，用户数据 sentinel 仍存在。

## 未执行 / 发布边界

- 未上传业务 ZIP 到 OSS、GitHub 或 Cloudflare。
- 未生成或上传 `ready stable.json`，未改动线上通道。
- 未在第二台物理电脑执行人工自动更新验收。
- 未用真实长直播素材重新跑一轮人工视觉/听感验收；本次功能由针对性和完整测试覆盖。
- 因为 Core 未变化，本次没有制作也不应制作全量包。
