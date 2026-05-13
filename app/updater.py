"""
LiveClipper 自动更新模块
- 启动时后台检查 GitHub Releases 是否有新版本
- 支持强制更新、下载进度、SHA256 校验
- 更新后提示用户重启
"""

import os
import sys
import json
import hashlib
import threading
import tempfile
import subprocess
import time
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from pathlib import Path

_NO_WINDOW = 0x08000000


# ============ 配置（发布时修改） ============

# GitHub 仓库（私有仓库需在 version.json 里放完整 URL）
GITHUB_REPO = "xingdawei-jpg/LiveClipper"

# version.json 的远程地址（优先使用这个）
# 如果设置了这个，会忽略 GITHUB_REPO
VERSION_URL = ""  # 使用 GITHUB_REPO 自动生成

# 当前版本号（每次发布时更新）
CURRENT_VERSION = "2026.5.9"

def init_installed_version():
    """First-launch: create .installed_version from version.json if not exists.
    Call this once at app startup before any update check."""
    try:
        vf = _get_installed_version_file()
        if not os.path.exists(vf):
            # Read version from bundled version.json
            if getattr(sys, 'frozen', False):
                vj = os.path.join(os.path.dirname(sys.executable), '_internal', 'version.json')
            else:
                vj = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.json')
            if os.path.exists(vj):
                with open(vj, 'r', encoding='utf-8-sig') as f:
                    vdata = json.load(f)
                ver = vdata.get('latest_version', '')
                if ver:
                    _set_installed_version(ver)
                    return
            _set_installed_version(CURRENT_VERSION)
    except Exception:
        _set_installed_version(CURRENT_VERSION)


def _get_installed_version_file():
    """Path to local version tracking file"""
    base = _get_install_base() if hasattr(sys, 'modules') else os.path.dirname(os.path.abspath(__file__))
    try:
        base = _get_install_base()
    except Exception:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, ".installed_version")

def _get_installed_version():
    """Read installed version from local file, fallback to CURRENT_VERSION"""
    try:
        vf = _get_installed_version_file()
        if os.path.exists(vf):
            with open(vf, "r", encoding="utf-8") as f:
                v = f.read().strip()
            if v:
                return v
    except Exception:
        pass
    return CURRENT_VERSION

def _set_installed_version(version):
    """Write installed version to local file after update"""
    try:
        vf = _get_installed_version_file()
        with open(vf, "w", encoding="utf-8") as f:
            f.write(version.strip())
    except Exception:
        pass


def init_installed_version():
    """First-launch: create .installed_version from version.json if not exists.
    Call this once at app startup before any update check."""
    try:
        vf = _get_installed_version_file()
        if not os.path.exists(vf):
            # Read version from bundled version.json
            if getattr(sys, 'frozen', False):
                vj = os.path.join(os.path.dirname(sys.executable), '_internal', 'version.json')
            else:
                vj = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'version.json')
            if os.path.exists(vj):
                with open(vj, 'r', encoding='utf-8-sig') as f:
                    data = json.load(f)
                ver = data.get('version', CURRENT_VERSION)
                _set_installed_version(ver)
                return ver
            else:
                _set_installed_version(CURRENT_VERSION)
                return CURRENT_VERSION
    except Exception:
        pass
    return CURRENT_VERSION

# 检查更新的 API 地址（GitHub Releases）
def get_version_url():
    """获取 version.json 的实际地址"""
    if VERSION_URL:
        return VERSION_URL
    if GITHUB_REPO and "/" in GITHUB_REPO:
        return f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/app/version.json"
    return ""


# ============ 版本比较 ============

def parse_version(version_str):
    """解析语义化版本号或日期版本号，返回可比较的元组"""
    import re
    # Strip optional "v" prefix
    vs = str(version_str).lstrip("vV")
    # Try date format first: 2026.4.26 or 2026.04.26
    match = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", vs)
    if match:
        return tuple(int(x) for x in match.groups())
    # Fall back to semantic version: 8.5.1
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", vs)
    if match:
        return tuple(int(x) for x in match.groups())
    return (0, 0, 0)


