# LiveClipper V4 Business Update 2026.9.3.3

状态：已发布至 V4 OSS 主更新通道；不是全量包。

## 冻结与范围

- 冻结提交：`e2d0ad5308c665681f04da82185c5cd36fe68259`。
- Cross-Window Sync Gate：已核对现有未跟踪实验资料、审计产物、历史候选及工作区文件，均排除；本候选仅纳入冻结提交与本目录证据。
- 相对 `a31c0496041fe23815eb5f3ea7d49d6da36e883c` 的 Core 边界差异为空：未修改 `runtime_v4/`、`web_client/desktop.py`、规格文件或 V4 更新源配置。
- 因此本次为 V4 业务增量，不需要全量包；兼容 Core `4.0.0`。

## 业务包

- 文件：`C:\\lc_v4_business\\LiveClipperBusiness_2026.9.3.3.zip`。
- 大小：`1,494,822` 字节；业务文件：`85`。
- ZIP SHA-256：`5e446e912d801bcbd61cc6672c45b6711b5973e6ab55baec3a0168646aaa8b20`。
- Bundle manifest SHA-256：`49479c467663aebaf4beac0f1e58b60ab23af2cc9cf4d5075c530e778481feca`。
- 签名 key id：`1905329f73f719d3`。
- 从干净提交构建两次，ZIP 哈希一致；签名和 Core `4.0.0` 兼容性验证通过。

## 自动更新与回退验收

- 已完成隔离的真实签名业务更新：`2026.9.3.2 -> 2026.9.3.3`。
- 更新后从安装目录的签名业务包导入 `run_m3_new_golden_plan_fidelity` 成功，且暴露 `_run_case`。
- 用户数据标记在更新和回退后保持不变。
- 故意破坏新版本入口后，启动器验证失败并自动恢复 `2026.9.3.2`。
- V4 包、通道、下载、切换、错误包拒绝和启动回退回归：`46` 项通过，`1` 项因 Windows 未授予符号链接权限跳过。
- 商业导演/M3/业务包定向回归：`61` 项通过，`1` 项同样因符号链接权限跳过。

## OSS 候选资产

- ZIP 已上传并从公网重新下载：`https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/LiveClipperBusiness_2026.9.3.3.zip`。
- 公网 ZIP SHA-256 与本地一致：`5e446e912d801bcbd61cc6672c45b6711b5973e6ab55baec3a0168646aaa8b20`；签名、85 个业务文件和 Core `4.0.0` 兼容性再次验证通过。
- 在 OSS ZIP 回读验收期间，正式 `stable.json` 保持 `2026.9.3.2 / hold`，没有客户端会提前安装 `2026.9.3.3`。

## 正式通道

- `stable.json` 已切换为 `2026.9.3.3 / ready`：`https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/stable.json`。
- 公网通道 SHA-256：`e8f02a1b762488ca7901ec2ab9ce411cfec032ccb22b9ae66f5456d5a838d0cc`；签名通过。
- 对 `2026.9.3.2 / Core 4.0.0` 的公网通道判定为 `update_available`。

## 完整发现测试的已知基线问题

- 干净提交执行 `python -m unittest discover -s tests -q` 共运行 `1012` 项，`13` 项错误、`4` 项跳过。
- 这 `13` 项都在导入仓库未跟踪的实验脚本或缺失的 `deploy/aliyun_fc_license_auth` 授权部署副本时失败；不在本业务包、商业导演预览或 V4 更新路径中。
- 该仓库完整性问题不被标记为通过；候选保持 hold，直到公网包回读和发布决定完成。
