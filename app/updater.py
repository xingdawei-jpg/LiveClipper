"""
LiveClipper 自动更新模块 v2.0（增量更新架构）
- 启动时后台检查 GitHub version.json
- 支持增量更新：只下载变化的 app/ 文件
- 支持全量更新：下载完整安装包
- 国内镜像加速 + 自动回退
"""

import os
import sys
import json
import hashlib
import time
import threading
import tempfile
import subprocess
import urllib.request
import urllib.error
import ssl
import tkinter as tk
from tkinter import messagebox
from pathlib import Path


# ============ 配置（发布时修改）============

# GitHub 仓库
GITHUB_REPO = "xingdawei-jpg/LiveClipper"

# 国内加速镜像列表（优先使用，失败自动回退）
MIRROR_PREFIXES = [
    "https://ghfast.top/https://",
    "https://mirror.ghproxy.com/https://",
    "https://gh-proxy.com/https://",
    "https://ghps.cc/https://",
]

# version.json 的远程地址（不再使用，由 get_version_url 自动生成）
VERSION_URL = ""

# 当前版本号（每次发布时更新）
CURRENT_VERSION = "5.3.10"

# 增量更新阈值：变化的文件数超过此值则建议全量更新
INCREMENTAL_FILE_LIMIT = 10


# ============ URL 与镜像 ============

def get_version_url():
    """获取 version.json 的实际地址"""
    if VERSION_URL:
        return VERSION_URL
    if GITHUB_REPO and "/" in GITHUB_REPO:
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/version.json"
    return ""


def get_mirrored_url(raw_github_url):
    """将 GitHub raw URL 转为镜像列表（镜像优先，原始地址兜底）"""
    if not raw_github_url or "raw.githubusercontent.com" not in raw_github_url:
        return [raw_github_url]
    path = raw_github_url.replace("https://", "")
    urls = [prefix + path for prefix in MIRROR_PREFIXES]
    urls.append(raw_github_url)  # 原始地址兜底
    return urls


def get_app_file_url(filename):
    """获取 app/ 下某个文件的 GitHub raw URL"""
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/app/{filename}"


# ============ 版本比较 ============

def parse_version(version_str):
    """解析语义化版本号"""
    import re
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", str(version_str))
    if not match:
        return (0, 0, 0)
    return tuple(int(x) for x in match.groups())


def is_newer(remote_version, local_version):
    """判断远程版本是否比本地新"""
    rv = parse_version(remote_version)
    lv = parse_version(local_version)
    return rv > lv


# ============ 下载与校验 ============

def compute_sha256(filepath):
    """计算文件的 SHA256 哈希值"""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def compute_sha256_bytes(data):
    """计算字节数据的 SHA256"""
    return hashlib.sha256(data).hexdigest()


def download_bytes(url, timeout=30):
    """下载文件内容到内存，返回 bytes"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": "LiveClipper-Updater/2.0"
    })

    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as response:
        return response.read()


def download_with_mirrors(github_raw_url, timeout=30):
    """通过镜像列表下载，失败自动回退，返回 bytes 或 None"""
    urls = get_mirrored_url(github_raw_url)
    for url in urls:
        try:
            data = download_bytes(url, timeout=timeout)
            # 验证不是 HTML 错误页
            text_prefix = data[:100].decode("utf-8", errors="ignore").strip()
            if text_prefix.startswith("<!"):
                continue
            return data
        except Exception:
            continue
    return None


def download_file(url, dest_path, progress_callback=None):
    """下载文件到本地，支持进度回调"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers={
        "User-Agent": "LiveClipper-Updater/2.0"
    })

    with urllib.request.urlopen(req, context=ctx, timeout=60) as response:
        total_size = int(response.headers.get("Content-Length", 0))
        downloaded = 0

        with open(dest_path, "wb") as f:
            while True:
                chunk = response.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, total_size)


# ============ 版本号持久化 ============