def is_newer(remote_version, local_version):
    """判断远程版本是否比本地新"""
    rv = parse_version(remote_version)
    lv = parse_version(local_version)
    return rv > lv


# ============ 网络请求（纯Python，不依赖curl.exe）============

def _fetch_url(url, timeout=15):
    """用 urllib 获取 URL 内容，支持 HTTPS 和重定向"""
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8")


def _fetch_json(url, timeout=15):
    """获取 URL 并解析为 JSON"""
    text = _fetch_url(url, timeout=timeout)
    return json.loads(text)


def _fetch_github_api_file(repo, path, timeout=15):
    """通过 GitHub API 获取仓库文件内容，返回 JSON 对象"""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    data = _fetch_json(url, timeout=timeout)
    import base64
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content)


def _get_update_urls():
    """生成更新检测 URL 列表（优先级从高到低）"""
    urls = []
    # 1. GitHub API - 无缓存，实时性最高
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/app/version.json"
    urls.append(("GitHub API", url))
    # 2. 直连 raw.githubusercontent.com
    urls.append(("GitHub Raw", get_version_url()))
    # 3. 代理镜像
    for prefix in ["https://ghfast.top/https://", "https://gh-proxy.com/https://"]:
        mirror_url = prefix + get_version_url().replace("https://", "")
        urls.append((f"Mirror", mirror_url))
    # 4. jsDelivr CDN（国内速度快但可能有缓存）
    cdn_url = f"https://cdn.jsdelivr.net/gh/{GITHUB_REPO}@main/app/version.json"
    urls.append((f"jsDelivr", cdn_url))
    return urls


# ============ 检查更新 ============

def check_update():
    """
    检查是否有新版本
    返回 dict（包含版本信息）或 None（无更新/出错）
    """
    local_ver = _get_installed_version()
    for name, url in _get_update_urls():
        try:
            if name == "GitHub API":
                data = _fetch_github_api_file(GITHUB_REPO, "app/version.json")
            else:
                data = _fetch_json(url, timeout=10)
        except Exception:
            continue

        remote_ver = data.get("latest_version", data.get("version", ""))
        if not remote_ver:
            continue
        if not is_newer(remote_ver, local_ver):
            continue
        # Found a newer version!
        return data

    return None


# ============ 下载与校验 ============

def compute_sha256(filepath):
    """计算文件的 SHA256 哈希值"""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def download_file(url, dest_path, progress_callback=None):
    """
    下载文件，纯 Python 实现（兼容网络限制）
    progress_callback(downloaded_bytes, total_bytes)
    """
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        total_size = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest_path, "wb") as f:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback and total_size > 0:
                    progress_callback(downloaded, total_size)
    if progress_callback:
        progress_callback(downloaded, downloaded)
    if os.path.exists(dest_path):
        with open(dest_path, "rb") as f:
            header = f.read(512)
        if b"<html" in header.lower() or b"<!doctype" in header.lower():
            os.remove(dest_path)
            raise Exception("下载被拦截，服务器返回了网页。请检查网络或手动下载更新。")


def _fetch_file_bytes(url, timeout=30):
    """下载单个文件，返回 bytes（纯 Python）"""
    import urllib.request, ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "LiveClipper/1.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()


def _download_file_bytes(fname, expected_sha):
    """下载单个文件的 bytes，尝试多个源+重试，验证 SHA256"""
    repo = GITHUB_REPO
    sources = [
        ("GitHub API", f"https://api.github.com/repos/{repo}/contents/app/{fname}?ref=main"),
        ("Raw", f"https://raw.githubusercontent.com/{repo}/main/app/{fname}"),
        ("jsDelivr", f"https://cdn.jsdelivr.net/gh/{repo}@main/app/{fname}"),
    ]
    for prefix in ["https://ghfast.top/https://", "https://gh-proxy.com/https://"]:
        sources.append((f"Mirror", prefix + f"raw.githubusercontent.com/{repo}/main/app/{fname}"))

    # 每个源尝试2次，超时递减
    for name, url in sources:
        for attempt in range(2):
            timeout = 15 if attempt == 0 else 30
            try:
                if "api.github.com" in url:
                    data = _fetch_json(url, timeout=timeout)
                    import base64
                    content = base64.b64decode(data["content"])
                else:
                    content = _fetch_file_bytes(url, timeout=timeout)
                if len(content) < 50 or content[:5] in (b"<htm", b"<!doc"):
                    continue
                actual = hashlib.sha256(content).hexdigest().lower()
                if actual == expected_sha.lower():
                    return content
            except Exception:
                continue
    return None


