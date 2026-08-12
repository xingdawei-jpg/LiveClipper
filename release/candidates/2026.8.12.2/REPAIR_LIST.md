# LiveClipper V4 2026.8.12.2 维修内容清单

状态：`hold` 候选，未发布

- 业务版本：`2026.8.12.2`
- 稳定 Core / Launcher：`4.0.0`（本轮不替换、不重装）
- 上一正式业务版本：`2026.8.12.1`
- 发布类型：签名业务增量

## 本次新增维修

### 自定义单品导出范围

- 时间校验完成后，可以按整个商品勾选或取消，也可以单独选择商品中的某一个讲解时段。
- 只有已勾选且被真实素材覆盖的时段会进入切割；取消的商品和时段不会生成文件。
- 没有保留任何可切割时段时禁止启动任务，并给出明确提示。
- 后端再次校验用户选择，避免前端取消的时段被错误导出。
- 结果反馈区分已生成、未覆盖和用户主动取消，便于核对本次实际输出。

### 单品扫描界面整理

- 将时间表、直播视频和导出位置合并为紧凑的“素材与输出”步骤，减少页面纵向占用。
- 结果列表使用独立的限定高度滚动区，商品和时段较多时不会把其他操作区域挤出页面。
- 已导入视频范围改为按需展开查看，默认界面更简洁。
- 增加已选时段数量、商品级选择状态和时段级选择状态展示。

## 纳入文件

- `web_client/server.py`
- `web_client/frontend/index.html`
- `web_client/frontend/assets/app.js`
- `web_client/frontend/assets/styles.css`
- `tests/test_runtime_v4_server_update_bridge.py`
- `app/version.json`

## 更新边界

- 不包含 `runtime_v4/`、`web_client/desktop.py`、PyInstaller spec、Launcher、Host、Python、FFmpeg、WebView2、模型或原生依赖。
- 只兼容稳定 Core `4.0.0`，更新时写入新的业务版本目录并保留 `2026.8.12.1` 回退。
- 不覆盖已经发布的 `LiveClipperBusiness_2026.8.12.1.zip`；新资产使用 `LiveClipperBusiness_2026.8.12.2.zip`。
- 候选验收期间保持 `hold`；全部自动门禁通过后才生成并发布新的 `ready stable.json`。
