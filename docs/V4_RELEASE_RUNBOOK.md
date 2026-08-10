# LiveClipper V4 发布与更新完整操作手册

> 状态：2026-08-11，当前全量候选 Core 为 4.0.1；4.0.0 已上线后不得同版本重打
> 给 Codex 的工程交接文档——按顺序执行即可复现全链路

---

## 一、架构回顾

### 目录布局（用户安装后）

```
LiveClipperWeb/
├── LiveClipperWeb.exe          ← 稳定 launcher（低频变化）
├── core/
│   └── 4.0.1/
│       ├── LiveClipperHost.exe  ← 桌面宿主机（含 WebView2/FFmpeg/Python/ML）
│       ├── _internal/           ← PyInstaller onedir 运行时
│       │   ├── web_client/
│       │   │   ├── desktop.py      ← 桌面壳（归 Core）
│       │   │   ├── __init__.py
│       │   │   └── native_file_drop_bridge.dll
│       │   ├── core_config/
│       │   │   └── runtime_v4_update_sources.json  ← 更新源 URL
│       │   ├── core_keys/
│       │   │   ├── release_update_public_key.pem
│       │   │   └── license_public_key.txt
│       │   └── ...（torch/funasr/ffmpeg/webview2_runtime 等）
│       ├── core_manifest.json    ← Core 全文件 SHA256 清单
│       └── core_manifest.sig     ← Ed25519 签名
├── versions/
│   ├── 2026.8.5.1/               ← 回滚备份版本
│   │   └── business/
│   │       ├── app/              ← 业务代码（app/*.py + app/version.json）
│   │       ├── web_client/       ← 前端 + server.py
│   │       ├── bundle_entry.py
│   │       ├── bundle_manifest.json  ← 业务包文件清单
│   │       └── bundle_manifest.sig   ← 签名
│   └── 2026.8.5.2/               ← 当前运行版本
│       └── business/ （同上结构）
└── current.json                  ← 版本选择状态
```

### 版本号体系

| 层 | 版本号 | 格式 | 举例 |
|---|---|---|---|
| Core | `4.0.1` | 语义化 | 低频变化；任何 Core 内容变化必须递增 |
| 业务包 | `YYYY.M.D.N` | 日期递增 | `2026.8.5.2` |
| Launcher | `4.0.1` | 语义化 | 与本次全量 Core 同步 |

**三个版本源必须一致**（同一提交的代码）：
1. `app/version.json` → UI 显示的版本号
2. `bundle_manifest.json` → 签名业务包的版本
3. `current.json` → 启动器选择的版本

---

## 二、构建系统

### 总控脚本

**`tools/build_v4_full_package.py`** 是全量基线包的唯一构建入口。一行命令完成构建、签名、完整校验和压缩；版本升级、更新通道发布和远端上传仍是明确的发布步骤，不能省略。

```powershell
$env:LIVECLIPPER_V4_BUILD_ROOT = "C:\lc_v4_build"

# 全量构建（首次或 Core 变化时）
python tools\build_v4_full_package.py --version 2026.8.11.1 --core-version 4.0.1 --backup-version 2026.8.11.0 --backup-archive release_build\v4_core_4.0.1_rollback\LiveClipperBusiness_2026.8.11.0.zip

# 快速构建（业务代码修改，跳过 Core/Launcher 重建）
python tools\build_v4_full_package.py --version 2026.8.11.2 --core-version 4.0.1 --skip-core --skip-launcher --backup-version 2026.8.11.0 --backup-archive release_build\v4_core_4.0.1_rollback\LiveClipperBusiness_2026.8.11.0.zip
```

#### 六步流程

| 步骤 | 操作 | 时间 | 何时跳过 |
|---|---|---|---|
| 1. Build Core | PyInstaller 打包 host（8889 文件/2GB） | ~40 min | `--skip-core` |
| 2. Build Launcher | PyInstaller 打包 launcher EXE | ~2 min | `--skip-launcher` |
| 3. Build Business | 构建签名业务包 ZIP | ~30s | 从不跳过 |
| 4. Sign + Assemble | 签名 Core manifest，安全解压并验证当前与回滚业务包 | ~2 min | 从不跳过 |
| 5. Verify | 校验 Core 全文件签名/哈希、两套业务包签名、版本三源和回滚选择 | ~1-3 min | 从不跳过 |
| 6. Zip | 压缩全量包并读取全部 ZIP 条目检查物理完整性 | ~4-8 min | 从不跳过 |

> 全量基线包必须同时提供 `--backup-version` 与 `--backup-archive`，且二者不能等于当前版本。脚本会拒绝无回滚目标、签名不匹配或 `app/version.json` 与 `--version` 不一致的构建。