# ============ GUI 组件 ============

class UpdateDialog(tk.Toplevel):
    """更新提示对话框"""
    
    def __init__(self, parent, version_info):
        super().__init__(parent)
        self.version_info = version_info
        self.result = None  # "update" / "skip" / "later"
        
        self.title("发现新版本")
        self.geometry("420x280")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        # 居中显示
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 420) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 280) // 2
        self.geometry(f"+{x}+{y}")
        
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_later)
    
    def _build_ui(self):
        vi = self.version_info
        version = vi.get("latest_version", "?")
        notes = vi.get("release_notes", "修复了一些问题")
        # Check if incremental update is available
        is_update = _is_installed() and vi.get("update_url", "")
        if is_update:
            notes += "\n\n（增量更新，仅下载变更文件，几秒完成）"
        force = vi.get("force_update", False)
        
        # 标题
        tk.Label(
            self, text=f"🎉 新版本 v{version.lstrip("vV")} 可用",
            font=("Microsoft YaHei UI", 13, "bold")
        ).pack(pady=(15, 5))
        
        # 更新说明
        tk.Label(
            self, text=notes,
            font=("Microsoft YaHei UI", 9),
            wraplength=380, justify="left",
            fg="#555555"
        ).pack(padx=20, pady=5)
        
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
            # 强制更新：只有更新按钮
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
    """下载进度对话框"""
    
    def __init__(self, parent, version_info, on_complete=None, on_error=None):
        super().__init__(parent)
        self.version_info = version_info
        self.on_complete = on_complete
        self.on_error = on_error
        self.cancelled = False
        
        self.title("正在下载更新")
        self.geometry("400x140")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        
        # 居中
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - 400) // 2
        y = parent.winfo_y() + (parent.winfo_height() - 140) // 2
        self.geometry(f"+{x}+{y}")
        
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        
        # 开始下载
        self.after(100, self._start_download)
    
    def _build_ui(self):
        tk.Label(
            self, text="⬇️ 正在下载更新包...",
            font=("Microsoft YaHei UI", 11)
        ).pack(pady=(15, 5))
        
        self._progress_value = 0
        self._progress_canvas = tk.Canvas(self, width=350, height=20, bg="#E0E0E0", highlightthickness=0)
        self._progress_canvas.pack(pady=5)
        self._progress_bar = self._progress_canvas.create_rectangle(0, 0, 0, 20, fill="#2196F3", outline="")
        
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
    
    def _format_size(self, size_bytes):
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.1f} MB"
    
    def _start_download(self):
        # Support incremental file-by-file updates from version.json "files" field
        files_info = self.version_info.get("files", {})
        has_update_url = self.version_info.get("update_url", "") or self.version_info.get("download_url", "")

        if files_info and not has_update_url:
            # Incremental update: download individual files
            self._do_incremental_update(files_info)
        elif has_update_url:
            # Full package download
            download_url = self.version_info.get("update_url", "") or self.version_info.get("download_url", "")
            self._do_full_download(download_url)
        else:
            self.on_error("无可用的更新方式")
            self.destroy()
            return

    def _do_incremental_update(self, files_info):
        """逐文件增量更新：下载app/下变化的文件，校验SHA256后直接替换"""
        import hashlib as _hl

        # Determine app directory
        if getattr(sys, 'frozen', False):
            app_dir = os.path.join(os.path.dirname(sys.executable), "app")
        else:
            app_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app")
            if not os.path.isdir(app_dir):
                app_dir = os.path.dirname(os.path.abspath(__file__))

        if not os.path.isdir(app_dir):
            self.on_error("找不到app目录")
            self.destroy()
            return

        file_list = list(files_info.items())
        total = len(file_list)
        success_count = 0
        fail_count = 0

        def update_thread():
            nonlocal success_count, fail_count
            for idx, (fname, expected_sha) in enumerate(file_list):
                if self.cancelled:
                    return

                # Skip version.json itself and non-code files
                if fname == "version.json":
                    success_count += 1
                    continue

                # Update progress
                pct = (idx / total) * 100
                self.after(0, lambda p=pct, f=fname: (
                    self._progress_canvas.coords(self._progress_bar, 0, 0, int(350 * p / 100), 20),
                    self.status_label.config(text=f"({idx+1}/{total}) {f}")
                ))

                # Download with Python (multi-source, SHA256 verified)
                content = _download_file_bytes(fname, expected_sha)
                if content is None:
                    fail_count += 1
                    continue

                # Verify SHA256
                actual_sha = _hl.sha256(content).hexdigest()
                if actual_sha.lower() != expected_sha.lower():
                    fail_count += 1
                    continue

                # Determine target dirs
        update_dir = _get_update_dir()
        target_dirs = [update_dir]
        if app_dir not in target_dirs:
            target_dirs.append(app_dir)
        
        # Write to app directory
        for td in target_dirs:
            td_path = os.path.join(td, fname)
            try:
                os.makedirs(os.path.dirname(td_path), exist_ok=True)
                with open(td_path, 'wb') as f:
                    f.write(content)
            except Exception:
                pass
        success_count += 1
                    # 同时写到 sys._MEIPASS/app/（兼容不同版本 EXE 的路径差异）
                    try:
                        _meipass_app = os.path.join(sys._MEIPASS, 'app')
                        if _meipass_app != app_dir:
                            _alt_dest = os.path.join(_meipass_app, fname)
                            with open(_alt_dest, 'wb') as f:
                                f.write(content)
                    except Exception:
                        pass
                except Exception:
                    fail_count += 1

            if self.cancelled:
                return

            # Update installed version from local version.json
            try:
                _vj_path = _os.path.join(app_dir, "version.json")
                if _os.path.exists(_vj_path):
                    _vj_data = json.load(open(_vj_path, "r", encoding="utf-8-sig"))
                    _new_ver = _vj_data.get("latest_version", _vj_data.get("version", ""))
                else:
                    _new_ver = ""
                if not _new_ver:
                    _new_ver = self.version_info.get("latest_version", self.version_info.get("version", ""))
                if _new_ver:
                    _set_installed_version(_new_ver)
            except Exception:
                pass

            # Clear __pycache__ so new .py files take effect immediately
            try:
                import shutil
                _cache_dir = _os.path.join(app_dir, "__pycache__")
                if _os.path.isdir(_cache_dir):
                    shutil.rmtree(_cache_dir)
            except Exception:
                pass

            self.after(0, lambda: self._progress_canvas.coords(self._progress_bar, 0, 0, int(350 * 100 / 100), 20))
            self.after(0, lambda: self.status_label.config(text="更新完成"))

            if fail_count == 0:
                msg = f"成功更新 {success_count} 个文件"
                self.after(500, lambda: self.on_complete(None, "", True))
                self.after(600, self.destroy)
            elif success_count > 0:
                msg = f"更新完成: {success_count} 成功, {fail_count} 失败"
                self.after(0, lambda: self.on_error(msg))
                self.after(0, self.destroy)
            else:
                self.after(0, lambda: self.on_error("所有文件下载失败，请检查网络"))
                self.after(0, self.destroy)

        threading.Thread(target=update_thread, daemon=True).start()

    def _do_full_download(self, download_url):
        """全量下载（zip/exe包）- 尝试镜像加速"""
        # 尝试用镜像替代直连GitHub
        mirror_prefixes = [
            "https://gh-proxy.com/https://",
            "https://ghfast.top/https://",
        ]
        mirror_url = None
        for prefix in mirror_prefixes:
            if "github.com" in download_url or "githubusercontent.com" in download_url:
                test_url = prefix + download_url.replace("https://", "")
                try:
                    result = subprocess.run(
                        ["curl.exe", "-s", "-k", "-L", "-I", "--max-time", "5", test_url],
                        capture_output=True, timeout=8
, creationflags=_NO_WINDOW)
                    if result.returncode == 0:
                        mirror_url = test_url
                        break
                except Exception:
                    continue
        
        if mirror_url:
            download_url = mirror_url

        expected_sha = self.version_info.get("sha256", "")
        self._is_incremental_update = False

        def progress_cb(downloaded, total):
            if self.cancelled:
                return
            if total > 0:
                pct = downloaded / total * 100
                self._progress_canvas.coords(self._progress_bar, 0, 0, int(350 * pct / 100), 20)
                self.status_label.config(
                    text=f"{self._format_size(downloaded)} / {self._format_size(total)}"
                )
            else:
                self.status_label.config(text=f"已下载 {self._format_size(downloaded)}")

        def download_thread():
            try:
                temp_dir = tempfile.mkdtemp(prefix="liveclipper_update_")
                filename = download_url.split("/")[-1] or "LiveClipper_Setup.exe"
                temp_path = os.path.join(temp_dir, filename)

                download_file(download_url, temp_path, progress_cb)

                if self.cancelled:
                    return

                if expected_sha and isinstance(expected_sha, str):
                    self.after(0, lambda: self.status_label.config(text="正在校验文件完整性..."))
                    actual_sha = compute_sha256(temp_path)
                    if actual_sha.lower() != expected_sha.lower():
                        self.after(0, lambda: self.on_error(
                            f"文件校验失败\n期望: {expected_sha[:16]}...\n实际: {actual_sha[:16]}..."
                        ))
                        self.after(0, self.destroy)
                        return

                self.after(0, lambda: self.on_complete(temp_path, filename, False))
                self.after(0, self.destroy)

            except Exception as e:
                if not self.cancelled:
                    self.after(0, lambda: self.on_error(f"下载失败: {str(e)}"))
                    self.after(0, self.destroy)

        threading.Thread(target=download_thread, daemon=True).start()
    def _on_cancel(self):
        self.cancelled = True
        self.destroy()


