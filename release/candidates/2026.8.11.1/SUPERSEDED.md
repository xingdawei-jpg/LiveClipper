# V4 2026.8.11.1 候选已被替代

状态：`superseded`，禁止发布 `stable.json`

- `2026.8.11.1` 从未进入线上 V4 `stable.json`，正式线上通道仍保持旧版本。
- 对应业务 ZIP 已上传 OSS，但没有正式通道引用，因此客户端不会自动安装。
- 为纳入单品扫描时间对齐与校验流程修正，新候选升级为 `2026.8.11.2`。
- 禁止用新的业务内容覆盖 OSS 上同名的 `LiveClipperBusiness_2026.8.11.1.zip`，避免同一版本出现不同 SHA256 与缓存污染。
- 本地生成但未上传的 `8.11.1 ready stable.json` 已隔离保存到 `C:\lc_v4_superseded_20260811\2026.8.11.1\stable.json`，只作审计留存。

后续构建、验收和发布只使用 `release/candidates/2026.8.11.2/` 与 `LiveClipperBusiness_2026.8.11.2.zip`。