#### 构建环境要求

- **Python 3.12**（`C:\Users\周美彤\AppData\Local\Programs\Python\Python312\python.exe`）
- WebView2 Runtime：`vendor\webview2_runtime_x64\Microsoft.WebView2.FixedVersionRuntime.149.0.4022.98.x64`
- 环境变量 `LIVECLIPPER_WEBVIEW2_RUNTIME_DIR` 指向上述路径
- **构建输出必须在短路径**（`C:\lc_v4_build`），避免 Windows 260 字符限制
- 生产私钥：`C:\Users\周美彤\.liveclipper-keys\release_update_private_key.pem`（⚠️ 不提交 Git）

### Core 构建注意事项

- **Spec**：`runtime_v4/liveclipper_host_v4.spec`
- 引用 WebView2 runtime 时用环境变量 `LIVECLIPPER_WEBVIEW2_RUNTIME_DIR`
- `web_client/desktop.py` 和 `web_client/__init__.py` 通过 datas 打包进 `_internal/web_client/`
- `hiddenimports` 需要含 `web_client.desktop`
- **excludes** 里不要出现 `server`/`updater`（它们从业务包动态加载）

### Launcher 构建注意事项

- **Spec**：`runtime_v4/liveclipper_launcher_v4.spec`
- 内嵌 `release_update_public_key.pem`
- 健康超时默认值：180 秒（`runtime_v4/launcher.py` 的 `--health-timeout` default）

---

## 三、发布前检查清单

### 代码冻结

```powershell
# 1. 确认工作区干净（只含要发布的修改）
git status

# 2. 全量测试
$env:PYTHONPATH="."
python -m unittest discover -s tests -p "test_*.py"

# 3. 版本号同步检查
python -c "import json; v=json.load(open('app/version.json','r',encoding='utf-8')); print(v['version'])"
# 输出应与要发布的业务版本号一致
```

### 构建前确保

- [ ] `app/version.json` 版本号正确
- [ ] `web_client/desktop.py` 是最新版（如有修改）
- [ ] `release/runtime_v4_update_sources.json` 指向正确的更新源 URL
- [ ] `release/runtime_v4_business_policy.json` include/exclude 列表正确

### 构建后验证

```powershell
# 验证全量包关键文件
python -c "
import zipfile, json
z = zipfile.ZipFile(r'LiveClipperWeb_v4.0.1_2026.8.x.x_全量包.zip')
# 版本三源一致
v1 = json.loads(z.read('LiveClipperWeb/current.json'))['current']['application_version']
v2 = json.loads(z.read('LiveClipperWeb/versions/<ver>/business/app/version.json'))['version']
v3 = json.loads(z.read('LiveClipperWeb/versions/<ver>/business/bundle_manifest.json'))['application_version']
print('一致' if v1==v2==v3 else '不一致!!!')
# EXE manifest 匹配
m = json.loads(z.read('LiveClipperWeb/core/4.0.1/core_manifest.json'))
exe = m['files']['LiveClipperHost.exe']['size']
zip_size = z.getinfo('LiveClipperWeb/core/4.0.1/LiveClipperHost.exe').file_size
print('EXE匹配' if exe==zip_size else f'EXE不匹配 {exe} vs {zip_size}')
"
```

---

## 四、更新通道管理

### 通道架构

```
OSS (lc-update.oss-cn-beijing.aliyuncs.com)
└── liveclipper/v4/
    ├── stable.json              ← 签名通道元数据（channel_status=ready）
    └── LiveClipperBusiness_<ver>.zip  ← 签名业务包
```

### 生成并发布通道

```powershell
# 1. 生成 ready 状态 channel。每个仍支持在线升级的 V4 版本都要重复一次 --from-version。
python tools\build_v4_update_channel.py ^
  --bundle C:\lc_v4_build\business\LiveClipperBusiness_<ver>.zip ^
  --output release\candidates\<ver>\stable.json ^
  --version 2026.8.x.x ^
  --from-version <已发布V4版本A> ^
  --from-version <已发布V4版本B> ^
  --core-version 4.0.0 ^
  --source "AliyunOSS=https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/LiveClipperBusiness_<ver>.zip" ^
  --source "CloudflarePages=https://liveclipper-updates.pages.dev/LiveClipperBusiness_<ver>.zip" ^
  --private-key C:\Users\周美彤\.liveclipper-keys\release_update_private_key.pem ^
  --status ready ^
  --release-notes "<更新说明>"

# 2. 上传到 OSS
ossutil cp C:\lc_v4_build\business\LiveClipperBusiness_<ver>.zip oss://lc-update/liveclipper/v4/ -f
ossutil cp release/candidates/<ver>/stable.json oss://lc-update/liveclipper/v4/stable.json -f

# 3. 验证本地签名通道和升级路径
python tools\verify_v4_update_channel.py release/candidates/<ver>/stable.json --public-key app\release_update_public_key.pem --current-version <上一版本号> --core-version 4.0.0
# 应输出 "decision.available: true, reason: update_available"

# 4. 发布后巡检所有 stable.json 和业务包镜像。任何一个配置端点失败都视为发布未完成。
python tools\check_v4_update_endpoints.py
```

