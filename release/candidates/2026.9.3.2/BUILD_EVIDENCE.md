# LiveClipper V4 Business Update 2026.9.3.2

状态：已发布至 V4 OSS 主更新通道；不是全量包。

## 冻结与验证

- 冻结提交：`a31c0496041fe23815eb5f3ea7d49d6da36e883c`。
- 完整回归：干净 AppData 下 `1033` 项通过，`2` 项环境跳过。
- 包：`C:\lc_v4_business\LiveClipperBusiness_2026.9.3.2.zip`，`1,458,359` 字节。
- ZIP SHA-256：`47632433d6711cf305d96f953389577f5329b359589848cef6b723cc8b5f1eef`。
- Bundle manifest SHA-256：`e5ec15d37b0d4ef195856a19719dc497526cb54a7d7a260ea0288527807567a0`。
- 两次独立构建字节完全一致；签名 key id：`1905329f73f719d3`；业务文件：`78`；兼容 Core：`4.0.0`。

## 实际升级与回滚

- 隔离 V4 基线升级：`2026.8.5.2 -> 2026.8.12.2 -> 2026.9.3.2`。
- 更新后的业务包签名验证通过；故意破坏 `2026.9.3.2` 入口后，启动器自动恢复到 `2026.8.12.2`。
- Launcher SHA-256 未变：`66a3a2b3858a849a6289a0d80490ef6f14d2775c0c92bfe301b4912b3d525194`。
- Host SHA-256 未变：`68d99edf504c79220d535a93accb11bfdef2d9e75c0eed142f90b5f0b7cd72a0`。
- 用户数据标记未被修改。

## 线上发布

- ZIP：`https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/LiveClipperBusiness_2026.9.3.2.zip`。
- 通道：`https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/stable.json`。
- 公网 ZIP SHA-256 与本地一致；公网通道文件 SHA-256：`a8d75a2d0ad74838c982e75abd3f76fd866afef0238e55b3b4e10802035121ac`。
- 公网通道已验签，`2026.8.12.2 / Core 4.0.0` 判定为 `update_available`。

## 已知边界

- 本次按用户授权优先发布业务包；另一台干净电脑的 GUI 复验尚未执行。
- `jsDelivr` 和 `pages.dev` 备用通道均为 404；OSS 主源正常且是客户端首选读取地址。
