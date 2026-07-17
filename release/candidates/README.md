# Release candidates

每个候选版本使用独立目录：

    release/candidates/<version>/stable.hold.json
    release/candidates/<version>/acceptance.json

候选只用于验收，不是线上通道。channel_status 必须为 hold。验收完成前不得用候选覆盖 release/stable.json。

acceptance.json 从 release/templates/acceptance.json 创建，并为每个必需 gate 填写 pass 和 evidence。构建输出、下载缓存和私钥不得放入本目录。

`business_runtime` 候选必须包含签名补丁，但 package 元数据为空，不要求新建全量 ZIP 或上传百度网盘。`full_baseline` 候选必须包含百度网盘全量包，且不得混入普通业务补丁。