def _version_file_path():
    """获取本地版本记录文件路径"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".installed_version")


def _set_installed_version(ver):
    """写入已安装版本号"""
    try:
        with open(_version_file_path(), "w") as f:
            f.write(ver.strip())
    except Exception:
        pass


def init_installed_version():
    """首次启动：从 _internal/version.json 读取版本号写入 .installed_version"""
    vf = _version_file_path()
    if os.path.exists(vf):
        return
    # 尝试从 app/version.json 读取
    try:
        if getattr(sys, 'frozen', False):
            base = os.path.join(os.path.dirname(sys.executable), "_internal", "app")
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        vj = os.path.join(base, "version.json")
        if os.path.exists(vj):
            with open(vj, "r", encoding="utf-8") as f:
                data = json.load(f)
            ver = data.get("version", CURRENT_VERSION)
            _set_installed_version(ver)
            return
    except Exception:
        pass
    _set_installed_version(CURRENT_VERSION)


def get_installed_version():
    """读取已安装版本号"""
    vf = _version_file_path()
    if os.path.exists(vf):
        try:
            with open(vf, "r") as f:
                return f.read().strip()
        except Exception:
            pass
    return CURRENT_VERSION


# ============ 检查更新 ============

def check_update():
    """
    检查是否有新版本
    返回 dict（包含版本信息）或 None（无更新/出错）
    """
    base_url = get_version_url()
    urls = get_mirrored_url(base_url)

    for try_url in urls:
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            # 加时间戳防 CDN 缓存
            sep = '&' if '?' in try_url else '?'
            no_cache_url = try_url + sep + '_t=' + str(int(time.time()))
            req = urllib.request.Request(no_cache_url, headers={
                "User-Agent": "LiveClipper-Updater/2.0",
                "Cache-Control": "no-cache"
            })
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip().startswith("{"):
                    continue
                data = json.loads(raw)

            remote_ver = data.get("latest_version", "")
            if not remote_ver or not is_newer(remote_ver, get_installed_version()):
                return None

            return data

        except Exception:
            continue

    return None


# ============ 增量更新 ============

def get_app_dir():
    """定位 app/ 目录"""
    if getattr(sys, 'frozen', False):
        # PyInstaller 模式
        candidates = [
            os.path.join(sys._MEIPASS, 'app'),
            os.path.join(os.path.dirname(sys.executable), '_internal', 'app'),
        ]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(base, 'app'),
            os.path.join(base, '_internal', 'app'),
        ]

    for d in candidates:
        d = os.path.normpath(d)
        if os.path.isdir(d):
            return d
    return None


def incremental_update(version_info, progress_callback=None):
    """
    增量更新：只下载变化的 app/ 文件
    progress_callback(phase, current, total, detail)
        phase: "check" / "download" / "verify" / "install"
    """
    files_info = version_info.get("files", {})
    if not files_info:
        # 无文件列表 = 仅版本号变更，直接更新版本号
        _set_installed_version(version_info["latest_version"])
        return True, "版本号已更新"

    app_dir = get_app_dir()
    if not app_dir:
        return False, "找不到 app/ 目录"

    file_list = list(files_info.items())
    total = len(file_list)

    # 第一阶段：检查哪些文件需要更新
    if progress_callback:
        progress_callback("check", 0, total, "检查文件...")

    need_update = []
    for i, (filename, info) in enumerate(file_list):
        if progress_callback:
            progress_callback("check", i + 1, total, f"检查 {filename}")

        local_path = os.path.join(app_dir, filename)
        expected_sha = info.get("sha256", "")

        if expected_sha and os.path.exists(local_path):
            local_sha = compute_sha256(local_path)
            if local_sha.lower() == expected_sha.lower():
                continue  # 文件没变化，跳过

        need_update.append((filename, info))

    if not need_update:
        # 所有文件都是最新的，只更新版本号
        _set_installed_version(version_info["latest_version"])
        return True, "已是最新"

    # 第二阶段：下载文件
    if progress_callback:
        progress_callback("download", 0, len(need_update), "下载文件...")

    downloaded = {}  # filename -> bytes
    for i, (filename, info) in enumerate(need_update):
        if progress_callback:
            progress_callback("download", i + 1, len(need_update), f"下载 {filename}")

        url = get_app_file_url(filename)
        data = download_with_mirrors(url)

        if data is None:
            return False, f"下载失败: {filename}"

        # 校验 SHA256
        expected_sha = info.get("sha256", "")
        if expected_sha:
            actual_sha = compute_sha256_bytes(data)
            if actual_sha.lower() != expected_sha.lower():
                return False, f"校验失败: {filename}"

        downloaded[filename] = data

    # 第三阶段：写入文件
    if progress_callback:
        progress_callback("install", 0, len(downloaded), "安装更新...")

    for i, (filename, data) in enumerate(downloaded.items()):
        if progress_callback:
            progress_callback("install", i + 1, len(downloaded), f"写入 {filename}")

        local_path = os.path.join(app_dir, filename)
        try:
            with open(local_path, "wb") as f:
                f.write(data)
        except Exception as e:
            return False, f"写入失败: {filename} ({e})"

    # 更新版本号
    _set_installed_version(version_info["latest_version"])

    return True, f"更新成功: {len(downloaded)} 个文件"


# ============ GUI 组件 ============

class UpdateDialog(tk.Toplevel):
    """更新提示对话框"""

    def __init__(self, parent, version_info):
        super().__init__(parent)
        self.version_info = version_info
        self.result = None  # "update" / "skip" / "later"

        self.title("发现新版本")
        self.geometry("450x300")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # 居中显示
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 450) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 300) // 2
        self.geometry(f"+{x}+{y}")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_later)

    def _build_ui(self):
        vi = self.version_info
        version = vi.get("latest_version", "?")
        notes = vi.get("release_notes", "修复了一些问题")
        force = vi.get("force_update", False)
        files_count = len(vi.get("files", {}))

        # 标题
        tk.Label(
            self, text=f"🎉 新版本 v{version} 可用",
            font=("Microsoft YaHei UI", 13, "bold")
        ).pack(pady=(15, 5))

        # 更新说明
        tk.Label(
            self, text=notes,
            font=("Microsoft YaHei UI", 9),
            wraplength=400, justify="left",
            fg="#555555"
        ).pack(padx=20, pady=5)

        # 更新大小提示
        if files_count > 0:
            size_hint = f"增量更新: {files_count} 个文件（约 {files_count * 20} KB）"
            tk.Label(
                self, text=size_hint,
                font=("Microsoft YaHei UI", 9),
                fg="#2196F3"
            ).pack(pady=(0, 5))

        # 强制更新提示
        if force:
            tk.Label(
                self, text="⚠️ 此版本为重要更新，需要立即升级",
                font=("Microsoft YaHei UI", 9),
                fg="#E74C3C"
            ).pack(pady=(0, 5))

        # 按钮
        btn_frame = tk.Frame(self)
        btn_frame.pack(pady=10)

        if force:
            tk.Button(
                btn_frame, text="立即更新", width=15,
                command=self._on_update,
                bg="#2196F3", fg="white",
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=5)
        else:
            tk.Button(
                btn_frame, text="立即更新", width=12,
                command=self._on_update,
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=5)
            tk.Button(
                btn_frame, text="稍后提醒", width=12,
                command=self._on_later,
                font=("Microsoft YaHei UI", 10)
            ).pack(side="left", padx=5)

    def _on_update(self):
        self.result = "update"
        self.destroy()

    def _on_later(self):
        self.result = "later"
        self.destroy()


class DownloadDialog(tk.Toplevel):
    """增量更新进度对话框"""

    def __init__(self, parent, version_info, on_complete=None, on_error=None):
        super().__init__(parent)
        self.version_info = version_info
        self.on_complete = on_complete
        self.on_error = on_error
        self.cancelled = False

        self.title("正在更新")
        self.geometry("420x170")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # 居中
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 420) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 170) // 2
        self.geometry(f"+{x}+{y}")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # 开始更新
        self.after(100, self._start_update)

    def _build_ui(self):
        tk.Label(
            self, text="⬇️ 正在下载更新...",
            font=("Microsoft YaHei UI", 11)
        ).pack(pady=(15, 5))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = tk.Progressbar(
            self, variable=self.progress_var,
            maximum=100, length=370
        )
        self.progress_bar.pack(pady=5)

        self.status_label = tk.Label(
            self, text="准备中...",
            font=("Microsoft YaHei UI", 9),
            fg="#888888"
        )
        self.status_label.pack()

        tk.Button(
            self, text="取消", width=10,
            command=self._on_cancel,
            font=("Microsoft YaHei UI", 9)
        ).pack(pady=(5, 0))

    def _start_update(self):
        def progress_cb(phase, current, total, detail):
            if self.cancelled:
                return
            if total > 0:
                pct = current / total * 100
                self.progress_var.set(pct)
            phase_text = {"check": "🔍 检查", "download": "⬇️ 下载", "verify": "✅ 校验", "install": "📦 安装"}
            self.status_label.config(text=f"{phase_text.get(phase, phase)} {detail}")

        def update_thread():
            try:
                success, msg = incremental_update(self.version_info, progress_cb)

                if self.cancelled:
                    return

                if success:
                    self.after(0, lambda: self.status_label.config(text="✅ 更新完成"))
                    self.after(500, lambda: self.on_complete(msg))
                    self.after(600, self.destroy)
                else:
                    self.after(0, lambda: self.on_error(f"更新失败: {msg}"))
                    self.after(0, self.destroy)

            except Exception as e:
                if not self.cancelled:
                    self.after(0, lambda: self.on_error(f"更新出错: {str(e)}"))
                    self.after(0, self.destroy)

        threading.Thread(target=update_thread, daemon=True).start()

    def _on_cancel(self):
        self.cancelled = True
        self.destroy()


# ============ 主入口 ============

def check_and_prompt_update(parent_window):
    """在后台检查更新，如果有新版本则弹出提示"""
    def _check():
        version_info = check_update()
        if version_info:
            parent_window.after(0, lambda: _show_dialog(version_info))

    threading.Thread(target=_check, daemon=True).start()


def _show_dialog(version_info):
    """在主线程中显示更新对话框"""
    try:
        root = tk._default_root
    except AttributeError:
        return

    if not root or not root.winfo_exists():
        return

    dlg = UpdateDialog(root, version_info)
    root.wait_window(dlg)

    if dlg.result == "update":
        download_dlg = DownloadDialog(
            root, version_info,
            on_complete=_on_update_complete,
            on_error=_on_update_error
        )
        root.wait_window(download_dlg)


def _on_update_complete(msg):
    """更新完成，提示用户重启"""
    try:
        result = messagebox.askyesno(
            "更新完成",
            f"{msg}\n\n需要重启程序以应用更新。\n\n点击「是」立即重启。",
            icon="info"
        )
        if result:
            # 重启程序
            exe = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
            subprocess.Popen([exe])
            try:
                root = tk._default_root
                if root:
                    root.quit()
            except Exception:
                pass
            sys.exit(0)
    except Exception:
        pass


def _on_update_error(msg):
    """更新失败"""
    messagebox.showerror("更新失败", msg, icon="error")


# ============ 独立运行测试 ============

if __name__ == "__main__":
    print(f"当前版本: {get_installed_version()}")
    print(f"检查地址: {get_version_url() or '(未配置)'}")
    print(f"app 目录: {get_app_dir()}")

    info = check_update()
    if info:
        print(f"发现新版本: v{info.get('latest_version')}")
        print(f"更新说明: {info.get('release_notes', '')}")
        print(f"变化文件: {len(info.get('files', {}))} 个")
    else:
        print("已是最新版本，或检查更新失败")