# ============ 主入口 ============

def check_and_prompt_update(parent_window):
    """
    在后台检查更新，如果有新版本则弹出提示
    parent_window: tkinter 根窗口
    """
    def _check():
        version_info = check_update()
        if version_info:
            # 在主线程弹出对话框
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
        # 显示下载对话框
        download_dlg = DownloadDialog(
            root, version_info,
            on_complete=_on_download_complete,
            on_error=_on_download_error
        )
        root.wait_window(download_dlg)


def _on_download_complete(filepath, filename, is_incremental=False):
    """下载完成，处理更新"""
    try:
        if is_incremental and filename.endswith('.zip'):
            # Incremental update: extract zip and restart
            success = _apply_update(filepath)
            # Clean up temp file
            try:
                os.remove(filepath)
                os.rmdir(os.path.dirname(filepath))
            except Exception:
                pass
            if success:
                # _apply_update handles restart via bat script
                pass
            else:
                messagebox.showerror(
                    "更新失败",
                    "解压更新包失败，请尝试重新下载。",
                    icon="error"
                )
        else:
            # Full update: auto-apply zip update
            success = _apply_update(filepath)
            try:
                os.remove(filepath)
                os.rmdir(os.path.dirname(filepath))
            except Exception:
                pass
            if success:
                result = messagebox.askyesno(
                    "更新完成",
                    "更新已安装成功！\n\n需要重启程序以应用更新。\n\n点击「是」立即重启。",
                    icon="info"
                )
                if result:
                    exe = sys.executable if getattr(sys, 'frozen', False) else sys.argv[0]
                    subprocess.Popen([exe], creationflags=_NO_WINDOW)
                    try:
                        root = tk._default_root
                        if root:
                            root.quit()
                    except Exception:
                        pass
                    sys.exit(0)
            else:
                messagebox.showerror(
                    "更新失败",
                    "自动安装更新失败，请手动下载最新版本。",
                    icon="error"
                )
    except Exception:
        pass