### 通道状态说明

| 状态 | 客户端行为 |
|---|---|
| `ready` | 检测到新版本，提示用户更新 |
| `hold` | 通道存在但客户端不安装（候选阶段用） |
| `paused` | 暂停更新 |
| `disabled` | 完全禁用 |

### 更新源配置

文件：`release/runtime_v4_update_sources.json`（内嵌在 Core 的 `core_config/`）

```json
{
  "urls": [
    "https://lc-update.oss-cn-beijing.aliyuncs.com/liveclipper/v4/stable.json",
    "https://cdn.jsdelivr.net/gh/xingdawei-jpg/LiveClipper@main/release/channel/v4/stable.json",
    "https://liveclipper-updates.pages.dev/stable.json"
  ]
}
```

客户端按顺序尝试这些 `stable.json` 地址。每个地址必须发布**同一份已签名通道文件**；通道中声明的每个业务包 URL 也必须上传相同 SHA256 的业务包。发布后必须运行巡检，不能把未上传的备用地址写进配置。

---

## 五、更新流程（客户端视角）

```
用户点"检查更新" / 自动检测
    ↓
Host 从 Core 内嵌源下载 stable.json（签名通道）
    ↓
verify_update_channel(stable.json, public_key)
    ↓
plan_business_update(channel, current_version, core_version)
  检查：channel_status==ready, 目标>当前, 源版本允许, Core 兼容
    ↓
download_business_bundle(channel, download_root)
  按签名通道中的业务包源顺序下载 + SHA256 校验 + 缓存复用 + 断点续传
    ↓
install_business_archive(install_root, archive)
  解压到 versions/<ver>/ → 原子切换 current.json → pending=true
    ↓
launcher 重启 → 健康检查（host 写回执）
  成功 → pending=false, confirmed
  失败 → 自动回滚到 previous 版本
```

### 重要行为

- **已确认状态（pending=false）**：不再做健康检查，直接启动
- **更新后状态（pending=true）**：必须等 host 写完健康回执，超时则回滚
- **无回滚版本时失败**：报错退出（所以全量包必须带 backup 版本）
- **健康回执**：在 uvicorn 启动后立刻写（不等到端口就绪），避免慢机器超时

---

## 六、已修复的已知问题

### 1. frozen 环境下 `from web_client import desktop` 失败
**根因**：`web_client/desktop.py` 不在 Core 也不在业务包
**修复**：desktop.py + `__init__.py` 加入 Core spec datas（`d4e3867`）

### 2. 另一台电脑启动后窗口卡死/拖不动/关不掉
**根因**：同上（desktop.py 缺失 → ModuleNotFoundError → windowed 无控制台 → 进程半死）
**修复**：同上

### 3. 深色模式下 AI 工作台部分亮色
**根因**：`styles.css` 中 13 处硬编码浅色（`#fff`、`#fbfcfe` 等）
**修复**：替换为 CSS 变量（`var(--surface)`、`var(--surface-soft)` 等）（`9b872cb`）

### 4. 任务进行中弹出两次黑色终端窗口
**根因**：部分 `subprocess.run/Popen` 缺少 `CREATE_NO_WINDOW`
**修复**：14 处补 `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)`（`9b872cb`）

### 5. AI 选片预览人脸检测不精准
**根因**：三级检测链"短路"逻辑导致人脸检测从不执行；采样帧全部失败时不裁切
**修复**：脸优先→上半身→HOG 懒惰策略 + 额外复检帧（`ab7fd20`）

### 6. 任务进度条卡在中间不动
**根因**：`_set_task(status="completed")` 用 `setdefault` 设 progress=100，但 key 已存在时不覆盖
**修复**：改为直接赋值 `updates["progress"] = 100`（`74f2bbc`）

### 7. 更新安装后自动回滚
**根因**：健康确认超时（慢机器 45s 不够）+ 已确认状态仍做健康检查
**修复**：pending=false 跳过健康检查（`a4aed3a`）；健康回执前置到 uvicorn 启动后（`993ba33`）

