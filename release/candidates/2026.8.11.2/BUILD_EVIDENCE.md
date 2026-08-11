# V4 业务增量 2026.8.11.2 构建与验收证据

状态：业务 ZIP 已上传 OSS；签名 `ready stable.json` 已在本地生成，尚未出现在客户端固定读取的线上地址

## 冻结来源

- 分支：`codex/v4-business-2026.8.11.2`
- 干净构建提交：`090555752b5b952830d5658003a3b489528f80a3`
- 发布类型：签名业务增量
- 稳定 Core / Launcher：`4.0.0`，本轮不替换
- `2026.8.11.1` 在正式发布前被本候选替代；禁止覆盖其同名 OSS ZIP 或发布旧通道文件。
- 相对线上 V4 基线 `2bd10b0`，本轮新增单品扫描改动不包含 `runtime_v4/`、`web_client/desktop.py`、PyInstaller spec 或 `vendor/` 变化。

## 自动化检查

- 单品扫描与 V4 Host 专项：20 项通过。
- `python -m unittest discover -s tests -p "test_*.py"`：558 项通过，2 项因 Windows 符号链接权限跳过。
- `tools/build_update_manifest.py --check`：版本与运行文件哈希一致。
- `release_preflight.py --phase development`：通过；保留旧 V3 baseline ZIP 不在 `release_dist`、业务版本领先旧 V3 stable 的提示。
- `node --check web_client/frontend/assets/app.js`、Python 编译检查和 `git diff --check`：通过。

## 签名业务包

- 文件：`C:\Users\周美彤\Desktop\LiveClipperBusiness_2026.8.11.2.zip`
- 大小：`1,017,623` 字节
- SHA-256：`99a70ad7c0da1ea4bf2dacbb4790a7b285fd17e722f527eb3f0785b620209864`
- 业务 manifest SHA-256：`a3c1fca3d56b8a5272cb1ef9aa2b14794408ea8f4b68fad11c71dc1981031625`
- 签名密钥 ID：`1905329f73f719d3`
- 文件数：60
- 兼容 Core：仅 `4.0.0`
- 独立包验签与解压后目录验签均通过。
- OSS 公网文件大小与 SHA-256 已重新下载核对，和本地业务包完全一致。

## 签名候选通道

- 文件：`stable.hold.json`
- 状态：`hold`
- 允许来源：`2026.8.5.2`、`2026.8.7.1`、`2026.8.8.1`、`2026.8.11.1`
- 文档 SHA-256：`ff025d7e422a211f59681b7de66aec8a622bb40bab27cd355134ecfbd1cf2848`
- 所有允许来源的本地计划结果：`available=false`、`reason=channel_hold`
- 当前只声明已经验证成功的 OSS 包地址。

## 签名 ready 通道

- 文件：`stable.json`
- 状态：`ready`
- 发布时间字段：`2026-08-11 21:53:23`
- 允许来源：`2026.8.5.2`、`2026.8.7.1`、`2026.8.8.1`、`2026.8.11.1`
- 兼容 Core：仅 `4.0.0`
- 文档 SHA-256：`e597ab76ad38932ac4172c2279917f80e0c63f81fbfd7ebfe8a34bfb43d7daef`
- 签名密钥 ID：`1905329f73f719d3`
- 四个允许来源的本地验证结果均为 `available=true`、`reason=update_available`。
- 正式上传对象名必须精确为 `oss://lc-update/liveclipper/v4/stable.json`；不能上传或改名使用 `stable.hold.json`。

## 从上一正式 V4 版本升级

- 基线：桌面留存的正式 `LiveClipperWeb_v4.0.0_2026.8.7.1_全量包.zip`，重新解压到 `C:\lc_v4_update_verify_20260811_2`，初始业务版本为 `2026.8.7.1`。
- 安装结果：`2026.8.11.2 / Core 4.0.0`，`activated=true`，首次状态为 `pending=true`。
- 根 Launcher SHA-256：升级前后均为 `66a3a2b3858a849a6289a0d80490ef6f14d2775c0c92bfe301b4912b3d525194`。
- `LiveClipperHost.exe` SHA-256：升级前后均为 `68d99edf504c79220d535a93accb11bfdef2d9e75c0eed142f90b5f0b7cd72a0`。
- 首次启动后：`current.json.pending=false`，当前业务版本保持 `2026.8.11.2`。
- `/api/runtime`：HTTP 200，业务版本 `2026.8.11.2`、Runtime Layout `4`、Core `4.0.0`、Launcher `4.0.0`、`code_source=bundled`。
- 业务完整性：`ok=true`、检查 60 个文件、无不一致，manifest SHA-256 与业务包一致。
- 隔离 AppData 哨兵升级前后保留。

## 自动回退

- 故意向新业务目录的 `bundle_entry.py` 追加内容，制造文件大小/签名清单不一致。
- Launcher 拒绝新业务选择并自动恢复为 `2026.8.7.1 / Core 4.0.0`，`pending=false`。
- 回退原因：`current selection failed verification: bundle file size mismatch: bundle_entry.py`。
- 回退版 `/api/runtime`：HTTP 200，业务完整性 `ok=true`，检查 57 个文件、无不一致。
- 隔离 AppData 哨兵在回退后仍保留。
- 验收 Host 进程已在检查完成后停止；未触碰用户正在使用的软件目录。

## 未完成与发布边界

- `LiveClipperBusiness_2026.8.11.2.zip` 已上传并通过远端哈希验证。
- 截至生成 ready 文件前，客户端固定读取的 OSS `/liveclipper/v4/stable.json` 返回 404；线上 `/liveclipper/v4/stable.hold.json` 仍是 `2026.8.11.1 / hold`。
- 本地 `8.11.2 stable.json` 尚未由本流程上传；上传后必须重新从公网下载并验签，不能只看 OSS 控制台文件名。
- 真实排品表与真实直播视频的单品扫描人工验收、另一台电脑的公网自动下载仍需完成。
- 当前 Core `4.0.0` 没有独立 RC/灰度通道；将 `ready stable.json` 上传正式 OSS 后，会同时向所有符合版本条件的 V4 用户开放更新。
- Core `4.0.1` 全量候选继续隔离，不属于本业务更新，也不要求现有 V4 用户重装。