def _is_installed():
    """Check if this is an existing installation (has FFmpeg)"""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
        ffmpeg_path = os.path.join(base, '_internal', 'ffmpeg', 'ffmpeg.exe')
        return os.path.exists(ffmpeg_path)
    return False


def _get_install_base():
    """Get the installation base directory"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _get_update_dir():
    """获取持久更新目录"""
    return os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'LiveClipper', 'app')

def _apply_update(zip_path):
    """Extract update zip and replace app files, handle GitHub zip structure"""
    import zipfile, tempfile, shutil as _shutil
    base = _get_install_base()
    
    try:
        # Extract to temp staging
        staging = os.path.join(tempfile.gettempdir(), "liveclipper_update_staging")
        if os.path.exists(staging):
            _shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging, exist_ok=True)

        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(staging)

        # Find app directory - handle GitHub zip structure (LiveClipper-main/app/)
        app_src = None
        # Check common patterns
        candidates = []
        for root, dirs, fns in os.walk(staging):
            if "ai_clipper.py" in fns and "gui.py" in fns:
                candidates.append(root)
        
        if candidates:
            # Prefer the one closest to staging root
            app_src = min(candidates, key=lambda x: len(x))
        
        if not app_src:
            return False

        # Determine target app directory
        update_dir = _get_update_dir()
        targets = [update_dir]  # ① 持久更新目录（首选）
        
        if getattr(sys, 'frozen', False):
            targets.append(os.path.join(base, "app"))           # ② exe同级
            targets.append(os.path.join(base, "_internal", "app"))  # ③ 内嵌
        else:
            # Dev mode: same directory
            dev_target = os.path.dirname(os.path.abspath(__file__))
            if os.path.basename(dev_target) != "app":
                dev_target = os.path.join(dev_target, "app")
            targets.append(dev_target)

        for target_app in targets:
            if not os.path.isdir(target_app):
                os.makedirs(target_app, exist_ok=True)

        copied = 0
        for fname in os.listdir(app_src):
            if fname.endswith(('.py', '.json')):
                src_f = os.path.join(app_src, fname)
                for target_app in targets:
                    dst_f = os.path.join(target_app, fname)
                    try:
                        _shutil.copy2(src_f, dst_f)
                    except Exception:
                        pass
                copied += 1

        # Also copy from LiveClipper-main/app/ if different from app_src
        gh_app = os.path.join(staging, "LiveClipper-main", "app")
        if os.path.isdir(gh_app) and gh_app != app_src:
            for fname in os.listdir(gh_app):
                if fname.endswith(('.py', '.json')):
                    src_f = os.path.join(gh_app, fname)
                    dst_f = os.path.join(target_app, fname)
                    try:
                        _shutil.copy2(src_f, dst_f)
                        copied += 1
                    except Exception:
                        pass

        # Update installed version
        try:
            vj_paths = [
                os.path.join(app_src, "version.json"),
                os.path.join(staging, "LiveClipper-main", "app", "version.json"),
            ]
            for vj in vj_paths:
                if os.path.exists(vj):
                    import json as _json
                    with open(vj, "r", encoding="utf-8") as f:
                        vdata = _json.load(f)
                    new_ver = vdata.get("latest_version", vdata.get("version", ""))
                    if new_ver:
                        _set_installed_version(new_ver)
                    break
        except Exception:
            pass

        # Clean up staging
        try:
            _shutil.rmtree(staging, ignore_errors=True)
        except Exception:
            pass

        return copied > 0

    except Exception:
        return False
def _restart_app():
    """Restart the application"""
    try:
        if getattr(sys, 'frozen', False):
            exe = sys.executable
        else:
            exe = sys.argv[0]
        subprocess.Popen([exe], shell=True, creationflags=_NO_WINDOW)
    except Exception:
        pass
    try:
        root = tk._default_root
        if root:
            root.quit()
    except Exception:
        pass
    sys.exit(0)


def _on_download_error(msg):
    """下载失败"""
    messagebox.showerror("更新失败", msg, icon="error")


# ============ 独立运行测试 ============

if __name__ == "__main__":
    # 测试模式：直接运行检查更新
    print(f"当前版本: {CURRENT_VERSION}")
    print(f"检查更新: {get_version_url() or '(未配置)'}")
    
    info = check_update()
    if info:
        print(f"发现新版本: v{info.get('latest_version')}")
        print(f"更新说明: {info.get('release_notes', '无')}")
        print(f"强制更新: {info.get('force_update', False)}")
    else:
        print("已是最新版本，或检查更新失败。")
