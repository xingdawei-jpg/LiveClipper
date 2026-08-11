# V4 业务增量 2026.8.11.1 构建与验收证据

状态：`hold`，未上传、未发布、未修改线上稳定通道

## 冻结来源

- 分支：`codex/v4-business-2026.8.11.1`
- 干净构建提交：`fe161cc`
- 发布类型：业务增量
- 稳定 Core / Launcher：`4.0.0`，本轮不替换
- 相对线上 V4 基线 `2bd10b0`，最终树不包含 `runtime_v4/`、`web_client/desktop.py`、PyInstaller spec 或 `vendor/` 变化。

## 自动化检查

- `python -m unittest discover -s tests -p "test_*.py"`
- 结果：556 项通过，2 项因 Windows 符号链接权限跳过。
- `release_preflight.py --phase development`：通过；保留旧 V3 baseline ZIP 不在 `release_dist`、业务版本领先 V3 stable 的提示。

## 签名业务包

- 文件：`LiveClipperBusiness_2026.8.11.1.zip`
- 大小：`1012085` 字节
- SHA-256：`00a6520e52d269fc71ed6b7cd0999ebe97a2087b6e4d9383ad40579e541bfdaa`
- 业务 manifest SHA-256：`f93dbdc019431dd84143a721c6ec0bf195fdcbcbcfd55e14fd4c2ff5a26b7956`
- 签名密钥 ID：`1905329f73f719d3`
- 文件数：60
- 兼容 Core：仅 `4.0.0`

## 签名候选通道

- 文件：`stable.hold.json`
- 状态：`hold`
- 允许来源：`2026.8.5.2`、`2026.8.7.1`、`2026.8.8.1`
- 文档 SHA-256：`eff8d088f0089fc0b3e4111e24b4e16fa28150fb1ac53d61c3f657ef7f1fb8be`
- 本地计划结果：`available=false`、`reason=channel_hold`

## 从上一正式 V4 版本升级

- 基线：桌面留存的正式 `LiveClipperWeb_v4.0.0_2026.8.7.1_全量包.zip`，外层 SHA-256 与随包校验文件一致。
- 安装结果：`2026.8.11.1 / Core 4.0.0`，`activated=true`。
- 根 Launcher SHA-256：升级前后相同。
- `LiveClipperHost.exe` SHA-256：升级前后相同。
- 首次启动：`/api/runtime` 返回业务版本 `2026.8.11.1`、Core `4.0.0`、Launcher `4.0.0`。
- 业务完整性：`ok=true`、检查 60 个文件、无不一致；`current.json.pending=false`。
- 用户数据：测试哨兵升级前后保留。

## 自动回退

- 故意改坏新业务 `bundle_entry.py` 后启动。
- Launcher 返回码：`2`。
- `current.json` 自动恢复为 `2026.8.7.1 / Core 4.0.0`，`pending=false`。
- 回退原因：`bundle file size mismatch: bundle_entry.py`。
- 恢复版 `/api/runtime` 完整性正常，57 个业务文件无不一致；用户数据哨兵仍保留。

## 未完成与分离事项

- 业务 ZIP 和 `stable.hold.json` 尚未上传，因此没有宣称远程自动下载成功。
- jsDelivr / Cloudflare Pages 配置端点未完成同内容发布前，不能切换 `ready`。
- 另一台 V4 机器的真实自动下载、真实素材和回退仍是最终人工验收项。
- Core `4.0.1` 全量基线候选已隔离到 `C:\lc_v4_hold_20260811\core_4.0.1_baseline_candidate`，不属于本业务更新，也不发给现有 V4 用户。
- 旧 `2026.8.7.1` 全量 ZIP 的 `_internal/web_client/desktop.py` 不符合当前全 Core 清单校验；Core 4.0.0 已确认版本只快速复查入口文件。这些属于独立 Core 基线问题，不能借本业务更新要求用户重装。