### 8. Version 号不更新
**根因**：`app/version.json` 还停留在旧版本，server 的 `_load_version()` 读它
**修复**：每次构建前用 `build_update_manifest.py --version <新版本号> --force` 更新

### 9. 全量包 EXE size mismatch
**根因**：手动复制 95MB EXE 时文件截断 82 字节
**修复**：统一使用 `build_v4_full_package.py` automate 全流程

### 10. 少数用户本地服务起不来
**根因**：安全软件拦截端口 + 端口范围太小 + 无错误日志
**修复**：端口 20→50，超时 15→30s，uvicorn 错误捕获到 `uvicorn-startup.log`（`8c9bb36`）

---

## 七、关键文件索引

| 文件 | 作用 | 层 |
|---|---|---|
| `tools/build_v4_full_package.py` | **总控构建脚本** | 工具 |
| `tools/build_v4_update_channel.py` | 生成签名通道 | 工具 |
| `tools/verify_v4_update_channel.py` | 验证通道 | 工具 |
| `tools/check_v4_update_endpoints.py` | 巡检已发布通道和所有业务包镜像 | 工具 |
| `tools/build_update_manifest.py` | 更新 app/version.json | 工具 |
| `runtime_v4/liveclipper_host_v4.spec` | Core PyInstaller spec | Core |
| `runtime_v4/liveclipper_launcher_v4.spec` | Launcher PyInstaller spec | Core |
| `runtime_v4/launcher.py` | 启动器（版本选择+健康检查+回滚） | Core |
| `runtime_v4/desktop_host.py` | Core 宿主机入口 | Core |
| `runtime_v4/update_channel.py` | 通道下载+验证+安装 | Core |
| `runtime_v4/update_agent.py` | 业务包安装+原子切换 | Core |
| `runtime_v4/update_service.py` | Host 注入的更新服务 | Core |
| `web_client/desktop.py` | 桌面壳（uvicorn+WebView2） | Core |
| `web_client/frontend/assets/app.js` | 前端 UI | 业务 |
| `web_client/frontend/assets/styles.css` | 前端样式 | 业务 |
| `web_client/server.py` | FastAPI 后端 | 业务 |
| `app/smart_crop.py` | 智能裁切+人脸检测 | 业务 |
| `app/version.json` | UI 显示版本号 | 业务 |
| `bundle_entry.py` | 业务包入口 | 业务 |
| `release/runtime_v4_business_policy.json` | 业务包 include/exclude 策略 | 业务 |
| `release/runtime_v4_update_sources.json` | 更新源 URL 列表 | Core |
| `C:\Users\周美彤\.liveclipper-keys\release_update_private_key.pem` | **生产私钥（勿提交）** | - |

---

## 八、版本号升级步骤

每次发布前执行：

```powershell
# 1. 修改代码
# 2. 更新版本号
python tools\build_update_manifest.py --version 2026.8.x.x --notes "更新说明" --force

# 3. 跑测试
$env:PYTHONPATH="."; python -m unittest discover -s tests -p "test_*.py"

# 4. Git 提交
git add app/version.json <修改的文件>
git commit -m "release: 2026.8.x.x <说明>"

# 5. 构建全量包
$env:LIVECLIPPER_V4_BUILD_ROOT="C:\lc_v4_build"
python tools\build_v4_full_package.py --version 2026.8.x.x --core-version <Core版本> --backup-version <回滚业务版本> --backup-archive <回滚业务包路径>

# 6. 生成、上传并巡检通道
python tools\build_v4_update_channel.py ...  # 每个支持来源版本重复 --from-version，见第四节
ossutil cp ...   # 向每个已配置镜像上传 bundle + 同一份 stable.json
python tools\check_v4_update_endpoints.py

# 7. 发布全量包到百度网盘
# 文件在桌面: LiveClipperWeb_v<Core版本>_<业务版本>_全量包.zip + .sha256.txt
```

## 九、快速参考

| 操作 | 命令 |
|---|---|
| 构建全量包（全量） | `python tools\build_v4_full_package.py --version 2026.8.x.x --core-version <Core版本> --backup-version ... --backup-archive ...` |
| 构建全量包（跳过 Core） | 加 `--skip-core --skip-launcher` |
| 更新 version.json | `python tools\build_update_manifest.py --version 2026.8.x.x --force` |
| 生成 ready channel | 见第四节 |
| 上传 OSS | `ossutil cp <文件> oss://lc-update/liveclipper/v4/ -f` |
| 全量测试 | `$env:PYTHONPATH="."; python -m unittest discover -s tests -p "test_*.py"` |
| 验证 ZIP | 见第三节"构建后验证" |
| 检查更新通道 | `python tools\verify_v4_update_channel.py ...` |
