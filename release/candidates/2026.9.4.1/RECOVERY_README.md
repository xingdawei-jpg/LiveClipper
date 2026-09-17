# 9.4.1 启动故障恢复

仅适用于 V4 9.3.2 / 9.3.3 出现 `business bundle file set mismatch`，且多出的文件位于 `workspace/ui_commerce_director_experiment` 的情况。

1. 关闭软件和报错窗口。
2. 将救援 ZIP 解压到独立文件夹，双击 `repair_v4_director_workspace.cmd`。
3. 选择包含 `LiveClipperWeb.exe`、`current.json`、`core`、`versions` 的软件安装文件夹。
4. 提示恢复成功后，从安装文件夹最外层的 `LiveClipperWeb.exe` 启动，检查更新并安装 `2026.9.4.1`。

工具会检查原有程序文件，并把放错位置的商业导演结果完整移到安装目录下的 `recovery` 文件夹，逐个核对备份哈希。不修改程序文件、版本指针、设置或授权，也不删除这些结果。

之前的导演结果保留在备份中；需要继续编辑时，请在 9.4.1 中重新生成方案。若提示程序文件缺失、已被改动或发现其他异常文件，工具会停止，请保留提示内容供进一步检查。

正常可以启动的用户，直接在软件中更新即可，无需运行此工具。
