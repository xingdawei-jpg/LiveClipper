# LiveClipper V4 2026.9.4.1

状态：已上传并回下载验证业务 ZIP，待最后切换 ready 通道。用户于 2026-09-04 明确要求先推送增量包。

## 版本与范围

- 源码提交：`51eb3480dbdc9d4b01257a0b679345738683dc1c`。
- 包含：商业导演写入用户数据区的启动修复（`50a8f870e39f8c8d879257d04b2674bd73c092d6`）；用户追加的切割句尾 100ms 保护、精确删词边界保持、预览缓存失效和对应测试。
- 本次 Git 纳入：`app/version.json`、`app/cutter_logic.py`、`web_client/server.py`、三个相关测试文件；不纳入未跟踪实验脚本、用户 workspace、审计材料、缓存或其他窗口资料。
- `2026.9.3.4` 候选因加入切割边界修复而作废，未发布。
- 发布类型：`runtime_v4_business`；Core 仍为 `4.0.0`。不包含 launcher、Host、更新器、公钥、原生依赖或 ML 组件。

## 构建与验证

- 从干净 worktree 的同一提交构建两次，ZIP 字节一致。
- 业务 ZIP：`release_artifacts/v4_2026.9.4.1/LiveClipperBusiness_2026.9.4.1.zip`。
- 大小：`1495679` 字节；业务文件数：`85`。
- SHA-256：`e0fdb3bbf705544112e80f02521e151819d8221569324985a9c60442336dcad0`。
- manifest SHA-256：`030e2e38a3aa766e60d580f85d83dd00949b2d1317513e94932ca945e2e76b40`。
- Ed25519 key id：`1905329f73f719d3`；签名及 Core 兼容性核验通过。
- `python -m unittest tests.test_director_opening_fidelity tests.test_preview_word_editing tests.test_ai_batch_reliability tests.test_preview_cache_identity tests.test_commercial_director_bundle_contract -q`：`179` 项通过。
- 新增打包回归实际提取签名业务 ZIP，在不引用源码目录的子进程中写导演结果，再校验业务目录，证明结果写入用户数据区且签名包无新增文件。
- 隔离的 `9.3.2 -> 9.4.1` 安装、两次结果写入/重启校验、真实 frozen Host 诊断已通过。回滚测试使用源码 launcher 的已确认 Core 分支，见下列限制，不能算完整冻结启动器验收。

## 远端业务资产

- 上传地址：`https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/LiveClipperBusiness_2026.9.4.1.zip`。
- 上传后公网回下载，大小和 SHA-256 与本地完全相同；再次通过 `tools/verify_business_bundle.py` 的签名与 Core 兼容性验证。
- 上传 ZIP 时公网 stable 为 `2026.9.3.3 / hold`。

## 未完成与已知问题（不计为通过）

- 第二台干净电脑 GUI 验收未完成；真实素材的句尾听感复核未完成。
- 已污染 `9.3.3` 的救援工具尚在验收，未随本业务 ZIP 发布。软件无法启动的用户需先恢复启动，不能指望应用内更新自行运行。
- 本机留存的旧测试 Core，以及桌面 `LiveClipperWeb_v4.0.0_2026.8.5.2_全量包.zip` 中的 `_internal/web_client/desktop.py` 均与该 Core 签名清单不一致；完整 Core 验证失败。本次未修改、重新签署或分发这个 Core，也未关闭生产校验。
- 为定位业务修复，验收脚本模拟已有 confirmed Core receipt，验证签名入口，并使用源码 launcher 的已确认分支测试业务包回滚。这不证明旧 Core 的全部文件完整，更不等价于完整 frozen launcher / 首次启动健康验收。
- 旧完整 discovery 存在未跟踪实验脚本及缺失授权部署副本的导入错误，未标记为通过。
- 用户在这些限制已告知后要求先推送业务修复。本次按此授权发布，不宣称完整发布矩阵全部通过。
