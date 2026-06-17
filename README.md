# LiveClipper

LiveClipper 是面向直播带货素材的本地剪辑工具，核心能力包括智能成片、混剪成片、AI 扫描、单品扫描、去重、字幕处理、画中画和直播录制。

当前仓库采用“一个核心，两套入口”的结构：

- `app/`：现有桌面版入口和剪辑核心逻辑。
- `web_client/`：新版 Web 桌面客户端，复用 `app/` 内的核心处理能力。
- `packaging/`：后续集中放打包配置、自检脚本、发布流程。
- `tools/`：后续集中放开发维护、诊断、迁移工具。
- `tests/`：后续集中放核心流程测试。

## 当前开发原则

1. 不拆成两个 Git 仓库，避免桌面版和 Web 版核心逻辑分叉。
2. Web 版和桌面版共用剪辑核心，优先把差异放在入口层。
3. 不随便升级版本号，只有确认要发布时才更新版本文件。
4. 不自动推送 GitHub，推送由维护者手动确认或手动执行。
5. 正式打包只从本仓库执行，不从临时工作目录打包。

## 常用入口

桌面版旧入口：

```powershell
python app\launcher.py
```

Web 本地服务：

```powershell
cd web_client
python server.py
```

Web 桌面壳：

```powershell
cd web_client
python desktop.py
```

## 重要提醒

- `release_artifacts/`、`release_dist/`、`build/`、`dist/`、日志、用户配置和密钥类文件不要提交。
- `app/ffmpeg/*.exe` 用于本地打包和测试，但不提交到 Git。
- Web 桌面版结构变化较大时，优先发布全量包，不直接套旧版增量更新。
