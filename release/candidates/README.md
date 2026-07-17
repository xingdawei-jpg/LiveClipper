# Release candidates

每个候选版本使用独立目录：

    release/candidates/<version>/stable.hold.json
    release/candidates/<version>/acceptance.json

候选只用于验收，不是线上通道。channel_status 必须为 hold。验收完成前不得用候选覆盖 release/stable.json。

acceptance.json 从 release/templates/acceptance.json 创建，并为每个必需 gate 填写 pass 和 evidence。构建输出、下载缓存和私钥不得放入本目录。
